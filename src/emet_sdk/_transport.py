"""Sends one turn's envelope to Emet in ordered batches and reports what happened."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, cast
from urllib.parse import urlsplit

from emet_sdk._delivery import GAP_KEYS, DeliveryResult, new_result
from emet_sdk._version import VERSION

DEFAULT_ENDPOINT = "https://app.emet.so"
TRACES_PATH = "/api/v1/traces/"
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")
BATCH_MAX_SPANS = 200
BATCH_MAX_BYTES = 4 * 1024 * 1024
ATTEMPT_TIMEOUT = 8.0
# Each batch must start inside Emet's 30 s idle seal, which runs from the previous batch's arrival
BATCH_BUDGET = 15.0
MAX_RETRY_AFTER = 5.0
SERVER_ERROR_WAIT = 0.5
MIN_ATTEMPT = 1.0


@dataclass(frozen=True)
class Reply:
    status: int
    headers: dict[str, str]
    body: bytes


Post = Callable[[str, dict[str, str], bytes, float], Reply]


def check_endpoint(url: str) -> str:
    """Returns the endpoint's origin. Only production and this machine are allowed."""
    parts = urlsplit(url.strip())
    if parts.username or parts.password or parts.path not in ("", "/") or parts.query or parts.fragment:
        raise ValueError(f"Emet's endpoint is an origin such as {DEFAULT_ENDPOINT}, not {url!r}")
    origin = f"{parts.scheme}://{parts.netloc}"
    if origin == DEFAULT_ENDPOINT:
        return origin
    if parts.scheme in ("http", "https") and parts.hostname in LOCAL_HOSTS:
        return origin
    raise ValueError(f"Emet's endpoint must be {DEFAULT_ENDPOINT} or a localhost address, not {url!r}")


def split(envelope: dict[str, Any]) -> list[dict[str, Any]]:
    """Splits a turn into batches, never clipping a span. The turn span and end travel in the last batch."""
    header = {k: v for k, v in envelope.items() if k not in ("spans", "end")}
    spans: list[dict[str, Any]] = envelope["spans"]
    # Emet seals a trace as soon as its turn span lands, so it goes last
    ordered = [s for s in spans if s.get("kind") != "turn"] + [s for s in spans if s.get("kind") == "turn"]
    groups: list[list[dict[str, Any]]] = [[]]
    size = 0
    for span in ordered:
        span_bytes = len(_encode(span))
        current = groups[-1]
        if current and (len(current) >= BATCH_MAX_SPANS or size + span_bytes > BATCH_MAX_BYTES):
            groups.append([])
            size = 0
        groups[-1].append(span)
        size += span_bytes
    return [{**header, "spans": group, "end": index == len(groups) - 1} for index, group in enumerate(groups)]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # A redirect would carry the key to another origin
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


_opener = urllib.request.build_opener(_NoRedirect())


def urllib_post(url: str, headers: dict[str, str], body: bytes, timeout: float) -> Reply:
    # check_endpoint has limited url to https://app.emet.so or localhost
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")  # noqa: S310
    try:
        with _opener.open(request, timeout=timeout) as response:
            return Reply(response.status, dict(response.headers.items()), response.read())
    except urllib.error.HTTPError as error:
        with error:
            return Reply(error.code, dict(error.headers.items()), error.read())


@dataclass
class _Outcome:
    body: dict[str, Any] | None = None
    reason: str = ""
    code: str = ""
    retried: bool = False


class Sender:
    """Posts batches one at a time and stops at the first one Emet did not accept."""

    def __init__(
        self,
        endpoint: str,
        key: str,
        post: Post | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._url = check_endpoint(endpoint) + TRACES_PATH
        self._headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": f"emet-sdk/{VERSION}",
        }
        self._post = post or urllib_post
        self._clock = clock
        self._sleep = sleep

    def send(self, envelope: dict[str, Any]) -> DeliveryResult:
        batches = split(envelope)
        names = {s["span_id"]: s.get("name", "") for s in envelope["spans"]}
        result = new_result("sent", trace_id=envelope["trace_id"])
        for index, batch in enumerate(batches):
            outcome = self._send_batch(batch)
            if outcome.body is None:
                result.update(status="stopped", reason=outcome.reason, code=outcome.code)
                result["batches_not_sent"] = len(batches) - index
                break
            body = outcome.body
            result["batches_sent"] += 1
            result["accepted"] += _int(body.get("accepted"))
            result["trace"] = result["trace"] or _str(body.get("trace"))
            result["conversation"] = result["conversation"] or _str(body.get("conversation"))
            result["unattributed"] = result["unattributed"] or body.get("unattributed") is True
            if body.get("late") is True:
                # The first attempt at the last batch most likely sealed the trace itself
                last = index == len(batches) - 1
                result["late_after_retry" if last and outcome.retried else "late"] = True
            gaps = _dict(body.get("gaps"))
            for key in GAP_KEYS:
                ids = gaps.get(key)
                for span_id in cast("list[Any]", ids) if isinstance(ids, list) else []:
                    name = names.get(span_id) or str(span_id)
                    if name not in result["gaps"][key]:
                        result["gaps"][key].append(name)
        return result

    def _send_batch(self, batch: dict[str, Any]) -> _Outcome:
        body = _encode(batch)
        started = self._clock()
        reason = ""
        for attempt in (1, 2):
            # A retry only starts with at least MIN_ATTEMPT left, checked below
            timeout = max(MIN_ATTEMPT, min(ATTEMPT_TIMEOUT, BATCH_BUDGET - (self._clock() - started)))
            wait = 0.0
            try:
                reply = self._post(self._url, self._headers, body, timeout)
            except TimeoutError:
                reason = "timeout"
            except urllib.error.URLError as error:
                if not isinstance(error.reason, TimeoutError):
                    return _Outcome(reason="unreachable")
                reason = "timeout"
            except OSError:
                return _Outcome(reason="unreachable")
            else:
                if reply.status == 202:
                    return _Outcome(body=_json(reply.body), retried=attempt == 2)
                if reply.status == 429:
                    reason = "rate_limited"
                    wait = _retry_after(reply.headers)
                    if wait > MAX_RETRY_AFTER:
                        return _Outcome(reason=reason)
                elif reply.status >= 500:
                    reason = "server_error"
                    wait = SERVER_ERROR_WAIT
                elif reply.status == 413:
                    return _Outcome(reason="span_too_large")
                elif reply.status >= 400:
                    return _Outcome(reason="http_4xx", code=_str(_json(reply.body).get("code")))
                else:
                    return _Outcome(reason="unexpected_response")
            if attempt == 2 or BATCH_BUDGET - (self._clock() - started) - wait < MIN_ATTEMPT:
                return _Outcome(reason=reason)
            if wait:
                self._sleep(wait)
        return _Outcome(reason=reason)  # pragma: no cover


def _encode(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _json(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except ValueError:
        return {}
    return _dict(value)


def _dict(value: Any) -> dict[str, Any]:
    return cast("dict[str, Any]", value) if isinstance(value, dict) else {}


def _retry_after(headers: dict[str, str]) -> float:
    value = next((v for k, v in headers.items() if k.lower() == "retry-after"), "")
    try:
        return max(0.0, float(value))
    except ValueError:
        # Missing, or an HTTP date: too long to wait inside a batch
        return 1.0 if not value else MAX_RETRY_AFTER + 1


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _str(value: Any) -> str:
    return value if isinstance(value, str) else ""
