"""The span processor: collects the spans of each turn() block and hands the finished turn on."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from opentelemetry.context import Context
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor

from emet_sdk._turn import TurnInfo, current_turn

logger = logging.getLogger("emet_sdk")

MAX_LIVE_TURNS = 1000
MAX_SPANS_PER_TURN = 2000
# A turn with a span that never ends is given up after this long
TURN_TTL_SECONDS = 600.0


@dataclass(frozen=True)
class CapturedTurn:
    """A finished turn: the turn() values and every span that started inside the block."""

    info: TurnInfo
    spans: tuple[ReadableSpan, ...]
    dropped_spans: int = 0


@dataclass
class _Live:
    info: TurnInfo
    opened_at: float
    spans: list[ReadableSpan] = field(default_factory=lambda: [])
    open_spans: set[tuple[int, int]] = field(default_factory=lambda: set())
    block_closed: bool = False
    dropped_spans: int = 0


class EmetSpanProcessor(SpanProcessor):
    """Collects turn spans without touching them, so other exporters see exactly what they saw before.

    A turn is finished once its turn() block has closed and every span started inside it has ended.
    Spans started outside any turn() block are ignored.
    """

    def __init__(self, on_turn: Callable[[CapturedTurn], None]) -> None:
        self._on_turn = on_turn
        self._lock = threading.Lock()
        self._live: dict[int, _Live] = {}
        self._span_turns: dict[tuple[int, int], int] = {}

    def turn_opened(self, info: TurnInfo) -> None:
        now = time.monotonic()
        with self._lock:
            self._evict_stale(now)
            if len(self._live) >= MAX_LIVE_TURNS:
                logger.warning("Emet skipped a turn: %d turns are already in progress", MAX_LIVE_TURNS)
                return
            self._live[info.id] = _Live(info, opened_at=now)

    def turn_closed(self, info: TurnInfo) -> None:
        with self._lock:
            live = self._live.get(info.id)
            if live is None:
                return
            live.block_closed = True
            done = self._pop_if_done(live)
        self._emit(done)

    def on_start(self, span: Span, parent_context: Context | None = None) -> None:
        try:
            info = current_turn(parent_context)
            if info is None or span.context is None:
                return
            with self._lock:
                live = self._live.get(info.id)
                if live is None:
                    return
                key = (span.context.trace_id, span.context.span_id)
                live.open_spans.add(key)
                self._span_turns[key] = info.id
        except Exception:  # never raise into the app
            logger.exception("Emet failed to track a span")

    def on_end(self, span: ReadableSpan) -> None:
        try:
            if span.context is None:
                return
            with self._lock:
                key = (span.context.trace_id, span.context.span_id)
                turn_id = self._span_turns.pop(key, None)
                live = self._live.get(turn_id) if turn_id is not None else None
                if live is None:
                    return
                live.open_spans.discard(key)
                if len(live.spans) < MAX_SPANS_PER_TURN:
                    live.spans.append(span)
                else:
                    live.dropped_spans += 1
                done = self._pop_if_done(live)
            self._emit(done)
        except Exception:
            logger.exception("Emet failed to collect a span")

    def shutdown(self) -> None:
        with self._lock:
            self._live.clear()
            self._span_turns.clear()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True

    def _pop_if_done(self, live: _Live) -> CapturedTurn | None:
        if not live.block_closed or live.open_spans:
            return None
        del self._live[live.info.id]
        if live.dropped_spans:
            logger.warning("Emet dropped %d spans from a turn over %d spans", live.dropped_spans, MAX_SPANS_PER_TURN)
        return CapturedTurn(live.info, tuple(live.spans), live.dropped_spans)

    def _evict_stale(self, now: float) -> None:
        stale = [live for live in self._live.values() if now - live.opened_at > TURN_TTL_SECONDS]
        for live in stale:
            del self._live[live.info.id]
            for key in live.open_spans:
                self._span_turns.pop(key, None)
            logger.warning("Emet gave up on a turn still open after %d seconds", TURN_TTL_SECONDS)

    def _emit(self, done: CapturedTurn | None) -> None:
        if done is None or not done.spans:
            return
        try:
            self._on_turn(done)
        except Exception:
            logger.exception("Emet failed to hand on a turn")
