"""One background thread that turns captured turns into delivery results, so the app never waits on Emet."""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from collections.abc import Callable

from emet_sdk._delivery import DeliveryResult, new_result
from emet_sdk._processor import CapturedTurn

logger = logging.getLogger("emet_sdk")

MAX_QUEUED_TURNS = 1000


class Worker:
    """Handles turns in arrival order on a daemon thread, started on first use and again after a fork."""

    def __init__(
        self, handle: Callable[[CapturedTurn], DeliveryResult | None], deliver: Callable[[DeliveryResult], None]
    ) -> None:
        self._handle = handle
        self._deliver = deliver
        self._reset()
        if hasattr(os, "register_at_fork"):  # not on Windows
            os.register_at_fork(after_in_child=self._reset)

    def _reset(self) -> None:
        # A forked child has the queue's memory but not its thread; turns queued in the parent stay the parent's
        self._cond = threading.Condition()
        self._queue: deque[CapturedTurn] = deque()
        self._pending = 0
        self._thread: threading.Thread | None = None

    def submit(self, captured: CapturedTurn) -> None:
        with self._cond:
            if len(self._queue) >= MAX_QUEUED_TURNS:
                logger.warning("Emet dropped a turn: %d turns are already waiting to be sent", MAX_QUEUED_TURNS)
                return
            if self._thread is None:
                thread = threading.Thread(target=self._run, name="emet-sdk", daemon=True)
                try:
                    thread.start()
                except RuntimeError:  # the interpreter is shutting down
                    logger.warning("Emet dropped a turn that finished while the app was exiting")
                    return
                self._thread = thread
            self._queue.append(captured)
            self._pending += 1
            self._cond.notify()

    def flush(self, timeout: float) -> bool:
        """Waits until every queued turn is handled. Returns False when the timeout ran out first."""
        if threading.current_thread() is self._thread:
            # Called from on_delivery: the turn being delivered cannot finish while it waits
            return False
        deadline = time.monotonic() + timeout
        with self._cond:
            while self._pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cond.wait(remaining)
        return True

    def _run(self) -> None:
        cond = self._cond
        while True:
            with cond:
                while not self._queue:
                    cond.wait()
                captured = self._queue.popleft()
            try:
                result = self._handle(captured)
            except Exception:  # the thread must survive any one turn
                logger.exception("Emet failed to send a turn")
                result = new_result("stopped", "sdk_error")
            try:
                if result is not None:
                    self._deliver(result)
            except Exception:
                logger.exception("Emet failed to report a turn")
            finally:
                with cond:
                    self._pending -= 1
                    cond.notify_all()
