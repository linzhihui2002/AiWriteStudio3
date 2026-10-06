"""Human answers and write approvals preserve the exact tool and file version."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace
import uuid

import pytest

from workbench.backend import config, db
from workbench.backend.services import (
    chat_interaction_service as interactions, chat_service,
    chat_workspace_tools as tools, file_change_service as changes, operation_log,
    project_service, scope_guard, settings_service,
)
from workbench.backend.services.errors import InvalidOperationError, NodeNotFoundError
from workbench.backend.tests.test_file_changes import BODY

_CAPTURE_ANSWER = interactions._capture_answer


class WaitControl:
    def __init__(self):
        self.waiting = False
        self.begun = self.ended = 0

    def begin_wait(self):
        self.waiting = True
        self.begun += 1

    def end_wait(self):
        self.waiting = False
        self.ended += 1


@pytest.fixture()
def book(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "books")
    monkeypatch.setattr(config, "RUNTIME_DIR", tmp_path / ".workbench")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / ".workbench" / "interactions.db")
    db.init_db()
    config.ensure_runtime_dirs()
    project = project_service.create_project(name="交互验收")
    _, root = project_service.get_project_dir(project["id"])
    session = chat_service.create_session(project_id=project["id"])
    run_id = str(uuid.uuid4())
    with db.get_conn() as conn:
        conn.execute("INSERT INTO chat_runs(id,session_id,project_id,client_request_id,request,status)"
                     " VALUES (?,?,?,?,?,'running')", (run_id, session["id"], project["id"], "test", "{}"))
    cancel, control = threading.Event(), WaitControl()
    captured = []
    monkeypatch.setattr(interactions, "_capture_answer", lambda run, item: captured.append(item["user_message_id"]))
    ctx = {"project_id": project["id"], "session_id": session["id"], "run_id": run_id,
           "tool_call_id": "write-one", "permission_mode": "ask", "auto_apply": True,
           "should_cancel": cancel.is_set, "wait_control": control, "change_ids": []}
    result = SimpleNamespace(id=project["id"], root=root, session=session["id"], run_id=run_id,
                             ctx=ctx, cancel=cancel, control=control, captured=captured)
    yield result
    cancel.set()
    interactions.interrupt_run(run_id)


def pending(book, kind=None):
    return pending_in(book, book.run_id, kind)


def pending_in(book, run_id, kind=None):
    until = time.monotonic() + 5
    while time.monotonic() < until:
        rows = interactions.list_interactions(run_id)
        match = next((item for item in rows if item["status"] == "pending" and (kind is None or item["kind"] == kind)), None)
        if match and book.control.waiting:
            return match
        time.sleep(.01)
    raise AssertionError("交互未进入等待")


def approve(book, item, decision="approve"):
    return interactions.respond(book.run_id, item["id"], {"decision": decision})


def seed(book, content="原文。\n保留这一行。\n", rel="备忘录/稿件.md"):
    target = book.root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    assert tools.execute("read_file", {"rel_path": rel}, book.ctx)["ok"]
    return rel, target


def test_question_other_wait_resume_and_answer_idempotence(book):
    payload = {"questions": [{"id": "direction", "question": "选择主线方向", "options": [{"label": "守城"}, {"label": "远行"}]}]}
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(tools.execute, "ask_user_question", payload, book.ctx)
        item = pending(book, "question")
        assert not future.done()
        with db.get_conn() as conn:
            assert conn.execute("SELECT status FROM chat_runs WHERE id=?", (book.run_id,)).fetchone()[0] == "waiting_input"
        with pytest.raises(InvalidOperationError):
            interactions.respond(book.run_id, item["id"], {"answers": [{"id": "direction", "selected": [], "custom": "  "}]})
        response = {"answers": [{"id": "direction", "selected": [], "custom": "先救人，再撤离"}]}
        answered = interactions.respond(book.run_id, item["id"], response)
        result = future.result(timeout=3)
    assert result["ok"] and result["answers"] == response["answers"]
    assert book.control.begun == book.control.ended == 1
    assert interactions.respond(book.run_id, item["id"], response) == answered
    assert interactions.ask(book.run_id, "write-one", "question", payload) == response
    assert len(book.captured) == 1
    messages = chat_service.list_messages(book.session)
    assert len(messages) == 1 and "先救人，再撤离" in messages[0]["content"]
    with pytest.raises(interactions.InteractionConflictError):
        interactions.respond(book.run_id, item["id"], {"answers": [{"id": "direction", "selected": ["守城"]}]})
    with db.get_conn() as conn:
        assert conn.execute("SELECT status FROM chat_runs WHERE id=?", (book.run_id,)).fetchone()[0] == "running"


def test_multiselect_answers_validate_all_ids_and_choices(book):
    payload = {"questions": [{"id": "one", "question": "选择元素", "multi_select": True,
                              "options": [{"label": "探险"}, {"label": "经营"}]},
                             {"id": "two", "question": "补充主角目标"}]}
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(interactions.ask, book.run_id, "choose", "question", payload,
                             book.cancel.is_set, book.control)
        item = pending(book)
        for answer in (
            {"answers": [{"id": "one", "selected": ["探险"]}]},
            {"answers": [{"id": "one", "selected": ["不存在"]}, {"id": "two", "custom": "归家"}]},
            {"answers": [{"id": "one", "selected": ["探险", "探险"]}, {"id": "two", "custom": "归家"}]},
            {"answers": [{"id": "one", "selected": ["探险"], "custom": "  "}, {"id": "two", "custom": "归家"}]},
        ):
            with pytest.raises(InvalidOperationError):
                interactions.respond(book.run_id, item["id"], answer)
        response = {"answers": [{"id": "one", "selected": ["探险", "经营"], "custom": "互助"},
                                {"id": "two", "selected": [], "custom": "归家"}]}
        interactions.respond(book.run_id, item["id"], response)
        assert future.result(timeout=3) == response


def test_approval_exact_preview_and_apply_once(book):
    rel, target = seed(book)
    before = target.read_text(encoding="utf-8")
    args = {"rel_path": rel, "old_text": "原文。", "new_text": "修订。"}
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(tools.execute, "replace_text", args, book.ctx)
        item = pending(book, "approval")
        preview = item["payload"]
        assert target.read_text(encoding="utf-8") == before
        assert changes.list_changes(book.run_id) == []
        assert preview["after_content"] == "修订。\n保留这一行。\n"
        assert preview["before_hash"] == changes.content_hash(before)
        assert preview["diff"]
        approve(book, item)
        result = future.result(timeout=3)
    assert result["ok"] and result["applied"]
    assert result["data"]["after_content"] == preview["after_content"]
    assert result["data"]["after_hash"] == preview["after_hash"]
    assert tools.execute("replace_text", args, book.ctx)["ok"]
    assert len(changes.list_changes(book.run_id)) == 1


@pytest.mark.parametrize("operation,args", [
    ("delete_file", {}), ("move_file", {"destination": "备忘录/改名.md"}),
])
def test_auto_destructive_operations_still_require_approval_and_reject(book, operation, args):
    book.ctx["permission_mode"] = "auto"
    rel, target = seed(book)
    before = target.read_text(encoding="utf-8")
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(tools.execute, operation, {"rel_path": rel, **args}, book.ctx)
        item = pending(book, "approval")
        assert "删除或移动" in item["payload"]["reason"]
        approve(book, item, "reject")
        result = future.result(timeout=3)
    assert not result["ok"] and result["code"] == "approval_rejected"
    assert target.read_text(encoding="utf-8") == before
    assert changes.list_changes(book.run_id) == []


@pytest.mark.parametrize("operation,args", [
    ("write_file", {"content": "整篇新内容"}),
    ("replace_text", {"old_text": "原文。\n保留这一行。\n", "new_text": "整篇替换"}),
    ("replace_text", {"old_text": "原文。", "new_text": "局部修订。"}),
])
def test_auto_rewrite_of_existing_body_skips_approval(book, operation, args):
    book.ctx["permission_mode"] = "auto"
    rel, _target = seed(book)
    result = tools.execute(operation, {"rel_path": rel, **args}, book.ctx)
    assert result["ok"] and result["applied"], result
    # 自动模式下改写已有正文不再产生审批卡，但仍写入门禁校验过的写前预览。
    assert interactions.list_interactions(book.run_id) == []
    assert len(changes.list_changes(book.run_id)) == 1


def test_auto_chapter_rewrite_and_whole_replace_skip_approval(book):
    book.ctx["permission_mode"] = "auto"
    rel = "章节/第0001章.txt"
    assert tools.execute("create_file", {"rel_path": rel, "content": BODY}, book.ctx)["ok"]
    original = changes.file_state(book.id, rel)["content"]
    book.ctx["tool_call_id"] = "chapter-rewrite"
    assert tools.execute("write_file", {"rel_path": rel, "content": original + "\n他数了数手里的铜钱，又放了回去。"},
                         book.ctx)["ok"]
    # 整章替换（old_text 覆盖已有正文）同样不弹卡
    book.ctx["tool_call_id"] = "chapter-whole-replace"
    replaced = tools.execute("replace_text", {"rel_path": rel, "old_text": changes.file_state(book.id, rel)["content"],
                                              "new_text": original}, book.ctx)
    assert replaced["ok"] and replaced["applied"], replaced
    assert changes.file_state(book.id, rel)["content"] == original
    assert interactions.list_interactions(book.run_id) == []


def test_auto_write_reuses_gate_checked_preview_without_approval(book, monkeypatch):
    """auto 模式无需审批时仍要把写前预览传给落盘，避免重复计算与绕过门禁。"""
    book.ctx["permission_mode"] = "auto"
    rel, target = seed(book)
    seen = {}
    original = changes.apply_change

    def spy(project_id, **kwargs):
        seen.update(kwargs)
        return original(project_id, **kwargs)

    monkeypatch.setattr(changes, "apply_change", spy)
    assert tools.execute("replace_text", {"rel_path": rel, "old_text": "原文。", "new_text": "修订。"}, book.ctx)["ok"]
    preview = seen.get("approved_preview")
    assert preview and preview["after_content"] == "修订。\n保留这一行。\n"
    assert target.read_text(encoding="utf-8") == "修订。\n保留这一行。\n"
    assert interactions.list_interactions(book.run_id) == []


def test_ask_task_approval_releases_later_writes_in_same_run(book):
    rel, target = seed(book)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(tools.execute, "replace_text",
                             {"rel_path": rel, "old_text": "原文。", "new_text": "第一次修订。"}, book.ctx)
        item = pending(book, "approval")
        responded = interactions.respond(book.run_id, item["id"], {"decision": "approve", "scope": "task"})
        assert responded["response"] == {"decision": "approve", "scope": "task"}
        assert future.result(timeout=3)["ok"]
    book.ctx["tool_call_id"] = "write-two"
    second = tools.execute("write_file", {"rel_path": rel, "content": "第二次整体改写。"}, book.ctx)
    assert second["ok"] and second["applied"]
    assert target.read_text(encoding="utf-8") == "第二次整体改写。"
    # 同一 run 内不再弹卡，任务级批准不落持久表。
    assert [item["kind"] for item in interactions.list_interactions(book.run_id)] == ["approval"]
    with db.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM chat_interactions").fetchone()[0] == 1


def test_reject_and_other_runs_do_not_inherit_task_approval(book):
    rel, _target = seed(book)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(tools.execute, "replace_text",
                             {"rel_path": rel, "old_text": "原文。", "new_text": "被打回。"}, book.ctx)
        item = pending(book, "approval")
        approve(book, item, "reject")
        assert future.result(timeout=3)["code"] == "approval_rejected"
    assert interactions.has_task_approval(book.run_id) is False
    # 任务级批准不跨 run：另一个 run 的写操作仍然弹卡。
    other_run = str(uuid.uuid4())
    with db.get_conn() as conn:
        conn.execute("UPDATE chat_runs SET status='completed' WHERE id=?", (book.run_id,))
        conn.execute("INSERT INTO chat_runs(id,session_id,project_id,client_request_id,request,status)"
                     " VALUES (?,?,?,?,?,'running')", (other_run, book.session, book.id, "other", "{}"))
    other_ctx = {**book.ctx, "run_id": other_run, "tool_call_id": "other-run-write"}
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(tools.execute, "write_file", {"rel_path": rel, "content": "另一轮改写。"}, other_ctx)
        card = pending_in(book, other_run, "approval")
        interactions.respond(other_run, card["id"], {"decision": "approve"})
        assert future.result(timeout=3)["ok"]
    assert interactions.has_task_approval(other_run) is False


def test_auto_new_and_local_replace_skip_approval(book):
    book.ctx["permission_mode"] = "auto"
    first = tools.execute("create_file", {"rel_path": "新目录/札记.md", "content": "甲。\n乙。"}, book.ctx)
    assert first["ok"]
    book.ctx["tool_call_id"] = "local-change"
    second = tools.execute("replace_text", {"rel_path": "新目录/札记.md", "old_text": "甲。", "new_text": "丙。"}, book.ctx)
    assert second["ok"]
    assert interactions.list_interactions(book.run_id) == []


def test_full_skips_approval_but_does_not_skip_boundaries_or_gates(book):
    book.ctx["permission_mode"] = "full"
    rel, target = seed(book)
    assert tools.execute("write_file", {"rel_path": rel, "content": "整体重写"}, book.ctx)["ok"]
    book.ctx["tool_call_id"] = "gates"
    rejected = tools.execute("new_chapter", {"title": "短章", "content": "太短。"}, book.ctx)
    assert not rejected["ok"] and rejected["code"] == "gate_rejected"
    book.ctx["tool_call_id"] = "escape"
    assert not tools.execute("create_file", {"rel_path": "../外部.md", "content": "越界"}, book.ctx)["ok"]
    assert interactions.list_interactions(book.run_id) == []


def test_approved_source_version_changed_expires_without_overwrite(book):
    rel, target = seed(book)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(tools.execute, "write_file", {"rel_path": rel, "content": "模型改稿"}, book.ctx)
        item = pending(book)
        target.write_text("作者在等待期间修改", encoding="utf-8")
        approve(book, item)
        result = future.result(timeout=3)
    assert not result["ok"] and result["code"] == "interaction_expired"
    assert target.read_text(encoding="utf-8") == "作者在等待期间修改"
    assert interactions.list_interactions(book.run_id)[0]["status"] == "expired"
    assert changes.list_changes(book.run_id) == []


def test_approved_destination_changes_expires(book):
    rel, target = seed(book)
    destination = book.root / "备忘录/目标.md"
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(tools.execute, "move_file", {"rel_path": rel, "destination": "备忘录/目标.md"}, book.ctx)
        item = pending(book)
        destination.write_text("作者新文件", encoding="utf-8")
        approve(book, item)
        result = future.result(timeout=3)
    assert result["code"] == "interaction_expired"
    assert target.exists() and destination.read_text(encoding="utf-8") == "作者新文件"


@pytest.mark.parametrize("status", ["cancelled", "interrupted"])
def test_wait_interruption_rejects_late_approval(book, status):
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(tools.execute, "create_file", {"rel_path": "新目录/不写.md", "content": "内容"}, book.ctx)
        item = pending(book)
        interactions.interrupt_run(book.run_id, status)
        result = future.result(timeout=3)
    assert result["code"] == "interaction_" + status
    assert not (book.root / "新目录").exists()
    with pytest.raises(interactions.InteractionEndedError):
        approve(book, item)
    assert not book.control.waiting


def test_cancellation_callback_unblocks_without_file_lock(book):
    rel, target = seed(book)
    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(tools.execute, "delete_file", {"rel_path": rel}, book.ctx)
        pending(book)
        # Waiting for a human holds neither the project lock nor a SQLite transaction.
        assert pool.submit(changes.file_state, book.id, rel).result(timeout=1)["exists"]
        book.cancel.set()
        result = future.result(timeout=1)
    assert result["code"] == "interaction_cancelled" and target.exists()


def test_disk_records_rebuild_cache_and_cross_run_answer_is_rejected(book):
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(interactions.ask, book.run_id, "approve", "approval", {"description": "检查缓存"},
                             book.cancel.is_set, book.control)
        item = pending(book)
        with db.get_conn() as conn:
            conn.execute("DELETE FROM chat_interactions")
        assert interactions.list_interactions(book.run_id)[0] == item
        with db.get_conn() as conn:
            assert conn.execute("SELECT COUNT(*) FROM chat_interactions").fetchone()[0] == 1
        with pytest.raises(NodeNotFoundError):
            interactions.respond(str(uuid.uuid4()), item["id"], {"decision": "approve"})
        approve(book, item)
        assert future.result(timeout=3) == {"decision": "approve"}


def test_preview_is_pure_and_applies_identical_normalized_chapter(book):
    rel = "章节/第0001章.txt"
    kwargs = {"operation": "create", "rel_path": rel, "content": BODY, "title": "规范化标题"}
    preview = changes.preview_change(book.id, **kwargs)
    assert preview["gates"]["passed"]
    # 章节正文为规范化的纯正文；预览内容必须与实际写入完全一致。
    assert preview["after_content"].endswith(BODY)
    assert not (book.root / rel).exists()
    assert changes.list_changes(book.run_id) == []
    result = changes.apply_change(book.id, run_id=book.run_id, session_id=book.session,
                                  tool_call_id="normalized", approved_preview=preview, **kwargs)
    assert result["after_hash"] == preview["after_hash"]
    assert (book.root / rel).read_text(encoding="utf-8") == preview["after_content"]


def test_preview_cannot_authorize_another_candidate(book):
    rel, target = seed(book)
    state = changes.file_state(book.id, rel)
    kwargs = {"operation": "rewrite", "rel_path": rel, "expected_hash": state["hash"]}
    preview = changes.preview_change(book.id, content="批准内容", **kwargs)
    with pytest.raises(changes.FileConflictError):
        changes.apply_change(book.id, run_id=book.run_id, session_id=book.session,
                             tool_call_id="changed-candidate", content="未经批准内容", approved_preview=preview, **kwargs)
    assert target.read_text(encoding="utf-8") == state["content"]


def test_invalid_questions_are_rejected_without_entering_wait(book):
    for payload in ({"questions": []}, {"questions": [{"id": "a", "question": "方向", "options": [{"label": "其他"}]}]},
                    {"questions": [{"id": "a", "question": "甲"}, {"id": "a", "question": "乙"}]}):
        with pytest.raises(InvalidOperationError):
            interactions.ask(book.run_id, "invalid", "question", payload)
    assert interactions.list_interactions(book.run_id) == []


def test_memory_projection_failure_preserves_answer_and_publishes_event(book, monkeypatch):
    def fail_projection(*_args):
        raise OSError("memory unavailable")
    monkeypatch.setattr(interactions, "_capture_answer", fail_projection)
    payload = {"questions": [{"id": "goal", "question": "主角目标是什么"}]}
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(interactions.ask, book.run_id, "goal", "question", payload,
                             book.cancel.is_set, book.control)
        item = pending(book)
        response = {"answers": [{"id": "goal", "selected": [], "custom": "找到失散家人"}]}
        answered = interactions.respond(book.run_id, item["id"], response)
        assert future.result(timeout=3) == response
    assert answered["status"] == "answered" and answered["warning"]
    assert len(chat_service.list_messages(book.session)) == 1
    with db.get_conn() as conn:
        events = [json.loads(row[0]) for row in conn.execute("SELECT payload FROM chat_run_events WHERE run_id=? ORDER BY seq", (book.run_id,))]
    assert any(event.get("interaction", {}).get("warning") for event in events)


def test_question_answer_projects_memory_and_acknowledges_only_after_vendor_result(book, monkeypatch):
    from workbench.backend.services import chat_memory_service
    monkeypatch.setattr(interactions, "_capture_answer", _CAPTURE_ANSWER)
    payload = {"questions": [{"id": "name", "question": "主角姓名是什么", "options": [{"label": "沈砚"}, {"label": "顾舟"}]}]}
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(interactions.ask, book.run_id, "name", "question", payload,
                             book.cancel.is_set, book.control)
        item = pending(book)
        response = {"answers": [{"id": "name", "selected": ["沈砚"]}]}
        answer = interactions.respond(book.run_id, item["id"], response)
        future.result(timeout=3)
    with db.get_conn() as conn:
        assert conn.execute("SELECT dsh_delivered FROM chat_messages WHERE id=?", (answer["user_message_id"],)).fetchone()[0] == 0
    entries = chat_memory_service.list_entries(book.id)
    assert any("沈砚" in entry["content"] for entry in entries)
    assert interactions.respond(book.run_id, item["id"], response) == answer
    assert chat_memory_service.list_entries(book.id) == entries
    interactions.mark_delivered(book.run_id, "name")
    with db.get_conn() as conn:
        assert conn.execute("SELECT dsh_delivered FROM chat_messages WHERE id=?", (answer["user_message_id"],)).fetchone()[0] == 1
    checkpoint = json.loads((config.runtime_dir() / "chat/sessions" / f"{book.session}.json").read_text(encoding="utf-8"))
    assert checkpoint["messages"][0]["dsh_delivered"] == 1


def test_answer_does_not_revive_cancelling_run(book):
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(interactions.ask, book.run_id, "stop", "approval", {"operation": "delete"},
                             book.cancel.is_set, book.control)
        item = pending(book)
        with db.get_conn() as conn:
            conn.execute("UPDATE chat_runs SET status='cancelling' WHERE id=?", (book.run_id,))
        with pytest.raises(interactions.InteractionEndedError):
            approve(book, item)
        with pytest.raises(interactions.InteractionEndedError):
            future.result(timeout=3)
    with db.get_conn() as conn:
        assert conn.execute("SELECT status FROM chat_runs WHERE id=?", (book.run_id,)).fetchone()[0] == "cancelling"


def test_restart_does_not_reuse_approval_before_uncommitted_write(book):
    args = {"rel_path": "备忘录/重启不写.md", "content": "尚未执行"}
    preview = changes.preview_change(book.id, operation="create", **args)
    payload = {**preview, "tool": "create_file", "label": "新建文档", "reason": "本轮设置为逐次请求批准"}
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(interactions.ask, book.run_id, book.ctx["tool_call_id"], "approval", payload,
                             book.cancel.is_set, book.control)
        item = pending(book)
        approve(book, item)
        assert future.result(timeout=3) == {"decision": "approve"}
    # The process stopped after accepting the answer, before a change journal existed.
    with db.get_conn() as conn:
        conn.execute("UPDATE chat_runs SET status='interrupted' WHERE id=?", (book.run_id,))
    interactions.interrupt_run(book.run_id, "interrupted")
    result = tools.execute("create_file", args, book.ctx)
    assert not result["ok"] and result["code"] == "interaction_interrupted"
    assert not (book.root / args["rel_path"]).exists()
    assert changes.list_changes(book.run_id) == []


def test_stale_cache_and_status_publish_do_not_reverse_finished_state(book):
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(interactions.ask, book.run_id, "state", "approval", {"operation": "create"},
                             book.cancel.is_set, book.control)
        pending_item = pending(book)
        answered = approve(book, pending_item)
        future.result(timeout=3)
    interactions._cache(pending_item)
    with db.get_conn() as conn:
        cached = json.loads(conn.execute("SELECT record FROM chat_interactions WHERE id=?", (answered["id"],)).fetchone()[0])
        assert cached["status"] == "answered"
        conn.execute("UPDATE chat_runs SET status='completed' WHERE id=?", (book.run_id,))
        last_seq = conn.execute("SELECT MAX(seq) FROM chat_run_events WHERE run_id=?", (book.run_id,)).fetchone()[0]
    interactions._emit(pending_item, "running")
    with db.get_conn() as conn:
        new_events = [json.loads(row[0]) for row in conn.execute("SELECT payload FROM chat_run_events WHERE run_id=? AND seq>?", (book.run_id,last_seq))]
    assert all(event.get("status") != "running" for event in new_events)
    assert new_events[0]["interaction"]["status"] == "answered"


def test_cancel_after_approval_during_snapshot_never_writes_manuscript(book, monkeypatch):
    from workbench.backend.services import snapshot_service
    rel, target = seed(book)
    before = target.read_text(encoding="utf-8")
    monkeypatch.setattr(snapshot_service, "snapshot_file", lambda *_args, **_kwargs: book.cancel.set())
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(tools.execute, "write_file", {"rel_path": rel, "content": "不要提交"}, book.ctx)
        item = pending(book)
        approve(book, item)
        result = future.result(timeout=3)
    assert not result["ok"]
    assert target.read_text(encoding="utf-8") == before
    assert all(change["status"] != "applied" for change in changes.list_changes(book.run_id))


def test_approval_never_disables_chapter_gates(book):
    preview = changes.preview_change(book.id, operation="create", rel_path="章节/第0001章.txt", content="短稿")
    with pytest.raises(changes.GateRejectedError):
        changes.apply_change(book.id, run_id=book.run_id, session_id=book.session, tool_call_id="gates-preview",
            operation="create", rel_path="章节/第0001章.txt", content="短稿", approved_preview=preview, validate_gates=False)
    assert not (book.root / "章节/第0001章.txt").exists()


def test_pending_creation_is_atomic_with_waiting_run_snapshot(book, monkeypatch):
    from workbench.backend.services import chat_run_service
    saved, release, reading = threading.Event(), threading.Event(), threading.Event()
    original_save = interactions._save

    def pause_after_pending_save(item):
        original_save(item)
        if item["status"] == "pending":
            saved.set()
            assert release.wait(3)

    def snapshot():
        reading.set()
        return chat_run_service.get_run(book.run_id)

    monkeypatch.setattr(interactions, "_save", pause_after_pending_save)
    with ThreadPoolExecutor(max_workers=2) as pool:
        waiting = pool.submit(interactions.ask, book.run_id, "atomic-card", "approval",
                              {"operation": "create"}, book.cancel.is_set, book.control)
        try:
            assert saved.wait(3)
            observed = pool.submit(snapshot)
            assert reading.wait(3)
            # The card is already on disk, but the public snapshot must wait for
            # its corresponding database state instead of publishing half a state.
            time.sleep(.1)
            assert not observed.done()
        finally:
            release.set()
        run = observed.result(timeout=3)
        assert run["status"] == "waiting_input"
        assert len(run["interactions"]) == 1 and run["interactions"][0]["status"] == "pending"
        approve(book, run["interactions"][0])
        assert waiting.result(timeout=3) == {"decision": "approve"}


def test_concurrent_duplicate_approvals_commit_only_once(book):
    rel, target = seed(book)
    with ThreadPoolExecutor(max_workers=3) as pool:
        waiting = pool.submit(tools.execute, "replace_text",
                              {"rel_path": rel, "old_text": "原文。", "new_text": "批准后的内容。"}, book.ctx)
        item = pending(book, "approval")
        replies = [pool.submit(approve, book, item) for _ in range(2)]
        answers = [reply.result(timeout=3) for reply in replies]
        assert answers[0] == answers[1]
        assert waiting.result(timeout=3)["ok"]
    assert target.read_text(encoding="utf-8") == "批准后的内容。\n保留这一行。\n"
    assert len(changes.list_changes(book.run_id)) == 1


# ───────────────────── 写域（两维判定）：只读 → 写域 → 批准强度 ─────────────────────


def _scoped_ctx(book, *, permission="auto", targets=("设定",), call="scope-write"):
    """带本轮写域的工具上下文（写域由路由声明的材料集合归一而来）。"""
    scope = scope_guard.build_scope(project_id=book.id, write_targets=list(targets))
    return {**book.ctx, "tool_call_id": call, "permission_mode": permission, "scope": scope}


def _seed_file(book, ctx, rel, content):
    target = book.root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    assert tools.execute("read_file", {"rel_path": rel}, ctx)["ok"]
    return target


@pytest.mark.parametrize("permission,expects_card", [("ask", True), ("auto", False), ("full", False)])
def test_in_scope_write_keeps_each_permission_mode_intact(book, permission, expects_card):
    ctx = _scoped_ctx(book, permission=permission, call=f"in-scope-{permission}")
    target = _seed_file(book, ctx, "设定/人物设定.md", "旧设定。\n")
    args = {"rel_path": "设定/人物设定.md", "old_text": "旧设定。", "new_text": "新设定。"}
    if not expects_card:
        result = tools.execute("replace_text", args, ctx)
        assert interactions.list_interactions(book.run_id) == []
    else:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(tools.execute, "replace_text", args, ctx)
            item = pending(book, "approval")
            assert "scope" not in item["payload"]      # 范围内不标越界
            approve(book, item)
            result = future.result(timeout=3)
    assert result["ok"] and result["applied"], result
    assert target.read_text(encoding="utf-8") == "新设定。\n"


def test_out_of_scope_in_auto_degrades_to_single_approval(book):
    ctx = _scoped_ctx(book, permission="auto", call="oos-auto")
    target = _seed_file(book, ctx, "大纲/大纲.md", "旧大纲。\n")
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(tools.execute, "write_file",
                             {"rel_path": "大纲/大纲.md", "content": "新大纲。"}, ctx)
        item = pending(book, "approval")
        payload = item["payload"]
        assert payload["scope"]["out_of_scope"] is True
        assert payload["scope"]["material"] == "大纲"
        assert payload["scope"]["label"] == "本轮范围：设定/"
        assert payload["reason"] == "越界：本轮范围是 设定/，此操作会改 大纲/"
        assert [option["id"] for option in payload["scope"]["options"]] == ["once", "material"]
        assert target.read_text(encoding="utf-8") == "旧大纲。\n"    # 弹卡期间不落盘
        assert changes.list_changes(book.run_id) == []
        approve(book, item)
        result = future.result(timeout=3)
    assert result["ok"] and result["applied"], result
    assert target.read_text(encoding="utf-8") == "新大纲。"


def test_out_of_scope_rejection_keeps_file_and_explains_reason(book):
    ctx = _scoped_ctx(book, permission="auto", call="oos-reject")
    target = _seed_file(book, ctx, "大纲/大纲.md", "旧大纲。\n")
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(tools.execute, "write_file",
                             {"rel_path": "大纲/大纲.md", "content": "不该写入。"}, ctx)
        item = pending(book, "approval")
        approve(book, item, "reject")
        result = future.result(timeout=3)
    assert result["ok"] is False and result["code"] == "approval_rejected"
    assert "越界" in result["summary"] and "大纲" in result["summary"]
    assert result["scope"]["out_of_scope"] is True
    assert target.read_text(encoding="utf-8") == "旧大纲。\n"
    assert changes.list_changes(book.run_id) == []


def test_out_of_scope_full_allows_with_trace_then_limit_forces_approval(book):
    ctx = _scoped_ctx(book, permission="full", call="oos-full")
    target = _seed_file(book, ctx, "大纲/大纲.md", "旧大纲。\n")
    result = tools.execute("write_file", {"rel_path": "大纲/大纲.md", "content": "完全访问新大纲。"}, ctx)
    assert result["ok"] and result["applied"], result
    assert result["scope"]["out_of_scope"] is True
    assert result["scope"]["resolved"] == "full_allow"
    assert target.read_text(encoding="utf-8") == "完全访问新大纲。"
    assert interactions.list_interactions(book.run_id) == []
    with db.get_conn() as conn:
        events = [json.loads(row[0]) for row in conn.execute(
            "SELECT payload FROM chat_run_events WHERE run_id=?", (book.run_id,))]
    assert any("越界写入已按完全访问放行" in str(event.get("message") or "") for event in events)
    assert any(entry["action"] == "chat-scope-full-allow" for entry in operation_log.recent(book.id))

    # 开启「完全访问也受写域限制」后，同一越界写改为请求批准。
    settings_service.update_settings({"chat": {"scope_limit_full": True}})
    ctx["tool_call_id"] = "oos-full-limited"
    _seed_file(book, ctx, "大纲/大纲.md", "旧大纲。\n")
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(tools.execute, "write_file",
                             {"rel_path": "大纲/大纲.md", "content": "受限后重写。"}, ctx)
        item = pending(book, "approval")
        assert item["payload"]["scope"]["out_of_scope"] is True
        approve(book, item)
        assert future.result(timeout=3)["ok"]


def test_strict_mode_rejects_out_of_scope_without_card(book):
    settings_service.update_settings({"chat": {"scope_strictness": "reject"}})
    ctx = _scoped_ctx(book, permission="auto", call="oos-strict")
    target = _seed_file(book, ctx, "大纲/大纲.md", "旧大纲。\n")
    result = tools.execute("write_file", {"rel_path": "大纲/大纲.md", "content": "不该写入。"}, ctx)
    assert result["ok"] is False and result["code"] == "out_of_scope"
    assert "越界" in result["summary"]
    assert result["scope"]["out_of_scope"] is True
    assert target.read_text(encoding="utf-8") == "旧大纲。\n"
    assert interactions.list_interactions(book.run_id) == []
    assert changes.list_changes(book.run_id) == []


@pytest.mark.parametrize("permission", ["ask", "auto", "full"])
def test_strict_reject_blocks_out_of_scope_in_every_permission_mode(book, permission):
    """严格模式下档位不能绕过写域：三档的越界写都被直接拒绝。"""
    settings_service.update_settings({"chat": {"scope_strictness": "reject"}})
    ctx = _scoped_ctx(book, permission=permission, call=f"oos-strict-{permission}")
    target = _seed_file(book, ctx, "大纲/大纲.md", "旧大纲。\n")
    result = tools.execute("write_file", {"rel_path": "大纲/大纲.md", "content": "不该写入。"}, ctx)
    assert result["code"] == "out_of_scope" and target.read_text(encoding="utf-8") == "旧大纲。\n"


@pytest.mark.parametrize("permission", ["ask", "auto", "full"])
def test_read_only_rejects_even_in_scope_writes(book, permission):
    ctx = _scoped_ctx(book, permission=permission, call=f"read-only-{permission}")
    target = _seed_file(book, ctx, "设定/人物设定.md", "旧设定。\n")
    ctx["read_only"] = True
    result = tools.execute("replace_text", {"rel_path": "设定/人物设定.md",
                                            "old_text": "旧设定。", "new_text": "不该写入。"}, ctx)
    assert result["ok"] is False and "只读" in result["summary"]
    assert target.read_text(encoding="utf-8") == "旧设定。\n"
    assert changes.list_changes(book.run_id) == []


def test_task_approval_does_not_exempt_out_of_scope(book):
    interactions.ensure_schema()
    interactions._save({"id": str(uuid.uuid4()), "run_id": book.run_id, "tool_call_id": "task-approve",
                        "kind": "approval", "payload": {"operation": "delete"}, "payload_hash": "pre",
                        "status": "answered", "response": {"decision": "approve", "scope": "task"},
                        "created_at": "2026-01-01T00:00:00+00:00"})
    assert interactions.has_task_approval(book.run_id) is True
    # 范围内写入仍受既有「本任务内不再询问」豁免（语义未收紧）
    in_scope = _scoped_ctx(book, permission="ask", call="in-scope-task-approval")
    assert tools.execute("create_file", {"rel_path": "设定/新设定.md", "content": "新设定。"},
                         in_scope)["ok"]
    assert interactions.list_interactions(book.run_id)[-1]["kind"] == "approval"

    ctx = _scoped_ctx(book, permission="auto", call="oos-task-approval")
    _seed_file(book, ctx, "大纲/大纲.md", "旧大纲。\n")
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(tools.execute, "write_file",
                             {"rel_path": "大纲/大纲.md", "content": "越界重写。"}, ctx)
        item = pending(book, "approval")           # 越界不被任务级批准免询
        assert item["payload"]["scope"]["out_of_scope"] is True
        approve(book, item)
        assert future.result(timeout=3)["ok"]


def test_material_grant_covers_rest_of_run(book):
    ctx = _scoped_ctx(book, permission="auto", call="oos-grant-1")
    _seed_file(book, ctx, "大纲/大纲.md", "旧大纲。\n")
    _seed_file(book, ctx, "状态/角色状态.md", "旧状态。\n")
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(tools.execute, "write_file",
                             {"rel_path": "大纲/大纲.md", "content": "一稿。"}, ctx)
        item = pending(book, "approval")
        interactions.respond(book.run_id, item["id"], {"decision": "approve", "scope": "material"})
        assert future.result(timeout=3)["ok"]
    assert scope_guard.grants(book.run_id) == {"大纲"}
    # 同类材料在 run 内不再弹卡
    ctx["tool_call_id"] = "oos-grant-2"
    second = tools.execute("write_file", {"rel_path": "大纲/大纲.md", "content": "二稿。"}, ctx)
    assert second["ok"] and second["applied"], second
    # 其他材料仍需批准
    ctx["tool_call_id"] = "oos-grant-3"
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(tools.execute, "write_file",
                             {"rel_path": "状态/角色状态.md", "content": "新状态。"}, ctx)
        card = pending(book, "approval")
        assert card["payload"]["scope"]["material"] == "状态"
        approve(book, card)
        assert future.result(timeout=3)["ok"]
    assert (book.root / "状态/角色状态.md").read_text(encoding="utf-8") == "新状态。"


def test_once_approval_does_not_grant_material(book):
    ctx = _scoped_ctx(book, permission="auto", call="oos-once-1")
    _seed_file(book, ctx, "大纲/大纲.md", "旧大纲。\n")
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(tools.execute, "write_file",
                             {"rel_path": "大纲/大纲.md", "content": "一稿。"}, ctx)
        item = pending(book, "approval")
        interactions.respond(book.run_id, item["id"], {"decision": "approve", "scope": "once"})
        assert future.result(timeout=3)["ok"]
    assert scope_guard.grants(book.run_id) == set()
    ctx["tool_call_id"] = "oos-once-2"
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(tools.execute, "write_file",
                             {"rel_path": "大纲/大纲.md", "content": "二稿。"}, ctx)
        card = pending(book, "approval")
        assert card["payload"]["scope"]["out_of_scope"] is True
        approve(book, card)
        assert future.result(timeout=3)["ok"]
    assert (book.root / "大纲/大纲.md").read_text(encoding="utf-8") == "二稿。"
