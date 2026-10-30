"""configure(): adds the Emet processor to the app's TracerProvider, never replacing it, and starts sending turns."""

from __future__ import annotations

import atexit
import json
import logging
import os
import threading
from collections.abc import Callable

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider

from emet_sdk import _turn
from emet_sdk._delivery import DeliveryResult, captured, new_result
from emet_sdk._envelope import TurnSkipped, build_envelope
from emet_sdk._processor import CapturedTurn, EmetSpanProcessor
from emet_sdk._redact import Scrubber
from emet_sdk._transport import DEFAULT_ENDPOINT, Sender, check_endpoint
from emet_sdk._worker import Worker

logger = logging.getLogger("emet_sdk")

KEY_VARIABLE = "EMET_APPLICATION_KEY"
OFF_MESSAGE = f"Emet is off: {KEY_VARIABLE} is not set. Locally, run `uvx emet-cli login`."
EXIT_FLUSH_SECONDS = 5.0

_lock = threading.Lock()
_processor: EmetSpanProcessor | None = None
_worker: Worker | None = None
_said_off = False


class _Pipeline:
    def __init__(
        self,
        sender: Sender,
        endpoint: str,
        key: str,
        service_name: str | None,
        on_delivery: Callable[[DeliveryResult], None] | None,
    ) -> None:
        self._sender = sender
        self._endpoint = endpoint
        self._secrets = (key,)
        self._service_name = service_name
        self._on_delivery = on_delivery
        self._last_outcome: tuple[object, ...] | None = None

    def handle(self, turn: CapturedTurn) -> DeliveryResult:
        try:
            envelope = build_envelope(turn, self._secrets)
        except TurnSkipped as skipped:
            return new_result("skipped", skipped.reason)
        if self._service_name:
            envelope["resource"]["service.name"] = Scrubber(self._secrets).text(self._service_name)
        return self._sender.send(envelope)

    def deliver(self, result: DeliveryResult) -> None:
        outcome = (
            result["status"],
            result["reason"],
            result["code"],
            result["unattributed"],
            result["late"] or result["late_after_retry"],
            json.dumps(result["gaps"], sort_keys=True),
        )
        if outcome != self._last_outcome:
            self._last_outcome = outcome
            self._log(result)
        if self._on_delivery is not None:
            try:
                self._on_delivery(result)
            except Exception:
                logger.exception("Emet's on_delivery callback failed")

    def _log(self, result: DeliveryResult) -> None:
        trace_id, reason = result["trace_id"], result["reason"]
        if result["status"] == "skipped":
            logger.warning("Emet skipped a turn: %s", reason)
        elif result["status"] == "stopped":
            detail = f"{reason} ({result['code']})" if result["code"] else reason
            logger.warning("Emet stopped sending turn %s: %s", trace_id, detail)
        elif captured(result):
            logger.info("Emet captured turn %s. It shows under AI usage: %s/ai-usage", trace_id, self._endpoint)
        else:
            problems = [
                *(["tenant_ref matched no Team"] if result["unattributed"] else []),
                *(["it arrived after the trace closed"] if result["late"] or result["late_after_retry"] else []),
                *(f"{key}: {', '.join(names)}" for key, names in result["gaps"].items() if names),
            ]
            logger.warning("Emet received turn %s, but %s", trace_id, "; ".join(problems))


def configure(
    *,
    service_name: str | None = None,
    on_delivery: Callable[[DeliveryResult], None] | None = None,
    endpoint: str = DEFAULT_ENDPOINT,
) -> None:
    """Adds Emet to the global TracerProvider, creating an SDK provider when the app has none.

    Reads the key from EMET_APPLICATION_KEY; without it, logs that Emet is off and does nothing else. Call it after
    the app's own tracing setup, such as logfire.configure(): OpenTelemetry keeps the first global provider. An app
    that already exports elsewhere keeps doing so, with the same sampling. Calling it again does nothing.

    service_name names the app in Emet. on_delivery receives each turn's delivery result on Emet's own thread.
    endpoint is for testing against Emet on this machine.
    """
    global _processor, _worker, _said_off
    origin = check_endpoint(endpoint)
    with _lock:
        if _processor is not None:
            return
        key = os.environ.get(KEY_VARIABLE, "").strip()
        if not key:
            if not _said_off:
                _said_off = True
                logger.warning(OFF_MESSAGE)
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
        pipeline = _Pipeline(Sender(origin, key), origin, key, service_name, on_delivery)
        worker = Worker(pipeline.handle, pipeline.deliver)
        processor = EmetSpanProcessor(worker.submit)
        provider.add_span_processor(processor)
        _turn._listeners.append(processor)  # pyright: ignore[reportPrivateUsage]
        _processor, _worker = processor, worker
        atexit.register(flush, EXIT_FLUSH_SECONDS)


def flush(timeout: float = 10.0) -> bool:
    """Waits until every finished turn has been sent. Returns False when the timeout ran out first.

    Call it before a short-lived process exits; Emet also flushes for up to 5 seconds at exit. A turn whose
    turn() block or spans are still open is not finished yet.
    """
    worker = _worker
    return True if worker is None else worker.flush(timeout)
