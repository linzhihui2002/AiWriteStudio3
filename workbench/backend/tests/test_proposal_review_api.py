"""收件箱候选复审接口：审查当前提案，作者明确应用后才写正文。"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from workbench.backend import config, db
from workbench.backend.api import writing
from workbench.backend.services import (
    agent_service,
    chapter_service,
    contract_service,
    file_change_service,
    generation_service,
    knowledge_service,
    project_service,
    prompt_registry_service,
    proposal_service,
    writing_preference_service,
)
from workbench.backend.services.errors import ServiceError


BODY = (
    "陆衡把六枚铜钱推到桌边。\n"
    "“照旧规矩，先付船资。”\n"
    "老船工抽走纸上的欠条，把夜里答应留下的木箱递给他。\n"
    "“钥匙在底下，别丢了。”"
)
OLD_BODY = "“先等天亮。”陆衡把铜钱放回袋里。"
PLOT = "陆衡支付船资"
CONNECT = "船工兑现前章留下木箱的承诺"


@pytest.fixture()
def inbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "books")
    monkeypatch.setattr(config, "RUNTIME_DIR", tmp_path / ".workbench")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / ".workbench/test.db")
    db.init_db()
    config.ensure_runtime_dirs()
    prompt_registry_service.ensure_registry()
    agent_service.ensure_builtin_agents()
    monkeypatch.setattr(knowledge_service, "enqueue", lambda *args, **kwargs: None)
    monkeypatch.setattr(knowledge_service, "search", lambda *args, **kwargs: {
        "hits": [], "degradation": []})
    template = tmp_path / "template"
    for directory in ("章节", "设定", "大纲", "状态"):
        (template / directory).mkdir(parents=True)
    project = project_service.create_project("候选复审接口测试", template=template)
    project_id = project["id"]
    _, root = project_service.get_project_dir(project_id)
    chapter = chapter_service.create_chapter(project_id, "木箱交接")
    rel = chapter["rel_path"]
    path = root / rel
    path.write_text(OLD_BODY, encoding="utf-8")
    writing_preference_service.update_writing_prefs(
        project_id, chapter_min_words=1, chapter_max_words=1000)
    (root / "大纲/大纲.md").write_text("陆衡付钱取回木箱。", encoding="utf-8")
    contract_service.freeze_outline(project_id)
    contract_service.generate_contract(project_id, rel, use_ai=False)
    contract_service.update_contract(project_id, rel, {
        "plot_points": [PLOT], "must_connect": [CONNECT], "constraints": []})
    contract_service.freeze_contract(project_id, rel)
    candidate = proposal_service.create_proposal(
        project_id=project_id, kind="chapter_draft", title="章节候选",
        target_path=rel, content=BODY, meta={"pipeline_candidate": {"protocol": 2}})
    calls = []

    def review_model(**kwargs):
        assert kwargs["task_type"] == "审稿", "测试不得调用非审稿任务或真实模型"
        calls.append(kwargs)
        body = kwargs["messages"][-1]["content"].split("【待审正文】\n", 1)[1]
        payload = {"逐项": [
            {"项": PLOT, "判定": "已完成", "证据": body.splitlines()[0]},
            {"项": CONNECT, "判定": "已完成", "证据": "把夜里答应留下的木箱递给他。"},
        ], "一致性": [], "结论": "通过", "修改指令": []}
        return {"ok": True, "text": json.dumps(payload, ensure_ascii=False)}

    monkeypatch.setattr(generation_service, "run_task", review_model)
    app = FastAPI()
    app.include_router(writing.router)

    @app.exception_handler(ServiceError)
    async def service_error(_request, error):
        return JSONResponse({"detail": error.message}, status_code=error.status_code)

    return SimpleNamespace(id=project_id, rel=rel, path=path, proposal=candidate,
                           calls=calls, client=TestClient(app))


def test_review_endpoint_inspects_exact_current_candidate_without_applying(inbox):
    # An omitted use_ai defaults to AI review; the API needs no chapter argument.
    response = inbox.client.post(f"/api/proposals/{inbox.proposal['id']}/review", json={})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["verdict"] == "通过" and result["ai_used"] is True
    assert result["rel_path"] == inbox.rel
    assert result["candidate_hash"] == file_change_service.content_hash(BODY)
    assert result["hard_gates"]["passed"] is True
    assert result["counts"] == {"已完成": 2, "未完成": 0, "待核实": 0}
    assert len(inbox.calls) == 1
    assert inbox.calls[0]["messages"][-1]["content"].endswith("【待审正文】\n" + BODY)
    assert inbox.path.read_text(encoding="utf-8") == OLD_BODY
    detail = inbox.client.get(f"/api/proposals/{inbox.proposal['id']}").json()
    assert detail["status"] == "pending"
    assert detail["meta"]["pipeline_review"]["candidate_hash"] == result["candidate_hash"]


def test_edited_candidate_is_rereviewed_then_explicitly_applied_via_api(inbox):
    proposal_id = inbox.proposal["id"]
    review_url = f"/api/proposals/{proposal_id}/review"
    assert inbox.client.post(review_url, json={"use_ai": True}).status_code == 200
    edited = BODY.replace("六枚", "七枚")
    saved = inbox.client.put(f"/api/proposals/{proposal_id}", json={"content": edited})
    assert saved.status_code == 200 and saved.json()["content"] == edited
    stale = inbox.client.post(f"/api/proposals/{proposal_id}/apply", json={})
    assert stale.status_code == 400 and "重新审稿" in stale.json()["detail"]
    assert inbox.path.read_text(encoding="utf-8") == OLD_BODY
    rereviewed = inbox.client.post(review_url, json={"use_ai": True})
    assert rereviewed.status_code == 200, rereviewed.text
    assert rereviewed.json()["verdict"] == "通过"
    assert rereviewed.json()["candidate_hash"] == file_change_service.content_hash(edited)
    assert inbox.calls[-1]["messages"][-1]["content"].endswith("【待审正文】\n" + edited)
    assert inbox.path.read_text(encoding="utf-8") == OLD_BODY
    applied = inbox.client.post(f"/api/proposals/{proposal_id}/apply", json={})
    assert applied.status_code == 200, applied.text
    assert applied.json()["proposal"]["status"] == "applied"
    assert inbox.path.read_text(encoding="utf-8") == edited


def test_invalid_and_nonpipeline_proposals_report_readable_errors(inbox):
    missing = inbox.client.post("/api/proposals/999999/review", json={})
    assert missing.status_code == 404 and "提案不存在" in missing.json()["detail"]
    ordinary = proposal_service.create_proposal(
        project_id=inbox.id, kind="chapter_draft", title="普通提案",
        target_path=inbox.rel, content=BODY)
    invalid = inbox.client.post(f"/api/proposals/{ordinary['id']}/review", json={})
    assert invalid.status_code == 400 and "管线章节候选" in invalid.json()["detail"]
    assert not inbox.calls
    assert inbox.path.read_text(encoding="utf-8") == OLD_BODY


def test_hard_gate_only_review_cannot_authorize_application(inbox):
    proposal_id = inbox.proposal["id"]
    response = inbox.client.post(f"/api/proposals/{proposal_id}/review", json={"use_ai": False})
    assert response.status_code == 200, response.text
    assert response.json()["ai_used"] is False
    assert not inbox.calls
    applied = inbox.client.post(f"/api/proposals/{proposal_id}/apply", json={})
    assert applied.status_code == 400 and "AI 审稿" in applied.json()["detail"]
    assert inbox.path.read_text(encoding="utf-8") == OLD_BODY
