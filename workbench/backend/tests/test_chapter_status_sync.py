"""Real chapter entrypoints and background knowledge workers, without models."""
from __future__ import annotations

from threading import Event, current_thread
from types import SimpleNamespace

import pytest

from workbench.backend import config, db
from workbench.backend.services import (
    chapter_service as chapters, generation_service, knowledge_candidate_service,
    knowledge_service as kb, knowledge_store, project_service, vector_service,
)


def settle(project_id):
    while True:
        with kb._lock:
            future = kb._running.get(project_id)
        if future is None:
            break
        future.result(timeout=20)
    assert kb.overview(project_id)["job"]["status"] == "done"


@pytest.fixture()
def book(tmp_path, monkeypatch):
    kb.shutdown(wait=True)
    knowledge_candidate_service.shutdown()
    for name, value in (("PROJECT_ROOT", tmp_path), ("PROJECTS_DIR", tmp_path / "projects"),
                        ("RUNTIME_DIR", tmp_path / ".workbench"),
                        ("DB_PATH", tmp_path / ".workbench/test.db")):
        monkeypatch.setattr(config, name, value)
    db.init_db()
    config.ensure_runtime_dirs()
    template = tmp_path / "template"
    for folder in ("章节", "状态", "设定", "大纲"):
        (template / folder).mkdir(parents=True)
    monkeypatch.setattr(knowledge_candidate_service, "enqueue_analysis", lambda *a, **k: None)
    monkeypatch.setattr(generation_service, "run_task", lambda **k: pytest.fail("unexpected paid generation"))
    monkeypatch.setattr(vector_service, "resolve_embedder", lambda *a: pytest.fail("disabled indexing must not resolve models"))
    project = project_service.create_project("状态同步", template=template)
    root = project_service.get_project_dir(project["id"])[1]
    chapter = chapters.create_chapter(project["id"], "鱼符旧诺")
    return SimpleNamespace(id=project["id"], root=root, rel=chapter["rel_path"], template=template)


@pytest.mark.parametrize("status", ["完成", "发表"])
@pytest.mark.parametrize("entry", ["save", "metadata"])
def test_final_status_builds_real_knowledge_without_opt_in(book, entry, status):
    body = "沈砚把青铜鱼符交给柳青，约定次日到南码头见面。"
    chapters.save_chapter(book.id, book.rel, body)
    settle(book.id)
    assert not kb.search(book.id, "青铜鱼符", mode="keyword")["hits"]
    if entry == "save":
        chapters.save_chapter(book.id, book.rel, body, status=status)
    else:
        chapters.update_chapter_meta(book.id, book.rel, status=status)
    settle(book.id)
    overview = kb.overview(book.id)
    doc = next(doc for doc in overview["documents"] if doc["rel_path"] == book.rel)
    assert doc["status"] == status and doc["index_status"] == "indexed"
    assert doc["chunk_count"] > 0
    assert kb.graph(book.id)["nodes"]
    hits = kb.search(book.id, "青铜鱼符")["hits"]
    assert hits and hits[0]["status"] == status
    assert hits[0]["text"] == body[hits[0]["start"]:hits[0]["end"]]
    with knowledge_store.connect(book.id) as conn:
        assert conn.execute("SELECT COUNT(*) FROM kb_chunks WHERE embedding IS NOT NULL").fetchone()[0] == 0
    assert not kb.auto_enabled(book.id)
    assert not knowledge_store.preference(book.id, "knowledge_enabled", False)


def test_demotion_removes_evidence_before_background_finishes(book, monkeypatch):
    chapters.save_chapter(book.id, book.rel, "沈砚收起青铜鱼符。", status="发表")
    settle(book.id)
    old = kb.search(book.id, "青铜鱼符", mode="keyword")["hits"][0]
    entered, release = Event(), Event()
    original = kb._perform_sync

    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return original(*args, **kwargs)

    monkeypatch.setattr(kb, "_perform_sync", blocked)
    try:
        chapters.update_chapter_meta(book.id, book.rel, status="草稿")
        assert entered.wait(5)
        assert not kb.search(book.id, "青铜鱼符", mode="keyword")["hits"]
        assert not kb.graph(book.id)["nodes"]
        with pytest.raises(Exception, match="失效"):
            kb.evidence(book.id, old["id"])
    finally:
        release.set()
    settle(book.id)
    with knowledge_store.connect(book.id) as conn:
        assert not conn.execute("SELECT 1 FROM kb_documents WHERE rel_path=?", (book.rel,)).fetchone()


def test_completed_edit_and_title_change_refresh_material(book):
    chapters.save_chapter(book.id, book.rel, "沈砚保管青铜鱼符。", status="完成")
    settle(book.id)
    chapters.save_chapter(book.id, book.rel, "柳青在茶园收到白玉书信。")
    settle(book.id)
    assert not kb.search(book.id, "青铜鱼符", mode="keyword")["hits"]
    assert kb.search(book.id, "白玉书信", mode="keyword")["hits"]
    chapters.update_chapter_meta(book.id, book.rel, title="茶园来信")
    settle(book.id)
    with knowledge_store.connect(book.id) as conn:
        assert conn.execute("SELECT title FROM kb_documents WHERE rel_path=?", (book.rel,)).fetchone()[0] == "茶园来信"
    assert chapters.read_chapter(book.id, book.rel)["status"] == "完成"


def test_automatic_history_is_isolated_between_books(book):
    other = project_service.create_project("隔壁书", template=book.template)
    second = chapters.create_chapter(other["id"], "另一本")
    chapters.save_chapter(book.id, book.rel, "沈砚保管青铜鱼符。", status="完成")
    chapters.save_chapter(other["id"], second["rel_path"], "柳青收到白玉书信。", status="发表")
    settle(book.id)
    settle(other["id"])
    assert kb.search(book.id, "青铜鱼符", mode="keyword")["hits"]
    assert not kb.search(other["id"], "青铜鱼符", mode="keyword")["hits"]
    assert knowledge_store.identity(book.id)["db_path"] != knowledge_store.identity(other["id"])["db_path"]


def test_disabled_book_open_indexes_external_finalized_edit(book):
    from workbench.backend.api.projects import get_project
    chapters.save_chapter(book.id, book.rel, "沈砚保管青铜鱼符。", status="完成")
    settle(book.id)
    (book.root / book.rel).write_text("柳青在茶园收到白玉书信。", encoding="utf-8")
    get_project(book.id)
    settle(book.id)
    assert kb.search(book.id, "白玉书信", mode="keyword")["hits"]
    assert not kb.search(book.id, "青铜鱼符", mode="keyword")["hits"]
    assert not kb.auto_enabled(book.id)


def test_recover_restarts_disabled_books_unfinished_lexical_job(book):
    chapters.save_chapter(book.id, book.rel, "沈砚保管青铜鱼符。", status="完成")
    settle(book.id)
    kb.invalidate(book.id, book.rel)
    with knowledge_store.connect(book.id) as conn:
        conn.execute("INSERT INTO kb_jobs(id,status) VALUES ('unfinished','running')")
    kb.recover()
    settle(book.id)
    assert kb.search(book.id, "青铜鱼符", mode="keyword")["hits"]
    assert not kb.auto_enabled(book.id)
    with knowledge_store.connect(book.id) as conn:
        assert conn.execute("SELECT status FROM kb_jobs WHERE id='unfinished'").fetchone()[0] == "interrupted"


@pytest.mark.parametrize("preference", ["knowledge", "vector"])
def test_explicit_opt_in_keeps_semantic_sync(book, monkeypatch, preference):
    embedded = []

    class FakeEmbedder:
        fingerprint = "status-test-embedding"

        def info(self):
            return {"name": "fake", "ready": True, "offline": True}

        def embed_documents(self, texts):
            embedded.extend(texts)
            return [[1.0, 0.0] for _ in texts]

    monkeypatch.setattr(vector_service, "resolve_embedder", lambda *a: FakeEmbedder())
    if preference == "knowledge":
        knowledge_store.set_preference(book.id, "knowledge_enabled", True)
    else:
        knowledge_store.set_preference(book.id, "vector", {"enabled": True, "mode": "local"})
    chapters.save_chapter(book.id, book.rel, "沈砚保管青铜鱼符。", status="完成")
    settle(book.id)
    assert embedded
    with knowledge_store.connect(book.id) as conn:
        assert conn.execute("SELECT COUNT(*) FROM kb_chunks WHERE embedding IS NOT NULL").fetchone()[0] > 0


def test_status_change_at_worker_close_is_not_lost(book, monkeypatch):
    chapters.save_chapter(book.id, book.rel, "沈砚保管青铜鱼符。")
    settle(book.id)
    original_lock = kb._lock
    close_pending, updated, update_finished = Event(), Event(), Event()

    class CloseAwareDirty(set):
        def discard(self, project_id):
            super().discard(project_id)
            if project_id == book.id and current_thread().name.startswith("story-knowledge") and not updated.is_set():
                close_pending.set()

    class CloseAwareLock:
        def __enter__(self):
            original_lock.acquire()
            return self

        def __exit__(self, *args):
            original_lock.release()
            if close_pending.is_set() and not updated.is_set():
                close_pending.clear()
                updated.set()
                # This is precisely after a worker decides no more work is
                # queued and before its finally block releases its registration.
                chapters.update_chapter_meta(book.id, book.rel, status="完成")
                update_finished.set()

    monkeypatch.setattr(kb, "_dirty", CloseAwareDirty(kb._dirty))
    monkeypatch.setattr(kb, "_lock", CloseAwareLock())
    kb.enqueue(book.id)
    assert update_finished.wait(5)
    settle(book.id)
    assert updated.is_set()
    with knowledge_store.connect(book.id) as conn:
        assert conn.execute("SELECT status,chunk_count FROM kb_documents WHERE rel_path=?", (book.rel,)).fetchone()["status"] == "完成"
    assert kb.search(book.id, "青铜鱼符", mode="keyword")["hits"]
