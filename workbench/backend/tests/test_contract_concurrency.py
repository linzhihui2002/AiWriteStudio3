"""Concurrent contract edits preserve chapters and reject stale generation."""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from workbench.backend import config, db
from workbench.backend.services import chapter_service, contract_service, generation_service, project_service
from workbench.backend.services.errors import InvalidOperationError


@pytest.fixture()
def book(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "books")
    monkeypatch.setattr(config, "RUNTIME_DIR", tmp_path / ".workbench")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / ".workbench" / "test.db")
    db.init_db()
    config.ensure_runtime_dirs()
    project = project_service.create_project(name="合同并发书")
    ident = project["id"]
    _, root = project_service.get_project_dir(ident)
    chapters = [chapter_service.create_chapter(ident, title)["rel_path"] for title in ("借款", "还款")]
    for rel in chapters:
        contract_service.generate_contract(ident, rel, use_ai=False)
    return SimpleNamespace(id=ident, root=root, first=chapters[0], second=chapters[1])


def _model_result() -> dict:
    return {"ok": True, "text": json.dumps({
        "情节点": ["查清铜钱的来源"], "字数预算": 2800, "钩子类型": "信息",
        "涉及实体": ["沈砚"], "必须承上": ["承接柜台上的账册"], "禁止事项": [],
    }, ensure_ascii=False)}


def _indexed(book, rel: str) -> dict:
    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM chapter_contracts WHERE project_id=? AND rel_path=?",
                           (book.id, rel)).fetchone()
    return dict(row)


def test_update_and_freeze_hold_book_lock_from_read_through_index_sync(book, monkeypatch):
    first_read = threading.Event()
    release_read = threading.Event()
    first_index = threading.Event()
    release_index = threading.Event()
    second_read = threading.Event()
    actual_read = contract_service._read_all
    actual_sync = contract_service._sync_index

    def controlled_read(directory):
        data = actual_read(directory)
        name = threading.current_thread().name
        if name.startswith("contract-first"):
            first_read.set()
            assert release_read.wait(3), "first contract read was never released"
        elif name.startswith("contract-second"):
            second_read.set()
        return data

    def controlled_sync(*args):
        if threading.current_thread().name.startswith("contract-first"):
            first_index.set()
            assert release_index.wait(3), "first contract index write was never released"
        return actual_sync(*args)

    monkeypatch.setattr(contract_service, "_read_all", controlled_read)
    monkeypatch.setattr(contract_service, "_sync_index", controlled_sync)
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="contract-first") as first_pool, \
            ThreadPoolExecutor(max_workers=1, thread_name_prefix="contract-second") as second_pool:
        first = first_pool.submit(contract_service.update_contract, book.id, book.first, {"word_budget": 4300})
        try:
            assert first_read.wait(3)
            second = second_pool.submit(contract_service.freeze_contract, book.id, book.second)
            assert not second_read.wait(0.15), "freeze read slipped between another chapter's read and write"
            release_read.set()
            assert first_index.wait(3)
            assert not second_read.wait(0.15), "book lock ended before the chapter index was synchronized"
        finally:
            release_read.set()
            release_index.set()
        assert first.result(timeout=3)["word_budget"] == 4300
        assert second.result(timeout=3)["status"] == "frozen"
    assert contract_service.get_contract(book.id, book.first)["word_budget"] == 4300
    assert contract_service.get_contract(book.id, book.second)["status"] == "frozen"
    assert _indexed(book, book.first)["word_budget"] == 4300
    assert _indexed(book, book.second)["status"] == "frozen"


def test_generation_runs_outside_lock_and_preserves_other_chapter_edits(book, monkeypatch):
    with ThreadPoolExecutor(max_workers=1) as pool:
        def fake_model(**_kwargs):
            # Waiting for another thread's edit also proves generation does not
            # hold the book lock across its potentially long model request.
            other = pool.submit(contract_service.update_contract, book.id, book.second,
                                {"word_budget": 4700, "must_connect": ["作者补充的承上点"]})
            assert other.result(timeout=3)["word_budget"] == 4700
            return _model_result()

        monkeypatch.setattr(generation_service, "run_task", fake_model)
        generated = contract_service.generate_contract(book.id, book.first, use_ai=True)
    assert generated["word_budget"] == 2800
    other = contract_service.get_contract(book.id, book.second)
    assert other["word_budget"] == 4700 and other["must_connect"] == ["作者补充的承上点"]
    assert _indexed(book, book.first)["word_budget"] == 2800
    assert _indexed(book, book.second)["word_budget"] == 4700


@pytest.mark.parametrize("mutation", ["update", "freeze"])
def test_generation_rejects_same_chapter_changes_without_overwriting_them(book, monkeypatch, mutation):
    with ThreadPoolExecutor(max_workers=1) as pool:
        def fake_model(**_kwargs):
            if mutation == "update":
                future = pool.submit(contract_service.update_contract, book.id, book.first,
                                     {"word_budget": 4600, "plot_points": ["作者刚确认的新情节点"]})
            else:
                future = pool.submit(contract_service.freeze_contract, book.id, book.first)
            future.result(timeout=3)
            return _model_result()

        monkeypatch.setattr(generation_service, "run_task", fake_model)
        with pytest.raises(InvalidOperationError, match="生成期间已变化"):
            contract_service.generate_contract(book.id, book.first, use_ai=True)
    current = contract_service.get_contract(book.id, book.first)
    if mutation == "update":
        assert current["word_budget"] == 4600 and current["plot_points"] == ["作者刚确认的新情节点"]
        assert _indexed(book, book.first)["word_budget"] == 4600
    else:
        assert current["status"] == "frozen" and _indexed(book, book.first)["status"] == "frozen"
    assert current["source"] == "fallback"


def test_generation_detects_concurrent_first_creation_of_target_contract(book, monkeypatch):
    third = chapter_service.create_chapter(book.id, "新章")["rel_path"]
    with ThreadPoolExecutor(max_workers=1) as pool:
        def fake_model(**_kwargs):
            created = pool.submit(contract_service.generate_contract, book.id, third, use_ai=False,
                                  word_budget=4200)
            assert created.result(timeout=3)["word_budget"] == 4200
            return _model_result()

        monkeypatch.setattr(generation_service, "run_task", fake_model)
        with pytest.raises(InvalidOperationError, match="生成期间已变化"):
            contract_service.generate_contract(book.id, third, use_ai=True)
    assert contract_service.get_contract(book.id, third)["word_budget"] == 4200
    assert _indexed(book, third)["word_budget"] == 4200

