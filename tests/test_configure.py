from __future__ import annotations

from collections.abc import Iterator

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.util._once import Once

import emet_sdk
from emet_sdk import _configure, _turn


@pytest.fixture(autouse=True)
def fresh_globals() -> Iterator[None]:
    def reset() -> None:
        trace._TRACER_PROVIDER_SET_ONCE = Once()
        trace._TRACER_PROVIDER = None
        _configure._processor = None
        _turn._listeners.clear()

    reset()
    yield
    reset()


def test_creates_an_sdk_provider_when_the_app_has_none() -> None:
    emet_sdk.configure()
    processor = _configure._processor
    provider = trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)
    assert processor in provider._active_span_processor._span_processors
    assert _turn._listeners == [processor]


def test_adds_to_the_apps_provider_without_replacing_it(caplog: pytest.LogCaptureFixture) -> None:
    provider = TracerProvider()
    trace.set_tracer_provider(provider)
    emet_sdk.configure()
    processor = _configure._processor
    assert trace.get_tracer_provider() is provider
    assert processor in provider._active_span_processor._span_processors
    with (
        caplog.at_level("DEBUG", logger="emet_sdk"),
        emet_sdk.turn("t", "c"),
        provider.get_tracer("test").start_as_current_span("chat"),
    ):
        pass
    assert "captured a turn with 1 spans" in caplog.text


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


def test_a_configured_app_hands_on_its_turns(caplog: pytest.LogCaptureFixture) -> None:
    emet_sdk.configure()
    tracer = trace.get_tracer("test")
    with caplog.at_level("DEBUG", logger="emet_sdk"), emet_sdk.turn("t", "c"), tracer.start_as_current_span("chat"):
        pass
    assert "captured a turn with 1 spans" in caplog.text
