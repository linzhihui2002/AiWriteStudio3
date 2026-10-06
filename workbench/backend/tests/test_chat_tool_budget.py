"""Blocking tools share run deadlines; delayed work cannot mutate a finished book."""
from __future__ import annotations

import threading
import time

import pytest

from workbench.backend.engine import dsh_chat
from workbench.backend.services import chat_workspace_tools as tools, file_change_service as files
from workbench.backend.services.errors import InvalidOperationError
from workbench.backend.tests.test_chat_runs import workspace


class Worker:
    def __init__(self, events):
        self.events = list(events)
        self.lock = threading.Lock()
        self.lock.acquire()
        self.initialized = True
        self.last_used = time.monotonic()
        self.sent = []
        self.closed = False

    def receive(self, timeout=0.1):
        return self.events.pop(0)

    def send(self, event):
        self.sent.append(event)

    def close(self, force=False):
        self.closed = True


def tool_request():
    return {"type": "tool_request", "name": "create_file", "call_id": "budget-call", "request_id": "budget-request", "arguments": {}}


def stream(runtime, control, execute_tool, cancelled=None):
    return list(runtime.stream(session_id="synthetic-budget", resume=True, project_root="synthetic-book",
        user_text="保存合成便签", provider="fake", model="fake", tool_specs=[], execute_tool=execute_tool,
        should_cancel=cancelled, wait_control=control))


@pytest.mark.parametrize("reason", ["total", "idle"])
def test_hung_tool_ends_promptly_and_late_write_is_refused(workspace, monkeypatch, reason):
    project, session, _ = workspace
    runtime = dsh_chat.DshChatRuntime(timeout_seconds=0.06 if reason == "total" else 1,
                                     idle_timeout_seconds=0.06 if reason == "idle" else 1)
    worker = Worker([tool_request()])
    monkeypatch.setattr(runtime, "_worker", lambda *_: worker)
    control = dsh_chat.RunControl()
    gate, returned = threading.Event(), threading.Event()
    results = []
    ctx = {"project_id": project["id"], "session_id": session["id"], "run_id": "late-tool",
           "tool_call_id": "budget-call", "permission_mode": "full", "should_cancel": control.should_cancel}

    def delayed_tool(*_args):
        gate.wait(2)
        try:
            results.append(tools.execute("create_file", {"rel_path": "备忘录/迟到.md", "content": "截止后不可保存"}, ctx))
        finally:
            returned.set()
        return results[-1]

    started = time.monotonic()
    try:
        events = stream(runtime, control, delayed_tool)
        assert time.monotonic() - started < 0.5
        assert events[-1]["type"] == "error" and events[-1]["code"] == "TIMEOUT"
        assert events[-1]["reason"] == reason
        assert worker.closed and not worker.lock.locked()
        assert any(event["type"] == "cancel" for event in worker.sent)
        assert not any(event["type"] == "tool_response" for event in worker.sent)
    finally:
        gate.set()
        assert returned.wait(2)
    assert results and not results[0]["ok"]
    assert not files.file_state(project["id"], "备忘录/迟到.md")["exists"]


def test_explicit_stop_does_not_wait_for_hung_tool_and_latches_control(monkeypatch):
    runtime = dsh_chat.DshChatRuntime(timeout_seconds=1, idle_timeout_seconds=1)
    worker = Worker([tool_request()])
    monkeypatch.setattr(runtime, "_worker", lambda *_: worker)
    control = dsh_chat.RunControl()
    stopped, gate, returned = threading.Event(), threading.Event(), threading.Event()
    timer = threading.Timer(0.04, stopped.set)
    def delayed(*_args):
        gate.wait(2)
        returned.set()
        return {"ok": True}
    timer.start()
    try:
        started = time.monotonic()
        events = stream(runtime, control, delayed, stopped.is_set)
        assert time.monotonic() - started < 0.5
        assert events[-1]["type"] == "cancelled" and control.should_cancel()
        assert worker.closed and not worker.lock.locked()
    finally:
        gate.set()
        timer.cancel()
        assert returned.wait(2)


def test_tool_human_wait_is_excluded_once_from_total_and_idle(monkeypatch):
    runtime = dsh_chat.DshChatRuntime(timeout_seconds=0.06, idle_timeout_seconds=0.06)
    worker = Worker([tool_request(), {"type": "done", "text": "批准后完成"}])
    monkeypatch.setattr(runtime, "_worker", lambda *_: worker)
    control = dsh_chat.RunControl()
    def waiting_tool(*_args):
        control.begin_wait()
        control.begin_wait()  # Nested reporting of the same wait is idempotent.
        time.sleep(0.15)
        control.end_wait()
        control.end_wait()
        return {"ok": True}
    events = stream(runtime, control, waiting_tool)
    assert events[-1]["type"] == "done"
    assert 0.14 <= control.paused_seconds() < 0.25
    assert not any(event["type"] == "cancel" for event in worker.sent)


def test_late_undo_is_refused_before_mutating_files(workspace):
    project, session, _ = workspace
    change = files.apply_change(project["id"], run_id="undo-budget", session_id=session["id"],
        tool_call_id="original", operation="create", rel_path="备忘录/撤回预算.md", content="必须保留")
    with pytest.raises(InvalidOperationError, match="已停止"):
        files.revert("undo-budget", project_id=project["id"], should_cancel=lambda: True)
    assert files.file_state(project["id"], change["path"])["content"] == "必须保留"
    assert files.list_changes("undo-budget", project["id"])[0]["status"] == "applied"


def test_deslop_subtask_inherits_remaining_budget_and_cancellation(workspace, monkeypatch):
    from workbench.backend.services import generation_service, skill_service
    project, _, _ = workspace
    control = dsh_chat.RunControl()
    control.configure(12, 6)
    captured = {}
    monkeypatch.setattr(skill_service, "skill_digest", lambda *_: "合成去味规则")
    def generate(**kwargs):
        captured.update(kwargs)
        return {"ok": True, "text": "合成正文"}
    monkeypatch.setattr(generation_service, "run_task", generate)
    ctx = {"wait_control": control, "should_cancel": control.should_cancel}
    assert tools._rewrite_chapter_body(project["id"], "候选正文", ctx) == "合成正文"
    assert 1 <= captured["timeout_seconds"] <= 12
    assert captured["should_cancel"] is ctx["should_cancel"]
    control.stop()
    captured.clear()
    assert tools._rewrite_chapter_body(project["id"], "候选正文", ctx) is None
    assert not captured


@pytest.mark.parametrize("phase", ["turn", "init"])
@pytest.mark.parametrize("message", [
    'HTTP 403 {"error":{"message":"用户额度不足，预扣费额度失败", "code":"insufficient_user_quota"}}',
    'HTTP 403 {"error":{"code":"insufficient_quota", "message":"余额不足"}}',
])
def test_explicit_provider_quota_is_not_misclassified_as_auth(monkeypatch, phase, message):
    runtime = dsh_chat.DshChatRuntime()
    worker = Worker([{"type": "error", "code": "AUTH", "message": message + "; Authorization: Bearer synthetic-secret-0123456789"}])
    worker.initialized = phase != "init"
    worker.project_root = "synthetic-book"
    monkeypatch.setattr(runtime, "_worker", lambda *_: worker)
    events = stream(runtime, dsh_chat.RunControl(), lambda *_: {})
    assert events[-1]["code"] == "QUOTA"
    assert "额度" in events[-1]["message"] or "余额" in events[-1]["message"]
    assert "synthetic-secret-0123456789" not in events[-1]["message"]


@pytest.mark.parametrize("event,expected", [
    ({"code": "AUTH", "status": 401, "message": "invalid API key"}, "AUTH"),
    ({"code": "AUTH", "status": 403, "message": "permission denied"}, "AUTH"),
    ({"code": "RATE_LIMIT", "status": 429, "message": "insufficient_quota"}, "RATE_LIMIT"),
])
def test_other_auth_and_rate_limit_errors_keep_their_identity(monkeypatch, event, expected):
    runtime = dsh_chat.DshChatRuntime()
    worker = Worker([{"type": "error", **event}])
    monkeypatch.setattr(runtime, "_worker", lambda *_: worker)
    assert stream(runtime, dsh_chat.RunControl(), lambda *_: {})[-1]["code"] == expected
