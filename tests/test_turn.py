from __future__ import annotations

import logging

import pytest

import emet_sdk
from emet_sdk import _processor
from emet_sdk._processor import CapturedTurn
from tests.conftest import Harness


def names(captured: CapturedTurn) -> list[str]:
    return sorted(span.name for span in captured.spans)


def test_spans_outside_a_turn_are_ignored(harness: Harness) -> None:
    with harness.tracer.start_as_current_span("chat"):
        pass
    assert harness.turns == []


def test_a_turn_collects_its_spans_with_its_values(harness: Harness) -> None:
    with (
        emet_sdk.turn(" team_editorial ", "thread-1", user=" ada@acme.example "),
        harness.tracer.start_as_current_span("invoke_agent"),
        harness.tracer.start_as_current_span("chat"),
    ):
        pass
    [captured] = harness.turns
    assert names(captured) == ["chat", "invoke_agent"]
    assert (captured.info.tenant_ref, captured.info.conversation_id, captured.info.user) == (
        "team_editorial",
        "thread-1",
        "ada@acme.example",
    )


def test_a_blank_user_is_none(harness: Harness) -> None:
    with emet_sdk.turn("t", "c", user="  "), harness.tracer.start_as_current_span("chat"):
        pass
    assert harness.turns[0].info.user is None


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"tenant_ref": " ", "conversation_id": "c"}, ValueError),
        ({"tenant_ref": "t", "conversation_id": ""}, ValueError),
        ({"tenant_ref": "t", "conversation_id": "c", "user": 7}, TypeError),
    ],
)
def test_turn_rejects_bad_values(kwargs: dict[str, object], error: type[Exception]) -> None:
    with pytest.raises(error):
        emet_sdk.turn(**kwargs)  # pyright: ignore[reportArgumentType]


def test_the_processor_never_writes_attributes(harness: Harness) -> None:
    with (
        emet_sdk.turn("team_editorial", "thread-1", user="ada@acme.example"),
        harness.tracer.start_as_current_span("chat", attributes={"gen_ai.operation.name": "chat"}),
    ):
        pass
    [span] = harness.other_exporter.get_finished_spans()
    assert dict(span.attributes or {}) == {"gen_ai.operation.name": "chat"}


def test_a_span_that_outlives_the_block_holds_the_turn_open(harness: Harness) -> None:
    with emet_sdk.turn("t", "c"):
        late = harness.tracer.start_span("background")
    assert harness.turns == []
    late.end()
    assert names(harness.turns[0]) == ["background"]


def test_a_block_with_no_spans_hands_on_an_empty_turn(harness: Harness) -> None:
    with emet_sdk.turn("t", "c"):
        pass
    [captured] = harness.turns
    assert captured.spans == ()


def test_two_turns_in_one_request_stay_apart(harness: Harness) -> None:
    with harness.tracer.start_as_current_span("POST /chat"):
        with emet_sdk.turn("t", "c1"), harness.tracer.start_as_current_span("first"):
            pass
        with emet_sdk.turn("t", "c2"), harness.tracer.start_as_current_span("second"):
            pass
    assert [(t.info.conversation_id, names(t)) for t in harness.turns] == [("c1", ["first"]), ("c2", ["second"])]


def test_a_nested_turn_takes_its_own_spans(harness: Harness) -> None:
    with (
        emet_sdk.turn("t", "outer"),
        harness.tracer.start_as_current_span("outer"),
        emet_sdk.turn("t", "inner"),
        harness.tracer.start_as_current_span("inner"),
    ):
        pass
    assert [(t.info.conversation_id, names(t)) for t in harness.turns] == [("inner", ["inner"]), ("outer", ["outer"])]


def test_turn_as_a_decorator(harness: Harness) -> None:
    @emet_sdk.turn("t", "c")
    def handle() -> str:
        with harness.tracer.start_as_current_span("chat"):
            return "ok"

    assert handle() == "ok"
    assert handle() == "ok"
    assert len(harness.turns) == 2
    assert harness.turns[0].info.id != harness.turns[1].info.id


async def test_turn_as_an_async_decorator(harness: Harness) -> None:
    @emet_sdk.turn("t", "c")
    async def handle() -> str:
        with harness.tracer.start_as_current_span("chat"):
            return "ok"

    assert await handle() == "ok"
    assert names(harness.turns[0]) == ["chat"]


def test_a_failing_handler_is_logged_not_raised(harness: Harness, caplog: pytest.LogCaptureFixture) -> None:
    def broken(_: CapturedTurn) -> None:
        raise RuntimeError("boom")

    harness.processor._on_turn = broken
    with emet_sdk.turn("t", "c"), harness.tracer.start_as_current_span("chat"):
        pass
    assert "Emet failed to hand on a turn" in caplog.text


def test_spans_over_the_cap_are_dropped(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(_processor, "MAX_SPANS_PER_TURN", 2)
    with emet_sdk.turn("t", "c"):
        for name in ("a", "b", "c"):
            with harness.tracer.start_as_current_span(name):
                pass
    [captured] = harness.turns
    assert (len(captured.spans), captured.dropped_spans) == (2, 1)
    assert "dropped 1 spans" in caplog.text


def test_a_turn_over_the_live_cap_is_skipped(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(_processor, "MAX_LIVE_TURNS", 0)
    with emet_sdk.turn("t", "c"), harness.tracer.start_as_current_span("chat"):
        pass
    assert [(t.spans, t.skipped) for t in harness.turns] == [((), "state_cap")]
    assert "turns are already in progress" in caplog.text


def test_a_stale_turn_is_given_up(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    clock = [1000.0]
    monkeypatch.setattr(_processor.time, "monotonic", lambda: clock[0])
    with emet_sdk.turn("t", "stuck"):
        stuck = harness.tracer.start_span("never ends")
    clock[0] += _processor.TURN_TTL_SECONDS + 1
    with caplog.at_level(logging.WARNING), emet_sdk.turn("t", "next"), harness.tracer.start_as_current_span("chat"):
        pass
    stuck.end()
    assert [(t.info.conversation_id, t.skipped) for t in harness.turns] == [
        ("stuck", "span_never_ended"),
        ("next", ""),
    ]
    assert "gave up on a turn" in caplog.text


def test_shutdown_drops_turns_in_progress(harness: Harness, caplog: pytest.LogCaptureFixture) -> None:
    with emet_sdk.turn("t", "c"):
        span = harness.tracer.start_span("open")
    harness.processor.shutdown()
    span.end()
    assert harness.turns == []
    assert "dropped 1 turns still in progress" in caplog.text


def test_force_flush_reports_a_turn_in_progress(harness: Harness) -> None:
    assert harness.processor.force_flush()
    with emet_sdk.turn("t", "c"):
        span = harness.tracer.start_span("open")
    assert not harness.processor.force_flush()
    span.end()
    assert harness.processor.force_flush()
