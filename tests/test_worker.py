from __future__ import annotations

import threading

import pytest

from emet_sdk import _worker
from emet_sdk._delivery import DeliveryResult, new_result
from emet_sdk._processor import CapturedTurn
from emet_sdk._turn import TurnInfo
from emet_sdk._worker import Worker


def turn(conversation: str) -> CapturedTurn:
    return CapturedTurn(TurnInfo(1, "t", conversation, None), ())


def test_turns_are_handled_in_order_off_the_calling_thread() -> None:
    threads: set[str] = set()
    delivered: list[DeliveryResult] = []

    def handle(captured: CapturedTurn) -> DeliveryResult:
        threads.add(threading.current_thread().name)
        return new_result("sent", trace_id=captured.info.conversation_id)

    worker = Worker(handle, delivered.append)
    for name in ("a", "b", "c"):
        worker.submit(turn(name))
    assert worker.flush(5)
    assert [r["trace_id"] for r in delivered] == ["a", "b", "c"]
    assert threads == {"emet-sdk"}


def test_flush_gives_up_at_its_timeout() -> None:
    release = threading.Event()

    def handle(captured: CapturedTurn) -> None:
        release.wait(5)

    worker = Worker(handle, lambda result: None)
    worker.submit(turn("slow"))
    assert worker.flush(0.05) is False
    release.set()
    assert worker.flush(5)


def test_a_failing_turn_does_not_stop_the_thread(caplog: pytest.LogCaptureFixture) -> None:
    delivered: list[DeliveryResult] = []

    def handle(captured: CapturedTurn) -> DeliveryResult:
        if captured.info.conversation_id == "bad":
            raise RuntimeError("boom")
        return new_result("sent")

    worker = Worker(handle, delivered.append)
    worker.submit(turn("bad"))
    worker.submit(turn("good"))
    assert worker.flush(5)
    assert [(r["status"], r["reason"]) for r in delivered] == [("stopped", "sdk_error"), ("sent", "")]
    assert "failed to send a turn" in caplog.text


def test_a_full_queue_drops_new_turns(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.setattr(_worker, "MAX_QUEUED_TURNS", 1)
    release = threading.Event()
    handled: list[str] = []

    def handle(captured: CapturedTurn) -> None:
        release.wait(5)
        handled.append(captured.info.conversation_id)

    worker = Worker(handle, lambda result: None)
    worker.submit(turn("busy"))
    while worker._queue:  # wait for the thread to take the first turn
        pass
    worker.submit(turn("queued"))
    worker.submit(turn("dropped"))
    release.set()
    assert worker.flush(5)
    assert handled == ["busy", "queued"]
    assert "dropped a turn" in caplog.text


def test_a_forked_child_starts_clean() -> None:
    worker = Worker(lambda captured: new_result("sent"), lambda result: None)
    worker.submit(turn("parent"))
    worker.flush(5)
    worker._reset()
    assert (worker._thread, worker._pending, list(worker._queue)) == (None, 0, [])
    worker.submit(turn("child"))
    assert worker.flush(5)


def test_a_turn_finishing_during_shutdown_is_dropped(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def refuse(self: threading.Thread) -> None:
        raise RuntimeError("can't create new thread at interpreter shutdown")

    monkeypatch.setattr(threading.Thread, "start", refuse)
    worker = Worker(lambda captured: new_result("sent"), lambda result: None)
    worker.submit(turn("late"))
    assert worker.flush(0.1)
    assert "while the app was exiting" in caplog.text


def test_flush_from_on_delivery_returns_at_once() -> None:
    answers: list[bool] = []
    worker: Worker

    def deliver(result: DeliveryResult) -> None:
        answers.append(worker.flush(5))

    worker = Worker(lambda captured: new_result("sent"), deliver)
    worker.submit(turn("a"))
    assert worker.flush(5)
    assert answers == [False]
