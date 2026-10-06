"""Outline candidate history, deduplication, refinement and API tests with fake models."""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from workbench.backend import config, db
from workbench.backend.api.content import router
from workbench.backend.services import outline_service, project_service, prompt_registry_service
from workbench.backend.services.errors import InvalidOperationError, NodeNotFoundError, ServiceError
from workbench.backend.services.file_change_service import FileConflictError, project_lock


@pytest.fixture()
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    projects, runtime = tmp_path / "projects", tmp_path / ".workbench"
    projects.mkdir()
    runtime.mkdir()
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(config, "PROJECTS_DIR", projects)
    monkeypatch.setattr(config, "RUNTIME_DIR", runtime)
    monkeypatch.setattr(config, "DB_PATH", runtime / "workbench.db")
    config.ensure_runtime_dirs()
    db.init_db()
    prompt_registry_service.ensure_registry()

    def unexpected_model(**kwargs):
        raise AssertionError("This test must install a fake model response")

    monkeypatch.setattr(outline_service.generation_service, "run_task", unexpected_model)
    project = project_service.create_project(name="候选测试书")
    return SimpleNamespace(project_id=project["id"], directory=projects / "候选测试书")


@pytest.fixture()
def client() -> TestClient:
    app = FastAPI()
    app.include_router(router)

    @app.exception_handler(ServiceError)
    async def service_error(_request, error: ServiceError):
        return JSONResponse({"detail": error.message}, status_code=error.status_code)

    return TestClient(app)


def _model(monkeypatch: pytest.MonkeyPatch, text: str):
    captured = []

    def respond(**kwargs):
        captured.append(kwargs)
        return {"ok": True, "text": text}

    monkeypatch.setattr(outline_service.generation_service, "run_task", respond)
    return captured


def _seed(workspace: SimpleNamespace, candidates: list[dict]) -> None:
    outline_service._write_meta(workspace.directory, {"candidates": candidates,
                                                    "history": [], "locked": None})


def test_generating_appends_unique_candidates_and_uses_previous_plots(workspace, monkeypatch):
    _model(monkeypatch, "候选一\n主角为了找回失踪妹妹潜入海底神殿。\n候选二\n商人沿边境运粮并揭发军需贪污。")
    first = outline_service.generate_candidates(workspace.project_id)
    old = first["candidates"]
    old_outline = (workspace.directory / outline_service.OUTLINE_FILE).read_bytes()
    captured = _model(monkeypatch, "候选一\n主角，为了找回失踪妹妹，潜入海底神殿！\n候选二\n药师调查百年前的瘟疫，重建城邦的防疫制度。")

    second = outline_service.generate_candidates(workspace.project_id, idea="调查瘟疫")

    assert second["candidates"][:2] == old
    assert second["count"] == 3
    assert second["added_count"] == 1
    assert second["duplicate_count"] == 1
    assert second["candidates"][-1]["title"] == "候选3"
    assert len({item["id"] for item in second["candidates"]}) == 3
    assert all(item["content"] in captured[0]["messages"][0]["content"] for item in old)
    assert "主线冲突" in captured[0]["messages"][0]["content"]
    assert outline_service.read_outline(workspace.project_id)["candidates"] == second["candidates"]
    assert (workspace.directory / outline_service.OUTLINE_FILE).read_bytes() == old_outline


def test_deduplication_ignores_headings_and_minor_rephrasing(workspace, monkeypatch):
    body = "少年从边境逃亡来到王都，凭账本追查家族覆灭真相，逐步联合码头工人和低阶官吏，最终公开秘密并建立新的贸易秩序。"
    _seed(workspace, [{"title": "旧方案", "content": "# 旧名称\n" + body}])
    _model(monkeypatch, "候选一：\n# 新名称\n" + body.replace("少年", "少女")
           + "\n候选二：\n" + body.replace("，", "！"))

    result = outline_service.generate_candidates(workspace.project_id)

    assert result["added_count"] == 0
    assert result["duplicate_count"] == 2
    assert result["count"] == 1
    assert "重复" in result["message"]


def test_deduplicates_within_one_response_and_preserves_inline_body(workspace, monkeypatch):
    _model(monkeypatch, "候选一：妹妹在海岛寻找失踪的船员。\n候选二：妹妹在海岛寻找失踪的船员！")

    result = outline_service.generate_candidates(workspace.project_id)

    assert result["added_count"] == 1
    assert result["duplicate_count"] == 1
    assert result["candidates"][0]["content"] == "妹妹在海岛寻找失踪的船员。"


@pytest.mark.parametrize("response", [{"ok": False, "error_message": "模拟引擎不可用"},
                                      {"ok": True, "text": "候选一：\n候选二："},
                                      {"ok": True, "text": "   "}])
def test_failed_generation_does_not_replace_or_create_placeholders(workspace, monkeypatch, response):
    _seed(workspace, [{"title": "原候选", "content": "主角回乡重修运河。", "source": "model"}])
    before = (workspace.directory / outline_service.META_FILE).read_bytes()
    monkeypatch.setattr(outline_service.generation_service, "run_task", lambda **_: response)

    with pytest.raises(InvalidOperationError):
        outline_service.generate_candidates(workspace.project_id)

    assert (workspace.directory / outline_service.META_FILE).read_bytes() == before
    assert outline_service.read_outline(workspace.project_id)["candidates"][0]["source"] == "model"


def test_offline_generation_creates_placeholders_once(workspace):
    first = outline_service.generate_candidates(workspace.project_id, use_ai=False)
    second = outline_service.generate_candidates(workspace.project_id, use_ai=False)

    assert first["added_count"] == first["count"] == 3
    assert second["added_count"] == 0
    assert second["candidates"] == first["candidates"]
    assert second["count"] == 3


def test_concurrent_generation_rechecks_duplicates_without_holding_model_lock(workspace, monkeypatch):
    barrier = threading.Barrier(2)
    shared = "边境女将带领难民穿越沙漠寻找故乡。"

    def respond(**kwargs):
        barrier.wait(timeout=10)
        idea = kwargs["messages"][0]["content"]
        unique = "捕快审理盐船失踪案件，追回官库被盗银钱。" if "盐案" in idea else "星际医生在废弃空间站重建生态系统。"
        return {"ok": True, "text": f"候选一\n{shared}\n候选二\n{unique}"}

    monkeypatch.setattr(outline_service.generation_service, "run_task", respond)
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = [pool.submit(outline_service.generate_candidates, workspace.project_id, idea=idea)
                   for idea in ("盐案", "空间站")]
        results = [future.result(timeout=20) for future in pending]

    all_candidates = outline_service.read_outline(workspace.project_id)["candidates"]
    assert len(all_candidates) == 3
    assert sum(item["added_count"] for item in results) == 3
    assert sum(item["duplicate_count"] for item in results) == 1
    assert len({item["id"] for item in all_candidates}) == 3
    assert sum(item["content"] == shared for item in all_candidates) == 1


def test_legacy_candidates_have_stable_ids_and_lock_by_identity(workspace):
    _seed(workspace, [{"title": "候选一", "content": "主角回乡修桥。"},
                      {"title": "候选一", "content": "船长带领船队寻找北方航线。"}])
    first = outline_service.read_outline(workspace.project_id)["candidates"]
    second = outline_service.read_outline(workspace.project_id)["candidates"]

    assert [item["title"] for item in first] == ["候选一", "候选一"]
    assert [item["id"] for item in first] == [item["id"] for item in second]
    assert len({item["id"] for item in first}) == 2
    locked = outline_service.lock_candidate(workspace.project_id, first[1]["id"])
    assert locked["content"] == first[1]["content"]
    assert locked["locked"]["id"] == first[1]["id"]
    assert outline_service.read_outline(workspace.project_id)["candidates"] == first


def test_refinement_appends_versions_and_inherits_full_conversation(workspace, monkeypatch):
    _seed(workspace, [{"title": "候选一", "content": "主角修复城墙，抵御兽潮。"}])
    original = outline_service.read_outline(workspace.project_id)["candidates"][0]
    original_outline = (workspace.directory / outline_service.OUTLINE_FILE).read_bytes()
    captured = _model(monkeypatch, "主角修复城墙，联合守城军民抵御兽潮，并查出兽潮起因。")
    first = outline_service.refine_candidate(workspace.project_id, original["id"], message="增加调查线")
    first_candidate = first["candidate"]

    assert original["content"] in captured[0]["messages"][0]["content"]
    assert captured[0]["messages"][-1] == {"role": "user", "content": "增加调查线"}
    assert first_candidate["parent_id"] == original["id"]
    assert first["candidates"][0] == original
    assert first_candidate["conversation"] == [
        {"role": "user", "content": "增加调查线"},
        {"role": "assistant", "content": first_candidate["content"]},
    ]

    captured = _model(monkeypatch, first_candidate["content"] + "第二卷揭晓商队操纵兽潮牟利的证据。")
    second = outline_service.refine_candidate(workspace.project_id, first_candidate["id"], message="细化第二卷")

    assert captured[0]["messages"][1:-1] == first_candidate["conversation"]
    assert second["candidate"]["parent_id"] == first_candidate["id"]
    assert second["candidate"]["conversation"][:2] == first_candidate["conversation"]
    assert second["count"] == 3
    assert second["candidates"][:2] == first["candidates"]
    assert (workspace.directory / outline_service.OUTLINE_FILE).read_bytes() == original_outline
    assert outline_service.read_outline(workspace.project_id)["locked"] is None


def test_refinement_retains_small_substantive_change_in_markdown_heading(workspace, monkeypatch):
    _seed(workspace, [{"title": "候选一", "content": "# 主线冲突：主角决定反叛城主\n调查运河账本。"}])
    original = outline_service.read_outline(workspace.project_id)["candidates"][0]
    _model(monkeypatch, "# 主线冲突：主角决定保护城主\n调查运河账本。")

    result = outline_service.refine_candidate(workspace.project_id, original["id"], message="改为保护城主")

    assert result["count"] == 2
    assert "保护城主" in result["candidate"]["content"]
    assert "反叛城主" in result["candidates"][0]["content"]


def test_numbered_wrapper_title_does_not_make_refinement_new(workspace, monkeypatch):
    _seed(workspace, [{"title": "候选一", "content": "# 候选1：保护城主\n调查运河账本。"}])
    original = outline_service.read_outline(workspace.project_id)["candidates"][0]
    _model(monkeypatch, "## 方案12：新的名字\n调查运河账本！")

    with pytest.raises(InvalidOperationError, match="实质差异"):
        outline_service.refine_candidate(workspace.project_id, original["id"], message="换个标题")

    assert len(outline_service.read_outline(workspace.project_id)["candidates"]) == 1


@pytest.mark.parametrize("response", [{"ok": False, "error_message": "模拟失败"},
                                      {"ok": True, "text": "   "},
                                      {"ok": True, "text": "主角，修复城墙！"}])
def test_failed_or_unchanged_refinement_keeps_original(workspace, monkeypatch, response):
    _seed(workspace, [{"title": "候选一", "content": "主角修复城墙。"}])
    original = outline_service.read_outline(workspace.project_id)["candidates"][0]
    before = (workspace.directory / outline_service.META_FILE).read_bytes()
    monkeypatch.setattr(outline_service.generation_service, "run_task", lambda **_: response)

    with pytest.raises(InvalidOperationError):
        outline_service.refine_candidate(workspace.project_id, original["id"], message="改进冲突")

    assert (workspace.directory / outline_service.META_FILE).read_bytes() == before


def test_refinement_rejects_blank_message_and_missing_candidate_before_model_call(workspace):
    with pytest.raises(InvalidOperationError):
        outline_service.refine_candidate(workspace.project_id, "missing", message="  ")
    with pytest.raises(NodeNotFoundError):
        outline_service.refine_candidate(workspace.project_id, "missing", message="细化成长")


def test_refinement_rejects_changed_target_without_overwriting_newer_metadata(workspace, monkeypatch):
    _seed(workspace, [{"title": "候选一", "content": "主角修复城墙。"}])
    original = outline_service.read_outline(workspace.project_id)["candidates"][0]

    def concurrent_change():
        with project_lock(workspace.project_id):
            data = outline_service._read_meta(workspace.directory)
            data["candidates"][0]["content"] = "作者的新版本：主角修筑运河。"
            outline_service._write_meta(workspace.directory, data)

    def respond(**_):
        # A second thread can commit while generation is in progress.
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(concurrent_change).result(timeout=10)
        return {"ok": True, "text": "主角修复城墙并找到失传的机关术。"}

    monkeypatch.setattr(outline_service.generation_service, "run_task", respond)
    with pytest.raises(FileConflictError):
        outline_service.refine_candidate(workspace.project_id, original["id"], message="加机关术")

    current = outline_service.read_outline(workspace.project_id)["candidates"]
    assert len(current) == 1
    assert current[0]["content"] == "作者的新版本：主角修筑运河。"


def test_outline_api_append_refine_validation_and_identity_lock(workspace, monkeypatch, client):
    prefix = f"/api/projects/{workspace.project_id}/outline"
    _model(monkeypatch, "候选一\n主角治河救民。\n候选二\n将军为流民守住边城。")
    generated = client.post(prefix + "/candidates", json={"idea": "民生"})
    assert generated.status_code == 200
    assert generated.json()["added_count"] == 2
    original = generated.json()["candidates"][0]
    refine_url = prefix + f"/candidates/{original['id']}/refine"

    assert client.post(refine_url, json={"message": ""}).status_code == 422
    assert client.post(refine_url, json={"message": "  "}).status_code == 400
    assert client.post(prefix + "/candidates/missing/refine", json={"message": "改进"}).status_code == 404
    _model(monkeypatch, "主角治河救民，揭露侵吞赈灾粮款的证据。")
    refined = client.post(refine_url, json={"message": "加入贪腐案"})
    assert refined.status_code == 200
    assert refined.json()["count"] == 3
    candidate = refined.json()["candidate"]
    assert candidate["parent_id"] == original["id"]
    locked = client.post(prefix + "/lock", json={"candidate_id": candidate["id"], "confirm": True})
    assert locked.status_code == 200
    assert locked.json()["content"] == candidate["content"]
    assert locked.json()["locked"]["id"] == candidate["id"]

    persisted = json.loads((workspace.directory / outline_service.META_FILE).read_text(encoding="utf-8"))
    assert persisted["candidates"][0]["id"] == original["id"]
    assert persisted["candidates"][-1]["conversation"][-1]["content"] == candidate["content"]
