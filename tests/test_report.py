from __future__ import annotations

import http.client
import json
import logging
import urllib.error
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import pytest

import emet_sdk
from emet_sdk import _report
from emet_sdk._delivery import new_result
from emet_sdk._report import ReportResult, Reports
from emet_sdk._transport import Reply
from emet_sdk._turn import TurnInfo
from emet_sdk._version import VERSION

KEY = "emet_test_key_not_real"
TRACE = "11111111-1111-1111-1111-111111111111"
URL = f"https://app.emet.so/api/v1/traces/{TRACE}/report/"


def document(status: str = "flagged", **extra: Any) -> dict[str, Any]:
    """An annotated answer as Emet returns it, with every value synthetic."""
    return {
        "version": 1,
        "report": {
            "id": "R-1",
            "uuid": "33333333-3333-3333-3333-333333333333",
            "revision": 1,
            "status": status,
            "status_text": status.title(),
            "checked_at": "2026-11-18T10:00:00+00:00",
            "digest": "d" * 64,
        },
        "trace_id": "abc-root",
        "conversation_id": "thread-1",
        "environment": "",
        "answer": {"text": "Spend was $40.", "sha256": "a" * 64},
        "counts": {"claims": 1, "verified": 0, "clarified": 0, "disputed": 1, "corrected": 0, "unchecked": 0},
        "claims": [
            {
                "id": "44444444-4444-4444-4444-444444444444",
                "sequence": 1,
                "text": "Spend was $40.",
                "verdict": "disputed",
                "verdict_text": "Disputed",
                "reason": "The tool returned 30.",
                "clarification": "",
                "correction": "",
                "ranges": [{"kind": "passage", "start": 0, "end": 14}, {"kind": "figure", "start": 10, "end": 13}],
            }
        ],
        **extra,
    }


def ready(status: str = "flagged", **extra: Any) -> Reply:
    return Reply(200, {"Cache-Control": "no-store"}, json.dumps(document(status, **extra)).encode())


def pending(retry_after: str | None = "5") -> Reply:
    headers = {} if retry_after is None else {"Retry-After": retry_after}
    return Reply(202, headers, b'{"status": "pending", "retry_after": 5}')


def error(status: int, code: str = "") -> Reply:
    body = {"error": "x", "code": code, "status_code": status} if code else {}
    return Reply(status, {}, json.dumps(body).encode())


Step = Reply | BaseException | Callable[["FakeEmet"], Reply]


@dataclass
class FakeEmet:
    """Answers each read with the next scripted step and keeps a clock that only moves when told."""

    steps: list[Step]
    now: float = 0.0
    urls: list[str] = field(default_factory=lambda: [])
    headers: list[dict[str, str]] = field(default_factory=lambda: [])
    timeouts: list[float] = field(default_factory=lambda: [])
    slept: list[float] = field(default_factory=lambda: [])

    def get(self, url: str, headers: dict[str, str], timeout: float) -> Reply:
        self.urls.append(url)
        self.headers.append(headers)
        self.timeouts.append(timeout)
        step = self.steps.pop(0) if len(self.steps) > 1 else self.steps[0]
        if isinstance(step, BaseException):
            self.now += timeout if isinstance(step, TimeoutError) else 0
            raise step
        return step(self) if callable(step) else step

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds

    async def async_sleep(self, seconds: float) -> None:
        self.sleep(seconds)

    def reports(self) -> Reports:
        return Reports(
            "https://app.emet.so",
            KEY,
            get=self.get,
            clock=lambda: self.now,
            sleep=self.sleep,
            async_sleep=self.async_sleep,
        )


def test_a_finished_report_comes_back_as_emet_sent_it() -> None:
    emet = FakeEmet([ready(added_later={"by": "Emet"})])
    result = emet.reports().read(TRACE, 30)
    assert (result.status, result.reason, result.trace) == ("ready", "", TRACE)
    # The document is not re-modelled, so a field Emet adds later is kept
    assert result.document == document(added_later={"by": "Emet"})
    assert result.flagged is True
    assert emet.urls == [URL]
    assert emet.headers[0] == {
        "Authorization": f"Bearer {KEY}",
        "Accept": "application/json",
        "User-Agent": f"emet-sdk/{VERSION}",
    }
    assert emet.slept == []


@pytest.mark.parametrize("status", ["clarified", "clear", "unchecked", "nothing"])
def test_only_a_flagged_report_reads_flagged(status: str) -> None:
    result = FakeEmet([ready(status)]).reports().read(TRACE, 30)
    assert (result.status, result.flagged) == ("ready", False)


@pytest.mark.parametrize("body", [{"report": "flagged"}, {"report": {"status": ["flagged"]}}, {"version": 2}])
def test_flagged_is_false_for_a_document_it_cannot_read(body: dict[str, Any]) -> None:
    assert ReportResult("ready", document=body).flagged is False
    assert ReportResult("not_ready").flagged is False


def test_it_waits_as_long_as_emet_asks_then_returns_the_report() -> None:
    emet = FakeEmet([pending("5"), pending("7"), ready()])
    result = emet.reports().read(TRACE, 60)
    assert (result.status, result.flagged) == ("ready", True)
    assert emet.slept == [5.0, 7.0]
    assert len(emet.urls) == 3


@pytest.mark.parametrize(
    ("header", "waited"),
    [(None, 5.0), ("soon", 5.0), ("nan", 5.0), ("0", 1.0), ("-3", 1.0), ("2.5", 2.5)],
)
def test_an_odd_retry_after_still_gives_a_sane_wait(header: str | None, waited: float) -> None:
    emet = FakeEmet([pending(header), ready()])
    assert emet.reports().read(TRACE, 60).status == "ready"
    assert emet.slept == [waited]


def test_the_retry_after_header_is_read_in_any_case() -> None:
    emet = FakeEmet([Reply(202, {"retry-after": "3"}, b"{}"), ready()])
    assert emet.reports().read(TRACE, 60).status == "ready"
    assert emet.slept == [3.0]


def test_it_stops_at_the_callers_budget() -> None:
    emet = FakeEmet([pending("5")])
    result = emet.reports().read(TRACE, 12)
    assert (result.status, result.reason, result.trace, result.document) == ("not_ready", "", TRACE, None)
    # Asked at 0, 5 and 10 seconds; the next wait would end past 12
    assert emet.slept == [5.0, 5.0]
    assert len(emet.urls) == 3
    assert emet.now == 10.0


@pytest.mark.parametrize("timeout", [0, -5, float("nan")])
def test_no_budget_asks_once(timeout: float) -> None:
    emet = FakeEmet([pending("5")])
    assert emet.reports().read(TRACE, timeout).status == "not_ready"
    assert (len(emet.urls), emet.slept, emet.timeouts) == (1, [], [1.0])


@pytest.mark.parametrize(
    ("reply", "status", "reason"),
    [
        (error(404, "not_assessed"), "not_assessed", ""),
        (error(404, "failed"), "failed", ""),
        (error(404, "not_found"), "not_found", ""),
        (error(404), "not_found", ""),
        (error(404, "elsewhere"), "not_found", "elsewhere"),
        (error(403, "authentication_failed"), "refused", "authentication_failed"),
        (error(401), "refused", ""),
        (error(429, "throttled"), "rate_limited", ""),
        (error(302), "unavailable", "unexpected_response"),
        (error(400, "invalid"), "unavailable", "unexpected_response"),
        (Reply(200, {}, b"not json"), "unavailable", "unexpected_response"),
        (Reply(200, {}, b"[1, 2]"), "unavailable", "unexpected_response"),
        (Reply(200, {}, b"\xff"), "unavailable", "unexpected_response"),
    ],
)
def test_every_other_answer_is_a_plain_result(reply: Reply, status: str, reason: str) -> None:
    emet = FakeEmet([reply])
    result = emet.reports().read(TRACE, 60)
    assert (result.status, result.reason, result.trace) == (status, reason, TRACE)
    assert (result.document, result.flagged) == (None, False)
    assert (len(emet.urls), emet.slept) == (1, [])


@pytest.mark.parametrize(
    ("steps", "reason", "asked"),
    [
        ([error(500), error(503)], "server_error", 2),
        ([TimeoutError(), TimeoutError()], "timeout", 2),
        ([urllib.error.URLError(TimeoutError()), TimeoutError()], "timeout", 2),
        ([urllib.error.URLError(ConnectionRefusedError())], "unreachable", 1),
        ([ConnectionResetError()], "unreachable", 1),
        ([http.client.IncompleteRead(b"{")], "unreachable", 1),
    ],
)
def test_a_network_or_server_error_is_a_result_not_an_exception(steps: list[Step], reason: str, asked: int) -> None:
    emet = FakeEmet([*steps, ready()])
    result = emet.reports().read(TRACE, 60)
    assert (result.status, result.reason, result.trace) == ("unavailable", reason, TRACE)
    assert len(emet.urls) == asked


def test_one_retry_recovers_a_server_error_or_a_timeout() -> None:
    emet = FakeEmet([error(502), pending("5"), TimeoutError(), ready()])
    assert emet.reports().read(TRACE, 60).status == "ready"
    assert emet.slept == [0.5, 5.0]
    # Each request gets at most 8 seconds
    assert emet.timeouts == [8.0, 8.0, 8.0, 8.0]


def test_no_retry_when_the_budget_is_spent() -> None:
    def slow_failure(emet: FakeEmet) -> Reply:
        emet.now += 9.5
        return error(500)

    emet = FakeEmet([slow_failure, ready()])
    result = emet.reports().read(TRACE, 10)
    assert (result.status, result.reason, len(emet.urls)) == ("unavailable", "server_error", 1)


def test_a_request_never_gets_more_time_than_the_budget_has_left() -> None:
    emet = FakeEmet([pending("5"), ready()])
    emet.reports().read(TRACE, 7)
    assert emet.timeouts == [7.0, 2.0]


@pytest.mark.parametrize("trace", [TRACE.upper(), f"  {TRACE} ", TRACE.replace("-", ""), UUID(TRACE)])
def test_the_trace_id_is_sent_in_one_form(trace: Any) -> None:
    emet = FakeEmet([ready()])
    assert emet.reports().read(trace, 0).trace == TRACE
    assert emet.urls == [URL]


@pytest.mark.parametrize(
    ("trace", "status", "reason"),
    [
        ("", "not_sent", "no_trace"),
        (None, "not_sent", "no_trace"),
        (7, "not_sent", "no_trace"),
        ("abc-root", "not_found", ""),
        (f"{TRACE}/../../users", "not_found", ""),
    ],
)
def test_a_value_that_is_no_trace_id_never_reaches_emet(trace: Any, status: str, reason: str) -> None:
    emet = FakeEmet([ready()])
    result = emet.reports().read(trace, 30)
    assert (result.status, result.reason, emet.urls) == (status, reason, [])


def sent(trace: str = TRACE) -> dict[str, Any]:
    return {**new_result("sent", trace_id="abc-root"), "trace": trace}


def test_a_turn_reads_the_trace_its_send_landed_on() -> None:
    emet = FakeEmet([ready()])
    reports = emet.reports()
    reports.record(7, sent())
    result = reports.read(TurnInfo(7, "team", "thread-1", None), 30)
    assert (result.status, result.trace, emet.urls) == ("ready", TRACE, [URL])


def test_it_waits_for_a_turn_that_is_still_being_sent() -> None:
    emet = FakeEmet([ready()])
    reports = emet.reports()
    turn = TurnInfo(7, "team", "thread-1", None)
    real_sleep = emet.sleep

    def sleep(seconds: float) -> None:
        real_sleep(seconds)
        if len(emet.slept) == 3:
            reports.record(7, sent())

    reports._sleep = sleep
    assert reports.read(turn, 30).status == "ready"
    assert emet.slept == [0.05, 0.05, 0.05]


def test_a_turn_never_sent_in_time_is_not_ready() -> None:
    emet = FakeEmet([ready()])
    result = emet.reports().read(TurnInfo(7, "team", "thread-1", None), 0.2)
    assert (result.status, result.reason, result.trace) == ("not_ready", "still_sending", "")
    assert emet.urls == []
    assert emet.now <= 0.2


@pytest.mark.parametrize(
    ("delivery", "status", "reason"),
    [
        (new_result("skipped", "no_spans"), "not_sent", "no_spans"),
        (new_result("stopped", "http_4xx"), "not_sent", "http_4xx"),
        (None, "not_sent", "sdk_error"),
        (sent(""), "not_sent", "no_trace"),
        (sent("t-uuid"), "not_found", ""),
    ],
)
def test_a_turn_that_left_no_trace_says_why(delivery: dict[str, Any] | None, status: str, reason: str) -> None:
    emet = FakeEmet([ready()])
    reports = emet.reports()
    reports.record(7, delivery)
    result = reports.read(TurnInfo(7, "team", "thread-1", None), 30)
    assert (result.status, result.reason, emet.urls, emet.slept) == (status, reason, [], [])


def test_a_turn_stopped_part_way_is_still_asked_about() -> None:
    emet = FakeEmet([error(404, "not_assessed")])
    reports = emet.reports()
    reports.record(7, {**new_result("stopped", "timeout"), "trace": TRACE})
    assert reports.read(TurnInfo(7, "team", "thread-1", None), 30).status == "not_assessed"


def test_asking_inside_the_turn_block_answers_at_once() -> None:
    emet = FakeEmet([ready()])
    reports = emet.reports()
    with emet_sdk.turn("team", "thread-1") as turn:
        result = reports.read(turn, 30)
    assert (result.status, result.reason, emet.slept, emet.urls) == ("not_sent", "turn_still_open", [], [])


def test_only_the_latest_turns_are_remembered(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_report, "MAX_REMEMBERED_TURNS", 2)
    emet = FakeEmet([ready()])
    reports = emet.reports()
    for turn_id in (1, 2, 3):
        reports.record(turn_id, sent())
    assert list(reports._sent) == [2, 3]
    assert reports.read(TurnInfo(1, "team", "thread-1", None), 0).reason == "still_sending"
    assert reports.read(TurnInfo(3, "team", "thread-1", None), 0).status == "ready"


def test_a_problem_is_logged_when_it_changes_and_never_with_the_key(caplog: pytest.LogCaptureFixture) -> None:
    emet = FakeEmet(
        [error(403, "authentication_failed"), error(403, "authentication_failed"), ready(), error(429), error(429)]
    )
    reports = emet.reports()
    with caplog.at_level(logging.DEBUG, logger="emet_sdk"):
        statuses = [reports.read(TRACE, 0).status for _ in range(5)]
    assert statuses == ["refused", "refused", "ready", "rate_limited", "rate_limited"]
    lines = [record.getMessage() for record in caplog.records]
    assert lines == [
        "Emet could not return a report: refused (authentication_failed)",
        "Emet could not return a report: rate_limited",
    ]
    # Nothing from the document is logged either
    assert KEY not in caplog.text and "Spend" not in caplog.text


def test_a_problem_that_returns_after_a_good_read_is_logged_again(caplog: pytest.LogCaptureFixture) -> None:
    emet = FakeEmet([error(429), ready(), error(429)])
    reports = emet.reports()
    for _ in range(3):
        reports.read(TRACE, 0)
    assert caplog.text.count("rate_limited") == 2


def test_an_outcome_that_is_no_fault_is_not_logged(caplog: pytest.LogCaptureFixture) -> None:
    emet = FakeEmet([pending(), error(404, "not_assessed"), error(404, "failed"), error(404, "not_found")])
    reports = emet.reports()
    with caplog.at_level(logging.DEBUG, logger="emet_sdk"):
        for _ in range(4):
            reports.read(TRACE, 0)
    assert caplog.records == []


async def test_the_async_read_waits_without_a_blocking_sleep() -> None:
    emet = FakeEmet([pending("5"), ready()])
    reports = emet.reports()
    waits: list[float] = []

    async def async_sleep(seconds: float) -> None:
        waits.append(seconds)
        emet.now += seconds

    reports._async_sleep = async_sleep
    result = await reports.aread(TRACE, 60)
    assert (result.status, result.flagged, result.document) == ("ready", True, document())
    assert (waits, emet.slept) == ([5.0], [])


async def test_the_async_read_stops_at_the_budget_and_never_raises() -> None:
    emet = FakeEmet([pending("5")])
    assert (await emet.reports().aread(TRACE, 12)).status == "not_ready"
    assert emet.slept == [5.0, 5.0]
    emet = FakeEmet([ConnectionResetError()])
    result = await emet.reports().aread(TRACE, 12)
    assert (result.status, result.reason) == ("unavailable", "unreachable")


async def test_the_async_read_sees_a_turn_block_that_is_still_open() -> None:
    emet = FakeEmet([ready()])
    reports = emet.reports()
    with emet_sdk.turn("team", "thread-1") as turn:
        result = await reports.aread(turn, 30)
    assert (result.status, result.reason, emet.slept) == ("not_sent", "turn_still_open", [])
