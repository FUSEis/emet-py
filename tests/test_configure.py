from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from typing import Any

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.util._once import Once

import emet_sdk
from emet_sdk import _configure, _transport, _turn
from emet_sdk._delivery import DeliveryResult
from emet_sdk._transport import Reply

KEY = "emet_test_key_not_real"


@pytest.fixture(autouse=True)
def fresh_globals(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    def reset() -> None:
        trace._TRACER_PROVIDER_SET_ONCE = Once()
        trace._TRACER_PROVIDER = None
        _configure._processor = None
        _configure._worker = None
        _configure._said_off = False
        _turn._listeners.clear()

    monkeypatch.setenv("EMET_APPLICATION_KEY", KEY)
    reset()
    yield
    reset()


@pytest.fixture
def posts(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Stands in for Emet, accepting every batch."""
    seen: list[dict[str, Any]] = []

    def post(url: str, headers: dict[str, str], body: bytes, timeout: float) -> Reply:
        batch = json.loads(body)
        seen.append({"url": url, "headers": headers, "batch": batch})
        answer = {"trace": "t-uuid", "conversation": "c-uuid", "accepted": len(batch["spans"]), "gaps": {}}
        return Reply(202, {}, json.dumps(answer).encode())

    monkeypatch.setattr(_transport, "urllib_post", post)
    return seen


def chat_turn(content: str = "hi") -> None:
    tracer = trace.get_tracer("test")
    attributes = {
        "gen_ai.operation.name": "chat",
        "gen_ai.input.messages": json.dumps([{"role": "user", "parts": [{"type": "text", "content": content}]}]),
    }
    with (
        emet_sdk.turn("team", "thread-1", user="ada@acme.example"),
        tracer.start_as_current_span("chat", attributes=attributes),
    ):
        pass


def test_creates_an_sdk_provider_when_the_app_has_none() -> None:
    emet_sdk.configure()
    processor = _configure._processor
    provider = trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)
    assert processor in provider._active_span_processor._span_processors
    assert _turn._listeners == [processor]


def test_adds_to_the_apps_provider_without_replacing_it(posts: list[dict[str, Any]]) -> None:
    provider = TracerProvider()
    trace.set_tracer_provider(provider)
    emet_sdk.configure()
    processor = _configure._processor
    assert trace.get_tracer_provider() is provider
    assert processor in provider._active_span_processor._span_processors
    chat_turn()
    assert emet_sdk.flush(5)
    assert len(posts[0]["batch"]["spans"]) == 2


def test_uses_the_provider_another_thread_set_first(monkeypatch: pytest.MonkeyPatch) -> None:
    theirs = TracerProvider()
    real_set = trace.set_tracer_provider

    def set_after_them(provider: trace.TracerProvider) -> None:
        real_set(theirs)
        real_set(provider)

    monkeypatch.setattr(trace, "set_tracer_provider", set_after_them)
    emet_sdk.configure()
    processor = _configure._processor
    assert trace.get_tracer_provider() is theirs
    assert processor in theirs._active_span_processor._span_processors


def test_calling_it_again_does_nothing() -> None:
    emet_sdk.configure()
    first = _configure._processor
    emet_sdk.configure()
    assert _configure._processor is first
    assert len(_turn._listeners) == 1


def test_names_the_fix_for_a_non_sdk_provider() -> None:
    trace.set_tracer_provider(trace.NoOpTracerProvider())
    with pytest.raises(TypeError, match=r"opentelemetry\.sdk\.trace\.TracerProvider"):
        emet_sdk.configure()


def test_a_configured_app_sends_its_turns_and_reports_them(
    posts: list[dict[str, Any]], caplog: pytest.LogCaptureFixture
) -> None:
    results: list[DeliveryResult] = []
    emet_sdk.configure(service_name="support-agent", on_delivery=results.append)
    with caplog.at_level(logging.INFO, logger="emet_sdk"):
        chat_turn(f"my key is {KEY}")
        chat_turn()
        assert emet_sdk.flush(5)
    first, _ = posts
    assert first["url"] == "https://app.emet.so/api/v1/traces/"
    assert first["headers"]["Authorization"] == f"Bearer {KEY}"
    batch = first["batch"]
    assert (batch["tenant_ref"], batch["enduser_ref"], batch["resource"]["service.name"]) == (
        "team",
        "ada@acme.example",
        "support-agent",
    )
    # The key is a secret like any other, wherever the app put it
    assert KEY not in json.dumps(batch)
    assert [(r["status"], r["trace"], r["accepted"]) for r in results] == [("sent", "t-uuid", 2)] * 2
    # One line for the first turn, none for a repeat of the same outcome
    assert caplog.text.count("Emet captured turn") == 1


def test_without_a_key_emet_is_off_and_says_so_once(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, posts: list[dict[str, Any]]
) -> None:
    monkeypatch.delenv("EMET_APPLICATION_KEY")
    emet_sdk.configure()
    emet_sdk.configure()
    chat_turn()
    assert caplog.text.count("Emet is off: EMET_APPLICATION_KEY is not set") == 1
    assert (_configure._processor, _turn._listeners, posts) == (None, [], [])
    assert emet_sdk.flush(1)


def test_a_skipped_turn_is_reported_not_sent(posts: list[dict[str, Any]], caplog: pytest.LogCaptureFixture) -> None:
    results: list[DeliveryResult] = []
    emet_sdk.configure(on_delivery=results.append)
    with emet_sdk.turn("team", "thread-1"):
        pass
    assert emet_sdk.flush(5)
    assert [(r["status"], r["reason"]) for r in results] == [("skipped", "no_spans")]
    assert posts == []
    assert "Emet skipped a turn: no_spans" in caplog.text


def test_a_failing_on_delivery_is_caught(posts: list[dict[str, Any]], caplog: pytest.LogCaptureFixture) -> None:
    def broken(result: DeliveryResult) -> None:
        raise RuntimeError("boom")

    emet_sdk.configure(on_delivery=broken)
    chat_turn()
    assert emet_sdk.flush(5)
    assert "on_delivery callback failed" in caplog.text


def test_problems_are_logged_when_the_outcome_changes(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    answers = [
        {"accepted": 2, "unattributed": True, "late": True, "gaps": {"missing_tool_results": ["x"]}},
        None,
    ]

    def post(url: str, headers: dict[str, str], body: bytes, timeout: float) -> Reply:
        answer = answers.pop(0)
        if answer is None:
            return Reply(403, {}, b'{"code": "permission_denied"}')
        return Reply(202, {}, json.dumps(answer).encode())

    monkeypatch.setattr(_transport, "urllib_post", post)
    emet_sdk.configure()
    chat_turn()
    chat_turn()
    assert emet_sdk.flush(5)
    assert "tenant_ref matched no Team; it arrived after the trace closed; missing_tool_results: x" in caplog.text
    assert "stopped sending turn" in caplog.text
    assert "http_4xx (permission_denied)" in caplog.text


def test_an_endpoint_other_than_production_or_localhost_is_refused() -> None:
    with pytest.raises(ValueError, match="localhost"):
        emet_sdk.configure(endpoint="https://example.com")
    assert _configure._processor is None
