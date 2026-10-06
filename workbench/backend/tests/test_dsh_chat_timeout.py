"""The native chat budget follows progress, not the provider request timeout."""

from __future__ import annotations

import threading

from workbench.backend import config
from workbench.backend.engine import dsh_chat


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now


class Worker:
    def __init__(self, clock: Clock, schedule: list[tuple[float, dict | None]]) -> None:
        self.clock = clock
        self.schedule = list(schedule)
        self.lock = threading.Lock()
        self.lock.acquire()
        self.initialized = True
        self.last_used = clock.now
        self.sent: list[dict] = []
        self.closed = False

    def send(self, message: dict) -> None:
        self.sent.append(message)

    def receive(self, timeout: float = 0.1) -> dict | None:
        if any(message.get("type") == "cancel" for message in self.sent):
            return {"type": "cancelled"}
        if not self.schedule:
            raise AssertionError("runtime waited past the scripted model events")
        elapsed, event = self.schedule.pop(0)
        self.clock.now += elapsed
        return event

    def close(self, force: bool = False) -> None:
        self.closed = True


def stream(monkeypatch, schedule: list[tuple[float, dict | None]],
           human_wait: float = 0) -> tuple[list[dict], Worker]:
    clock = Clock()
    monkeypatch.setattr(dsh_chat.time, "monotonic", clock.monotonic)
    runtime = dsh_chat.DshChatRuntime()
    worker = Worker(clock, schedule)
    monkeypatch.setattr(runtime, "_worker", lambda _session, _root: worker)
    control = dsh_chat.RunControl()

    def execute_tool(*_args):
        if human_wait:
            control.begin_wait()
            clock.now += human_wait
            control.end_wait()
        return {"ok": True}

    events = list(runtime.stream(session_id="book-chat", resume=True, project_root="book",
        user_text="继续写作", provider="test", model="test", tool_specs=[],
        execute_tool=execute_tool, wait_control=control))
    return events, worker


def test_progressing_run_can_outlive_old_ten_minute_cap(monkeypatch) -> None:
    assert config.DSH_TIMEOUT_SECONDS == 1800
    assert config.DSH_IDLE_TIMEOUT_SECONDS == 600
    schedule = [(100, {"type": "delta", "text": "仍在写。"}) for _ in range(12)]
    schedule.append((100, {"type": "done", "text": "完成"}))
    events, worker = stream(monkeypatch, schedule)
    assert events[-1]["type"] == "done"
    assert not any(message.get("type") == "cancel" for message in worker.sent)


def test_idle_timeout_explains_lack_of_progress(monkeypatch) -> None:
    events, worker = stream(monkeypatch, [(601, None)])
    assert events[-1]["type"] == "error"
    assert events[-1]["code"] == "TIMEOUT"
    assert events[-1]["reason"] == "idle"
    assert "连续 10 分钟" in events[-1]["message"]
    assert any(message.get("type") == "cancel" for message in worker.sent)


def test_total_timeout_is_distinct_from_idle_timeout(monkeypatch) -> None:
    events, _worker = stream(monkeypatch,
        [(100, {"type": "delta", "text": "有进展。"}) for _ in range(19)])
    assert events[-1]["type"] == "error"
    assert events[-1]["reason"] == "total"
    assert "30 分钟" in events[-1]["message"]


def test_human_approval_wait_pauses_both_budgets(monkeypatch) -> None:
    events, worker = stream(monkeypatch, [
        (1, {"type": "tool_request", "name": "write_file", "call_id": "call-1", "request_id": "req-1"}),
        (100, {"type": "done", "text": "作者批准后完成"}),
    ], human_wait=1900)
    assert events[-1]["type"] == "done"
    assert any(message.get("type") == "tool_response" for message in worker.sent)
    assert not any(message.get("type") == "cancel" for message in worker.sent)


def test_same_run_control_shares_total_budget_across_plan_steps(monkeypatch) -> None:
    clock = Clock()
    monkeypatch.setattr(dsh_chat.time, "monotonic", clock.monotonic)
    runtime = dsh_chat.DshChatRuntime()
    control = dsh_chat.RunControl()
    first = Worker(clock, [(200, {"type": "delta", "text": "进展"}) for _ in range(4)] + [(200, {"type": "done"})])
    second = Worker(clock, [(200, {"type": "delta", "text": "进展"}) for _ in range(5)])
    workers = iter([first, second])
    monkeypatch.setattr(runtime, "_worker", lambda *_: next(workers))
    params = dict(session_id="book-chat", resume=True, project_root="book", user_text="继续", provider="fake", model="fake",
                  tool_specs=[], execute_tool=lambda *_: {"ok": True}, wait_control=control)
    assert list(runtime.stream(**params))[-1]["type"] == "done"
    events = list(runtime.stream(**params))
    assert events[-1]["code"] == "TIMEOUT" and events[-1]["reason"] == "total"
    assert control.active_seconds() >= 1800
