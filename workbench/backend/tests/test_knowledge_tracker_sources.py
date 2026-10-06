"""Knowledge candidates cannot approve withdrawn automatic tracker history."""
from __future__ import annotations

import json

import pytest

from workbench.backend.services import (
    chapter_service, generation_service, ingestion_service as ingestion,
    knowledge_candidate_service as candidates, knowledge_service as kb,
    proposal_service,
)
from workbench.backend.services.errors import InvalidOperationError
from workbench.backend.tests.test_ingestion_commit import book


@pytest.fixture()
def extraction(monkeypatch):
    candidates.shutdown()
    calls = []
    monkeypatch.setattr(candidates, "enqueue_analysis", lambda *a, **k: None)
    monkeypatch.setattr(candidates, "_model_fingerprint", lambda _: {
        "provider": "test", "model": "fake", "prompt_version": candidates.PROMPT_VERSION,
    })

    def run(**kwargs):
        calls.append(kwargs)
        text = json.loads(kwargs["messages"][0]["content"])["text"]
        quote = next((line for line in text.splitlines() if "铜钥匙" in line), None)
        payload = {"entities": [{"name": "铜钥匙", "kind": "item", "description": "来历不明",
                                  "quote": quote}]} if quote else {"entities": []}
        return {"ok": True, "task_id": len(calls), "text": json.dumps(payload, ensure_ascii=False)}

    monkeypatch.setattr(generation_service, "run_task", run)
    yield calls
    candidates.shutdown()


def analyze(book):
    job = candidates.extract(book.id, paths=[ingestion.FORESHADOW_FILE],
                             types=["entity"], background=False)
    assert job["status"] == "completed", job
    return job


def current_candidate(book):
    pending = candidates.list_candidates(book.id, "pending")["candidates"]
    assert len(pending) == 1
    return pending[0]


def withdraw(book, mutation):
    if mutation == "draft":
        chapter_service.set_chapter_meta(book.id, book.rel, status="草稿")
    elif mutation == "rewrite":
        book.path.write_text("陆衡在茶园整理白玉书信。", encoding="utf-8")
    else:
        book.path.unlink()


def approve(book, candidate):
    return candidates.review(book.id, [{"id": candidate["id"], "version": candidate["version"],
                                       "decision": "approve"}])


def test_current_tracker_candidates_keep_raw_hash_and_exact_evidence(book, extraction):
    ingestion.ingest_chapter(book.id, book.rel, use_ai=False)
    analyze(book)
    candidate = current_candidate(book)
    doc, = candidates._documents(book.id, [ingestion.FORESHADOW_FILE])
    original = (book.root / ingestion.FORESHADOW_FILE).read_text(encoding="utf-8")
    proof = candidate["evidence"]
    assert len(doc["text"]) == len(original)
    assert doc["hash"] == proof["document_hash"] == kb._hash(original)
    assert doc["projection_hash"] == kb._hash(doc["text"])
    assert proof["quote"] == original[proof["start"]:proof["end"]]
    assert "wb-ingest:" not in doc["text"]
    assert extraction[0]["context_snapshot"]["hash"] == kb._hash(original)


@pytest.mark.parametrize("mutation", ["draft", "rewrite", "delete"])
@pytest.mark.parametrize("phase", ["pending", "staged"])
def test_withdrawn_tracker_candidate_cannot_reach_author_knowledge(book, extraction, mutation, phase):
    ingestion.ingest_chapter(book.id, book.rel, use_ai=False)
    analyze(book)
    candidate = current_candidate(book)
    tracker = (book.root / ingestion.FORESHADOW_FILE).read_text(encoding="utf-8")
    target = book.root / candidate["target_path"]
    assert not target.exists()
    if phase == "staged":
        result = approve(book, candidate)
        assert not result["failures"]
        proposal_id = result["proposals"][0]["id"]

    withdraw(book, mutation)

    if phase == "staged":
        with pytest.raises(InvalidOperationError, match="失效"):
            proposal_service.apply_proposal(proposal_id)
        assert proposal_service.get_proposal(proposal_id)["status"] == "pending"
    else:
        result = approve(book, candidate)
        assert result["failures"] and "失效" in result["failures"][0]["error"]
        assert not result["proposals"]
    assert candidates.list_candidates(book.id)["candidates"][0]["review_status"] == "stale"
    assert not any(node.get("candidate_id") for node in kb.graph(book.id)["nodes"])
    assert not target.exists()
    assert (book.root / ingestion.FORESHADOW_FILE).read_text(encoding="utf-8") == tracker


def test_refresh_hides_pending_tracker_overlay_immediately(book, extraction):
    ingestion.ingest_chapter(book.id, book.rel, use_ai=False)
    analyze(book)
    assert any(node.get("candidate_id") for node in kb.graph(book.id)["nodes"])
    withdraw(book, "draft")
    assert not any(node.get("candidate_id") for node in kb.graph(book.id)["nodes"])
    assert candidates.list_candidates(book.id)["candidates"][0]["review_status"] == "stale"


def test_extraction_cache_follows_tracker_projection_without_changing_raw_file(book, extraction):
    ingestion.ingest_chapter(book.id, book.rel, use_ai=False)
    analyze(book)
    original_candidate = current_candidate(book)
    doc, = candidates._documents(book.id, [ingestion.FORESHADOW_FILE])
    assert analyze(book)["cached_chunks"] == 1
    assert len(extraction) == 1

    withdraw(book, "draft")
    hidden, = candidates._documents(book.id, [ingestion.FORESHADOW_FILE])
    assert hidden["hash"] == doc["hash"]
    assert hidden["projection_hash"] != doc["projection_hash"]
    assert "铜钥匙" not in hidden["text"]
    assert analyze(book)["cached_chunks"] == 0
    assert len(extraction) == 2
    assert not candidates.list_candidates(book.id, "pending")["candidates"]

    chapter_service.set_chapter_meta(book.id, book.rel, status="发表")
    assert analyze(book)["cached_chunks"] == 1
    renewed = current_candidate(book)
    assert renewed["id"] == original_candidate["id"]
    assert renewed["version"] == original_candidate["version"] + 1
    assert len(extraction) == 2


def test_candidate_quote_cannot_cross_blanked_tracker_metadata(book, extraction):
    ingestion.ingest_chapter(book.id, book.rel, use_ai=False)
    doc, = candidates._documents(book.id, [ingestion.FORESHADOW_FILE])
    original = doc["original_text"]
    start = original.index("<!-- wb-ingest:")
    end = doc["text"].index("\n", doc["text"].index("铜钥匙"))
    quote = doc["text"][start:end]
    assert "铜钥匙" in quote and quote != original[start:end]
    chunk = {"text": doc["text"], "start": 0, "end": len(doc["text"])}
    assert candidates._evidence(doc, chunk, {"quote": quote, "start": start}) is None


def test_author_prose_and_applied_author_knowledge_survive_source_withdrawal(book, extraction):
    ingestion.ingest_chapter(book.id, book.rel, use_ai=False)
    path = book.root / ingestion.FORESHADOW_FILE
    path.write_text(path.read_text(encoding="utf-8") + "\n作者注：银钥匙尚未拆开。\n", encoding="utf-8")
    analyze(book)
    candidate = current_candidate(book)
    result = approve(book, candidate)
    assert not result["failures"]
    proposal_service.apply_proposal(result["proposals"][0]["id"])
    target = book.root / candidate["target_path"]
    approved = target.read_text(encoding="utf-8")

    withdraw(book, "draft")

    assert candidates.list_candidates(book.id)["candidates"][0]["review_status"] == "applied"
    doc, = candidates._documents(book.id, [ingestion.FORESHADOW_FILE])
    assert "作者注：银钥匙尚未拆开。" in doc["text"] and "铜钥匙" not in doc["text"]
    assert target.read_text(encoding="utf-8") == approved
    kb.sync(book.id, background=False, enable=False, lexical_only=True)
    assert any(hit["rel_path"] == candidate["target_path"]
               for hit in kb.search(book.id, "铜钥匙", mode="keyword")["hits"])
