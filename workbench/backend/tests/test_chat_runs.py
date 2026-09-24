"""Durable chat orchestration contracts, independent of a paid model/provider."""
from __future__ import annotations

import threading
import time

import pytest

from workbench.backend import config, db
from workbench.backend.services import chat_service, chat_run_service as runs, file_change_service as changes
from workbench.backend.services import project_service, agent_service, chapter_service
from workbench.backend.services.errors import InvalidOperationError


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    for name in ("books", "agents", "skills", "rules", "workflows"):
        (tmp_path/name).mkdir()
    monkeypatch.setattr(config,"PROJECT_ROOT",tmp_path)
    monkeypatch.setattr(config,"PROJECTS_DIR",tmp_path/"books")
    monkeypatch.setattr(config,"RUNTIME_DIR",tmp_path/".workbench")
    monkeypatch.setattr(config,"DB_PATH",tmp_path/".workbench/workbench.db")
    db.init_db()
    config.ensure_runtime_dirs()
    agent_service.ensure_builtin_agents()
    project=project_service.create_project(name="对话验收小说")
    session=chat_service.create_session(project_id=project["id"])
    monkeypatch.setattr(chat_service, "schedule_title", lambda *args, **kwargs: None)
    yield project,session,tmp_path
    runs.shutdown()


class FakeNative:
    def __init__(self, turns=None, hold=False):
        self.calls=[]
        self.turns=list(turns or [])
        self.hold=hold
        self.entered=threading.Event()

    def stream(self, **kwargs):
        self.calls.append(kwargs)
        yield {"type":"session_ready","session_id":kwargs["session_id"]}
        yield {"type":"delivery_ack", "message_ids":[*[m["id"] for m in kwargs.get("history", [])],
                                                       kwargs["message_id"]]}
        self.entered.set()
        if self.hold:
            while not kwargs["should_cancel"]():
                time.sleep(.01)
            yield {"type":"cancelled"}
            return
        actions=self.turns.pop(0) if self.turns else ["已完成检查。"]
        text=""
        for index, action in enumerate(actions):
            if isinstance(action,tuple):
                name,args=action
                call_id=f"call-{len(self.calls)}-{index}"
                yield {"type":"tool_call","call_id":call_id,"name":name,"arguments":args}
                result=kwargs["execute_tool"](name,args,call_id)
                yield {"type":"tool_result","call_id":call_id,"name":name,"result":result}
            elif isinstance(action,dict):
                yield action
                if action.get("type")=="error":
                    return
            else:
                text+=action
                yield {"type":"delta","text":action}
        yield {"type":"done","text":text}

    def close(self, _id):
        pass

    def close_all(self):
        pass


def install(monkeypatch, **kwargs):
    engine=FakeNative(**kwargs)
    monkeypatch.setattr(runs,"_runtime",engine)
    return engine


def finish(run):
    list(runs.iter_events(run["id"]))
    return runs.get_run(run["id"])


def test_native_rounds_resume_and_import_history_once(workspace,monkeypatch):
    _,s,_=workspace
    engine=install(monkeypatch)
    chat_service._append_message(s["id"],"user","我喜欢克制的叙述。")
    chat_service._append_message(s["id"],"assistant","已记住。")
    first=finish(runs.create_run(s["id"],text="讨论开头",client_request_id="first"))
    second=finish(runs.create_run(s["id"],text="继续讨论",client_request_id="second"))
    assert first["ok"] and second["ok"]
    assert engine.calls[0]["resume"] is False
    assert engine.calls[1]["resume"] is True
    assert engine.calls[0]["session_id"]==engine.calls[1]["session_id"]
    assert any("我喜欢克制" in m["content"] for m in engine.calls[0]["history"])
    assert engine.calls[1]["history"] == []
    assert len(chat_service.list_messages(s["id"]))==6


def test_request_idempotency_and_busy_session(workspace,monkeypatch):
    _,s,_=workspace
    engine=install(monkeypatch,hold=True)
    first=runs.create_run(s["id"],text="讨论剧情",client_request_id="same")
    assert engine.entered.wait(5)
    same=runs.create_run(s["id"],text="讨论剧情",client_request_id="same")
    assert same["id"]==first["id"]
    with pytest.raises(runs.RunConflictError):
        runs.create_run(s["id"],text="另一个问题",client_request_id="same")
    with pytest.raises(runs.RunConflictError):
        runs.create_run(s["id"],text="另一个问题")
    with pytest.raises(InvalidOperationError):
        chat_service.delete_session(s["id"])
    runs.cancel(first["id"])
    assert finish(first)["status"]=="cancelled"
    assert len(engine.calls)==1
    assert len(chat_service.list_messages(s["id"]))==2


def test_events_resume_without_duplicate_delta(workspace,monkeypatch):
    _,s,_=workspace
    install(monkeypatch,turns=[["第一段。","第二段。"]])
    result=finish(runs.create_run(s["id"],text="讨论剧情"))
    events=runs.events_after(result["id"])
    assert [e["seq"] for e in events]==list(range(1,len(events)+1))
    delta=next(e for e in events if e["event"]=="delta")
    remaining=list(runs.iter_events(result["id"],delta["seq"]))
    assert all(e["seq"]>delta["seq"] for e in remaining)
    assert remaining[-1]["event"]=="done"
    assert result["text"]=="第一段。第二段。"


def test_tools_write_revert_and_persist_cards(workspace,monkeypatch):
    p,s,_=workspace
    install(monkeypatch,turns=[[
        ("create_file",{"rel_path":"备忘录/线索.md","content":"茶杯缺了一角。"}),
        "已创建线索。"]])
    result=finish(runs.create_run(s["id"],text="新建一个线索备忘录"))
    assert result["ok"],result
    assert result["changes"][0]["status"]=="applied"
    assert changes.file_state(p["id"],"备忘录/线索.md")["exists"]
    restored=runs.revert(result["id"])
    assert restored["changes"][0]["status"]=="reverted"
    assert not changes.file_state(p["id"],"备忘录/线索.md")["exists"]
    assert chat_service.list_messages(s["id"])[-1]["meta"]["changes"][0]["status"]=="reverted"


def test_review_has_no_mutation_tools_even_when_model_tries(workspace,monkeypatch):
    p,s,_=workspace
    engine=install(monkeypatch,turns=[[("create_file",{"rel_path":"备忘录/越权.md","content":"不可写"}),"审稿结束。"]])
    result=finish(runs.create_run(s["id"],text="帮我审稿，只提建议"))
    names={(x.get("function") or x)["name"] for x in engine.calls[0]["tool_specs"]}
    assert "create_file" not in names
    assert next(step for step in result["steps"] if step.get("tool")=="create_file")["status"]=="error"
    assert not changes.file_state(p["id"],"备忘录/越权.md")["exists"]


@pytest.mark.parametrize("mode,text,readonly",[("read","修改这一章",True),
    ("write","审稿",True),("write","审稿并修改",False),("write","改这一段",False),
    ("write","怎么修改这个开头",True),("write","写下一章",False)])
def test_intent_write_authority(mode,text,readonly):
    assert runs.is_read_only({"mode":mode},text) is readonly


def test_model_and_agent_selection_apply_on_first_round(workspace,monkeypatch):
    _,s,_=workspace
    engine=install(monkeypatch)
    chat_service.update_session(s["id"],{"provider_id":"my-provider","model_id":"my-model",
        "agent":"planner","agent_pinned":1})
    result=finish(runs.create_run(s["id"],text="审稿"))
    assert result["agent"]=="planner"
    assert engine.calls[0]["provider"]=="my-provider" and engine.calls[0]["model"]=="my-model"


def test_error_is_persistent_and_no_fallback(workspace,monkeypatch):
    _,s,_=workspace
    engine=install(monkeypatch,turns=[[{"type":"error","code":"DSH_PROCESS_EXITED","message":"连接失败"}]])
    result=finish(runs.create_run(s["id"],text="讨论一下"))
    assert result["status"]=="failed" and result["error_code"]=="DSH_PROCESS_EXITED"
    assert len(engine.calls)==1
    assert chat_service.list_messages(s["id"])[-1]["meta"]["error_message"]=="连接失败"


def test_bodyless_403_uses_provider_diagnostic_in_persisted_run(workspace, monkeypatch):
    from workbench.backend.services import provider_service

    _, session, _ = workspace
    chat_service.update_session(session["id"], {"provider_id": "Fluxl", "model_id": "glm"})
    install(monkeypatch, turns=[[{"type": "error", "code": "AUTH",
                                 "message": "403 status code (no body)"}]])
    observed = []

    def diagnose(provider_id, status):
        observed.append((provider_id, status))
        return "模型服务拒绝访问（HTTP 403）：API Key 所属分组已删除（GROUP_DELETED）"

    monkeypatch.setattr(provider_service, "explain_auth_denial", diagnose)
    result = finish(runs.create_run(session["id"], text="讨论一下"))
    assert result["status"] == "failed" and result["error_code"] == "AUTH"
    assert "API Key 所属分组已删除" in result["error_message"]
    assert observed == [("Fluxl", 403)]


def test_restart_marks_orphan_without_reexecuting(workspace,monkeypatch):
    _,s,_=workspace
    engine=install(monkeypatch)
    run=runs.create_run(s["id"],text="修改人物",start=False)
    runs.recover_interrupted()
    result=runs.get_run(run["id"])
    assert result["status"]=="interrupted" and not engine.calls
    assert runs.active_run(s["id"]) is None
    assert list(runs.iter_events(run["id"]))[-1]["status"]=="interrupted"


def test_context_hash_selection_and_skill_injection(workspace,monkeypatch):
    p,s,root=workspace
    skill=root/"skills/novel-review/SKILL.md"
    skill.parent.mkdir()
    skill.write_text("---\nname: novel-review\ndescription: 审稿\n---\n禁止在审稿报告里改写正文",encoding="utf-8")
    _,book=project_service.get_project_dir(p["id"])
    (book/"备忘录/线索.md").write_text("茶杯缺角。",encoding="utf-8")
    state=changes.file_state(p["id"],"备忘录/线索.md")
    engine=install(monkeypatch)
    context={"active_file":state["path"],"base_hash":state["hash"],"selection":{"text":"缺角"}}
    assert finish(runs.create_run(s["id"],text="审稿",context=context))["ok"]
    assert "禁止在审稿报告里改写正文" in engine.calls[0]["system"]
    assert "作者选中的文字" in engine.calls[0]["context"]
    context["base_hash"]="stale"
    assert finish(runs.create_run(s["id"],text="修改这段",context=context))["status"]=="failed"
    assert len(engine.calls)==1


def test_target_chapter_is_separate_from_viewed_reference(workspace, monkeypatch):
    project, session, _ = workspace
    chapter = chapter_service.create_chapter(project["id"], "起点")
    _, book = project_service.get_project_dir(project["id"])
    (book / "设定/世界设定.md").write_text("# 世界设定\n只供参考。\n", encoding="utf-8")
    reference = changes.file_state(project["id"], "设定/世界设定.md")
    engine = install(monkeypatch, turns=[[("read_file", {}), "已读取目标。"]])
    context = {"active_file": reference["path"], "base_hash": reference["hash"],
               "target_chapter": chapter["rel_path"]}
    result = finish(runs.create_run(session["id"], text="继续写当前章节", context=context))
    assert result["ok"]
    assert result["request"]["context"]["target_chapter"] == chapter["rel_path"]
    assert f"当前参考文件：{reference['path']}" in engine.calls[0]["context"]
    assert f"本轮正文目标章节：{chapter['rel_path']}" in engine.calls[0]["context"]
    read_step = next(step for step in result["steps"] if step.get("tool") == "read_file")
    assert chapter["rel_path"] in read_step["result"]
    assert reference["path"] not in read_step["result"]


def test_target_chapter_rejects_nonchapter_or_missing_file(workspace):
    project, session, _ = workspace
    for target in ("设定/世界设定.md", "章节/第9999章.md", "../其他书/章节/第0001章.md"):
        with pytest.raises(InvalidOperationError):
            runs.create_run(session["id"], text="写正文", context={"target_chapter": target})
    assert chat_service.list_messages(session["id"]) == []


def test_explicit_continuation_retains_original_task_and_applied_changes(workspace, monkeypatch):
    project, session, _ = workspace
    engine = install(monkeypatch, turns=[[
        ("create_file", {"rel_path": "备忘录/已完成.md", "content": "已完成的记录。"}),
        {"type": "error", "code": "TIMEOUT", "message": "模型无进展"}],
        ["接着处理剩余事项。"], ["再次续接。"]])
    first = finish(runs.create_run(session["id"], text="完成本书设定与后续章节", client_request_id="origin"))
    assert first["status"] == "failed" and first["changes"][0]["status"] == "applied"
    second = finish(runs.create_run(session["id"], text="", continue_from_run_id=first["id"],
                                    client_request_id="continue-1"))
    assert second["ok"] and second["request"]["text"] == "继续完成上一轮未完成的任务。"
    assert second["request"]["continue_from_run_id"] == first["id"]
    assert engine.calls[1]["user_text"] == "继续完成上一轮未完成的任务。"
    assert "完成本书设定与后续章节" in engine.calls[1]["context"]
    assert "备忘录/已完成.md" in engine.calls[1]["context"]
    assert len(second["changes"]) == 0
    assert changes.file_state(project["id"], "备忘录/已完成.md")["content"] == "已完成的记录。"
    third = finish(runs.create_run(session["id"], text="", continue_from_run_id=second["id"]))
    assert third["ok"] and "完成本书设定与后续章节" in engine.calls[2]["context"]
    assert "备忘录/已完成.md" in engine.calls[2]["context"]
    same = runs.create_run(session["id"], text="", continue_from_run_id=first["id"],
                           client_request_id="continue-1")
    assert same["id"] == second["id"]


def test_continuation_requires_terminal_run_in_same_session(workspace, monkeypatch):
    project, session, _ = workspace
    other = chat_service.create_session(project_id=project["id"])
    install(monkeypatch)
    queued = runs.create_run(session["id"], text="原任务", start=False)
    with pytest.raises(InvalidOperationError):
        runs.create_run(session["id"], text="", continue_from_run_id=queued["id"])
    runs.cancel(queued["id"])
    with pytest.raises(InvalidOperationError):
        runs.create_run(other["id"], text="", continue_from_run_id=queued["id"])
    with pytest.raises(InvalidOperationError):
        runs.create_run(session["id"], text="")


def test_run_api_and_default_write(workspace,monkeypatch):
    from fastapi.testclient import TestClient
    from workbench.backend.app import create_app
    p,_,_=workspace
    install(monkeypatch)
    with TestClient(create_app()) as client:
        s=client.post("/api/chat/sessions",json={"project_id":p["id"],"model_id":"chosen","agent_pinned":True}).json()
        assert s["auto_apply"]==1 and s["mode"]=="write" and s["agent_pinned"]==1
        r=client.post(f"/api/chat/sessions/{s['id']}/runs",json={"text":"讨论剧情","client_request_id":"ui"})
        assert r.status_code==201,r.text
        run=r.json()
        response=client.get(f"/api/chat/runs/{run['id']}/events")
        assert response.status_code==200 and '"event": "done"' in response.text
        finished=client.get(f"/api/chat/runs/{run['id']}").json()
        assert finished["status"]=="completed"
        empty=client.get(f"/api/chat/runs/{run['id']}/events?after={finished['last_seq']}")
        assert empty.text==""
        detail=client.get(f"/api/chat/sessions/{s['id']}").json()
        assert detail["active_run"] is None and len(detail["messages"])==2


def wait_interaction(run):
    end = time.monotonic() + 8
    while time.monotonic() < end:
        result = runs.get_run(run["id"])
        pending = [item for item in result["interactions"] if item["status"] == "pending"]
        if pending:
            assert result["status"] == "waiting_input"
            return pending[0]
        assert result["status"] not in runs.TERMINAL, result
        time.sleep(.02)
    pytest.fail("Expected a pending interaction")


def test_followup_keeps_write_authority_and_invalid_auto_attachment_is_warning(workspace, monkeypatch):
    project, session, _ = workspace
    engine = install(monkeypatch, turns=[
        ["请确认主角姓名。"],
        ["已收到李长歌。"],
        [("create_file", {"rel_path": "备忘录/确认.md", "content": "主角李长歌。"}), "已保存。"],
    ])
    finish(runs.create_run(session["id"], text="完善世界设定"))
    answer = finish(runs.create_run(session["id"], text="主角名字用哪个：李长歌。",
                                   context={"active_file": "章节/第一章：test.txt"}))
    assert answer["ok"] and len(answer["context_warnings"]) == 1
    assert engine.calls[1]["user_text"] == "主角名字用哪个：李长歌。"
    continued = finish(runs.create_run(session["id"], text="请接着完成上一轮未完成的要求"))
    assert continued["ok"]
    assert changes.file_state(project["id"], "备忘录/确认.md")["content"] == "主角李长歌。"
    assert {"create_file", "delete_file", "move_file"} <= {s["name"] for s in engine.calls[2]["tool_specs"]}


def test_preflight_failure_is_repaired_as_history_on_next_native_turn(workspace, monkeypatch):
    _, session, _ = workspace
    engine = install(monkeypatch)
    finish(runs.create_run(session["id"], text="开始讨论"))
    failed = finish(runs.create_run(session["id"], text="主角李长歌，代币来自击杀现实侧妖魔。",
                                   context={"files": ["错误附件.txt"]}))
    assert failed["status"] == "failed"
    assert len(engine.calls) == 1
    with db.get_conn() as conn:
        assert conn.execute("SELECT dsh_delivered FROM chat_messages WHERE id=?",
                            (failed["user_message_id"],)).fetchone()[0] == 0
    finish(runs.create_run(session["id"], text="继续"))
    assert [(h["role"], h["content"]) for h in engine.calls[1]["history"]] == [
        ("user", "主角李长歌，代币来自击杀现实侧妖魔。")]
    finish(runs.create_run(session["id"], text="再继续"))
    assert engine.calls[2]["history"] == []


def test_legacy_undelivered_author_fact_is_added_to_book_memory(workspace, monkeypatch):
    from workbench.backend.services import chat_memory_service as memory
    project, session, _ = workspace
    chat_service._append_message(session["id"], "user", "主角名字是李长歌。代币来源是现实侧击杀妖魔获取。")
    # Even a different conversation in the same book imports the old confirmed
    # author facts without delivering/reexecuting that conversation's task.
    session = chat_service.create_session(project_id=project["id"])
    engine = install(monkeypatch)
    result = finish(runs.create_run(session["id"], text="继续"))
    assert result["ok"]
    assert "李长歌" in engine.calls[0]["context"]
    assert len(memory.list_entries(project["id"])) == 2
    assert any("会话" in item["source"] for item in result["context_preview"]["items"])


@pytest.mark.parametrize("damaged", ['broken', '{"version":1,"entries":[{"id":"x","content":"主角姓名：李长歌","status":"confirmed","sources":[null],"history":[]}]}'])
def test_damaged_optional_memory_does_not_drop_author_message(workspace, monkeypatch, damaged):
    project, session, _ = workspace
    _, root = project_service.get_project_dir(project["id"])
    (root/".meta").mkdir(exist_ok=True)
    (root/".meta/chat-memory.json").write_text(damaged, encoding="utf-8")
    engine = install(monkeypatch)
    result = finish(runs.create_run(session["id"], text="主角名字是李长歌"))
    assert result["ok"] and result["context_warnings"]
    assert engine.calls[0]["user_text"] == "主角名字是李长歌"


def test_approval_wait_continue_and_next_round_permission_snapshot(workspace, monkeypatch):
    from workbench.backend.services import chat_interaction_service as interactions
    project, session, _ = workspace
    chat_service.update_session(session["id"], {"permission_mode": "ask"})
    install(monkeypatch, turns=[[("create_file", {"rel_path": "备忘录/批准.md", "content": "确认后的正文"}),
                                "保存完成，可以继续创作。"]])
    run = runs.create_run(session["id"], text="继续")
    card = wait_interaction(run)
    assert card["kind"] == "approval"
    assert not changes.file_state(project["id"], "备忘录/批准.md")["exists"]
    with pytest.raises(runs.RunConflictError):
        runs.create_run(session["id"], text="不应并发")
    chat_service.update_session(session["id"], {"permission_mode": "full"})
    assert interactions.list_interactions(run["id"])[0]["status"] == "pending"
    interactions.respond(run["id"], card["id"], {"decision": "approve"})
    result = finish(run)
    assert result["ok"] and result["permission_mode"] == "ask"
    assert changes.file_state(project["id"], "备忘录/批准.md")["content"] == "确认后的正文"
    assert result["interactions"][0]["status"] == "answered"
    assert chat_service.list_messages(session["id"])[-1]["meta"]["interactions"][0]["id"] == card["id"]


def test_question_other_answer_continues_same_run_and_can_be_reloaded(workspace, monkeypatch):
    from workbench.backend.services import chat_interaction_service as interactions
    _, session, _ = workspace
    install(monkeypatch, turns=[[("ask_user_question", {"questions": [{"id": "hero", "question": "主角姓名？",
                       "options": [{"label": "林玄"}, {"label": "李长歌"}]}]}), "已经收到你的选择。"]])
    run = runs.create_run(session["id"], text="先确认主角再继续")
    card = wait_interaction(run)
    assert runs.active_run(session["id"])["interactions"][0]["id"] == card["id"]
    response = {"answers": [{"id": "hero", "selected": [], "custom": "李长歌"}]}
    interactions.respond(run["id"], card["id"], response)
    result = finish(run)
    assert result["ok"] and result["text"] == "已经收到你的选择。"
    assert result["interactions"][0]["response"] == response
    assert len([m for m in chat_service.list_messages(session["id"]) if m["meta"].get("interaction_id")]) == 1


def test_cancel_while_waiting_never_applies_change(workspace, monkeypatch):
    project, session, _ = workspace
    chat_service.update_session(session["id"], {"permission_mode": "ask"})
    install(monkeypatch, turns=[[("create_file", {"rel_path": "备忘录/取消.md", "content": "不应出现"})]])
    run = runs.create_run(session["id"], text="创建备忘录")
    wait_interaction(run)
    runs.cancel(run["id"])
    result = finish(run)
    assert result["status"] == "cancelled"
    assert result["interactions"][0]["status"] == "cancelled"
    assert not changes.file_state(project["id"], "备忘录/取消.md")["exists"]


def test_disk_run_index_restores_completed_and_orphan_without_execution(workspace, monkeypatch):
    import uuid
    from workbench.backend.services import chat_interaction_service as interactions
    project, session, _ = workspace
    engine = install(monkeypatch)
    completed = finish(runs.create_run(session["id"], text="开始讨论"))
    orphan = runs.create_run(session["id"], text="继续完善", start=False)
    card_id = str(uuid.uuid4())
    interactions.ensure_schema()
    interactions._save({"id": card_id, "run_id": orphan["id"], "tool_call_id": "ask-orphan",
                        "kind": "question", "payload": {"questions": []}, "payload_hash": "test",
                        "status": "pending", "response": None, "created_at": runs._now()})
    changes.apply_change(project["id"], run_id=orphan["id"], session_id=session["id"],
                         tool_call_id="saved-before-crash", operation="create",
                         rel_path="备忘录/重启前.md", content="已经完成的修改", expected_hash=None)
    with db.get_conn() as conn:
        conn.execute("DELETE FROM chat_run_events")
        conn.execute("DELETE FROM chat_runs")
        conn.execute("DELETE FROM chat_interactions")
    runs.recover_interrupted()
    restored = runs.get_run(completed["id"])
    assert restored["status"] == "completed" and restored["text"] == completed["text"]
    assert restored["last_seq"] == completed["last_seq"]
    interrupted = runs.get_run(orphan["id"])
    assert interrupted["status"] == "interrupted"
    assert interrupted["interactions"][0]["status"] == "interrupted"
    assert interrupted["changes"][0]["status"] == "applied"
    assert changes.file_state(project["id"], "备忘录/重启前.md")["content"] == "已经完成的修改"
    assert len(engine.calls) == 1
    runs.recover_interrupted()
    assert len([m for m in chat_service.list_messages(session["id"])
                if m["role"] == "assistant" and m["meta"]["run_id"] == orphan["id"]]) == 1


def test_question_response_and_memory_api_contracts(workspace, monkeypatch):
    from fastapi.testclient import TestClient
    from workbench.backend.app import create_app
    from workbench.backend.services import chat_memory_service as memory
    project, session, _ = workspace
    questions = {"questions": [
        {"id": "hero", "question": "主角姓名最终确定为？", "options": [{"label": "李长歌"}, {"label": "林玄"}]},
        {"id": "scope", "question": "本书保留哪些方向？", "multi_select": True,
         "options": [{"label": "修仙"}, {"label": "搜打撤"}]}]}
    install(monkeypatch, turns=[[("ask_user_question", questions), "收到选择。"], [("ask_user_question", questions)]])
    with TestClient(create_app()) as client:
        run = client.post(f"/api/chat/sessions/{session['id']}/runs", json={"text": "确定主角"}).json()
        card = wait_interaction(run)
        url = f"/api/chat/runs/{run['id']}/interactions/{card['id']}/respond"
        response = {"answers": [{"id": "hero", "selected": [], "custom": ""},
                                {"id": "scope", "selected": ["修仙", "搜打撤"]}]}
        assert client.post(url, json={"response": response}).status_code == 400
        response["answers"][0]["custom"] = "李长歌"
        assert client.post(url, json={"response": response}).status_code == 200
        assert client.post(url, json={"response": response}).status_code == 200
        assert finish(run)["text"] == "收到选择。"
        other = runs.create_run(session["id"], text="再确认")
        pending = wait_interaction(other)
        cross = client.post(f"/api/chat/runs/{other['id']}/interactions/{card['id']}/respond", json={"response": response})
        assert cross.status_code == 404
        runs.cancel(other["id"])
        finish(other)
        assert client.post(f"/api/chat/runs/{other['id']}/interactions/{pending['id']}/respond",
                           json={"response": response}).status_code == 409
        entries = client.get(f"/api/projects/{project['id']}/chat-memory").json()["entries"]
        hero = next(entry for entry in entries if "李长歌" in entry["content"])
        assert hero["source_session_id"] == session["id"] and hero["source_message_id"]
        memory_url = f"/api/projects/{project['id']}/chat-memory/{hero['id']}"
        assert client.patch(memory_url, json={"content": "主角姓名确定为顾远"}).status_code == 200
        assert "顾远" in memory.context_blocks(project["id"])[0]["text"]
        assert client.delete(memory_url).status_code == 200
        assert not any("顾远" in block["text"] for block in memory.context_blocks(project["id"]))
        monkeypatch.setattr(chat_service, "schedule_title", lambda sid, **kwargs: {"id": sid, "title_status": "pending"})
        assert client.post(f"/api/chat/sessions/{session['id']}/title/regenerate").json()["title_status"] == "pending"


def test_disk_run_recovery_uses_book_path_when_project_index_ids_change(workspace, monkeypatch):
    project, session, _ = workspace
    install(monkeypatch)
    completed = finish(runs.create_run(session["id"], text="讨论设定"))
    new_id = project["id"] + 100
    with db.get_conn() as conn:
        conn.execute("DELETE FROM chat_runs")
        conn.execute("DELETE FROM chat_run_events")
        conn.execute("DELETE FROM chat_messages")
        conn.execute("DELETE FROM chat_sessions")
        conn.execute("UPDATE projects SET id=? WHERE id=?", (new_id, project["id"]))
    runs.recover_interrupted()
    restored = runs.get_run(completed["id"])
    assert restored["status"] == "completed"
    assert restored["project_id"] == new_id
    assert restored["request"]["session"]["project_id"] == new_id
    assert restored["last_seq"] == completed["last_seq"]
