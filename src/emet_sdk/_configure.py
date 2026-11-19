"""configure(): adds the Emet processor to the app's TracerProvider, never replacing it, and starts sending turns."""

from __future__ import annotations

import atexit
import json
import logging
import os
import threading
from collections.abc import Callable
from uuid import UUID

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider

from emet_sdk import _turn
from emet_sdk._delivery import DeliveryResult, captured, new_result
from emet_sdk._envelope import TurnSkipped, build_envelope
from emet_sdk._processor import CapturedTurn, EmetSpanProcessor
from emet_sdk._redact import Scrubber
from emet_sdk._report import ReportResult, Reports
from emet_sdk._transport import DEFAULT_ENDPOINT, ENDPOINT_VARIABLE, Sender, resolve_endpoint
from emet_sdk._turn import TurnInfo
from emet_sdk._worker import Worker

logger = logging.getLogger("emet_sdk")

KEY_VARIABLE = "EMET_APPLICATION_KEY"
OFF_MESSAGE = f"Emet is off: {KEY_VARIABLE} is not set. Create a key for your Application in Emet and set it."
EXIT_FLUSH_SECONDS = 5.0

_lock = threading.Lock()
_processor: EmetSpanProcessor | None = None
_worker: Worker | None = None
_reports: Reports | None = None
_said_off = False
_said_bad_endpoint = False
_said_bad_provider = False


class _Pipeline:
    def __init__(
        self,
        sender: Sender,
        reports: Reports,
        endpoint: str,
        key: str,
        service_name: str | None,
        on_delivery: Callable[[DeliveryResult], None] | None,
    ) -> None:
        self._sender = sender
        self._reports = reports
        self._endpoint = endpoint
        self._secrets = (key,)
        self._service_name = service_name
        self._on_delivery = on_delivery
        self._last_outcome: tuple[object, ...] | None = None

    def handle(self, turn: CapturedTurn) -> DeliveryResult:
        result: DeliveryResult | None = None
        try:
            result = self._send(turn)
            return result
        finally:
            # Also when sending raised, so report() is not left waiting for this turn
            self._reports.record(turn.info.id, result)

    def _send(self, turn: CapturedTurn) -> DeliveryResult:
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
    endpoint: str | None = None,
) -> None:
    """Adds Emet to the global TracerProvider, creating an SDK provider when the app has none.

    Reads the key from EMET_APPLICATION_KEY; without it, logs that Emet is off and does nothing else. Call it after
    the app's own tracing setup, such as logfire.configure(): OpenTelemetry keeps the first global provider. An app
    that already exports elsewhere keeps doing so, with the same sampling. Calling it again does nothing.

    service_name names the app in Emet. on_delivery receives each turn's delivery result on Emet's own thread.
    endpoint is for testing against Emet on this machine; without it, EMET_ENDPOINT is used, then production.
    """
    global _processor, _worker, _reports, _said_off, _said_bad_endpoint, _said_bad_provider
    # An explicit bad endpoint raises here, before anything is installed
    origin, source = resolve_endpoint(endpoint, None) if endpoint is not None else (DEFAULT_ENDPOINT, "")
    with _lock:
        if _processor is not None:
            return
        if endpoint is None:
            try:
                origin, source = resolve_endpoint(None, os.environ.get(ENDPOINT_VARIABLE))
            except ValueError:
                # The value is not logged: a URL can carry credentials
                if not _said_bad_endpoint:
                    _said_bad_endpoint = True
                    logger.error(
                        "Emet is off: %s is not https://app.emet.so or a localhost address. "
                        "Unset it, or set it to your local Emet, such as http://localhost:8000.",
                        ENDPOINT_VARIABLE,
                    )
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
        # Not an isinstance check: Logfire's provider wraps the SDK's without subclassing it
        add_span_processor = getattr(provider, "add_span_processor", None)
        if not callable(add_span_processor):
            if not _said_bad_provider:
                _said_bad_provider = True
                logger.error(
                    "Emet is off: the app's tracer provider, %s, cannot take a span processor. "
                    "Set an opentelemetry.sdk.trace.TracerProvider before calling emet_sdk.configure().",
                    type(provider).__name__,
                )
            return
        reports = Reports(origin, key)
        pipeline = _Pipeline(Sender(origin, key), reports, origin, key, service_name, on_delivery)
        worker = Worker(pipeline.handle, pipeline.deliver)

        def submit(turn: CapturedTurn) -> None:
            dropped = worker.submit(turn)
            if dropped:
                # On record, so report() does not wait for a turn that will never be sent
                reports.record(turn.info.id, new_result("skipped", dropped))

        processor = EmetSpanProcessor(submit)
        try:
            add_span_processor(processor)
        except Exception:
            logger.exception("Emet is off: the app's tracer provider refused its span processor")
            return
        _turn._listeners.append(processor)  # pyright: ignore[reportPrivateUsage]
        _processor, _worker, _reports = processor, worker, reports
        if origin != DEFAULT_ENDPOINT:
            # A local endpoint left in a deployed app would lose every turn as unreachable
            logger.warning(
                "Emet sends turns to %s, not production (set by %s). Remove it to send to production.", origin, source
            )
        atexit.register(flush, EXIT_FLUSH_SECONDS)


def flush(timeout: float = 10.0) -> bool:
    """Waits until every finished turn has been sent. Returns False when the timeout ran out first.

    Call it before a short-lived process exits; Emet also flushes for up to 5 seconds at exit. A turn whose
    turn() block or spans are still open is not finished yet.
    """
    worker = _worker
    return True if worker is None else worker.flush(timeout)


def report(turn: TurnInfo | str | UUID, *, timeout: float) -> ReportResult:
    """Waits up to timeout seconds for Emet's report on a turn and returns it, or the reason there is none.

    turn is what the turn() block gave (`with emet_sdk.turn(...) as sent`), asked after the block has closed, or
    the trace id from a delivery result. Emet says when to ask again while it is still checking. A timeout of 0
    never waits: it asks once about a trace id, and answers not_ready for a turn that is still being sent. No
    request starts once the timeout has passed; one started with under a second left gets one second. Never
    raises: a network or server error is a result too. Check result.flagged, and read result.document for the
    answer and its claims. Call areport() from async code, since this one blocks.
    """
    try:
        reports = _readable()
        return reports if isinstance(reports, ReportResult) else reports.read(turn, timeout)
    except Exception:  # never raise into the app
        logger.exception("Emet failed to read a report")
        return ReportResult("unavailable", "sdk_error")


async def areport(turn: TurnInfo | str | UUID, *, timeout: float) -> ReportResult:
    """report() for async code: the same result, without blocking the event loop."""
    try:
        reports = _readable()
        return reports if isinstance(reports, ReportResult) else await reports.aread(turn, timeout)
    except Exception:  # never raise into the app
        logger.exception("Emet failed to read a report")
        return ReportResult("unavailable", "sdk_error")


def _readable() -> Reports | ReportResult:
    reports, worker = _reports, _worker
    if reports is None or worker is None:
        return ReportResult("not_sent", "emet_off")
    if worker.on_own_thread():
        # Called from on_delivery: waiting here would hold up every turn still to be sent
        return ReportResult("unavailable", "sdk_thread")
    return reports
