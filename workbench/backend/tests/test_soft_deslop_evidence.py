"""Soft editing suggestions must be grounded in one isolated manuscript version."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from workbench.backend import config, db
from workbench.backend.services import (
    chapter_service,
    context_service,
    contract_service,
    generation_service,
    knowledge_service,
    project_service,
    prompt_registry_service,
    proposal_service,
    review_service,
    style_service,
)


BODY = (
    "陆衡把六枚铜钱推到桌边。\n"
    "“我只问这趟船的钱。”\n"
    "船工拽住木箱，没松手。"
)
ORIGINAL = "陆衡把六枚铜钱推到桌边。"
REPLACEMENT = "陆衡数出六枚铜钱，推到桌边。"


@pytest.fixture()
def book(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setattr(config, "RUNTIME_DIR", tmp_path / ".workbench")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / ".workbench/test.db")
    db.init_db()
    config.ensure_runtime_dirs()
    prompt_registry_service.ensure_registry()
    monkeypatch.setattr(knowledge_service, "enqueue", lambda *args, **kwargs: None)
    monkeypatch.setattr(knowledge_service, "search", lambda *args, **kwargs: {
        "hits": [], "degradation": []})

    def no_real_model(*args, **kwargs):
        raise AssertionError("This fixture never authorizes a real model call")

    monkeypatch.setattr(generation_service, "run_task", no_real_model)
    template = tmp_path / "template"
    for directory in ("章节", "设定", "大纲", "状态"):
        (template / directory).mkdir(parents=True)
    project = project_service.create_project("软审证据隔离书", template=template)
    _, root = project_service.get_project_dir(project["id"])
    chapter = chapter_service.create_chapter(project["id"], "船资")
    path = root / chapter["rel_path"]
    path.write_text(BODY, encoding="utf-8")
    return SimpleNamespace(id=project["id"], root=root, rel=chapter["rel_path"], path=path)


def _suggestion(**changes) -> dict:
    return {"行": 1, "问题": "解释重复", "原文": ORIGINAL,
            "建议": REPLACEMENT, **changes}


def _model(monkeypatch: pytest.MonkeyPatch, payload, *, during=None) -> list[dict]:
    calls = []

    def run(**kwargs):
        calls.append(kwargs)
        if during:
            during()
        return {"ok": True, "text": json.dumps(payload, ensure_ascii=False)}

    monkeypatch.setattr(generation_service, "run_task", run)
    return calls


def _assert_rejected(result: dict, count: int = 1) -> None:
    assert result["suggestions"] == []
    assert result["proposal_ids"] == []
    assert isinstance(result["rejected_suggestions"], list)
    assert len(result["rejected_suggestions"]) == count
    assert all(isinstance(item, dict) for item in result["rejected_suggestions"])


@pytest.mark.parametrize("line", [1, "1", "01"])
def test_valid_line_numbers_produce_version_bound_read_only_suggestions(book, monkeypatch, line):
    calls = _model(monkeypatch, {"建议": [_suggestion(**{"行": line})]})
    before = book.path.read_text(encoding="utf-8")
    digest = hashlib.sha256(before.encode("utf-8")).hexdigest()
    result = review_service.soft_deslop(book.id, book.rel)
    assert result["ai_used"] is True and result["ai_error"] == ""
    assert result["source_changed"] is False
    assert result["candidate_hash"] == digest
    assert result["rejected_suggestions"] == []
    assert result["suggestions"][0]["line"] == 1
    assert result["suggestions"][0]["original"] == ORIGINAL
    assert result["suggestions"][0]["replacement"] == REPLACEMENT
    assert len(result["proposal_ids"]) == 1 and len(calls) == 1
    proposal = proposal_service.get_proposal(result["proposal_ids"][0])
    assert proposal["base_hash"] == digest
    assert proposal["kind"] == "deslop" and proposal["status"] == "pending"
    with db.get_conn() as conn:
        stored = conn.execute("SELECT payload FROM proposals WHERE id = ?",
                              (proposal["id"],)).fetchone()
    assert json.loads(stored["payload"])["base"] == before
    assert book.path.read_text(encoding="utf-8") == before


@pytest.mark.parametrize("line", [
    True, False, 1.0, -1, 0, 4, "-1", "1.0", " 1 ", "第一行", None, [], {},
])
def test_invalid_line_numbers_are_reported_without_proposals(book, monkeypatch, line):
    _model(monkeypatch, {"建议": [_suggestion(**{"行": line})]})
    result = review_service.soft_deslop(book.id, book.rel)
    _assert_rejected(result)
    assert proposal_service.list_proposals(book.id) == []
    assert book.path.read_text(encoding="utf-8") == BODY


@pytest.mark.parametrize("changes", [
    {"原文": ""}, {"原文": None}, {"原文": 123},
    {"原文": "船工拽住木箱，没松手。"},
    {"原文": "这是不存在的解释句。"},
    {"原文": ORIGINAL + "\n“我只问这趟船的钱。”"},
    {"原文": ORIGINAL + "\r"},
    {"建议": ""}, {"建议": None}, {"建议": 123},
    {"建议": ORIGINAL}, {"建议": REPLACEMENT + "\n新的一行。"},
    {"建议": REPLACEMENT + "\r新的一行。"},
])
def test_invalid_quote_or_replacement_cannot_become_an_edit(book, monkeypatch, changes):
    _model(monkeypatch, {"建议": [_suggestion(**changes)]})
    result = review_service.soft_deslop(book.id, book.rel)
    _assert_rejected(result)
    assert proposal_service.list_proposals(book.id) == []


def test_repeated_quote_within_requested_line_is_ambiguous(book, monkeypatch):
    book.path.write_text("船工摸了摸木箱，又把木箱往身后挪。", encoding="utf-8")
    _model(monkeypatch, {"建议": [_suggestion(**{"原文": "木箱", "建议": "箱子"})]})
    _assert_rejected(review_service.soft_deslop(book.id, book.rel))


def test_quote_unique_in_requested_line_but_repeated_elsewhere_is_rejected(book, monkeypatch):
    raw = BODY + "\n" + ORIGINAL
    book.path.write_text(raw, encoding="utf-8")
    _model(monkeypatch, {"建议": [_suggestion()]})
    result = review_service.soft_deslop(book.id, book.rel)
    _assert_rejected(result)
    assert proposal_service.list_proposals(book.id) == []
    assert book.path.read_text(encoding="utf-8") == raw


def test_quote_repeated_in_frontmatter_cannot_become_full_file_replacement(book, monkeypatch):
    raw = "---\n作者备注: " + ORIGINAL + "\n---\n" + BODY
    book.path.write_text(raw, encoding="utf-8")
    _model(monkeypatch, {"建议": [_suggestion()]})
    result = review_service.soft_deslop(book.id, book.rel)
    _assert_rejected(result)
    assert proposal_service.list_proposals(book.id) == []
    assert book.path.read_text(encoding="utf-8") == raw


def test_duplicate_and_overlapping_quotes_keep_first_suggestion(book, monkeypatch):
    first = _suggestion(**{"原文": "六枚铜钱", "建议": "六枚钱"})
    duplicate = _suggestion(**{"原文": "六枚铜钱", "建议": "六个铜钱"})
    overlap = _suggestion(**{"原文": "把六枚铜钱推到", "建议": "将六枚钱推到"})
    independent = _suggestion(**{"行": 3, "原文": "没松手", "建议": "手没松开"})
    _model(monkeypatch, {"建议": [first, duplicate, overlap, independent]})
    result = review_service.soft_deslop(book.id, book.rel)
    assert [(item["original"], item["replacement"]) for item in result["suggestions"]] == [
        ("六枚铜钱", "六枚钱"), ("没松手", "手没松开")]
    assert len(result["rejected_suggestions"]) == 2
    assert len(result["proposal_ids"]) == 2
    assert book.path.read_text(encoding="utf-8") == BODY


def test_malformed_entries_do_not_hide_later_valid_suggestion(book, monkeypatch):
    _model(monkeypatch, {"建议": [None, "一句建议", 123, [], _suggestion()]})
    result = review_service.soft_deslop(book.id, book.rel)
    assert len(result["rejected_suggestions"]) == 4
    assert len(result["suggestions"]) == len(result["proposal_ids"]) == 1


@pytest.mark.parametrize("payload", [
    {"建议": {}}, {"建议": "文字建议"}, {"建议": None}, {}, ["不是审稿对象"],
])
def test_suggestions_schema_must_be_a_list(book, monkeypatch, payload):
    _model(monkeypatch, payload)
    result = review_service.soft_deslop(book.id, book.rel)
    assert result["ai_used"] is False and result["ai_error"]
    assert result["suggestions"] == [] and result["proposal_ids"] == []


@pytest.mark.parametrize("failure", ["failed", "exception", "invalid_json"])
def test_model_failure_is_visible_and_preserves_style_comparison(book, monkeypatch, failure):
    comparison = {"reference": {"dialog_ratio": 0.3}, "observations": ["人工参照"]}
    monkeypatch.setattr(style_service, "compare", lambda *args, **kwargs: comparison)

    def run(**kwargs):
        if failure == "exception":
            raise RuntimeError("isolated provider failure")
        if failure == "invalid_json":
            return {"ok": True, "text": "审稿文本不是 JSON"}
        return {"ok": False, "text": "", "error": "isolated provider failure"}

    monkeypatch.setattr(generation_service, "run_task", run)
    result = review_service.soft_deslop(book.id, book.rel)
    assert result["ai_used"] is False and isinstance(result["ai_error"], str)
    assert result["ai_error"]
    assert result["style_comparison"] == comparison
    assert result["suggestions"] == [] and result["proposal_ids"] == []
    assert proposal_service.list_proposals(book.id) == []
    assert book.path.read_text(encoding="utf-8") == BODY


def test_model_returns_no_issues_as_successful_empty_review(book, monkeypatch):
    _model(monkeypatch, {"建议": []})
    result = review_service.soft_deslop(book.id, book.rel)
    assert result["ai_used"] is True and result["ai_error"] == ""
    assert result["suggestions"] == result["proposal_ids"] == result["rejected_suggestions"] == []


def test_manuscript_changed_during_review_does_not_get_rebased_proposals(book, monkeypatch):
    before = book.path.read_text(encoding="utf-8")
    after = "作者已把船资改成七枚铜钱。\n“等我拿回木箱。”"
    _model(monkeypatch, {"建议": [_suggestion()]},
           during=lambda: book.path.write_text(after, encoding="utf-8"))
    result = review_service.soft_deslop(book.id, book.rel)
    assert result["source_changed"] is True
    assert result["candidate_hash"] == hashlib.sha256(before.encode("utf-8")).hexdigest()
    assert result["proposal_ids"] == [] and proposal_service.list_proposals(book.id) == []
    assert book.path.read_text(encoding="utf-8") == after


def test_source_deleted_during_model_review_expires_suggestions_without_exception(book, monkeypatch):
    before = book.path.read_text(encoding="utf-8")
    _model(monkeypatch, {"建议": [_suggestion()]}, during=book.path.unlink)
    result = review_service.soft_deslop(book.id, book.rel)
    assert result["source_changed"] is True
    assert result["candidate_hash"] == hashlib.sha256(before.encode("utf-8")).hexdigest()
    assert result["proposal_ids"] == [] and proposal_service.list_proposals(book.id) == []
    assert not book.path.exists()


def test_frontmatter_is_not_counted_as_body_lines_but_is_part_of_version_hash(book, monkeypatch):
    raw = "---\n标题: 旧格式元数据\n---\n" + BODY
    book.path.write_text(raw, encoding="utf-8")
    calls = _model(monkeypatch, {"建议": [_suggestion()]})
    result = review_service.soft_deslop(book.id, book.rel)
    assert result["candidate_hash"] == hashlib.sha256(raw.encode("utf-8")).hexdigest()
    assert result["suggestions"][0]["line"] == 1
    assert len(result["proposal_ids"]) == 1
    numbered_input = calls[0]["messages"][-1]["content"]
    assert "1: " + ORIGINAL in numbered_input
    assert book.path.read_text(encoding="utf-8") == raw


def test_soft_review_context_contains_contract_and_author_approved_style(book, monkeypatch):
    sample_text = "老船工把钱推回去。“先拿到木箱，船才开。”他低头看绳结，没再看人。"
    style_service.sample(book.id, book.rel, note="对白短，留住人物各自的目的。")
    book.path.write_text(sample_text, encoding="utf-8")
    style_service.sample(book.id, book.rel, note="对白短，留住人物各自的目的。")
    chapter = chapter_service.create_chapter(book.id, "后续船资")
    target = book.root / chapter["rel_path"]
    target.write_text(BODY, encoding="utf-8")
    contract_service.generate_contract(book.id, chapter["rel_path"], use_ai=False)
    contract_service.update_contract(book.id, chapter["rel_path"], {
        "plot_points": ["陆衡支付六枚铜钱船资"], "must_connect": ["船工必须交回木箱"],
        "constraints": ["不得新增角色知道钥匙去向"]})
    calls = _model(monkeypatch, {"建议": [_suggestion()]})
    actual_assemble = context_service.assemble
    assembly_calls = []

    def assemble(*args, **kwargs):
        assembly_calls.append(kwargs)
        return actual_assemble(*args, **kwargs)

    monkeypatch.setattr(context_service, "assemble", assemble)
    result = review_service.soft_deslop(book.id, chapter["rel_path"], create_proposals=False)
    assert result["ai_used"] is True and result["proposal_ids"] == []
    assert assembly_calls[0]["include_style"] is True
    combined = "\n".join(item["content"] for item in calls[0]["messages"])
    assert "陆衡支付六枚铜钱船资" in combined and "船工必须交回木箱" in combined
    assert "先拿到木箱，船才开" in combined and "对白短，留住人物各自的目的" in combined
    snapshot = calls[0]["context_snapshot"]
    assert snapshot["candidate_hash"] == hashlib.sha256(BODY.encode("utf-8")).hexdigest()
    assert snapshot["style_reference"]["samples"]


def test_review_without_ai_is_explicit_and_never_creates_suggestions(book):
    result = review_service.soft_deslop(book.id, book.rel, use_ai=False)
    assert result["ai_used"] is False
    assert result["suggestions"] == [] and result["proposal_ids"] == []
    assert result["source_changed"] is False
    assert result["candidate_hash"] == hashlib.sha256(BODY.encode("utf-8")).hexdigest()
