"""Reversible outline freeze state without changing manuscripts or chapter contracts."""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from workbench.backend import config, db
from workbench.backend.api.content import router as content_router
from workbench.backend.api.writing import router as writing_router
from workbench.backend.services import (
    chapter_service,
    contract_service,
    file_change_service,
    operation_log,
    outline_service,
    project_service,
    prompt_registry_service,
)
from workbench.backend.services.errors import InvalidOperationError, ServiceError


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
        raise AssertionError("Outline freeze tests never invoke a model")

    monkeypatch.setattr(outline_service.generation_service, "run_task", unexpected_model)
    project = project_service.create_project(name="冻结测试书")
    chapter = chapter_service.create_chapter(project["id"], "运河初成")
    return SimpleNamespace(project_id=project["id"], directory=projects / "冻结测试书",
                           chapter_rel=chapter["rel_path"])


@pytest.fixture()
def client() -> TestClient:
    app = FastAPI()
    app.include_router(writing_router)
    app.include_router(content_router)

    @app.exception_handler(ServiceError)
    async def service_error(_request, error: ServiceError):
        return JSONResponse({"detail": error.message}, status_code=error.status_code)

    return TestClient(app)


def _book_files(directory: Path) -> dict[str, bytes]:
    return {path.relative_to(directory).as_posix(): path.read_bytes()
            for path in directory.rglob("*") if path.is_file()}


def test_freeze_unfreeze_save_and_freeze_again(workspace):
    outline_service.save_outline(workspace.project_id, "主角修复运河，将灾民送入安全城镇。")
    frozen = outline_service.freeze(workspace.project_id)
    assert frozen["outline_frozen"] is True
    assert frozen["outline_frozen_at"]
    with pytest.raises(InvalidOperationError, match="差异确认"):
        outline_service.save_outline(workspace.project_id, "主角修复运河并清查官粮。")

    cancelled = outline_service.unfreeze(workspace.project_id)
    assert cancelled["outline_frozen"] is False
    assert cancelled["outline_frozen_at"] is None
    assert outline_service.read_outline(workspace.project_id)["frozen"] is False
    saved = outline_service.save_outline(workspace.project_id, "主角修复运河并清查官粮。")
    assert saved["content"] == "主角修复运河并清查官粮。"
    assert saved["frozen"] is False

    refrozen = outline_service.freeze(workspace.project_id)
    assert refrozen["outline_frozen"] is True
    assert refrozen["outline_frozen_at"]
    with pytest.raises(InvalidOperationError, match="差异确认"):
        outline_service.save_outline(workspace.project_id, "未确认的新大纲。")
    actions = [item["action"] for item in operation_log.recent(workspace.project_id)]
    assert actions.count("outline-freeze") == 2
    assert actions.count("outline-unfreeze") == 1


def test_unfreeze_is_idempotent_and_preserves_other_gates_and_book_files(workspace):
    outline_service.save_outline(workspace.project_id, "主角修复运河。")
    outline_service.generate_candidates(workspace.project_id, use_ai=False)
    contract = {"status": "frozen", "plot_points": ["船队穿过水闸"], "word_budget": 2500,
                "hook_type": "悬念", "frozen_at": "2026-09-20T08:00:00"}
    contract_service._write_all(workspace.directory, {workspace.chapter_rel: contract},
                                project_id=workspace.project_id)
    contract_service._sync_index(workspace.project_id, workspace.chapter_rel, contract)
    contract_service.write_gates(workspace.directory, {"style_ready": True,
                                                       "knowledge": {"accepted": ["abc"]},
                                                       "custom_flag": "保留"})
    outline_service.freeze(workspace.project_id)
    before_files = _book_files(workspace.directory)
    before_gates = contract_service.read_gates(workspace.directory)

    first = outline_service.unfreeze(workspace.project_id)
    first_bytes = (workspace.directory / contract_service.GATE_FILE).read_bytes()
    second = outline_service.unfreeze(workspace.project_id)

    assert first == second == {**before_gates, "outline_frozen": False, "outline_frozen_at": None}
    assert (workspace.directory / contract_service.GATE_FILE).read_bytes() == first_bytes
    after_files = _book_files(workspace.directory)
    changed = [path for path in before_files if before_files[path] != after_files[path]]
    assert changed == [contract_service.GATE_FILE]
    assert before_files.keys() == after_files.keys()
    assert contract_service.get_contract(workspace.project_id, workspace.chapter_rel) == contract
    with db.get_conn() as conn:
        row = conn.execute("SELECT status, frozen_at FROM chapter_contracts WHERE project_id = ?",
                           (workspace.project_id,)).fetchone()
    assert row["status"] == "frozen"
    assert row["frozen_at"] == contract["frozen_at"]


def test_unfreeze_before_first_freeze_is_safe(workspace):
    before_files = _book_files(workspace.directory)

    gates = outline_service.unfreeze(workspace.project_id)

    assert gates == {"outline_frozen": False, "outline_frozen_at": None}
    after_files = _book_files(workspace.directory)
    assert {path: content for path, content in after_files.items() if path != contract_service.GATE_FILE} == before_files
    assert outline_service.read_outline(workspace.project_id)["frozen"] is False


def test_check_prerequisites_blocks_again_after_unfreeze_but_contract_stays_ready(workspace):
    outline_service.save_outline(workspace.project_id, "主角修复运河。")
    contract_service._write_all(workspace.directory, {workspace.chapter_rel: {"status": "frozen"}},
                                project_id=workspace.project_id)
    outline_service.freeze(workspace.project_id)
    assert contract_service.check_prerequisites(workspace.project_id, workspace.chapter_rel)["blocked"] is False

    outline_service.unfreeze(workspace.project_id)

    result = contract_service.check_prerequisites(workspace.project_id, workspace.chapter_rel)
    assert result["blocked"] is True
    assert next(item for item in result["checks"] if item["key"] == "outline")["ok"] is False
    assert "冻结大纲" in result["reasons"][0]
    assert contract_service.check_contract_ready(workspace.project_id, workspace.chapter_rel)["ok"] is True


@pytest.mark.parametrize("action, frozen", [(outline_service.freeze, True), (outline_service.unfreeze, False)])
def test_freeze_changes_wait_for_project_lock_and_merge_latest_gates(workspace, monkeypatch, action, frozen):
    real_lock = file_change_service.project_lock
    requested = threading.Event()

    @contextmanager
    def observed_lock(project_id):
        requested.set()
        with real_lock(project_id):
            yield

    monkeypatch.setattr(file_change_service, "project_lock", observed_lock)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with real_lock(workspace.project_id):
            pending = pool.submit(action, workspace.project_id)
            assert requested.wait(timeout=5)
            assert not pending.done()
            contract_service.write_gates(workspace.directory, {"concurrent_gate": {"latest": True}})
        result = pending.result(timeout=10)

    assert result["outline_frozen"] is frozen
    assert result["concurrent_gate"] == {"latest": True}
    assert contract_service.read_gates(workspace.directory)["concurrent_gate"] == {"latest": True}


def test_freeze_without_confirmation_preserves_existing_state(workspace):
    contract_service.write_gates(workspace.directory, {"outline_frozen": False, "custom": "保留"})
    before = (workspace.directory / contract_service.GATE_FILE).read_bytes()

    with pytest.raises(InvalidOperationError, match="作者确认"):
        outline_service.freeze(workspace.project_id, confirm=False)

    assert (workspace.directory / contract_service.GATE_FILE).read_bytes() == before


def test_unfreeze_api_no_body_clears_state_and_allows_editing(workspace, client):
    prefix = f"/api/projects/{workspace.project_id}/outline"
    assert client.put(prefix, json={"content": "主角修复运河。"}).status_code == 200
    frozen = client.post(prefix + "/freeze")
    assert frozen.status_code == 200
    assert frozen.json()["outline_frozen"] is True
    assert client.get(prefix).json()["frozen"] is True
    assert client.put(prefix, json={"content": "未确认的修改。"}).status_code == 400

    cancelled = client.post(prefix + "/unfreeze")

    assert cancelled.status_code == 200
    assert cancelled.json()["outline_frozen"] is False
    assert cancelled.json()["outline_frozen_at"] is None
    assert client.get(prefix).json()["frozen"] is False
    assert client.put(prefix, json={"content": "主角修复运河并清查官粮。"}).status_code == 200
    assert client.post(prefix + "/unfreeze").json() == cancelled.json()
    assert client.post(prefix + "/freeze").json()["outline_frozen"] is True
    assert client.post("/api/projects/999999/outline/unfreeze").status_code == 404

    data = json.loads((workspace.directory / contract_service.GATE_FILE).read_text(encoding="utf-8"))
    assert data["outline_frozen"] is True
    assert data["outline_frozen_at"]
