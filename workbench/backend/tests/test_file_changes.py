"""Real filesystem and SQLite checks for the chat workspace change boundary."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from workbench.backend import config, db
from workbench.backend.services import (
    chapter_service, chat_workspace_tools as tools, file_change_service as changes,
    project_service, proposal_service, review_service, snapshot_service,
)
from workbench.backend.services.errors import InvalidOperationError, ServiceError


BODY = (
    "码头上的风带着咸味。老周把缆绳在桩子上绕了两圈，手背上的旧伤被磨得发亮。\n"
    "“今夜潮水涨得早。”他抬头看了一眼天。\n"
    "沈砚没答话，他从怀里摸出半块干饼，掰了一角递过去。"
    "“你上次说，北边那批货晚三天到。”\n"
    "“晚三天。”老周接了饼，没吃，捏在手里，“船老大的儿子病了，"
    "耽误了两日。第三日是逆风。”\n"
) * 30


@pytest.fixture()
def book(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "books")
    monkeypatch.setattr(config, "RUNTIME_DIR", tmp_path / ".workbench")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / ".workbench" / "test.db")
    db.init_db()
    config.ensure_runtime_dirs()
    p = project_service.create_project(name="测试小说")
    _, root = project_service.get_project_dir(p["id"])
    ctx = {"project_id": p["id"], "session_id": 1, "run_id": "run-one",
           "tool_call_id": "call-one", "auto_apply": True, "read_only": False,
           "read_hashes": {}, "change_ids": [], "proposals": []}
    return SimpleNamespace(id=p["id"], root=root, ctx=ctx, parent=tmp_path)


def apply(book, path="备忘录/测试.md", text="旧稿", *, call="one", operation="create", **kwargs):
    return changes.apply_change(book.id, run_id="run-one", session_id=1, tool_call_id=call,
                                operation=operation, rel_path=path, content=text, **kwargs)


def test_create_rewrite_replace_and_reverse_undo(book):
    first = apply(book, text="一行。\n原文。\n")
    second = apply(book, text="一行。\n修订。\n", call="two", operation="rewrite",
                   expected_hash=first["after_hash"])
    third = apply(book, text=None, call="three", operation="replace_exact",
                  expected_hash=second["after_hash"], old_text="修订。", new_text="定稿。")
    assert any(line["type"] == "add" for line in third["diff"])
    assert changes.revert("run-one", project_id=book.id)["reverted"] == 3
    assert not (book.root / first["path"]).exists()
    assert all(item["status"] == "reverted" for item in changes.list_changes("run-one"))
    assert changes.revert("run-one", project_id=book.id)["reverted"] == 0


@pytest.mark.parametrize("kind", ["is_symlink", "is_junction"])
def test_registered_book_root_cannot_be_a_link(book, monkeypatch, kind):
    # The root must be checked before resolve() hides its link identity. The
    # same guard applies to Windows junctions, which need no symlink privilege.
    path_type = type(book.root)
    original = getattr(path_type, kind, lambda _path: False)
    monkeypatch.setattr(path_type, kind,
                        lambda path: path == book.root or original(path), raising=False)
    result = tools.execute("create_file", {"rel_path": "备忘录/越界.md", "content": "不写入"}, book.ctx)
    assert not result["ok"] and "链接" in result["summary"]
    assert not (book.root / "备忘录/越界.md").exists()
    assert changes.list_changes("run-one") == []


def test_new_chapter_real_gates_title_and_replay(book):
    args = {"title": "码头夜话", "content": BODY}
    result = tools.execute("new_chapter", args, book.ctx)
    assert result["ok"], result
    replay = tools.execute("new_chapter", args, book.ctx)
    assert replay["ok"], replay
    assert len(changes.list_changes("run-one")) == 1
    chapter = chapter_service.read_chapter(book.id, result["data"]["path"])
    assert chapter["title"] == "码头夜话"
    assert chapter["meta"]["状态"] == "草稿"
    assert chapter["hash"] == result["data"]["after_hash"]
    assert len(chapter_service.list_chapters(book.id)) == 1
    changes.revert("run-one", project_id=book.id)
    assert chapter_service.list_chapters(book.id) == []


def test_rejected_chapter_is_retained_without_writing(book):
    result = tools.execute("new_chapter", {"title": "失败稿", "content": "突然来了。"}, book.ctx)
    assert not result["ok"] and result["code"] == "gate_rejected"
    assert result["candidate_retained"]
    assert chapter_service.list_chapters(book.id) == []
    candidate = changes.list_changes("run-one")[0]
    assert candidate["status"] == "rejected"
    assert "突然来了。" in candidate["after_content"]
    assert not candidate["can_revert"]
    again = tools.execute("new_chapter", {"title": "失败稿", "content": "突然来了。"}, book.ctx)
    assert not again["ok"] and again["change_id"] == result["change_id"]


def test_gate_rejection_keeps_existing_chapter_unchanged(book):
    first = apply(book, "章节/第0001章.md", BODY, title="开端")
    original = (book.root / first["path"]).read_text(encoding="utf-8")
    with pytest.raises(changes.GateRejectedError):
        apply(book, first["path"], "短稿", call="two", operation="rewrite",
              expected_hash=first["after_hash"])
    assert (book.root / first["path"]).read_text(encoding="utf-8") == original


def test_hash_prevents_stale_ai_and_manual_saves(book):
    chapter = chapter_service.create_chapter(book.id, "标题")
    original = chapter_service.save_chapter(book.id, chapter["rel_path"], "未完成稿",
                                           expected_hash=chapter["hash"])
    assert original["hash"] != chapter["hash"]
    with pytest.raises(changes.FileConflictError):
        chapter_service.save_chapter(book.id, chapter["rel_path"], "过时编辑器",
                                     expected_hash=chapter["hash"])
    with pytest.raises(changes.FileConflictError):
        apply(book, chapter["rel_path"], BODY, operation="rewrite", expected_hash=chapter["hash"])
    assert chapter_service.read_chapter(book.id, chapter["rel_path"])["body"] == original["body"]


def test_manual_nonchapter_edit_preserves_document_kind(book):
    path = "设定/人物设定.md"
    before = chapter_service.read_chapter(book.id, path)
    saved = chapter_service.save_chapter(book.id, path, "# 人物\n甲是船夫。\n", "草稿",
                                         expected_hash=before["hash"])
    assert "状态" not in saved["meta"]
    assert "合同ID" not in saved["meta"]
    assert "甲是船夫" in saved["body"]


def test_same_version_two_writers_only_one_wins(book):
    first = apply(book)
    def write(which):
        try:
            apply(book, text=which, call=which, operation="rewrite", expected_hash=first["after_hash"])
            return "success"
        except changes.FileConflictError:
            return "conflict"
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(write, ["甲稿", "乙稿"]))
    assert sorted(results) == ["conflict", "success"]


def test_tool_latest_hash_replay_and_explicit_stale_hash(book):
    first = apply(book)
    rel = first["path"]
    assert tools.execute("read_file", {"rel_path": rel}, book.ctx)["ok"]
    args = {"rel_path": rel, "old_text": "旧稿", "new_text": "第二稿"}
    result = tools.execute("replace_text", args, book.ctx)
    assert result["ok"], result
    assert tools.execute("replace_text", args, book.ctx)["ok"]
    book.ctx["tool_call_id"] = "next"
    result = tools.execute("replace_text", {"rel_path": rel, "old_text": "第二稿", "new_text": "第三稿"}, book.ctx)
    assert result["ok"]
    book.ctx["tool_call_id"] = "stale"
    result = tools.execute("write_file", {"rel_path": rel, "content": "错误版本", "expected_hash": first["after_hash"]}, book.ctx)
    assert not result["ok"] and result["code"] == "file_conflict"


def test_move_delete_and_undo_restore_original_file(book):
    first = apply(book)
    moved = apply(book, first["path"], None, call="move", operation="move",
                  destination="自建目录/迁移.md", expected_hash=first["after_hash"])
    removed = apply(book, "自建目录/迁移.md", None, call="delete", operation="delete",
                    expected_hash=moved["after_hash"])
    assert removed["trash_rel"]
    assert not (book.root / "自建目录/迁移.md").exists()
    result = changes.revert("run-one", [moved["id"], removed["id"]], project_id=book.id)
    assert result["reverted"] == 2
    assert (book.root / first["path"]).read_text(encoding="utf-8") == "旧稿"
    assert not (book.root / "自建目录/迁移.md").exists()


def test_undo_checks_entire_batch_before_touching_files(book):
    first = apply(book, "备忘录/甲.md", "甲")
    second = apply(book, "备忘录/乙.md", "乙", call="two")
    (book.root / first["path"]).write_text("作者后改", encoding="utf-8")
    with pytest.raises(changes.FileConflictError):
        changes.revert("run-one", project_id=book.id)
    assert (book.root / second["path"]).read_text(encoding="utf-8") == "乙"
    assert (book.root / first["path"]).read_text(encoding="utf-8") == "作者后改"


def test_undo_does_not_depend_on_pruned_snapshots(book):
    first = apply(book)
    second = apply(book, text="修订", call="two", operation="rewrite", expected_hash=first["after_hash"])
    snapshot_service.clear_snapshots(book.id)
    changes.revert("run-one", [second["id"]], project_id=book.id)
    assert (book.root / first["path"]).read_text(encoding="utf-8") == "旧稿"


@pytest.mark.parametrize("path", ["../越界.md", "/绝对.md", "C:/越界.md", ".meta/记录.md",
                                  "设定/.secret.md", "设定/文件.txt", "章节/乱名.md", "project.md/../越界.md"])
def test_paths_are_scoped_and_hidden_files_protected(book, path):
    with pytest.raises(ServiceError):
        apply(book, path, "不应写入")


def test_readonly_rejects_all_write_tools_and_allows_reads(book):
    book.ctx["read_only"] = True
    result = tools.execute("create_file", {"rel_path": "备忘录/不写.md", "content": "内容"}, book.ctx)
    assert not result["ok"]
    assert changes.list_changes("run-one") == []
    assert tools.execute("list_files", {}, book.ctx)["ok"]
    assert all(not spec["write"] for spec in tools.list_specs(read_only=True))
    with pytest.raises(InvalidOperationError):
        apply(book, read_only=True)


def test_exact_replace_requires_one_nonempty_match(book):
    first = apply(book, text="同句。同句。")
    with pytest.raises(InvalidOperationError):
        apply(book, text=None, call="two", operation="replace_exact", expected_hash=first["after_hash"],
              old_text="同句", new_text="改变")
    assert (book.root / first["path"]).read_text(encoding="utf-8") == "同句。同句。"


def test_cancel_before_commit_keeps_file_and_journal_consistent(book):
    calls = iter([False, True])
    with pytest.raises(InvalidOperationError):
        apply(book, should_cancel=lambda: next(calls))
    assert not (book.root / "备忘录/测试.md").exists()
    assert changes.list_changes("run-one")[0]["status"] == "failed"


def test_recover_interrupted_commit_without_overwriting(book):
    first = apply(book)
    with db.get_conn() as conn:
        conn.execute("UPDATE workspace_changes SET status='applying' WHERE id=?", (first["id"],))
    assert changes.recover_pending()[0]["status"] == "applied"
    with db.get_conn() as conn:
        conn.execute("UPDATE workspace_changes SET status='applying' WHERE id=?", (first["id"],))
    (book.root / first["path"]).write_text("外部编辑", encoding="utf-8")
    assert changes.recover_pending()[0]["status"] == "conflict"
    assert (book.root / first["path"]).read_text(encoding="utf-8") == "外部编辑"


def test_pending_proposal_new_chapter_applies_and_stale_proposal_rejected(book):
    proposal = proposal_service.create_proposal(project_id=book.id, kind="chapter_draft", title="新章",
        target_path="章节/第0001章.md", content=BODY, meta={"new": True, "title": "标题保留"})
    result = proposal_service.apply_proposal(proposal["id"])
    assert chapter_service.read_chapter(book.id, "章节/第0001章.md")["title"] == "标题保留"
    assert result["change"]["status"] == "applied"
    proposal = proposal_service.create_proposal(project_id=book.id, kind="setting", title="旧建议",
        target_path="设定/人物设定.md", content="新设定")
    (book.root / "设定/人物设定.md").write_text("作者已修改", encoding="utf-8")
    with pytest.raises(changes.FileConflictError):
        proposal_service.apply_proposal(proposal["id"])
    assert proposal_service.get_proposal(proposal["id"])["status"] == "pending"


def test_legacy_review_before_apply_keeps_disk_untouched(book):
    book.ctx["auto_apply"] = False
    result = tools.execute("create_file", {"rel_path": "备忘录/建议.md", "content": "建议内容"}, book.ctx)
    assert result["ok"] and not result["applied"]
    assert not (book.root / "备忘录/建议.md").exists()
    proposal_service.apply_proposal(result["proposal_ids"][0])
    assert (book.root / "备忘录/建议.md").read_text(encoding="utf-8") == "建议内容"


def test_gate_counts_han_only_and_bounds_whitelist_to_book(book):
    text = "甲" * 1999 + "，。！？" * 100
    result = review_service.run_hard_gates(text)
    assert result["words"] == 1999 and "字数门" in result["blocking_gates"]
    (book.root.parent / ".deslop-whitelist").write_text("Alpha\n", encoding="utf-8")
    result = review_service.run_hard_gates("他说，Alpha。", project_dir=book.root, rel_path="章节/第0001章.md")
    assert "语言门" in result["blocking_gates"]
    (book.root / ".deslop-whitelist").write_text("Alpha\n", encoding="utf-8")
    result = review_service.run_hard_gates("他说，Alpha。", project_dir=book.root, rel_path="章节/第0001章.md")
    assert "语言门" not in result["blocking_gates"]


def test_snapshot_restore_checks_project_and_expected_hash(book):
    chapter = chapter_service.create_chapter(book.id)
    saved = chapter_service.save_chapter(book.id, chapter["rel_path"], "初稿")
    chapter_service.save_chapter(book.id, chapter["rel_path"], "二稿")
    snapshot = snapshot_service.list_snapshots(book.id)[0]
    other = project_service.create_project(name="另一本书")
    with pytest.raises(InvalidOperationError):
        snapshot_service.restore_snapshot(snapshot["id"], project_id=other["id"])
    with pytest.raises(changes.FileConflictError):
        snapshot_service.restore_snapshot(snapshot["id"], project_id=book.id, expected_hash=saved["hash"])


def test_run_binding_and_duplicate_call_parameters_are_protected(book):
    first = apply(book)
    replay = apply(book)
    assert replay["id"] == first["id"]
    with pytest.raises(InvalidOperationError):
        apply(book, text="参数变化")
    other = project_service.create_project(name="另一本")
    with pytest.raises(InvalidOperationError):
        changes.apply_change(other["id"], run_id="run-one", session_id=1, tool_call_id="other",
                             operation="create", rel_path="备忘录/新.md", content="内容")
