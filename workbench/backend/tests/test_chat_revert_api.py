"""Author HTTP undo shares the model's denial and scope policy."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from workbench.backend.api.orchestration import router
from workbench.backend.services import chat_service, chat_run_service as runs
from workbench.backend.services import file_change_service as changes, settings_service
from workbench.backend.services.errors import ServiceError
from workbench.backend.tests.test_chat_runs import workspace, install, finish


def http_client():
    app = FastAPI()
    app.include_router(router)
    @app.exception_handler(ServiceError)
    async def service_error(_request, error):
        return JSONResponse({"detail": error.message}, status_code=error.status_code)
    return TestClient(app)


def applied_run(workspace, monkeypatch):
    project, session, _ = workspace
    install(monkeypatch, turns=[[("create_file", {"rel_path": "设定/人物验收.md", "content": "姓名林舟，目标查清旧案。"}), "已保存人物。"]])
    result = finish(runs.create_run(session["id"], text="请创建设定/人物验收.md，保存人物卡。"))
    assert result["changes"][0]["status"] == "applied"
    return project, session, result


def test_author_http_revert_updates_file_and_history(workspace, monkeypatch):
    project, _, run = applied_run(workspace, monkeypatch)
    response = http_client().post(f"/api/chat/runs/{run['id']}/revert", json={"change_ids": [run['changes'][0]['id']]})
    assert response.status_code == 200, response.text
    assert response.json()["changes"][0]["status"] == "reverted"
    assert not changes.file_state(project["id"], "设定/人物验收.md")["exists"]


@pytest.mark.parametrize("permission", ["ask", "auto", "full"])
def test_current_discussion_only_blocks_direct_http_undo(workspace, monkeypatch, permission):
    project, session, run = applied_run(workspace, monkeypatch)
    prior = changes.file_state(project["id"], "设定/人物验收.md")
    chat_service.update_session(session["id"], {"discussion_only": True, "permission_mode": permission})
    response = http_client().post(f"/api/chat/runs/{run['id']}/revert", json={})
    assert response.status_code == 400 and "只读" in response.json()["detail"]
    assert changes.file_state(project["id"], "设定/人物验收.md")["hash"] == prior["hash"]
    assert runs.get_run(run["id"])["changes"][0]["status"] == "applied"


@pytest.mark.parametrize("role", ["reviewer", "teardown-analyst", "chief-editor", "writing-assistant"])
def test_current_pinned_readonly_role_blocks_http_undo(workspace, monkeypatch, role):
    project, session, run = applied_run(workspace, monkeypatch)
    chat_service.update_session(session["id"], {"agent": role, "agent_pinned": 1, "permission_mode": "full"})
    response = http_client().post(f"/api/chat/runs/{run['id']}/revert", json={})
    assert response.status_code == 400 and "只读" in response.json()["detail"]
    assert changes.file_state(project["id"], "设定/人物验收.md")["exists"]


def test_strict_scope_rejects_author_undo_of_outside_material(workspace, monkeypatch):
    project, session, run = applied_run(workspace, monkeypatch)
    outside = changes.apply_change(project["id"], run_id=run["id"], operation="create",
        session_id=session["id"], tool_call_id="old-approved-change",
        rel_path="大纲/先前越界.md", content="旧的批准后大纲", expected_hash=None)
    settings_service.update_settings({"chat": {"scope_strictness": "reject"}})
    response = http_client().post(f"/api/chat/runs/{run['id']}/revert", json={"change_ids": [outside["id"]]})
    assert response.status_code == 400 and "越界" in response.json()["detail"]
    assert changes.file_state(project["id"], "大纲/先前越界.md")["exists"]


def test_author_http_undo_refuses_while_another_run_is_active(workspace, monkeypatch):
    project, session, old = applied_run(workspace, monkeypatch)
    runs.create_run(session["id"], text="继续讨论", start=False)
    response = http_client().post(f"/api/chat/runs/{old['id']}/revert", json={})
    assert response.status_code == 409
    assert changes.file_state(project["id"], "设定/人物验收.md")["exists"]
