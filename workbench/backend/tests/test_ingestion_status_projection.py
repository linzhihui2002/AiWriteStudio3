"""Tracker history follows source status and version, with exact citations."""
from __future__ import annotations

import json

import pytest

from workbench.backend.services import (
    chapter_service, context_service, ingestion_commit, ingestion_records,
    ingestion_service as ingestion, knowledge_service as kb, proposal_service,
)
from workbench.backend.services.errors import InvalidOperationError
from workbench.backend.tests.test_ingestion_commit import BODY, book, _files, _model


def sync(book):
    kb.sync(book.id, background=False, enable=False, lexical_only=True)
    assert kb.overview(book.id)["job"]["status"] == "done"


def test_draft_ingestion_rejected_without_writing_trackers(book):
    chapter_service.set_chapter_meta(book.id, book.rel, status="草稿")
    before = _files(book)
    with pytest.raises(InvalidOperationError, match="完成.*发表"):
        ingestion.ingest_chapter(book.id, book.rel, use_ai=False)
    assert _files(book) == before
    assert not proposal_service.list_proposals(project_id=book.id)


def test_demotion_during_extraction_rejects_commit(book, monkeypatch):
    before = _files(book)
    _model(monkeypatch, {"时间线索": [], "资源变更": [], "伏笔": [], "状态变更": []},
           during=lambda: chapter_service.set_chapter_meta(book.id, book.rel, status="草稿"))
    with pytest.raises(InvalidOperationError, match="完成.*发表"):
        ingestion.ingest_chapter(book.id, book.rel)
    assert _files(book) == before
    assert ingestion_commit.latest(book.id, book.rel) is None


@pytest.mark.parametrize("status", ["完成", "发表"])
def test_current_tracker_citations_match_raw_file(book, status):
    chapter_service.set_chapter_meta(book.id, book.rel, status=status)
    ingestion.ingest_chapter(book.id, book.rel, use_ai=False)
    sync(book)
    hits = kb.search(book.id, "铜钥匙", mode="keyword")["hits"]
    assert any(hit["rel_path"] == ingestion.FORESHADOW_FILE for hit in hits)
    for hit in hits:
        original = (book.root / hit["rel_path"]).read_text(encoding="utf-8")
        assert hit["text"] == original[hit["start"]:hit["end"]]
        proof = kb.evidence(book.id, hit["id"])
        assert proof["document_hash"] == kb._hash(original)
        assert proof["text"] == original[proof["start"]:proof["end"]]
    assert context_service._level7(book.id, book.root, "章节/第0002章.txt")


@pytest.mark.parametrize("mutation", ["draft", "rewrite"])
def test_stale_trackers_and_summary_exit_history_without_deleting_files(book, mutation):
    ingestion.ingest_chapter(book.id, book.rel, use_ai=False)
    sync(book)
    before = _files(book)
    assert kb.search(book.id, "铜钥匙", mode="keyword")["hits"]
    if mutation == "draft":
        chapter_service.set_chapter_meta(book.id, book.rel, status="草稿")
    else:
        chapter_service.save_chapter(book.id, book.rel, "陆衡在茶园整理白玉书信。")
    assert _files(book) == before
    assert not kb.search(book.id, "铜钥匙", mode="keyword")["hits"]
    assert not any("铜钥匙" in node["label"] for node in kb.graph(book.id)["nodes"])
    assert not context_service._level7(book.id, book.root, "章节/第0002章.txt")
    assembly = context_service.assemble(book.id, chapter_rel="章节/第0002章.txt", query="铜钥匙")
    assert "铜钥匙" not in "\n".join(block["text"] for block in assembly["blocks"])
    sync(book)
    assert not kb.search(book.id, "铜钥匙", mode="keyword")["hits"]
    assert _files(book) == before


def test_reingestion_replaces_hidden_old_projection_with_current_evidence(book):
    ingestion.ingest_chapter(book.id, book.rel, use_ai=False)
    sync(book)
    chapter_service.save_chapter(book.id, book.rel, "次日，陆衡获得 4 枚银币。\n伏笔：白玉书信出自茶园。")
    assert not kb.search(book.id, "铜钥匙", mode="keyword")["hits"]
    ingestion.ingest_chapter(book.id, book.rel, use_ai=False)
    sync(book)
    assert kb.search(book.id, "白玉书信", mode="keyword")["hits"]
    assert not kb.search(book.id, "铜钥匙", mode="keyword")["hits"]
    summary = context_service._level7(book.id, book.root, "章节/第0002章.txt")
    assert summary and "白玉书信" in summary[0]["text"] and "铜钥匙" not in summary[0]["text"]


def test_ingestion_state_proposal_cannot_apply_after_source_demotion(book, monkeypatch):
    _model(monkeypatch, {"状态变更": [{"角色": "陆衡", "字段": "位置", "新值": "南码头",
                                     "依据": BODY.splitlines()[2]}]})
    result = ingestion.ingest_chapter(book.id, book.rel)
    assert result["proposals_created"]
    proposal_id = result["proposals_created"][0]
    before = _files(book)
    chapter_service.set_chapter_meta(book.id, book.rel, status="草稿")
    with pytest.raises(InvalidOperationError, match="完成.*发表"):
        proposal_service.apply_proposal(proposal_id)
    assert _files(book) == before
    assert proposal_service.get_proposal(proposal_id)["status"] == "pending"


def test_unknown_or_unclosed_managed_blocks_hide_body_and_preserve_offsets(book):
    text = "作者笔记。\n<!-- wb-ingest:unknown:timeline -->\n不可核验的旧剧情。\n"
    projected = ingestion_records.project_text(book.id, text)
    assert len(projected) == len(text)
    assert projected.count("\n") == text.count("\n")
    assert "作者笔记。" in projected and "不可核验" not in projected
    assert ingestion_records.project_text(book.id, text, preserve_offsets=False) == "作者笔记。\n"


def test_legacy_summary_compatibility_still_requires_final_status(book):
    assert ingestion_commit.summary_is_current(book.id, book.rel, "旧手工摘要")
    chapter_service.set_chapter_meta(book.id, book.rel, status="草稿")
    assert not ingestion_commit.summary_is_current(book.id, book.rel, "旧手工摘要")


def test_modified_summary_value_is_not_current_even_with_same_source(book):
    result = ingestion.ingest_chapter(book.id, book.rel, use_ai=False)
    assert ingestion_commit.summary_is_current(book.id, book.rel, result["summary"])
    data = json.loads((book.root / ingestion.SUMMARIES_FILE).read_text(encoding="utf-8"))
    data[book.rel] = "没有摄取依据的新摘要"
    (book.root / ingestion.SUMMARIES_FILE).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    assert not context_service._level7(book.id, book.root, "章节/第0002章.txt")


def test_target_and_future_tracker_events_never_enter_earlier_context(book):
    later = chapter_service.create_chapter(book.id, "后章")
    chapter_service.save_chapter(book.id, later["rel_path"], "次日，陆衡获得 4 枚银币。\n伏笔：白玉书信出自茶园。", status="完成")
    ingestion.ingest_chapter(book.id, later["rel_path"], use_ai=False)
    sync(book)
    assert kb.search(book.id, "白玉书信", mode="keyword")["hits"]
    assert not kb.search(book.id, "白玉书信", chapter_rel=later["rel_path"], mode="keyword")["hits"]
    assembly = context_service.assemble(book.id, chapter_rel=later["rel_path"], query="白玉书信")
    assert "白玉书信" not in "\n".join(block["text"] for block in assembly["blocks"])


def test_missing_source_hides_trackers_without_breaking_other_material(book):
    result = ingestion.ingest_chapter(book.id, book.rel, use_ai=False)
    sync(book)
    book.path.unlink()
    assert not ingestion_commit.summary_is_current(book.id, book.rel, result["summary"])
    assert not kb.search(book.id, "铜钥匙", mode="keyword")["hits"]
    assert "陆衡" in (book.root / "设定/人物设定.md").read_text(encoding="utf-8")
    sync(book)
    assert kb.search(book.id, "陆衡", mode="keyword")["hits"]
