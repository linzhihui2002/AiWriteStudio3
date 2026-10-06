"""Session permissions, disk recovery and independent title jobs without providers."""
from __future__ import annotations

import json
import sqlite3
import threading

import pytest

from workbench.backend import config, db
from workbench.backend.engine import router
from workbench.backend.services import (
    chat_preference_service, chat_service as chats, project_service, settings_service,
)
from workbench.backend.services.errors import InvalidOperationError, NodeNotFoundError


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "books")
    monkeypatch.setattr(config, "RUNTIME_DIR", tmp_path / ".workbench")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / ".workbench/workbench.db")
    db.init_db()
    config.ensure_runtime_dirs()
    project = project_service.create_project(name="标题与权限测试书")
    monkeypatch.setattr(chats.routing_service, "route", lambda *args, **kwargs: {})
    monkeypatch.setattr(chats, "_pick_agent", lambda *args: ("test-agent", {"provider_id": "agent-provider", "model_id": "agent-model"}))
    monkeypatch.setattr(router, "resolve_model", lambda **kwargs: {"provider": kwargs["provider"], "model": kwargs["model"]})
    workers = []
    releases = []
    yield project, workers, releases
    for release in releases:
        release.set()
    for worker in workers:
        worker.join(5)
        assert not worker.is_alive(), "title worker leaked across the fixture's config change"


def title_job(workspace, monkeypatch, session, *, text="《修仙世界的代币规则》", ok=True, raises=False):
    _project, workers, releases = workspace
    entered, release = threading.Event(), threading.Event()
    calls = []
    releases.append(release)

    def generate(**kwargs):
        calls.append(kwargs)
        entered.set()
        assert release.wait(5)
        if raises:
            raise RuntimeError("provider unavailable")
        return {"ok": ok, "text": text}

    monkeypatch.setattr(chats.generation_service, "run_task", generate)
    pending = chats.schedule_title(session["id"])
    worker = chats._title_workers[(str(config.DB_PATH), session["id"])]
    workers.append(worker)
    assert entered.wait(5)
    return pending, calls, worker, release


def test_default_settings_and_session_values_are_independent(workspace):
    project, _, _ = workspace
    first = chats.create_session(project_id=project["id"])
    assert (first["permission_mode"], first["discussion_only"], first["mode"], first["auto_apply"]) == ("auto", False, "write", 1)
    settings_service.update_settings({"chat": {"permission_mode": "ask", "discussion_only": True}})
    second = chats.create_session(project_id=project["id"])
    assert (second["permission_mode"], second["discussion_only"]) == ("ask", True)
    assert chats.get_session(first["id"])["permission_mode"] == "auto"
    changed = chats.update_session(first["id"], {"permission_mode": "full"})
    assert (changed["permission_mode"], changed["discussion_only"]) == ("full", False)
    # 写域两项（scope_strictness / scope_limit_full）是后加的对话偏好，只断言权限两项。
    saved_chat = settings_service.read_settings()["chat"]
    assert (saved_chat["permission_mode"], saved_chat["discussion_only"]) == ("ask", True)


def test_book_defaults_apply_only_to_new_sessions(workspace):
    project, _, _ = workspace
    project_id = project["id"]
    first = chats.create_session(project_id=project_id)
    assert chat_preference_service.get_chat_defaults(project_id) == {
        "permission_mode": "auto", "discussion_only": False, "source": "global",
    }
    saved = chat_preference_service.update_chat_defaults(
        project_id, permission_mode="full", discussion_only=False,
    )
    assert saved == {"permission_mode": "full", "discussion_only": False, "source": "book"}
    path = project_service.get_project_dir(project_id)[1] / ".meta/chat-preferences.json"
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "permission_mode": "full", "discussion_only": False,
    }
    second = chats.create_session(project_id=project_id)
    assert (second["permission_mode"], second["discussion_only"]) == ("full", False)
    assert (chats.get_session(first["id"])["permission_mode"],
            chats.get_session(first["id"])["discussion_only"]) == ("auto", False)
    settings_service.update_settings({"chat": {"permission_mode": "ask", "discussion_only": True}})
    third = chats.create_session(project_id=project_id)
    assert (third["permission_mode"], third["discussion_only"]) == ("full", False)
    other = project_service.create_project(name="另一部书")
    other_session = chats.create_session(project_id=other["id"])
    assert (other_session["permission_mode"], other_session["discussion_only"]) == ("ask", True)


def test_book_defaults_preserve_explicit_permissions_and_legacy_values(workspace):
    project, _, _ = workspace
    project_id = project["id"]
    chat_preference_service.update_chat_defaults(
        project_id, permission_mode="full", discussion_only=False,
    )
    explicit = chats.create_session(project_id=project_id, permission_mode="ask", discussion_only=True)
    legacy = chats.create_session(project_id=project_id, mode="write", auto_apply=False)
    legacy_read = chats.create_session(project_id=project_id, mode="read", auto_apply=True)
    assert (explicit["permission_mode"], explicit["discussion_only"]) == ("ask", True)
    assert (legacy["permission_mode"], legacy["discussion_only"]) == ("ask", False)
    assert (legacy_read["permission_mode"], legacy_read["discussion_only"]) == ("ask", True)


@pytest.mark.parametrize("bad", ["{", "[]", '{"permission_mode":"full"}',
                                       '{"permission_mode":"full","discussion_only":1}'])
def test_malformed_book_defaults_fail_closed(workspace, bad):
    project, _, _ = workspace
    project_id = project["id"]
    settings_service.update_settings({"chat": {"permission_mode": "full", "discussion_only": False}})
    path = project_service.get_project_dir(project_id)[1] / ".meta/chat-preferences.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(bad, encoding="utf-8")
    defaults = chat_preference_service.get_chat_defaults(project_id)
    session = chats.create_session(project_id=project_id)
    # 书级文件损坏时不继承全局值，而是回退到书级安全默认（与全局默认同为自动执行）。
    assert defaults == {"permission_mode": "auto", "discussion_only": False, "source": "book"}
    assert (session["permission_mode"], session["discussion_only"]) == ("auto", False)
    assert path.read_text(encoding="utf-8") == bad


def test_book_defaults_api(workspace):
    from fastapi.testclient import TestClient
    from workbench.backend.app import create_app

    project, _, _ = workspace
    path = f"/api/projects/{project['id']}/chat-defaults"
    with TestClient(create_app()) as client:
        initial = client.get(path)
        assert initial.status_code == 200
        assert initial.json() == {"permission_mode": "auto", "discussion_only": False,
                                  "source": "global"}
        put = client.put(path, json={"permission_mode": "full", "discussion_only": False})
        assert put.status_code == 200, put.text
        assert put.json() == {"permission_mode": "full", "discussion_only": False,
                              "source": "book"}
        created = client.post("/api/chat/sessions", json={"project_id": project["id"]})
        assert created.status_code == 201, created.text
        assert created.json()["permission_mode"] == "full"
        invalid = client.put(path, json={"permission_mode": "full", "discussion_only": "false"})
        assert invalid.status_code == 422
        assert client.get(path).json() == put.json()
        assert client.get("/api/projects/99999/chat-defaults").status_code == 404


@pytest.mark.parametrize("patch", [{"permission_mode": "invalid"}, {"permission_mode": []}, {"discussion_only": "false"}, {"auto_apply": "false"}, {"auto_apply": 9}])
def test_session_rejects_invalid_permissions(workspace, patch):
    project, _, _ = workspace
    with pytest.raises(InvalidOperationError):
        chats.create_session(project_id=project["id"], **patch)


@pytest.mark.parametrize("chat", [{"permission_mode": "invalid"}, {"permission_mode": []}, {"discussion_only": 1}, None, "full"])
def test_settings_reject_invalid_permissions_without_writing(workspace, chat):
    before = settings_service.read_settings()
    with pytest.raises(InvalidOperationError):
        settings_service.update_settings({"chat": chat})
    assert settings_service.read_settings() == before


def test_invalid_saved_permission_falls_back_to_discussion(workspace):
    project, _, _ = workspace
    settings_service.settings_file().write_text(json.dumps({"chat": {"permission_mode": "obsolete", "discussion_only": False}}), encoding="utf-8")
    session = chats.create_session(project_id=project["id"])
    assert (session["permission_mode"], session["discussion_only"]) == ("ask", True)


@pytest.mark.parametrize("legacy,expected", [
    ({"mode": "read", "auto_apply": True}, ("ask", True)),
    ({"mode": "read"}, ("ask", True)),
    ({"mode": "write", "auto_apply": False}, ("ask", False)),
    ({"mode": "write", "auto_apply": True}, ("auto", False)),
    ({"mode": "write"}, ("ask", False)),
])
def test_legacy_session_api_maps_permissions(workspace, legacy, expected):
    project, _, _ = workspace
    session = chats.create_session(project_id=project["id"], **legacy)
    assert (session["permission_mode"], session["discussion_only"]) == expected


def test_old_permissions_migrate_conservatively_and_idempotently(workspace, tmp_path):
    legacy_db = tmp_path / "legacy.sqlite"
    with sqlite3.connect(legacy_db) as conn:
        conn.execute("CREATE TABLE chat_sessions(id INTEGER PRIMARY KEY,project_id INTEGER,title TEXT,agent TEXT,provider_id TEXT,model_id TEXT,auto_apply INTEGER,mode TEXT,created_at TEXT,updated_at TEXT)")
        conn.executemany("INSERT INTO chat_sessions(id,title,mode,auto_apply) VALUES (?,?,?,?)", [
            (1, "新对话", "read", 1), (2, "原收件箱", "write", 0), (3, "旧直写", "write", 1),
            (4, "未知模式", "other", 1), (5, "未知开关", "write", 7), (6, "空模式", None, None),
        ])
    db.init_db(legacy_db)
    db.init_db(legacy_db)
    with db.get_conn(legacy_db) as conn:
        sessions = [dict(row) for row in conn.execute("SELECT * FROM chat_sessions ORDER BY id")]
        assert [(s["permission_mode"], s["discussion_only"]) for s in sessions] == [
            ("ask", 1), ("ask", 0), ("auto", 0), ("ask", 1), ("ask", 1), ("ask", 1)]
        assert sessions[0]["title_source"] == "default"
        assert all(s["title_source"] == "user" for s in sessions[1:])
        conn.execute("UPDATE chat_sessions SET permission_mode='full',discussion_only=0 WHERE id=1")
    db.init_db(legacy_db)
    with db.get_conn(legacy_db) as conn:
        assert conn.execute("SELECT permission_mode FROM chat_sessions WHERE id=1").fetchone()[0] == "full"


def test_waiting_input_reserves_session_active_slot(workspace):
    project, _, _ = workspace
    session = chats.create_session(project_id=project["id"])
    with db.get_conn() as conn:
        conn.execute("INSERT INTO chat_runs(id,session_id,client_request_id,request,status) VALUES (?,?,?,?,?)", ("waiting", session["id"], "first", "{}", "waiting_input"))
    with pytest.raises(sqlite3.IntegrityError), db.get_conn() as conn:
        conn.execute("INSERT INTO chat_runs(id,session_id,client_request_id,request,status) VALUES (?,?,?,?,?)", ("queued", session["id"], "second", "{}", "queued"))


def test_title_starts_independently_of_reply_and_uses_session_model(workspace, monkeypatch):
    project, _, _ = workspace
    session = chats.create_session(project_id=project["id"], provider_id="chosen-provider", model_id="chosen-model")
    source = "讨论修仙世界搜打撤的代币机制"
    chats._append_message(session["id"], "user", source)
    pending, calls, worker, release = title_job(workspace, monkeypatch, session)
    assert pending["title_status"] in {"pending", "running"}
    assert chats.list_messages(session["id"])[0]["role"] == "user"
    assert len(chats.list_messages(session["id"])) == 1  # No successful main reply is needed.
    call = calls[0]
    assert (call["provider"], call["model"], call["engine"]) == ("chosen-provider", "chosen-model", "direct-api")
    assert call["messages"] == [{"role": "user", "content": source}]
    assert call["max_tokens"] == 256 and call["timeout_seconds"] == 30
    assert call["extra"] == {"max_retries": 0} and "tools" not in call
    release.set()
    worker.join(5)
    final = chats.get_session(session["id"])
    assert (final["title"], final["title_source"], final["title_status"]) == ("修仙世界的代币规则", "ai", "complete")
    assert chats.list_sessions(project["id"])[0]["title"] == final["title"]


def test_manual_title_cannot_be_overwritten_by_late_ai(workspace, monkeypatch):
    project, _, _ = workspace
    session = chats.create_session(project_id=project["id"])
    chats._append_message(session["id"], "user", "确认主角名为李长歌")
    _pending, _calls, worker, release = title_job(workspace, monkeypatch, session)
    chats.update_session(session["id"], {"title": "作者自己命名", "title_source": "ai", "title_status": "running"})
    release.set()
    worker.join(5)
    final = chats.get_session(session["id"])
    assert (final["title"], final["title_source"], final["title_status"], final["title_job_id"]) == ("作者自己命名", "user", "complete", None)


@pytest.mark.parametrize("raises", [False, True])
def test_title_failure_keeps_fallback_and_can_regenerate(workspace, monkeypatch, raises):
    project, _, _ = workspace
    session = chats.create_session(project_id=project["id"])
    chats._append_message(session["id"], "user", "帮我规划前三章")
    pending, _calls, worker, release = title_job(workspace, monkeypatch, session, ok=False, raises=raises)
    release.set()
    worker.join(5)
    final = chats.get_session(session["id"])
    assert final["title_status"] == "failed" and final["title"] == pending["title"]
    monkeypatch.setattr(chats.generation_service, "run_task", lambda **kwargs: {"ok": True, "text": "前三章规划"})
    chats.schedule_title(session["id"], force=True)
    next_worker = chats._title_workers.get((str(config.DB_PATH), session["id"]))
    if next_worker:
        workspace[1].append(next_worker)
        next_worker.join(5)
    assert chats.get_session(session["id"])["title"] == "前三章规划"


def test_restart_ends_orphaned_title_jobs_but_keeps_live_worker(workspace, monkeypatch):
    project, _, _ = workspace
    orphan = chats.create_session(project_id=project["id"])
    live = chats.create_session(project_id=project["id"])
    chats._append_message(live["id"], "user", "写作方向")
    _pending, _calls, worker, release = title_job(workspace, monkeypatch, live)
    with db.get_conn() as conn:
        conn.execute("UPDATE chat_sessions SET title_status='running',title_job_id='orphan' WHERE id=?", (orphan["id"],))
    chats.recover_title_jobs()
    assert chats.get_session(orphan["id"])["title_status"] == "failed"
    assert chats.get_session(orphan["id"])["title_job_id"] is None
    assert chats.get_session(live["id"])["title_status"] == "running"
    release.set()
    worker.join(5)


def test_old_worker_does_not_remove_newer_job_registry(workspace, monkeypatch):
    project, workers, releases = workspace
    session = chats.create_session(project_id=project["id"])
    chats._append_message(session["id"], "user", "初始想法")
    _pending, _calls, old_worker, old_release = title_job(workspace, monkeypatch, session)
    chats.update_session(session["id"], {"title": "改过名字"})
    entered, release = threading.Event(), threading.Event()
    releases.append(release)
    def second(**kwargs):
        entered.set()
        assert release.wait(5)
        return {"ok": True, "text": "再次命名"}
    monkeypatch.setattr(chats.generation_service, "run_task", second)
    chats.schedule_title(session["id"], force=True)
    current_worker = chats._title_workers[(str(config.DB_PATH), session["id"])]
    workers.append(current_worker)
    assert entered.wait(5)
    old_release.set()
    old_worker.join(5)
    assert chats._title_workers[(str(config.DB_PATH), session["id"])] is current_worker
    chats.recover_title_jobs()
    assert chats.get_session(session["id"])["title_status"] == "running"
    release.set()
    current_worker.join(5)
    assert chats.get_session(session["id"])["title"] == "再次命名"


def test_disk_session_recovery_preserves_permissions_native_id_and_delivery(workspace):
    project, _, _ = workspace
    session = chats.create_session(project_id=project["id"], permission_mode="full", discussion_only=False)
    delivered = chats._append_message(session["id"], "user", "主角确认叫李长歌")
    unsent = chats._append_message(session["id"], "user", "现实侧击杀妖魔获得代币", {"failed": True})
    with db.get_conn() as conn:
        conn.execute("UPDATE chat_messages SET dsh_delivered=1 WHERE id=?", (delivered,))
        conn.execute("UPDATE chat_sessions SET dsh_session_id='native-stable-id',dsh_ready=1 WHERE id=?", (session["id"],))
    chats.persist_session(session["id"])
    with db.get_conn() as conn:
        conn.execute("DELETE FROM chat_messages WHERE session_id=?", (session["id"],))
        conn.execute("DELETE FROM chat_sessions WHERE id=?", (session["id"],))
    chats.recover_sessions()
    chats.recover_sessions()
    restored = chats.get_session(session["id"])
    assert restored["permission_mode"] == "full" and restored["dsh_session_id"] == "native-stable-id"
    with db.get_conn() as conn:
        assert [(r["id"], r["dsh_delivered"]) for r in conn.execute("SELECT * FROM chat_messages WHERE session_id=? ORDER BY id", (session["id"],))] == [(delivered, 1), (unsent, 0)]
    assert chats.list_messages(session["id"])[1]["meta"] == {"failed": True}


def test_deleted_session_is_not_restored_by_late_title(workspace, monkeypatch):
    project, _, _ = workspace
    session = chats.create_session(project_id=project["id"])
    chats._append_message(session["id"], "user", "取个名字")
    _pending, _calls, worker, release = title_job(workspace, monkeypatch, session)
    chats.delete_session(session["id"])
    release.set()
    worker.join(5)
    chats.recover_sessions()
    with pytest.raises(NodeNotFoundError):
        chats.get_session(session["id"])
    assert not (config.runtime_dir() / "chat/sessions" / f'{session["id"]}.json').exists()


def test_corrupt_session_archive_does_not_prevent_recovery(workspace):
    project, _, _ = workspace
    session = chats.create_session(project_id=project["id"])
    path = config.runtime_dir() / "chat/sessions/987.json"
    path.write_text(json.dumps({"session": {"id": 987, "not_a_column": 1}, "messages": [{"id": 3, "role": None, "content": "broken"}]}), encoding="utf-8")
    chats.recover_sessions()
    assert chats.get_session(session["id"])["project_id"] == project["id"]
    with pytest.raises(NodeNotFoundError):
        chats.get_session(987)
