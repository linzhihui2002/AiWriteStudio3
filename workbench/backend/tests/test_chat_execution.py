"""Conversation contracts with fake models and synthetic books only."""
from __future__ import annotations
import threading
import sqlite3
from contextlib import contextmanager
import pytest
from workbench.backend import config
from workbench.backend.services import chat_run_service as runs, chat_service, routing_service, agent_service
from workbench.backend.services import chat_execution_service as policy, file_change_service as files, provider_service, context_service
from workbench.backend.tests.test_chat_runs import workspace, install, finish, plan_route, TWO_STEPS


@pytest.mark.parametrize("at", ["construct", "start"])
def test_worker_launch_failure_is_terminal_and_never_replays_same_request(workspace, monkeypatch, at):
    _, session, _ = workspace
    engine = install(monkeypatch)
    launches = []
    def unavailable(*args, **kwargs):
        launches.append(at)
        raise RuntimeError("synthetic worker launch failure")
    if at == "construct":
        monkeypatch.setattr(runs.threading, "Thread", unavailable)
    else:
        monkeypatch.setattr(runs.threading.Thread, "start", unavailable)
    first = runs.create_run(session["id"], text="讨论开篇", client_request_id="launch-failure")
    assert first["status"] == "failed" and first["error_code"] == "RUN_START_FAILED"
    assert runs.active_run(session["id"]) is None
    assert not runs._workers and not engine.calls
    retry = runs.create_run(session["id"], text="讨论开篇", client_request_id="launch-failure")
    assert retry["id"] == first["id"] and len(launches) == 1
    newer = runs.create_run(session["id"], text="新任务", client_request_id="new-launch-failure", start=False)
    assert newer["status"] == "queued"


def test_report_role_and_scope_ignore_reference_material_keyword_count(workspace, monkeypatch):
    project, session, _ = workspace
    text = "审稿当前章节，只报告连续性问题，不能改稿。请回读章节及世界设定，引用本书文件和具体句子。"
    decision = routing_service.route(None, text, record=False)
    assert decision["agent"] == "reviewer" and decision["write_targets"] == [] and not decision["plan"]
    install(monkeypatch)
    result = finish(runs.create_run(session["id"], text=text))
    assert result["read_only"] and result["routing"]["write_targets"] == []
    assert result["routing"]["scope_label"] == "本轮只读"


def test_semantic_first_executor_cannot_drop_second_ordered_output(workspace, monkeypatch):
    from workbench.backend.services import intent_service as intents
    project, _, _ = workspace
    monkeypatch.setattr(intents, "_llm_decision", lambda *args, **kwargs: ({"primary_agent":"setting-keeper",
        "intent":"设定管理", "write_targets":["设定"], "plan":[], "confidence":0.9,
        "reason":"先写设定再写大纲"}, ""))
    decision = routing_service.route(project["id"], "先创建设定/续接规则.md，再创建大纲/续接任务.md，两份实际保存。", record=False)
    assert [step["agent"] for step in decision["plan"]] == ["setting-keeper", "planner"]
    assert decision["write_targets"] == ["设定", "大纲"]


@pytest.mark.parametrize("text,agent,targets", [
    ("不改大纲，只完善设定", "setting-keeper", ["设定"]),
    ("根据世界设定、人物设定和场景设定写正文", "writer", ["章节"]),
    ("请润色正文中的场景描写，保持设定不变", "writer", ["章节"]),
    ("按世界设定把大纲补完整", "planner", ["大纲"]),
    ("不要改大纲\n只完善设定", "setting-keeper", ["设定"]),
    ("不要改大纲只完善设定", "setting-keeper", ["设定"]),
    ("不要误改大纲，只完善设定", "setting-keeper", ["设定"]),
])
def test_action_and_author_prohibitions_survive_offline_fallback(workspace, text, agent, targets):
    decision = routing_service.route(None, text, record=False)
    assert decision["agent"] == agent
    assert decision["write_targets"] == targets
    assert len(decision["plan"]) <= 1


def test_route_receives_current_file_target_and_bounded_previous_context(workspace, monkeypatch):
    project, session, _ = workspace
    chat_service._append_message(session["id"], "assistant", "选项二：改用插叙。")
    recorded = {}
    real = routing_service.route
    def spy(project_id, text, **kwargs):
        recorded.update(kwargs)
        return real(project_id, text, **kwargs)
    monkeypatch.setattr(routing_service, "route", spy)
    install(monkeypatch)
    result = finish(runs.create_run(session["id"], text="按第二个方案来", context={"active_file": "设定/世界设定.md"}))
    assert result["ok"]
    assert recorded["active_file"] == "设定/世界设定.md"
    assert "选项二" in recorded["context_hint"] and len(recorded["context_hint"]) <= 4000


def test_public_route_preview_accepts_optional_context_without_new_authority(monkeypatch):
    from workbench.backend.api.orchestration import RouteIn, routing_route
    captured = {}
    def decide(project_id, text, **kwargs):
        captured.update(kwargs)
        return {"agent": "writing-assistant", "write_targets": []}
    monkeypatch.setattr(routing_service, "route", decide)
    response = routing_route(RouteIn(text="按第二项", active_file="设定/人物.md", context_hint="选项二：插叙"))
    assert response["write_targets"] == []
    assert captured["active_file"] == "设定/人物.md" and captured["context_hint"] == "选项二：插叙"
    assert captured["target_chapter"] == "" and captured["record"] is False


@pytest.mark.parametrize("agent", sorted(agent_service.READ_ONLY_AGENTS))
def test_readonly_roles_deny_forged_writes_and_undo(workspace, monkeypatch, agent):
    project, session, _ = workspace
    engine = install(monkeypatch, turns=[[("create_file", {"rel_path": "备忘录/越权.md", "content": "不许写"}),
                                       ("revert_last_changes", {}), "报告完毕"]])
    result = finish(runs.create_run(session["id"], text="修改设定并撤回上一轮", agent=agent))
    assert result["read_only"]
    assert not files.file_state(project["id"], "备忘录/越权.md")["exists"]
    assert not any(s["name"] in {"create_file", "revert_last_changes"} for s in engine.calls[0]["tool_specs"])


def test_write_plan_without_journal_receipt_stops_dependents(workspace, monkeypatch):
    _, session, _ = workspace
    plan_route(monkeypatch, TWO_STEPS)
    engine = install(monkeypatch, turns=[["设定已经补好了。"], ["大纲已完成。"]])
    result = finish(runs.create_run(session["id"], text="先补设定再列卷纲"))
    assert result["error_code"] == "DELIVERY_UNVERIFIED" and len(engine.calls) == 1
    assert result["completion"]["status"] == "unverified"
    assert result["plan_state"]["steps"][0]["status"] == "failed"
    assert result["plan_state"]["steps"][1]["status"] == "pending"


def test_continuation_skips_verified_step_and_delivers_new_author_message(workspace, monkeypatch):
    project, session, _ = workspace
    plan_route(monkeypatch, TWO_STEPS)
    engine = install(monkeypatch, turns=[
        [("create_file", {"rel_path": "设定/交付.md", "content": "真实设定。"}), "已保存。"],
        [{"type": "error", "code": "MOCK_ERROR", "message": "合成失败"}],
        [("create_file", {"rel_path": "大纲/交付.md", "content": "真实卷纲。"}), "已保存。"],
    ])
    first = finish(runs.create_run(session["id"], text="先补设定再列卷纲"))
    assert first["plan_state"]["steps"][0]["status"] == "verified"
    second = finish(runs.create_run(session["id"], text="继续", continue_from_run_id=first["id"]))
    assert second["ok"] and len(engine.calls) == 3
    assert second["plan_state"]["steps"][0]["reused"]
    assert engine.calls[-1]["message_id"] == second["user_message_id"]
    assert files.file_state(project["id"], "大纲/交付.md")["exists"]


def test_changed_or_reverted_receipt_cannot_skip_step(workspace):
    project, session, _ = workspace
    first = files.apply_change(project["id"], run_id="test", session_id=session["id"], tool_call_id="one",
                               operation="create", rel_path="备忘录/版本.md", content="初版")
    receipts = policy.receipts([first])
    assert policy.verify_receipts(project["id"], receipts)
    files.apply_change(project["id"], run_id="other", session_id=session["id"], tool_call_id="two",
        operation="rewrite", rel_path="备忘录/版本.md", content="作者修改", expected_hash=first["after_hash"])
    assert not policy.verify_receipts(project["id"], receipts)


@pytest.mark.parametrize("at", ["request", "manifest", "session", "started-event"])
def test_admission_disk_failure_closes_identity_before_retry(workspace, monkeypatch, at):
    _, session, _ = workspace
    engine = install(monkeypatch)
    write = runs.atomic_write_text
    persist = chat_service.persist_session
    emit = runs.emit
    def write_failure(path, text):
        if path.name == {"request":"request.json", "manifest":"run.json"}.get(at):
            raise OSError("合成磁盘故障")
        return write(path, text)
    def session_failure(session_id):
        if at == "session":
            raise OSError("合成会话投影故障")
        return persist(session_id)
    def event_failure(run_id, event, **kwargs):
        if at == "started-event" and event == "started":
            raise OSError("合成事件投影故障")
        return emit(run_id, event, **kwargs)
    monkeypatch.setattr(runs, "atomic_write_text", write_failure)
    monkeypatch.setattr(chat_service, "persist_session", session_failure)
    monkeypatch.setattr(runs, "emit", event_failure)
    first = runs.create_run(session["id"], text="讨论设定", client_request_id="disk-failure")
    assert first["status"] == "failed" and first["error_code"] == "RUN_PERSISTENCE_FAILED"
    second = runs.create_run(session["id"], text="讨论设定", client_request_id="disk-failure")
    assert second["id"] == first["id"] and runs.active_run(session["id"]) is None and not engine.calls


@pytest.mark.parametrize("at", ["run-row", "message-row", "message-link", "commit"])
def test_admission_database_transaction_failure_leaves_no_session_occupancy(workspace, monkeypatch, at):
    _, session, _ = workspace
    engine = install(monkeypatch)
    original = runs.db.get_conn
    broken = False
    class Connection:
        def __init__(self, real): self.real, self.admission = real, False
        def execute(self, sql, *args):
            nonlocal broken
            self.admission |= sql.startswith("INSERT INTO chat_runs")
            point = {"run-row":"INSERT INTO chat_runs", "message-row":"INSERT INTO chat_messages",
                     "message-link":"UPDATE chat_runs SET user_message_id"}.get(at)
            if not broken and point and sql.startswith(point):
                broken = True
                raise sqlite3.OperationalError("synthetic transaction failure")
            return self.real.execute(sql, *args)
        def __getattr__(self, name): return getattr(self.real, name)
    @contextmanager
    def faulty(*args, **kwargs):
        nonlocal broken
        with original(*args, **kwargs) as real:
            connection = Connection(real)
            yield connection
            if at == "commit" and connection.admission and not broken:
                broken = True
                raise sqlite3.OperationalError("synthetic commit failure")
    monkeypatch.setattr(runs.db, "get_conn", faulty)
    with pytest.raises(sqlite3.OperationalError):
        runs.create_run(session["id"], text="讨论开篇", client_request_id="db-failure")
    assert broken and runs.active_run(session["id"]) is None and not engine.calls
    with original() as conn:
        assert conn.execute("SELECT count(*) FROM chat_runs").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM chat_messages WHERE role='user'").fetchone()[0] == 0
    assert runs.create_run(session["id"], text="讨论开篇", client_request_id="db-failure", start=False)["status"] == "queued"


def test_material_budget_reserves_system_tools_output_and_history(monkeypatch):
    monkeypatch.setattr(provider_service, "get_provider", lambda _key: {"models": [{"id": "small", "context_window": 16000, "max_tokens": 2000}]})
    budget = policy.material_budget({"provider": "mock", "model": "small"}, "系统规则" * 500, [{"name": "read_file"}])
    assert budget["material_tokens"] + sum(budget[k] for k in ("static_tokens", "output_reserved", "history_reserved", "safety_reserved")) == 16000
    with pytest.raises(Exception, match="窗口不足"):
        policy.material_budget({"provider": "mock", "model": "small"}, "系统规则" * 10000, [])


def test_native_compaction_bridge_never_persists_summary_output(workspace, monkeypatch):
    _, session, _ = workspace
    install(monkeypatch, turns=[[{"type": "compaction", "phase": "start", "status": "running", "compaction_id": "c", "activity_id": "compact-c", "summary": "秘密摘要", "rawOutput": "秘密推理"},
        {"type": "compaction", "phase": "end", "status": "done", "compaction_id": "c", "activity_id": "compact-c"}, "报告"]])
    result = finish(runs.create_run(session["id"], text="讨论开篇"))
    events = [e for e in runs.events_after(result["id"]) if e["event"] == "compaction"]
    assert len(events) == 2 and result["metrics"]["compactions"] == 1
    assert all("summary" not in e and "rawOutput" not in e for e in events)


def test_shared_tool_limit_stops_remaining_plan_and_no_41st_mutation(workspace, monkeypatch):
    project, session, _ = workspace
    plan_route(monkeypatch, TWO_STEPS)
    first = [("read_file", {"rel_path": "设定/世界设定.md"})] * 39
    first += [("create_file", {"rel_path": "设定/已交付.md", "content": "第40次工具保存"}), "完成"]
    engine = install(monkeypatch, turns=[first,
        [("create_file", {"rel_path": "大纲/不可写入.md", "content": "第41次被拒绝"}), "完成"]])
    result = finish(runs.create_run(session["id"], text="先补设定再列卷纲"))
    assert result["error_code"] == "TOOL_LIMIT" and len(engine.calls) == 2
    assert engine.calls[-1]["should_cancel"]()
    assert result["metrics"]["tool_calls"] == 41
    assert files.file_state(project["id"], "设定/已交付.md")["exists"]
    assert not files.file_state(project["id"], "大纲/不可写入.md")["exists"]


def test_reference_material_is_immutable_even_in_full_access(workspace, monkeypatch):
    project, session, _ = workspace
    chat_service.update_session(session["id"], {"permission_mode": "full"})
    install(monkeypatch, turns=[[("create_file", {"rel_path": "设定/不许附带改.md", "content": "依据不可动"}),
        ("create_file", {"rel_path": "大纲/按依据编写.md", "content": "可保存的纲要"}), "完成"]])
    result = finish(runs.create_run(session["id"], text="根据世界设定列大纲"))
    assert result["ok"] and result["completion"]["status"] == "verified"
    assert not files.file_state(project["id"], "设定/不许附带改.md")["exists"]
    assert files.file_state(project["id"], "大纲/按依据编写.md")["exists"]


def test_specific_file_prohibition_keeps_other_same_material_editable(workspace, monkeypatch):
    project, session, _ = workspace
    chat_service.update_session(session["id"], {"permission_mode": "full"})
    install(monkeypatch, turns=[[("create_file", {"rel_path": "设定/禁改.md", "content": "不能写"}),
        ("create_file", {"rel_path": "设定/允许.md", "content": "可保存"}), "完成"]])
    result = finish(runs.create_run(session["id"], text="不要改设定/禁改.md，只完善设定/允许.md 并保存"))
    assert result["ok"]
    assert result["routing"]["forbidden_targets"] == ["设定/禁改.md"]
    assert not files.file_state(project["id"], "设定/禁改.md")["exists"]
    assert files.file_state(project["id"], "设定/允许.md")["exists"]


def test_explicit_material_wins_background_and_extreme_budget_is_bounded(workspace, monkeypatch):
    project, _, _ = workspace
    monkeypatch.setattr(context_service, "_level2", lambda *_: [context_service._block(2, "背景", "background", "背景" * 400)])
    assembly = context_service.assemble(project["id"], explicit=[{"title": "指定文本", "source": "selection", "text": "作者指定" * 20, "protected": True}],
        use_retrieval=False, budget_tokens=100)
    selected = next(b for b in assembly["blocks"] if b["source"] == "selection")
    assert selected["text"] == "作者指定" * 20
    assert assembly["total_tokens"] <= 100
    tiny = context_service.assemble(project["id"], explicit=[{"title": "巨大指定文本", "source": "selection", "text": "指定" * 1000, "protected": True}],
        use_retrieval=False, budget_tokens=10)
    assert tiny["total_tokens"] <= 10 and tiny["degradation"]


def test_real_undo_is_verified_and_readonly_cannot_bypass_scope(workspace, monkeypatch):
    project, session, _ = workspace
    chat_service.update_session(session["id"], {"permission_mode": "full"})
    install(monkeypatch, turns=[[("create_file", {"rel_path": "设定/撤回验收.md", "content": "待撤回"}), "已保存"],
        [("revert_last_changes", {}), "已撤回"]])
    first = finish(runs.create_run(session["id"], text="完善设定"))
    second = finish(runs.create_run(session["id"], text="撤回上一轮设定修改"))
    assert first["ok"] and second["ok"]
    assert second["completion"]["status"] == "verified" and second["completion"]["reverted_changes"]
    assert not files.file_state(project["id"], "设定/撤回验收.md")["exists"]
