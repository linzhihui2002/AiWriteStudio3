"""Immutable book/database identity and destructive lifecycle isolation."""
from pathlib import Path
import shutil
import sqlite3

import pytest

from workbench.backend import config, db
from workbench.backend.services import knowledge_service, knowledge_store, project_service, transfer_service
from workbench.backend.services.errors import InvalidOperationError, ProjectNotFoundError


@pytest.fixture()
def books(tmp_path, monkeypatch):
    projects, runtime = tmp_path / "projects", tmp_path / ".workbench"
    projects.mkdir()
    runtime.mkdir()
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(config, "PROJECTS_DIR", projects)
    monkeypatch.setattr(config, "RUNTIME_DIR", runtime)
    monkeypatch.setattr(config, "DB_PATH", runtime / "workbench.db")
    db.init_db()
    config.ensure_runtime_dirs()
    monkeypatch.setattr(knowledge_service, "enqueue", lambda *a, **k: None)
    monkeypatch.setattr(knowledge_service, "cancel", lambda *a, **k: None)
    first = project_service.create_project(name="甲书")
    second = project_service.create_project(name="乙书")
    return first, second, projects, runtime


def test_legacy_registration_migration_is_idempotent_and_keys_are_immutable(books):
    first, _, projects, _ = books
    original = knowledge_store.identity(first["id"])
    with db.get_conn() as conn:
        cursor = conn.execute("INSERT INTO projects(name,path,created_at,archived) VALUES (?,?,?,0)",
                              ("旧登记", str(projects / "旧登记"), "2026-01-01T00:00:00"))
        legacy_id = cursor.lastrowid
    db.init_db()
    key = knowledge_store.identity(legacy_id)["knowledge_key"]
    db.init_db()
    assert knowledge_store.identity(legacy_id)["knowledge_key"] == key
    assert knowledge_store.identity(first["id"])["knowledge_key"] == original["knowledge_key"]
    assert key != original["knowledge_key"] and len(key) == 32
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        with db.get_conn() as conn:
            conn.execute("UPDATE projects SET knowledge_key=? WHERE id=?", ("a" * 32, first["id"]))


def test_copying_another_books_database_is_rejected_before_reading(books):
    first, second, _, _ = books
    knowledge_store.set_preference(first["id"], "marker", "甲书数据")
    left, right = knowledge_store.identity(first["id"]), knowledge_store.identity(second["id"])
    target = Path(right["db_path"])
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(left["db_path"], target)
    with pytest.raises(InvalidOperationError, match="不匹配"):
        knowledge_store.preference(second["id"], "marker")
    assert knowledge_store.preference(first["id"], "marker") == "甲书数据"


def test_unbound_database_is_not_implicitly_attached_to_a_book(books):
    first, _, _, _ = books
    path = Path(knowledge_store.identity(first["id"])["db_path"])
    # Project creation may initialize an empty bound projection; replace only
    # this test fixture database with an unbound legacy-shaped artifact.
    if path.exists():
        path.unlink()
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE alien_vectors(text TEXT)")
        conn.execute("INSERT INTO alien_vectors VALUES ('外书内容')")
    with pytest.raises(InvalidOperationError, match="未绑定"):
        with knowledge_store.connect(first["id"]):
            pass


def test_rename_and_soft_restore_preserve_key_and_database(books):
    first, _, _, _ = books
    before = knowledge_store.identity(first["id"])
    knowledge_store.set_preference(first["id"], "marker", {"book": "甲"})
    project_service.update_project(first["id"], name="甲书改名")
    renamed = knowledge_store.identity(first["id"])
    assert renamed["knowledge_key"] == before["knowledge_key"]
    assert renamed["db_path"] == before["db_path"]
    project_service.delete_project(first["id"])
    with pytest.raises(ProjectNotFoundError):
        knowledge_store.identity(first["id"])
    deleted = knowledge_store.identity(first["id"], include_deleted=True)
    assert deleted["knowledge_key"] == before["knowledge_key"]
    assert Path(deleted["db_path"]).is_file()
    project_service.restore_project(first["id"])
    assert knowledge_store.identity(first["id"])["db_path"] == before["db_path"]
    assert knowledge_store.preference(first["id"], "marker") == {"book": "甲"}


def test_importing_backup_as_new_book_creates_distinct_key_and_no_preferences(books):
    first, _, _, _ = books
    knowledge_store.set_preference(first["id"], "vector", {"mode": "local", "model": "source-model"})
    package = transfer_service.export_project_package(first["id"])
    restored = transfer_service.restore_project_package(package, new_name="甲书备份副本")["project"]
    source_key = knowledge_store.identity(first["id"])["knowledge_key"]
    copy_key = knowledge_store.identity(restored["id"])["knowledge_key"]
    assert source_key != copy_key
    assert knowledge_store.preference(restored["id"], "vector", {}) == {}
    assert knowledge_store.preference(first["id"], "vector")["model"] == "source-model"


def test_purge_removes_only_the_selected_books_projection(books):
    first, second, projects, _ = books
    knowledge_store.set_preference(first["id"], "marker", "甲")
    knowledge_store.set_preference(second["id"], "marker", "乙")
    removed = Path(knowledge_store.identity(first["id"])["db_path"]).parent
    retained = Path(knowledge_store.identity(second["id"])["db_path"])
    project_service.purge_project(first["id"])
    assert not removed.exists() and not (projects / first["name"]).exists()
    assert retained.exists() and (projects / second["name"]).exists()
    assert knowledge_store.preference(second["id"], "marker") == "乙"
    with pytest.raises(ProjectNotFoundError):
        project_service.fetch_project_row(first["id"], include_deleted=True)


def test_windows_busy_database_keeps_registration_and_book_files(books, monkeypatch):
    first, second, projects, _ = books
    knowledge_store.set_preference(first["id"], "marker", "甲")
    folder = Path(knowledge_store.identity(first["id"])["db_path"]).parent
    real_rmtree = shutil.rmtree
    def busy(target, *args, **kwargs):
        if Path(target).resolve() == folder.resolve():
            raise PermissionError("Windows: database file is in use")
        return real_rmtree(target, *args, **kwargs)
    monkeypatch.setattr(knowledge_store.shutil, "rmtree", busy)
    with pytest.raises(PermissionError, match="in use"):
        project_service.purge_project(first["id"])
    assert project_service.fetch_project_row(first["id"])["name"] == first["name"]
    assert (projects / first["name"]).is_dir() and folder.is_dir()
    assert project_service.fetch_project_row(second["id"])["name"] == second["name"]


def test_new_book_schedules_projection_after_global_opt_in(books, monkeypatch):
    from workbench.backend.services import embedding_service

    calls = []
    monkeypatch.setattr(embedding_service, "settings_for", lambda: {"enabled": True})
    monkeypatch.setattr(knowledge_service, "enqueue", lambda project_id: calls.append(project_id))
    created = project_service.create_project(name="启用后新书")
    assert calls == [created["id"]]
    assert (books[2] / created["name"] / "project.md").is_file()


def test_opening_enabled_book_schedules_external_file_reconciliation(books, monkeypatch):
    from workbench.backend.api import projects as projects_api

    first, _, _, _ = books
    calls = []
    monkeypatch.setattr(knowledge_service, "auto_enabled", lambda project_id: True)
    monkeypatch.setattr(knowledge_service, "enqueue", lambda project_id: calls.append(project_id))
    detail = projects_api.get_project(first["id"])
    assert detail["id"] == first["id"]
    assert calls == [first["id"]]
