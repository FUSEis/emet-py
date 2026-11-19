"""Reads a turn's report back from Emet: the document once the check has finished, or a plain reason."""

from __future__ import annotations

import asyncio
import http.client
import json
import logging
import os
import threading
import time
import urllib.error
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Generator
from dataclasses import dataclass
from typing import Any, cast
from uuid import UUID

from emet_sdk import _transport
from emet_sdk._delivery import DeliveryResult
from emet_sdk._transport import (
    ATTEMPT_TIMEOUT,
    MIN_ATTEMPT,
    SERVER_ERROR_WAIT,
    Get,
    Reply,
    check_endpoint,
    retry_after_header,
    retry_after_seconds,
)
from emet_sdk._turn import TurnInfo, is_open
from emet_sdk._version import VERSION

logger = logging.getLogger("emet_sdk")

REPORT_PATH = "/api/v1/traces/{trace}/report/"
# Used when a 202 names no wait
DEFAULT_RETRY_AFTER = 5.0
# A zero wait would ask in a tight loop
MIN_RETRY_AFTER = 1.0
# How often a turn still being sent is looked for
SENDING_POLL = 0.05
MAX_REMEMBERED_TURNS = 1000
FLAGGED = "flagged"
MISSES = ("not_assessed", "failed", "not_found")
# Logged, since they need someone's attention
PROBLEMS = ("refused", "rate_limited", "unavailable")


@dataclass(frozen=True)
class ReportResult:
    """What asking Emet for a turn's report gave.

    status is ready, not_ready, not_assessed, failed, not_found, refused, rate_limited, not_sent or unavailable.
    reason says more where there is more to say. trace is Emet's id of the trace, to ask again later. document is
    the report as Emet returned it, and only a ready result has one.
    """

    status: str
    reason: str = ""
    trace: str = ""
    document: dict[str, Any] | None = None

    @property
    def flagged(self) -> bool:
        """Whether Emet disputed a claim in the answer. False for every result without a document."""
        report = (self.document or {}).get("report")
        return isinstance(report, dict) and cast("dict[str, Any]", report).get("status") == FLAGGED


class Reports:
    """Asks Emet for reports, and remembers the trace each sent turn landed on."""

    def __init__(
        self,
        endpoint: str,
        key: str,
        get: Get | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        async_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._url = check_endpoint(endpoint) + REPORT_PATH
        self._headers = {
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
            "User-Agent": f"emet-sdk/{VERSION}",
        }
        self._get = get or _transport.urllib_get
        self._clock = clock
        self._sleep = sleep
        self._async_sleep = async_sleep
        self._last_problem: tuple[str, str] | None = None
        self._reset()
        if hasattr(os, "register_at_fork"):  # not on Windows
            os.register_at_fork(after_in_child=self._reset)

    def _reset(self) -> None:
        # A forked child may inherit the lock held by a parent thread; the parent's turns are the parent's
        self._lock = threading.Lock()
        self._sent: OrderedDict[int, tuple[str, str]] = OrderedDict()

    def record(self, turn_id: int, delivery: DeliveryResult | None) -> None:
        """Remembers what sending a turn gave: Emet's trace id, or why there is none. None means sending failed."""
        trace = str(delivery["trace"]) if delivery else ""
        reason = "" if trace else (str(delivery["reason"]) if delivery else "sdk_error") or "no_trace"
        with self._lock:
            self._sent[turn_id] = (trace, reason)
            while len(self._sent) > MAX_REMEMBERED_TURNS:
                self._sent.popitem(last=False)

    def read(self, turn: object, timeout: float) -> ReportResult:
        steps = self._steps(turn, *self._budget(timeout))
        while not isinstance(step := _advance(steps), ReportResult):
            self._sleep(step)
        return self._noted(step)

    async def aread(self, turn: object, timeout: float) -> ReportResult:
        # The clock starts here: waiting for a free thread counts against the budget
        steps = self._steps(turn, *self._budget(timeout))
        # Each request runs on a thread, so the event loop is never blocked
        while not isinstance(step := await asyncio.to_thread(_advance, steps), ReportResult):
            await self._async_sleep(step)
        return self._noted(step)

    def _budget(self, timeout: float) -> tuple[float, bool]:
        """Returns when the read must stop, and whether it has no time to wait at all."""
        budget = float(timeout)
        # A negative or NaN budget is no budget
        once = not budget > 0
        return self._clock() + (0.0 if once else budget), once

    def _steps(self, turn: object, deadline: float, once: bool) -> Generator[float, None, ReportResult]:
        """Runs one read, yielding each wait in seconds so the caller sleeps its own way."""
        while (found := self._trace(turn)) is None:
            left = deadline - self._clock()
            if left <= 0:
                return ReportResult("not_ready", "still_sending")
            yield min(SENDING_POLL, left)
        if isinstance(found, ReportResult):
            return found
        if not once and self._clock() >= deadline:
            # The budget went on waiting for the send, or for a free thread. Without a budget, it still asks once
            return ReportResult("not_ready", trace=found)
        while True:
            answer = self._ask(found, deadline)
            if isinstance(answer, ReportResult):
                return answer
            # The next request needs MIN_ATTEMPT left after the wait, so it never runs past the deadline
            if self._clock() + answer + MIN_ATTEMPT > deadline:
                return ReportResult("not_ready", trace=found)
            yield answer

    def _trace(self, turn: object) -> ReportResult | str | None:
        """Returns the trace to ask about, a result when there is none, or None while the turn is still being sent."""
        if isinstance(turn, TurnInfo):
            with self._lock:
                sent = self._sent.get(turn.id)
            if sent is None:
                if is_open(turn):
                    # The turn is only sent once its block has closed
                    return ReportResult("not_sent", "turn_still_open")
                return None
            trace, reason = sent
            if not trace:
                return ReportResult("not_sent", reason)
        else:
            trace = str(turn).strip() if isinstance(turn, (str, UUID)) else ""
            if not trace:
                return ReportResult("not_sent", "no_trace")
        try:
            # The id goes into the URL path, so only a real UUID gets there
            return str(UUID(trace))
        except ValueError:
            return ReportResult("not_found")

    def _ask(self, trace: str, deadline: float) -> ReportResult | float:
        """Asks once, with one retry on a timeout or a server error. Returns the result, or the seconds to wait."""
        url = self._url.format(trace=trace)
        reason = ""
        for attempt in (1, 2):
            # Only a call's first request can have under MIN_ATTEMPT left; it gets that much, to ask at all
            timeout = max(MIN_ATTEMPT, min(ATTEMPT_TIMEOUT, deadline - self._clock()))
            wait = 0.0
            try:
                reply = self._get(url, self._headers, timeout)
            except TimeoutError:
                reason = "timeout"
            except urllib.error.URLError as error:
                if not isinstance(error.reason, TimeoutError):
                    return ReportResult("unavailable", "unreachable", trace)
                reason = "timeout"
            except (OSError, http.client.HTTPException):
                # HTTPException covers a connection closed mid-response
                return ReportResult("unavailable", "unreachable", trace)
            else:
                if reply.status < 500:
                    return _answer(reply, trace)
                reason = "server_error"
                wait = SERVER_ERROR_WAIT
            if attempt == 2 or deadline - self._clock() - wait < MIN_ATTEMPT:
                break
            if wait:
                self._sleep(wait)
        return ReportResult("unavailable", reason, trace)

    def _noted(self, result: ReportResult) -> ReportResult:
        problem = (result.status, result.reason) if result.status in PROBLEMS else None
        if problem is not None and problem != self._last_problem:
            detail = f"{result.status} ({result.reason})" if result.reason else result.status
            logger.warning("Emet could not return a report: %s", detail)
        # A problem that comes back after a good read is said again
        self._last_problem = problem
        return result


def _advance(steps: Generator[float, None, ReportResult]) -> ReportResult | float:
    try:
        return next(steps)
    except StopIteration as stop:
        return cast("ReportResult", stop.value)


def _answer(reply: Reply, trace: str) -> ReportResult | float:
    """Maps one reply below 500 to a result, or to the seconds a 202 asks to wait."""
    body = _object(reply.body)
    code = body.get("code")
    code = code if isinstance(code, str) else ""
    if reply.status == 200:
        report = body.get("report")
        status = cast("dict[str, Any]", report).get("status") if isinstance(report, dict) else None
        if not isinstance(status, str) or not status:
            # A 200 that is no report, from a proxy for one, must not read as a clear turn
            return ReportResult("unavailable", "unexpected_response", trace)
        return ReportResult("ready", trace=trace, document=body)
    if reply.status == 202:
        return _retry_after(reply.headers)
    if reply.status == 404:
        # A 404 without one of Emet's codes, such as an Emet too old to have the endpoint, reads not_found
        return ReportResult(code, trace=trace) if code in MISSES else ReportResult("not_found", code, trace)
    if reply.status in (401, 403):
        return ReportResult("refused", code, trace)
    if reply.status == 429:
        return ReportResult("rate_limited", trace=trace)
    return ReportResult("unavailable", "unexpected_response", trace)


def _object(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except ValueError:
        return {}
    return cast("dict[str, Any]", value) if isinstance(value, dict) else {}


def _retry_after(headers: dict[str, str]) -> float:
    seconds = retry_after_seconds(retry_after_header(headers))
    # Not a wait so short the loop spins
    return DEFAULT_RETRY_AFTER if seconds is None else max(MIN_RETRY_AFTER, seconds)
