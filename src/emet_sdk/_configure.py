"""configure(): adds the Emet processor to the app's TracerProvider, never replacing it."""

from __future__ import annotations

import logging
import threading

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider

from emet_sdk import _turn
from emet_sdk._envelope import build_envelope
from emet_sdk._processor import CapturedTurn, EmetSpanProcessor

logger = logging.getLogger("emet_sdk")

_lock = threading.Lock()
_processor: EmetSpanProcessor | None = None


def _hand_on(captured: CapturedTurn) -> None:
    envelope = build_envelope(captured)
    if envelope is None:
        return
    # Transport replaces this in the next milestone
    logger.debug("Emet built a turn with %d spans", len(envelope["spans"]))


def configure() -> None:
    """Adds Emet to the global TracerProvider, creating an SDK provider when the app has none.

    Call it after the app's own tracing setup, such as logfire.configure(): OpenTelemetry keeps the first global
    provider. An app that already exports elsewhere keeps doing so, with the same sampling. Calling it again does
    nothing.
    """
    global _processor
    with _lock:
        if _processor is not None:
            return
        if isinstance(trace.get_tracer_provider(), trace.ProxyTracerProvider):
            trace.set_tracer_provider(TracerProvider())
        # Read it back: another thread may have set the provider first
        provider = trace.get_tracer_provider()
        if not isinstance(provider, TracerProvider):
            raise TypeError(
                f"Emet needs the OpenTelemetry SDK's TracerProvider, but the app set {type(provider).__name__}. "
                "Set an opentelemetry.sdk.trace.TracerProvider before calling emet_sdk.configure()."
            )
        processor = EmetSpanProcessor(_hand_on)
        provider.add_span_processor(processor)
        _turn._listeners.append(processor)  # pyright: ignore[reportPrivateUsage]
        _processor = processor
