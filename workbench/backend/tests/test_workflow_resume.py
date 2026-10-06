"""Workflow recovery contracts with an isolated book and synthetic generation."""
from __future__ import annotations

import threading
import asyncio
from collections import Counter
from types import SimpleNamespace

import pytest

from workbench.backend import db
from workbench.backend.services import context_service, generation_service, pipeline_service, proposal_service
from workbench.backend.services import workflow_service as workflows
from workbench.backend.services.errors import InvalidOperationError
from workbench.backend.tests.test_chat_runs import workspace


CHAPTERS = ["章节/第0001章.txt", "章节/第0002章.txt", "章节/第0003章.txt"]


def pipeline_workflow(stop_on_failure=True):
    definition = workflows.default_definition()
    definition.update(name="合成恢复流程", is_builtin=False, batch={"stop_on_failure": stop_on_failure})
    return workflows.save_workflow(definition)


def custom_workflow(stop_on_failure=True):
    return workflows.save_workflow({"name": "合成双节点流程", "batch": {"stop_on_failure": stop_on_failure}, "nodes": [
        {"id": "first", "type": "大纲生成", "label": "先构思", "next": "second", "instruction": "保存构思"},
        {"id": "second", "type": "审稿", "label": "再检查", "next": "", "instruction": "依据构思检查"},
    ]})


def minimal_context(monkeypatch):
    monkeypatch.setattr(context_service, "assemble", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(context_service, "to_messages", lambda _assembly: ("", [{"role": "user", "content": "合成章节材料"}]))


def test_failed_chapter_is_retried_without_repeating_completed_chapters(workspace, monkeypatch):
    project, _, _ = workspace
    definition = pipeline_workflow()
    calls = []
    def pipeline(_project, chapter, **_kwargs):
        calls.append(chapter)
        return {"status": "failed" if chapter == CHAPTERS[1] and calls.count(chapter) == 1 else "done", "steps": {}}
    monkeypatch.setattr(pipeline_service, "run_pipeline", pipeline)
    run = workflows.start_run(definition["name"], project["id"], chapters=CHAPTERS)
    first = workflows.execute_run(run["id"], use_ai=False)
    assert first["status"] == "failed"
    assert first["progress"]["completed"] == CHAPTERS[:1]
    assert first["progress"]["chapter_index"] == 1
    second = workflows.execute_run(run["id"], use_ai=False)
    assert second["status"] == "done" and second["progress"]["completed"] == CHAPTERS
    assert calls == [CHAPTERS[0], CHAPTERS[1], CHAPTERS[1], CHAPTERS[2]]


def test_paused_chapter_keeps_its_cursor_and_is_not_reported_failed(workspace, monkeypatch):
    project, _, _ = workspace
    definition = pipeline_workflow()
    calls = []
    def pipeline(_project, chapter, **_kwargs):
        calls.append(chapter)
        return {"status": "paused" if chapter == CHAPTERS[1] and calls.count(chapter) == 1 else "done", "steps": {}}
    monkeypatch.setattr(pipeline_service, "run_pipeline", pipeline)
    run = workflows.start_run(definition["name"], project["id"], chapters=CHAPTERS)
    first = workflows.execute_run(run["id"], use_ai=False)
    assert first["status"] == "paused"
    assert first["progress"]["chapter_index"] == 1
    resumed = workflows.execute_run(run["id"], use_ai=False)
    assert resumed["status"] == "done"
    assert calls.count(CHAPTERS[0]) == 1 and calls.count(CHAPTERS[1]) == 2


def test_interrupted_run_resumes_from_unfinished_chapter(workspace, monkeypatch):
    project, _, _ = workspace
    definition = pipeline_workflow()
    run = workflows.start_run(definition["name"], project["id"], chapters=CHAPTERS)
    # An older interrupted record can have advanced its cursor past failures.
    payload = {**run["payload"], "completed": CHAPTERS[:1], "chapter_index": 3}
    workflows._update_run(run["id"], status="interrupted", payload=payload)
    calls = []
    def pipeline(_project, chapter, **_kwargs):
        calls.append(chapter)
        return {"status": "done", "steps": {}}
    monkeypatch.setattr(pipeline_service, "run_pipeline", pipeline)
    result = workflows.execute_run(run["id"], use_ai=False)
    assert result["status"] == "done" and calls == CHAPTERS[1:]


@pytest.mark.parametrize("stop", ["failed", "paused", "exception"])
def test_custom_node_checkpoint_reuses_persisted_first_proposal(workspace, monkeypatch, stop):
    project, _, _ = workspace
    definition = custom_workflow()
    minimal_context(monkeypatch)
    calls = []
    cancelled = threading.Event()
    def generate(**kwargs):
        node = kwargs["context_snapshot"]["node"]
        calls.append(node)
        assert callable(kwargs.get("should_cancel"))
        if node == "second" and calls.count(node) == 1:
            if stop == "exception":
                raise OSError("合成节点调用故障")
            if stop == "paused":
                cancelled.set()
            return {"ok": False, "error_code": "CANCELLED" if stop == "paused" else "MOCK_FAILURE", "error_message": "合成暂停或失败"}
        return {"ok": True, "text": "合成节点产物：" + node, "engine": "fake"}
    monkeypatch.setattr(generation_service, "run_task", generate)
    run = workflows.start_run(definition["name"], project["id"], chapters=CHAPTERS[:1])
    first = workflows.execute_run(run["id"], should_cancel=cancelled.is_set)
    assert first["status"] == ("failed" if stop == "exception" else stop)
    original = proposal_service.list_proposals(project["id"], status=None)
    assert len(original) == 1 and original[0]["meta"]["node"] == "first"
    cancelled.clear()
    resumed = workflows.execute_run(run["id"], should_cancel=cancelled.is_set)
    assert resumed["status"] == "done"
    assert calls == ["first", "second", "second"]
    proposals = proposal_service.list_proposals(project["id"], status=None)
    assert len(proposals) == 2 and original[0]["id"] in {item["id"] for item in proposals}


@pytest.mark.parametrize("damage", ["deleted", "edited"])
def test_modified_or_missing_proposal_invalidates_completed_node(workspace, monkeypatch, damage):
    project, _, _ = workspace
    definition = custom_workflow()
    minimal_context(monkeypatch)
    calls = []
    def generate(**kwargs):
        node = kwargs["context_snapshot"]["node"]
        calls.append(node)
        if node == "second" and calls.count(node) == 1:
            return {"ok": False, "error_message": "合成断点"}
        return {"ok": True, "text": f"{node} 的生成版本 {calls.count(node)}", "engine": "fake"}
    monkeypatch.setattr(generation_service, "run_task", generate)
    run = workflows.start_run(definition["name"], project["id"], chapters=CHAPTERS[:1])
    assert workflows.execute_run(run["id"])["status"] == "failed"
    first_proposal = proposal_service.list_proposals(project["id"], status=None)[0]
    if damage == "deleted":
        with db.get_conn() as conn:
            conn.execute("DELETE FROM proposals WHERE id=?", (first_proposal["id"],))
    else:
        proposal_service.update_proposal_content(first_proposal["id"], "作者手动精修版本，不能当原生成版本")
    resumed = workflows.execute_run(run["id"])
    assert resumed["status"] == "failed"
    assert calls == ["first", "second"]
    assert "新建运行" in resumed["log"][-1]["error"]
    assert not resumed["progress"]["completed"]
    if damage == "edited":
        assert proposal_service.get_proposal(first_proposal["id"])["content"] == "作者手动精修版本，不能当原生成版本"


def test_continue_on_failure_does_not_report_batch_success_and_retry_keeps_successes(workspace, monkeypatch):
    project, _, _ = workspace
    definition = pipeline_workflow(stop_on_failure=False)
    calls = []
    def pipeline(_project, chapter, **_kwargs):
        calls.append(chapter)
        return {"status": "failed" if chapter == CHAPTERS[0] and calls.count(chapter) == 1 else "done", "steps": {}}
    monkeypatch.setattr(pipeline_service, "run_pipeline", pipeline)
    run = workflows.start_run(definition["name"], project["id"], chapters=CHAPTERS)
    failed = workflows.execute_run(run["id"], use_ai=False)
    assert failed["status"] == "failed"
    assert set(failed["progress"]["completed"]) == set(CHAPTERS[1:])
    retried = workflows.execute_run(run["id"], use_ai=False)
    assert retried["status"] == "done"
    assert Counter(calls) == Counter({CHAPTERS[0]: 2, CHAPTERS[1]: 1, CHAPTERS[2]: 1})


def test_running_workflow_cannot_execute_twice_concurrently(workspace, monkeypatch):
    project, _, _ = workspace
    definition = pipeline_workflow()
    entered, release = threading.Event(), threading.Event()
    calls, result = [], []
    def pipeline(_project, chapter, **_kwargs):
        calls.append(chapter)
        entered.set()
        assert release.wait(2)
        return {"status": "done", "steps": {}}
    monkeypatch.setattr(pipeline_service, "run_pipeline", pipeline)
    run = workflows.start_run(definition["name"], project["id"], chapters=CHAPTERS[:1])
    thread = threading.Thread(target=lambda: result.append(workflows.execute_run(run["id"], use_ai=False)))
    thread.start()
    try:
        assert entered.wait(2)
        with pytest.raises(InvalidOperationError, match="执行|运行|忙|处理中"):
            workflows.execute_run(run["id"], use_ai=False)
    finally:
        release.set()
        thread.join(2)
    assert not thread.is_alive() and result[0]["status"] == "done" and calls == CHAPTERS[:1]


def test_run_keeps_definition_frozen_across_workflow_edits(workspace, monkeypatch):
    project, _, _ = workspace
    definition = custom_workflow()
    minimal_context(monkeypatch)
    run = workflows.start_run(definition["name"], project["id"], chapters=CHAPTERS[:1])
    edited = {**definition, "nodes": [{**definition["nodes"][1], "next": "", "label": "改名后的单步"}]}
    workflows.save_workflow(edited)
    calls = []
    def generate(**kwargs):
        calls.append(kwargs["context_snapshot"]["node"])
        return {"ok": True, "text": "冻结定义的合成产物", "engine": "fake"}
    monkeypatch.setattr(generation_service, "run_task", generate)
    assert workflows.execute_run(run["id"])["status"] == "done"
    assert calls == ["first", "second"]


def test_restart_marks_stale_running_interrupted_without_replaying_models(workspace, monkeypatch):
    project, _, _ = workspace
    definition = pipeline_workflow()
    records = {}
    for status in ("running", "pending", "paused", "failed", "done"):
        run = workflows.start_run(definition["name"], project["id"], chapters=CHAPTERS)
        payload = {**run["payload"], "completed": CHAPTERS[:1], "chapter_index": 1,
                   "node_checkpoints": {CHAPTERS[1]: {"first": {"ok": False, "error": "合成断点"}}}}
        records[status] = workflows._update_run(run["id"], status=status, payload=payload)
    def must_not_generate(*_args, **_kwargs):
        raise AssertionError("recovery must not replay generation or tools")
    monkeypatch.setattr(pipeline_service, "run_pipeline", must_not_generate)
    monkeypatch.setattr(generation_service, "run_task", must_not_generate)
    workflows.recover_interrupted()
    for status, original in records.items():
        recovered = workflows.get_run(original["id"])
        assert recovered["status"] == ("interrupted" if status == "running" else status)
        assert recovered["payload"] == original["payload"]
    calls = []
    def pipeline(_project, chapter, **_kwargs):
        calls.append(chapter)
        return {"status": "done", "steps": {}}
    monkeypatch.setattr(pipeline_service, "run_pipeline", pipeline)
    resumed = workflows.execute_run(records["running"]["id"], use_ai=False)
    assert resumed["status"] == "done" and calls == CHAPTERS[1:]


def test_app_startup_actually_recovers_workflow_running_rows(workspace, monkeypatch):
    from workbench.backend import app as application
    from workbench.backend.services import provider_service, chat_run_service, knowledge_service, knowledge_candidate_service, embedding_service, asset_card_service
    project, _, _ = workspace
    definition = pipeline_workflow()
    run = workflows.start_run(definition["name"], project["id"], chapters=CHAPTERS[:1])
    workflows._update_run(run["id"], status="running")
    monkeypatch.setattr(provider_service, "project_to_dsh_home", lambda: None)
    monkeypatch.setattr(application, "run_startup_selfcheck", lambda: {})
    monkeypatch.setattr(chat_run_service, "recover_interrupted", lambda: None)
    monkeypatch.setattr(chat_run_service, "shutdown", lambda: None)
    for service in (knowledge_service, knowledge_candidate_service, embedding_service, asset_card_service):
        monkeypatch.setattr(service, "recover", lambda: None)
        monkeypatch.setattr(service, "shutdown", lambda: None)
    async def startup():
        async with application._lifespan(SimpleNamespace(state=SimpleNamespace())):
            assert workflows.get_run(run["id"])["status"] == "interrupted"
    asyncio.run(startup())


def test_node_system_instruction_skill_and_prior_proposal_survive_continuation(workspace, monkeypatch):
    from workbench.backend.services import skill_service
    project, _, _ = workspace
    definition = custom_workflow()
    definition["nodes"][0].update(skill="novel-planning", instruction="第一步：明确失踪船只的谜团")
    definition["nodes"][1].update(skill="novel-review", instruction="第二步：检查前序谜团的连续性")
    definition = workflows.save_workflow(definition)
    monkeypatch.setattr(context_service, "assemble", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(context_service, "to_messages", lambda _assembly: ("本书上下文系统规则", [{"role": "user", "content": "合成材料"}]))
    compiled, requests = [], []
    def compile_skills(names):
        compiled.extend(names)
        return {"text": "完整技能正文：" + names[0], "unavailable": [], "skills": []}
    monkeypatch.setattr(skill_service, "compile_skills", compile_skills)
    def generate(**kwargs):
        requests.append(kwargs)
        node = kwargs["context_snapshot"]["node"]
        if node == "second" and sum(r["context_snapshot"]["node"] == "second" for r in requests) == 1:
            return {"ok": False, "error_message": "合成服务故障"}
        return {"ok": True, "text": "失踪船只谜团的真实前序提案" if node == "first" else "连续性验收结论", "engine": "fake"}
    monkeypatch.setattr(generation_service, "run_task", generate)
    run = workflows.start_run(definition["name"], project["id"], chapters=CHAPTERS[:1])
    assert workflows.execute_run(run["id"])["status"] == "failed"
    assert workflows.execute_run(run["id"])["status"] == "done"
    assert [r["context_snapshot"]["node"] for r in requests] == ["first", "second", "second"]
    assert compiled == ["novel-planning", "novel-review", "novel-review"]
    assert requests[0]["system"] == "本书上下文系统规则\n\n完整技能正文：novel-planning"
    assert "第一步：明确失踪船只的谜团" in requests[0]["messages"][-1]["content"]
    assert "第二步：检查前序谜团的连续性" in requests[1]["messages"][-1]["content"]
    assert any("第0001章 · 先构思" in message["content"] and "失踪船只谜团的真实前序提案" in message["content"]
               for message in requests[1]["messages"])
    assert requests[1]["system"] == requests[2]["system"]
    assert requests[1]["messages"] == requests[2]["messages"]


def test_missing_selected_node_skill_fails_before_any_model_call(workspace, monkeypatch):
    from workbench.backend.services import skill_service
    project, _, _ = workspace
    definition = custom_workflow()
    definition["nodes"][0]["skill"] = "missing-required-skill"
    definition = workflows.save_workflow(definition)
    minimal_context(monkeypatch)
    monkeypatch.setattr(skill_service, "compile_skills", lambda names: {
        "text": "", "unavailable": [{"name": names[0], "error": "指定技能源不可读取"}], "skills": []})
    calls = []
    monkeypatch.setattr(generation_service, "run_task", lambda **kwargs: calls.append(kwargs))
    run = workflows.start_run(definition["name"], project["id"], chapters=CHAPTERS[:1])
    failed = workflows.execute_run(run["id"])
    assert failed["status"] == "failed"
    assert "技能不可用" in failed["log"][-1]["error"]
    assert not calls and not proposal_service.list_proposals(project["id"], status=None)
