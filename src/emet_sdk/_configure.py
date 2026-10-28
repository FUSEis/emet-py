"""configure(): adds the Emet processor to the app's TracerProvider, never replacing it."""

from __future__ import annotations

import logging
import threading

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider

from emet_sdk import _turn
from emet_sdk._processor import CapturedTurn, EmetSpanProcessor

logger = logging.getLogger("emet_sdk")

_lock = threading.Lock()
_processor: EmetSpanProcessor | None = None


def _hand_on(captured: CapturedTurn) -> None:
    # The envelope and transport replace this in the next milestone
    logger.debug("Emet captured a turn with %d spans", len(captured.spans))


def configure() -> EmetSpanProcessor:
    """Adds Emet to the global TracerProvider, creating an SDK provider when the app has none.

    An app that already exports elsewhere keeps doing so, with the same sampling. Calling it again does nothing.
    """
    global _processor
    with _lock:
        if _processor is not None:
            return _processor
        provider = trace.get_tracer_provider()
        if not isinstance(provider, TracerProvider):
            if not isinstance(provider, trace.ProxyTracerProvider):
                raise TypeError(
                    f"Emet needs the OpenTelemetry SDK's TracerProvider, but the app set {type(provider).__name__}. "
                    "Set an opentelemetry.sdk.trace.TracerProvider before calling emet_sdk.configure()."
                )
            provider = TracerProvider()
            trace.set_tracer_provider(provider)
        processor = EmetSpanProcessor(_hand_on)
        provider.add_span_processor(processor)
        _turn._listeners.append(processor)  # pyright: ignore[reportPrivateUsage]
        _processor = processor
        return processor
