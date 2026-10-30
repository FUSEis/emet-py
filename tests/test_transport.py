from __future__ import annotations

import http.server
import json
import socket
import threading
import urllib.error
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any, ClassVar

import pytest

from emet_sdk import _transport
from emet_sdk._delivery import new_result
from emet_sdk._transport import Reply, Sender, check_endpoint, split, urllib_post
from emet_sdk._version import VERSION

KEY = "emet_test_key_not_real"


def accepted(spans: int, **extra: Any) -> Reply:
    body = {
        "trace": "11111111-1111-1111-1111-111111111111",
        "conversation": "22222222-2222-2222-2222-222222222222",
        "accepted": spans,
        "late": False,
        "unattributed": False,
        "gaps": {"missing_tool_arguments": [], "missing_tool_results": [], "conflicting_final_outputs": []},
        **extra,
    }
    return Reply(202, {"Content-Type": "application/json"}, json.dumps(body).encode())


def error(status: int, code: str = "", headers: dict[str, str] | None = None) -> Reply:
    body = {"error": "x", "code": code, "status_code": status, "details": {}} if code else {}
    return Reply(status, headers or {}, json.dumps(body).encode())


Step = Reply | BaseException | Callable[["FakeEmet", dict[str, Any]], Reply]


@dataclass
class FakeEmet:
    """Answers each post with the next scripted step and keeps a clock that only moves when told."""

    steps: list[Step]
    now: float = 0.0
    posts: list[dict[str, Any]] = field(default_factory=lambda: [])
    timeouts: list[float] = field(default_factory=lambda: [])
    headers: list[dict[str, str]] = field(default_factory=lambda: [])
    slept: list[float] = field(default_factory=lambda: [])

    def post(self, url: str, headers: dict[str, str], body: bytes, timeout: float) -> Reply:
        assert url == "https://app.emet.so/api/v1/traces/"
        batch = json.loads(body)
        self.posts.append(batch)
        self.timeouts.append(timeout)
        self.headers.append(headers)
        step = self.steps.pop(0) if self.steps else accepted(len(batch["spans"]))
        if isinstance(step, BaseException):
            self.now += timeout if isinstance(step, TimeoutError) else 0
            raise step
        return step(self, batch) if callable(step) else step

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds

    def sender(self) -> Sender:
        return Sender("https://app.emet.so", KEY, post=self.post, clock=lambda: self.now, sleep=self.sleep)


def envelope(spans: int = 3, payload: str = "x") -> dict[str, Any]:
    out: list[dict[str, Any]] = [{"span_id": "root", "parent_span_id": "", "name": "agent", "kind": "turn"}]
    for i in range(spans - 1):
        out.append(
            {"span_id": f"s{i}", "parent_span_id": "root", "name": f"tool{i}", "kind": "tool", "payload": payload}
        )
    return {
        "tenant_ref": "team_editorial",
        "conversation_id": "thread-1",
        "trace_id": "abc-root",
        "ingestion_mode": "sdk",
        "semconv_version": "emet-genai-1",
        "resource": {"service.name": "support-agent"},
        "spans": out,
        "end": True,
    }


def test_production_and_localhost_are_the_only_endpoints() -> None:
    assert check_endpoint("https://app.emet.so/") == "https://app.emet.so"
    assert check_endpoint("http://localhost:8000") == "http://localhost:8000"
    assert check_endpoint("http://127.0.0.1:8000/") == "http://127.0.0.1:8000"
    assert check_endpoint("http://[::1]:8000") == "http://[::1]:8000"
    for bad in (
        "http://app.emet.so",
        "https://app.emet.so.evil.example",
        "https://example.com",
        "https://app.emet.so/api/v1/traces/",
        "https://user:pw@app.emet.so",
        "ftp://localhost",
    ):
        with pytest.raises(ValueError):
            check_endpoint(bad)


def test_a_small_turn_is_one_batch() -> None:
    [batch] = split(envelope(3))
    assert batch["end"] is True
    assert [s["span_id"] for s in batch["spans"]] == ["s0", "s1", "root"]


def test_a_large_turn_splits_in_order_with_the_turn_span_and_end_last() -> None:
    batches = split(envelope(450))
    assert [len(b["spans"]) for b in batches] == [200, 200, 50]
    assert [b["end"] for b in batches] == [False, False, True]
    assert batches[-1]["spans"][-1]["kind"] == "turn"
    assert all(b["trace_id"] == "abc-root" and b["resource"] == {"service.name": "support-agent"} for b in batches)
    ids = [s["span_id"] for b in batches for s in b["spans"]]
    assert ids == [f"s{i}" for i in range(449)] + ["root"]


def test_batches_stay_under_the_byte_limit_and_a_huge_span_travels_whole(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_transport, "BATCH_MAX_BYTES", 1000)
    turn = envelope(4, payload="y" * 400)
    turn["spans"][2]["payload"] = "z" * 5000
    batches = split(turn)
    assert [[s["span_id"] for s in b["spans"]] for b in batches] == [["s0"], ["s1"], ["s2", "root"]]
    assert batches[1]["spans"][0]["payload"] == "z" * 5000


def test_a_sent_turn_reports_what_emet_took() -> None:
    emet = FakeEmet([])
    result = emet.sender().send(envelope(3))
    assert result == {
        **new_result("sent", trace_id="abc-root"),
        "written_at": result["written_at"],
        "trace": "11111111-1111-1111-1111-111111111111",
        "conversation": "22222222-2222-2222-2222-222222222222",
        "accepted": 3,
        "batches_sent": 1,
    }
    assert emet.headers[0] == {
        "Authorization": f"Bearer {KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": f"emet-sdk/{VERSION}",
    }
    assert emet.timeouts == [8.0]


def test_gaps_name_the_tools_across_batches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_transport, "BATCH_MAX_SPANS", 2)
    gaps = {"missing_tool_arguments": ["s0"], "missing_tool_results": [], "conflicting_final_outputs": []}
    later = {
        "missing_tool_arguments": ["s0", "s2"],
        "missing_tool_results": ["unknown"],
        "conflicting_final_outputs": [],
    }
    emet = FakeEmet([accepted(2, gaps=gaps), accepted(2, gaps=later, unattributed=True)])
    result = emet.sender().send(envelope(4))
    assert result["gaps"] == {
        "missing_tool_arguments": ["tool0", "tool2"],
        "missing_tool_results": ["unknown"],
        "conflicting_final_outputs": [],
    }
    assert (result["accepted"], result["batches_sent"], result["unattributed"]) == (4, 2, True)


def test_a_late_batch_is_recorded_and_the_rest_still_go(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_transport, "BATCH_MAX_SPANS", 1)
    emet = FakeEmet([accepted(1), accepted(1, late=True), accepted(1)])
    result = emet.sender().send(envelope(3))
    assert (result["status"], result["batches_sent"], result["late"], result["late_after_retry"]) == (
        "sent",
        3,
        True,
        False,
    )


def test_a_retried_last_batch_answering_late_is_probably_captured() -> None:
    emet = FakeEmet([TimeoutError(), accepted(3, late=True)])
    result = emet.sender().send(envelope(3))
    assert (result["status"], result["late"], result["late_after_retry"]) == ("sent", False, True)
    # The retry fits in what is left of the 15 s budget
    assert emet.timeouts == [8.0, 7.0]


def test_the_first_refused_batch_stops_the_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_transport, "BATCH_MAX_SPANS", 1)
    emet = FakeEmet([accepted(1), error(400, "invalid")])
    result = emet.sender().send(envelope(3))
    assert (result["status"], result["reason"], result["code"]) == ("stopped", "http_4xx", "invalid")
    assert (result["batches_sent"], result["batches_not_sent"], result["accepted"]) == (1, 2, 1)
    assert len(emet.posts) == 2


@pytest.mark.parametrize(
    ("steps", "reason", "code", "posts"),
    [
        ([error(403, "authentication_failed")], "http_4xx", "authentication_failed", 1),
        ([error(404)], "http_4xx", "", 1),
        ([error(413, "request_too_large")], "span_too_large", "", 1),
        ([error(302, headers={"Location": "https://elsewhere.example"})], "unexpected_response", "", 1),
        ([error(500), error(503)], "server_error", "", 2),
        ([TimeoutError(), TimeoutError()], "timeout", "", 2),
        ([urllib.error.URLError(TimeoutError()), TimeoutError()], "timeout", "", 2),
        ([urllib.error.URLError(ConnectionRefusedError())], "unreachable", "", 1),
        ([ConnectionResetError()], "unreachable", "", 1),
        ([error(429, headers={"Retry-After": "3600"})], "rate_limited", "", 1),
        ([error(429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"})], "rate_limited", "", 1),
        ([error(429, headers={"retry-after": "2"}), error(429, headers={"Retry-After": "2"})], "rate_limited", "", 2),
    ],
)
def test_failures_stop_with_a_reason(steps: list[Step], reason: str, code: str, posts: int) -> None:
    emet = FakeEmet(list(steps))
    result = emet.sender().send(envelope(3))
    assert (result["status"], result["reason"], result["code"]) == ("stopped", reason, code)
    assert (len(emet.posts), result["batches_not_sent"]) == (posts, 1)


def test_one_retry_recovers_a_server_error_or_a_short_rate_limit() -> None:
    emet = FakeEmet([error(502), accepted(3), error(429, headers={"Retry-After": "2"}), accepted(3)])
    sender = emet.sender()
    assert sender.send(envelope(3))["status"] == "sent"
    assert sender.send(envelope(3))["status"] == "sent"
    assert emet.slept == [0.5, 2.0]


def test_no_retry_when_the_budget_is_spent() -> None:
    def slow_failure(emet: FakeEmet, batch: dict[str, Any]) -> Reply:
        emet.now += 14.5
        return error(500)

    emet = FakeEmet([slow_failure])
    result = emet.sender().send(envelope(3))
    assert (result["reason"], len(emet.posts)) == ("server_error", 1)


def test_a_202_with_an_odd_body_still_counts_as_sent() -> None:
    emet = FakeEmet([Reply(202, {}, b"not json")])
    result = emet.sender().send(envelope(3))
    assert (result["status"], result["batches_sent"], result["accepted"], result["trace"]) == ("sent", 1, 0, "")


class _Handler(http.server.BaseHTTPRequestHandler):
    answers: ClassVar[list[tuple[int, dict[str, str], bytes]]] = []
    seen: ClassVar[list[tuple[str, dict[str, str], bytes]]] = []

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers["Content-Length"]))
        self.seen.append((self.path, dict(self.headers.items()), body))
        status, headers, answer = self.answers.pop(0)
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(answer)))
        self.end_headers()
        self.wfile.write(answer)

    def log_message(self, format: str, *args: Any) -> None:
        pass


@pytest.fixture
def local_emet() -> Iterator[tuple[str, type[_Handler]]]:
    _Handler.answers, _Handler.seen = [], []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", _Handler
    finally:
        server.shutdown()
        server.server_close()


def test_sends_over_http_to_a_local_emet(local_emet: tuple[str, type[_Handler]]) -> None:
    origin, handler = local_emet
    reply = accepted(3)
    handler.answers = [(202, {"Content-Type": "application/json"}, reply.body)]
    result = Sender(origin, KEY).send(envelope(3))
    assert (result["status"], result["accepted"]) == ("sent", 3)
    [(path, headers, body)] = handler.seen
    assert path == "/api/v1/traces/"
    assert headers["Authorization"] == f"Bearer {KEY}"
    assert json.loads(body)["trace_id"] == "abc-root"


def test_reads_an_error_body_and_never_follows_a_redirect(local_emet: tuple[str, type[_Handler]]) -> None:
    origin, handler = local_emet
    handler.answers = [
        (403, {}, json.dumps({"code": "permission_denied"}).encode()),
        (307, {"Location": f"{origin}/elsewhere/"}, b""),
    ]
    url = origin + "/api/v1/traces/"
    assert json.loads(urllib_post(url, {}, b"{}", 5).body)["code"] == "permission_denied"
    assert urllib_post(url, {}, b"{}", 5).status == 307
    assert [path for path, _, _ in handler.seen] == ["/api/v1/traces/", "/api/v1/traces/"]


def test_an_unreachable_local_emet_stops_the_turn() -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    result = Sender(f"http://127.0.0.1:{port}", KEY).send(envelope(3))
    assert (result["status"], result["reason"]) == ("stopped", "unreachable")
