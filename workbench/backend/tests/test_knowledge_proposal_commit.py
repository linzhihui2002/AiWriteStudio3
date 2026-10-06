"""Knowledge proposal commits use the real file journal and repair derived state."""
from __future__ import annotations

import pytest

from workbench.backend import config, db
from workbench.backend.services import (
    file_change_service as changes, knowledge_service, project_service, proposal_service,
)
from workbench.backend.services.errors import InvalidOperationError


@pytest.fixture()
def book(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setattr(config, "RUNTIME_DIR", tmp_path / ".workbench")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / ".workbench/workbench.db")
    config.ensure_runtime_dirs()
    db.init_db()
    monkeypatch.setattr(knowledge_service, "enqueue", lambda *a, **k: None)
    project = project_service.create_project(name="知识提交凭据原创样例")
    _, root = project_service.get_project_dir(project["id"])
    target = root / "设定/人物设定.md"
    target.write_text("# 沈砚\n身份：守桥人。\n", encoding="utf-8")
    return project["id"], target


def proposal(book):
    project_id, target = book
    return proposal_service.create_proposal(project_id=project_id, kind="setting",
        title="更新知识", target_path="设定/人物设定.md", content="# 沈砚\n身份：巡桥人。\n",
        meta={"knowledge": {"knowledge_key": "fixture", "candidates": [{"id": "candidate", "version": 1}]}})


def test_knowledge_preflight_receives_actual_edited_content_and_can_block(book, monkeypatch):
    from workbench.backend.services import knowledge_candidate_service as candidates
    item = proposal(book)
    before = book[1].read_bytes()
    seen = []

    def reject(project_id, actual):
        seen.append(actual["content"])
        raise InvalidOperationError("原文版本已变化")

    monkeypatch.setattr(candidates, "validate_proposal", reject)
    with pytest.raises(InvalidOperationError, match="原文版本"):
        proposal_service.apply_proposal(item["id"], content="作者修改后的知识记录。")
    assert seen == ["作者修改后的知识记录。"]
    assert book[1].read_bytes() == before
    assert proposal_service.get_proposal(item["id"])["status"] == "pending"
    assert changes.list_changes(f"proposal:{item['id']}", book[0]) == []


def test_failed_derived_reconciliation_retries_without_replaying_file(book, monkeypatch):
    from workbench.backend.services import knowledge_candidate_service as candidates
    item = proposal(book)
    validates = []
    monkeypatch.setattr(candidates, "validate_proposal", lambda *a: validates.append(a))

    def unavailable(*a, **k):
        raise RuntimeError("派生库暂不可写")

    monkeypatch.setattr(candidates, "reconcile_proposal", unavailable)
    result = proposal_service.apply_proposal(item["id"])
    assert result["knowledge"]["status"] == "reconciliation_pending"
    assert result["proposal"]["status"] == "applied"
    first = changes.list_changes(f"proposal:{item['id']}", book[0])
    assert len(first) == 1 and first[0]["status"] == "applied"

    # A later valid author edit must not be overwritten while repairing a receipt.
    book[1].write_text(book[1].read_text(encoding="utf-8") + "别名：砚生。\n", encoding="utf-8")
    current = book[1].read_bytes()
    received = []
    monkeypatch.setattr(candidates, "reconcile_proposal",
        lambda project_id, actual, receipt: received.append(receipt) or {"status": "applied"})
    repaired = proposal_service.apply_proposal(item["id"])
    assert repaired["knowledge"]["status"] == "applied"
    assert received[0]["id"] == first[0]["id"]
    assert book[1].read_bytes() == current
    assert len(validates) == 1
    assert len(changes.list_changes(f"proposal:{item['id']}", book[0])) == 1


def test_receipt_repairs_pending_proposal_after_disk_commit(book, monkeypatch):
    from workbench.backend.services import knowledge_candidate_service as candidates
    item = proposal(book)
    monkeypatch.setattr(candidates, "validate_proposal", lambda *a: None)
    monkeypatch.setattr(candidates, "reconcile_proposal", lambda *a: {"status": "applied"})
    first = proposal_service.apply_proposal(item["id"])
    with db.get_conn() as conn:
        conn.execute("UPDATE proposals SET status='pending',applied_at=NULL WHERE id=?", (item["id"],))

    def obsolete(*a):
        raise InvalidOperationError("写回后依据版本自然发生变化")

    monkeypatch.setattr(candidates, "validate_proposal", obsolete)
    again = proposal_service.apply_proposal(item["id"])
    assert again["change"]["id"] == first["change"]["id"]
    assert again["proposal"]["status"] == "applied"
    assert len(changes.list_changes(f"proposal:{item['id']}", book[0])) == 1


def test_discarded_knowledge_proposal_immediately_releases_candidates(book, monkeypatch):
    from workbench.backend.services import knowledge_candidate_service as candidates
    item = proposal(book)
    before = book[1].read_bytes()
    received = []
    monkeypatch.setattr(candidates, "reconcile_proposal",
        lambda project_id, actual: received.append(actual) or {"applied": 0})
    discarded = proposal_service.discard_proposal(item["id"])
    assert discarded["status"] == received[0]["status"] == "discarded"
    assert received[0]["id"] == item["id"]
    assert book[1].read_bytes() == before
