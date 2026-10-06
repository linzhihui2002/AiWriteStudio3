"""Knowledge reviews, temporal boundaries and journal recovery; no paid model calls."""
import json
from pathlib import Path
from threading import Event

import pytest

from workbench.backend import config, db
from workbench.backend.services import (chapter_service, generation_service, knowledge_candidate_service as km,
    knowledge_service as kb, knowledge_store, project_service, proposal_service, file_change_service,
    knowledge_lifecycle as lifecycle)
from workbench.backend.services.errors import InvalidOperationError, NodeNotFoundError


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    km.shutdown()
    runtime, projects = tmp_path / ".workbench", tmp_path / "projects"
    runtime.mkdir()
    projects.mkdir()
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(config, "PROJECTS_DIR", projects)
    monkeypatch.setattr(config, "RUNTIME_DIR", runtime)
    monkeypatch.setattr(config, "DB_PATH", runtime / "workbench.db")
    db.init_db()
    config.ensure_runtime_dirs()
    monkeypatch.setattr(kb, "enqueue", lambda *_args, **_kw: None)
    monkeypatch.setattr(km, "enqueue_analysis", lambda *_args, **_kw: None)
    monkeypatch.setattr(generation_service, "run_task", lambda **_: pytest.fail("unexpected model call"))
    monkeypatch.setattr(km, "_model_fingerprint", lambda _: {"provider": "test", "model": "fake", "prompt_version": km.PROMPT_VERSION})
    yield tmp_path
    km.shutdown()
    kb.shutdown()


def book(name="知识样例"):
    project = project_service.create_project(name=name)
    ident, root = project["id"], project_service.get_project_dir(project["id"])[1]
    (root / "设定/人物设定.md").write_text("## 沈砚\n- 身份：守桥人\n- 别名：阿砚\n- 爱好：木雕\n", encoding="utf-8")
    return ident, root


def chapter(ident, text, status="草稿"):
    row = chapter_service.create_chapter(ident, "原创样例")
    chapter_service.save_chapter(ident, row["rel_path"], text, status=status)
    return row["rel_path"]


def stub(monkeypatch, payload, calls=None):
    def run(**kwargs):
        if calls is not None:
            calls.append(kwargs)
        value = payload(kwargs) if callable(payload) else payload
        return {"ok": True, "task_id": 101, "text": json.dumps(value, ensure_ascii=False)}
    monkeypatch.setattr(generation_service, "run_task", run)


def fields(value="巡桥人", quote="沈砚的身份改为巡桥人。", **extra):
    return {"fields": [{"entity": "沈砚", "entity_kind": "character", "field": "身份", "value": value, "quote": quote, **extra}]}


def analyze(ident, rel):
    job = km.extract(ident, paths=[rel], background=False)
    assert job["status"] == "completed", job
    return km.list_candidates(ident)["candidates"]


def approve(ident, candidate, **extra):
    return km.review(ident, [{"id": candidate["id"], "version": candidate["version"], "decision": "approve", **extra}])


def test_written_draft_entity_section_heading_cannot_leak_into_history(isolated, monkeypatch):
    from workbench.backend.services import context_service
    ident, root = book("新人物计划标题边界")
    rel = chapter(ident, "许澄是北岸新来的账房。")
    stub(monkeypatch, {"entities": [{"name": "许澄", "kind": "character", "description": "北岸账房", "quote": "许澄是北岸新来的账房。"}]})
    candidate, = analyze(ident, rel)
    result = approve(ident, candidate)
    proposal_service.apply_proposal(result["proposals"][0]["id"])
    written = (root / "设定/人物设定.md").read_text(encoding="utf-8")
    assert "## 许澄 · 计划与草稿" in written
    assert "许澄" not in lifecycle.project_text(written, profile="history")
    kb.sync(ident, background=False)
    assert not kb.search(ident, "许澄", mode="keyword")["hits"]
    assert kb.search(ident, "许澄", mode="keyword", profile="planning")["hits"]
    assembled = context_service.assemble(ident, use_retrieval=False,
        exclude_levels=[1, 2, 3, 6, 7, 8], budget_tokens=20000)
    assert "许澄" not in "\n".join(block["text"] for block in assembled["blocks"])


def test_exact_evidence_all_drafts_outline_and_generation_accounting(isolated, monkeypatch):
    ident, root = book()
    rel = chapter(ident, "沈砚的身份改为巡桥人。")
    outline = "大纲/故事大纲.md"
    (root / outline).write_text("沈砚的身份改为巡桥人。", encoding="utf-8")
    calls = []
    stub(monkeypatch, fields(), calls)
    draft = analyze(ident, rel)[0]
    assert draft["lifecycle"] == "draft"
    assert draft["provenance"] == "model"
    assert draft["evidence"]["quote"] == "沈砚的身份改为巡桥人。"
    assert draft["evidence"]["start"] == 0
    assert draft["generation_task_id"] == 101
    analyze(ident, outline)
    assert any(c["lifecycle"] == "planned" for c in km.list_candidates(ident)["candidates"])
    assert len(calls) == 2
    assert calls[0]["prompt_version"] == km.PROMPT_VERSION
    assert calls[0]["engine"] == "direct-api"
    assert "output_path" not in calls[0]


def test_forged_quotes_and_ambiguous_repeated_spans_are_rejected(isolated, monkeypatch):
    ident, _ = book()
    rel = chapter(ident, "沈砚的身份改为巡桥人。\n沈砚的身份改为巡桥人。")
    stub(monkeypatch, {"fields": [fields()["fields"][0], fields(quote="沈砚没有出现在这段原文。")["fields"][0]]})
    assert analyze(ident, rel) == []
    stub(monkeypatch, fields(start=0))
    km.extract(ident, paths=[rel], force=True, background=False)
    assert len(km.list_candidates(ident)["candidates"]) == 1


def test_incremental_chunk_cache_avoids_duplicate_calls_and_reanchors_changed_document(isolated, monkeypatch):
    ident, root = book()
    rel = chapter(ident, "沈砚的身份改为巡桥人。\n" + "甲" * 3980 + "\n" + "余文。")
    calls = []
    stub(monkeypatch, fields(), calls)
    first = analyze(ident, rel)[0]
    count = len(calls)
    job = km.extract(ident, paths=[rel], background=False)
    assert job["cached_chunks"] == job["chunks_total"]
    assert len(calls) == count
    text = (root / rel).read_text(encoding="utf-8")
    (root / rel).write_text(text + "结尾新增一句。", encoding="utf-8")
    assert km.list_candidates(ident)["candidates"][0]["review_status"] == "stale"
    updated = analyze(ident, rel)[0]
    assert updated["id"] == first["id"]
    assert updated["version"] == first["version"] + 1
    assert updated["evidence"]["document_hash"] != first["evidence"]["document_hash"]
    assert len(calls) == count + 1


def test_cache_fingerprint_changes_with_prompt_and_model(isolated, monkeypatch):
    ident, _ = book()
    rel = chapter(ident, "沈砚的身份改为巡桥人。")
    calls = []
    stub(monkeypatch, fields(), calls)
    analyze(ident, rel)
    monkeypatch.setattr(km, "_model_fingerprint", lambda _: {"provider": "test", "model": "fake2", "prompt_version": "next"})
    analyze(ident, rel)
    assert len(calls) == 2


def test_plans_write_separate_sections_never_overwrite_canon(isolated, monkeypatch):
    ident, root = book()
    rel = chapter(ident, "沈砚的身份改为巡桥人。")
    stub(monkeypatch, fields())
    candidate = analyze(ident, rel)[0]
    result = approve(ident, candidate)
    assert not result["failures"]
    proposal = result["proposals"][0]
    assert proposal["diff"]
    assert "- 身份：守桥人" in proposal["content"]
    assert "计划与草稿" in proposal["content"]
    assert "巡桥人" not in lifecycle.project_text(proposal["content"], preserve_offsets=False)
    assert "巡桥人" in lifecycle.project_text(proposal["content"], profile="planning", preserve_offsets=False)
    assert km.list_candidates(ident)["candidates"][0]["review_status"] == "staged"
    assert "巡桥人" not in (root / "设定/人物设定.md").read_text(encoding="utf-8")


def test_confirmed_unscoped_field_replaces_only_selected_field_and_keeps_provenance(isolated, monkeypatch):
    ident, root = book()
    source = "大纲/故事大纲.md"
    (root / source).write_text("沈砚的身份改为巡桥人。", encoding="utf-8")
    stub(monkeypatch, fields())
    candidate = analyze(ident, source)[0]
    result = approve(ident, candidate, lifecycle="setting_fact")
    proposal = result["proposals"][0]
    assert "- 身份：守桥人" not in proposal["content"]
    assert "- 身份：巡桥人" in proposal["content"]
    assert "- 爱好：木雕" in proposal["content"]
    km.validate_proposal(ident, proposal)
    applied = proposal_service.apply_proposal(proposal["id"])
    assert applied["knowledge"]["applied"] == 1
    assert km.list_candidates(ident)["candidates"][0]["review_status"] == "applied"
    meta = lifecycle.parse_records((root / proposal["target_path"]).read_text(encoding="utf-8"))[0]["metadata"]
    assert meta["provenance"] == "model" and meta["reviewer"]["decision"] == "approved"
    assert next(e for e in km.entity_definitions(ident) if e["label"] == "沈砚")["fields"]["身份"] == "巡桥人"
    retried = proposal_service.apply_proposal(proposal["id"])
    assert retried["change"]["id"] == applied["change"]["id"]


def test_source_and_target_versions_are_both_checked_before_and_after_review(isolated, monkeypatch):
    ident, root = book()
    rel = chapter(ident, "沈砚的身份改为巡桥人。")
    stub(monkeypatch, fields())
    candidate = analyze(ident, rel)[0]
    proposal = approve(ident, candidate)["proposals"][0]
    (root / rel).write_text("沈砚改去了茶园。", encoding="utf-8")
    with pytest.raises(InvalidOperationError, match="变化"):
        proposal_service.apply_proposal(proposal["id"])
    assert km.list_candidates(ident)["candidates"][0]["review_status"] == "stale"
    assert "计划与草稿" not in (root / "设定/人物设定.md").read_text(encoding="utf-8")
    (root / rel).write_text("沈砚的身份改为巡桥人。", encoding="utf-8")
    updated = analyze(ident, rel)[0]
    (root / "设定/人物设定.md").write_text("## 沈砚\n- 身份：书吏\n", encoding="utf-8")
    assert approve(ident, updated)["failures"]


def test_modified_metadata_or_removed_marker_cannot_promote_draft(isolated, monkeypatch):
    ident, _ = book()
    rel = chapter(ident, "沈砚的身份改为巡桥人。")
    stub(monkeypatch, fields())
    candidate = analyze(ident, rel)[0]
    proposal = approve(ident, candidate)["proposals"][0]
    with pytest.raises(InvalidOperationError, match="元数据"):
        km.validate_proposal(ident, {**proposal, "content": proposal["content"].replace('"lifecycle":"draft"', '"lifecycle":"historical_event"')})
    with pytest.raises(InvalidOperationError, match="标记"):
        km.validate_proposal(ident, {**proposal, "content": "沈砚的身份：巡桥人"})
    with pytest.raises(InvalidOperationError, match="候选审核"):
        proposal_service.apply_proposal(proposal["id"], content=proposal["content"] + "\n沈砚的身份：巡桥人\n")
    proposal_service.update_proposal_content(proposal["id"], proposal["content"] + "\n计划外复制：巡桥人\n")
    with pytest.raises(InvalidOperationError, match="候选审核"):
        proposal_service.apply_proposal(proposal["id"])


def test_same_file_batch_one_proposal_and_incompatible_values_require_choice(isolated, monkeypatch):
    ident, _ = book()
    rel = chapter(ident, "沈砚的身份改为巡桥人。沈砚喜欢临帖。")
    payload = fields()
    payload["fields"].append({"entity": "沈砚", "entity_kind": "character", "field": "爱好", "value": "临帖", "quote": "沈砚喜欢临帖。"})
    stub(monkeypatch, payload)
    candidates = analyze(ident, rel)
    result = km.review(ident, [{"id": c["id"], "version": c["version"], "decision": "approve",
        "entity_anchor": c["entity"]["anchor"], **({"target_entity_anchor": c["target_entity"]["anchor"]} if c["target_entity"] else {})} for c in candidates])
    assert len(result["proposals"]) == 1 and len(result["candidates"]) == 2
    assert len(result["proposals"][0]["meta"]["knowledge"]["candidates"]) == 2


def test_entity_alias_ambiguity_requires_explicit_definition_choice(isolated, monkeypatch):
    ident, root = book()
    (root / "设定/人物设定.md").write_text("## 沈砚\n- 别名：阿砚\n\n## 纪砚\n- 别名：阿砚\n", encoding="utf-8")
    rel = chapter(ident, "阿砚的身份改为巡桥人。")
    payload = fields(quote="阿砚的身份改为巡桥人。")
    payload["fields"][0]["entity"] = "阿砚"
    stub(monkeypatch, payload)
    candidate = analyze(ident, rel)[0]
    assert candidate["requires_entity_choice"] and len(candidate["entity_options"]) == 2
    assert "归属" in approve(ident, candidate)["failures"][0]["error"]
    selected = candidate["entity_options"][0]
    result = approve(ident, candidate, entity_anchor=selected["anchor"])
    assert result["candidates"][0]["entity"]["label"] == selected["label"]


def test_adjacent_chapter_states_are_evolution_overlap_is_conflict(isolated, monkeypatch):
    ident, _ = book()
    early = chapter(ident, "沈砚的身份改为巡桥人。", "完成")
    later = chapter(ident, "沈砚的身份改为书吏。", "完成")
    stub(monkeypatch, fields())
    analyze(ident, early)
    stub(monkeypatch, fields(value="书吏", quote="沈砚的身份改为书吏。"))
    analyze(ident, later)
    assert all(c["review_status"] == "pending" for c in km.list_candidates(ident)["candidates"])
    stub(monkeypatch, fields(value="旧港书吏", quote="沈砚的身份改为书吏。", chapter_start=1, chapter_end=2))
    km.extract(ident, paths=[later], force=True, background=False)
    conflicts = km.list_candidates(ident, "conflict")["candidates"]
    assert len(conflicts) == 3
    assert all(c["conflicts"] for c in conflicts)


def test_new_entity_and_relation_survive_reindex_and_future_analysis(isolated, monkeypatch):
    ident, root = book()
    rel = chapter(ident, "柳青在旧港给沈砚递来鱼符。")
    payload = {"entities": [{"name": "柳青", "kind": "character", "description": "旧港来客", "quote": "柳青在旧港给沈砚递来鱼符。"}],
               "relations": [{"subject": "柳青", "subject_kind": "character", "object": "沈砚", "object_kind": "character",
                              "relation": "交付鱼符", "quote": "柳青在旧港给沈砚递来鱼符。"}]}
    stub(monkeypatch, payload)
    candidates = analyze(ident, rel)
    result = km.review(ident, [{"id": c["id"], "version": c["version"], "decision": "approve",
        "entity_anchor": c["entity"]["anchor"], **({"target_entity_anchor": c["target_entity"]["anchor"]} if c["target_entity"] else {})} for c in candidates])
    assert {p["target_path"] for p in result["proposals"]} == {"设定/人物设定.md", "设定/关系网络.md"}
    for proposal in result["proposals"]:
        proposal_service.apply_proposal(proposal["id"])
    new = next(e for e in km.entity_definitions(ident) if e["label"] == "柳青")
    entity_candidate = next(c for c in candidates if c["kind"] == "entity")
    assert new["anchor"] == entity_candidate["entity"]["anchor"]
    # Existing names, including newly reviewed draft entities, are not duplicated.
    km.extract(ident, paths=[rel], background=False)
    assert len(km.list_candidates(ident)["candidates"]) == 2
    with knowledge_store.connect(ident) as conn:
        kb._init(conn)
        conn.execute("DELETE FROM kb_nodes")
        conn.execute("DELETE FROM kb_chunks")
    assert len(km.list_candidates(ident, "applied")["candidates"]) == 2


def test_independent_job_cancel_discards_late_result_and_preserves_valid_candidates(isolated, monkeypatch):
    ident, _ = book()
    rel = chapter(ident, "沈砚的身份改为巡桥人。")
    entered, release = Event(), Event()
    def slow(**kwargs):
        entered.set()
        assert release.wait(5)
        return {"ok": True, "task_id": 101, "text": json.dumps(fields(), ensure_ascii=False)}
    monkeypatch.setattr(generation_service, "run_task", slow)
    job = km.extract(ident, paths=[rel], background=True)
    assert entered.wait(5)
    future = km._futures[(ident, job["id"])]
    km.cancel_job(ident, job["id"])
    release.set()
    future.result(timeout=5)
    assert km.get_job(ident, job["id"])["status"] == "cancelled"
    assert km.list_candidates(ident)["candidates"] == []
    stub(monkeypatch, fields())
    succeeding = km.extract(ident, paths=[rel], background=False)
    assert succeeding["status"] == "completed"
    assert len(km.list_candidates(ident)["candidates"]) == 1


def test_failed_material_retry_keeps_success_cache_and_real_progress(isolated, monkeypatch):
    ident, _ = book()
    first = chapter(ident, "沈砚的身份改为巡桥人。")
    second = chapter(ident, "沈砚喜欢临帖。")
    calls = []
    def flaky(**kwargs):
        calls.append(kwargs)
        if second in kwargs["messages"][0]["content"]:
            return {"ok": False, "error_message": "模型不可用"}
        return {"ok": True, "task_id": 101, "text": json.dumps(fields(), ensure_ascii=False)}
    monkeypatch.setattr(generation_service, "run_task", flaky)
    failed = km.extract(ident, paths=[first, second], background=False)
    assert failed["status"] == "failed" and failed["files_completed"] == 2
    assert failed["chunks_completed"] == failed["chunks_total"]
    assert failed["failed_documents"][0]["rel_path"] == second
    by_path = {p["rel_path"]: p for p in failed["document_progress"]}
    assert by_path[first]["status"] == "completed" and by_path[second]["status"] == "failed"
    assert all(p["chunks_completed"] == p["chunks_total"] for p in by_path.values())
    stub(monkeypatch, {})
    retried = km.extract(ident, retry_job_id=failed["id"], background=False)
    assert retried["status"] == "completed" and retried["paths"] == [second]
    assert len(km.list_candidates(ident)["candidates"]) == 1


def test_book_isolation_and_proposal_receipt_cannot_be_forged(isolated, monkeypatch):
    ident, _ = book("甲书")
    other, _ = book("乙书")
    rel = chapter(ident, "沈砚的身份改为巡桥人。")
    stub(monkeypatch, fields())
    job = km.extract(ident, paths=[rel], background=False)
    candidate = km.list_candidates(ident)["candidates"][0]
    proposal = approve(ident, candidate)["proposals"][0]
    with pytest.raises(NodeNotFoundError):
        km.get_job(other, job["id"])
    with pytest.raises(InvalidOperationError, match="小说"):
        km.validate_proposal(other, proposal)
    assert not km.list_candidates(other)["candidates"]
    assert km.reconcile_proposal(ident, {**proposal, "status": "applied"}, {"id": 123, "status": "applied"})["applied"] == 0
    assert km.list_candidates(ident)["candidates"][0]["review_status"] == "staged"


def test_ignore_and_discard_are_reviewed_without_author_file_changes(isolated, monkeypatch):
    ident, root = book()
    rel = chapter(ident, "沈砚的身份改为巡桥人。")
    stub(monkeypatch, fields())
    candidate = analyze(ident, rel)[0]
    baseline = (root / "设定/人物设定.md").read_text(encoding="utf-8")
    ignored = km.review(ident, [{"id": candidate["id"], "version": candidate["version"], "decision": "ignore"}])
    assert not ignored["proposals"] and ignored["candidates"][0]["review_status"] == "ignored"
    assert (root / "设定/人物设定.md").read_text(encoding="utf-8") == baseline


def test_managed_definition_anchor_survives_unrelated_prefix_and_context_masks_plans(isolated):
    ident, root = book()
    anchor = next(e for e in km.entity_definitions(ident) if e["label"] == "沈砚")["anchor"]
    target = root / "设定/人物设定.md"
    target.write_text("说明段落。\n\n" + target.read_text(encoding="utf-8"), encoding="utf-8")
    assert next(e for e in km.entity_definitions(ident) if e["label"] == "沈砚")["anchor"] == anchor


def test_debounce_merges_paths_and_intents_without_cancelling_explicit_jobs(isolated, monkeypatch):
    ident, _ = book()
    first = chapter(ident, "沈砚的身份改为巡桥人。")
    second = chapter(ident, "沈砚喜欢临帖。")
    monkeypatch.setattr(kb, "auto_enabled", lambda _: True)
    fired = []
    class Timer:
        def __init__(self, delay, fn, args):
            self.delay, self.fn, self.args, self.cancelled = delay, fn, args, False
        def start(self):
            pass
        def cancel(self):
            self.cancelled = True
    monkeypatch.setattr(km.threading, "Timer", Timer)
    monkeypatch.setattr(km, "extract", lambda project_id, **kw: fired.append((project_id, kw)))
    km.enqueue(ident, [first], "extract")
    timer = km._timers[ident]
    km.enqueue(ident, [second], "retry")
    assert timer.cancelled and timer.delay == 5
    km._flush(ident)
    assert fired == [(ident, {"paths": sorted([first, second]), "intent": "extract+retry"})]


def test_unsafe_paths_are_rejected_without_model_calls(isolated):
    ident, _ = book()
    with pytest.raises(InvalidOperationError):
        km.extract(ident, paths=["../另一书/章节/第0001章.txt"])
    with pytest.raises(InvalidOperationError):
        km.extract(ident, paths=["备忘录/便签.md"])


def test_committed_journal_repairs_staged_bookkeeping_without_rewriting_files(isolated, monkeypatch):
    ident, root = book()
    rel = chapter(ident, "沈砚的身份改为巡桥人。")
    stub(monkeypatch, fields())
    candidate = analyze(ident, rel)[0]
    proposal = approve(ident, candidate)["proposals"][0]
    real_reconcile = km.reconcile_proposal
    monkeypatch.setattr(km, "reconcile_proposal", lambda *_args, **_kw: (_ for _ in ()).throw(RuntimeError("派生库暂不可写")))
    applied = proposal_service.apply_proposal(proposal["id"])
    assert applied["knowledge"]["status"] == "reconciliation_pending"
    # A refresh can mark the staged target as stale after the successful write.
    assert km.list_candidates(ident)["candidates"][0]["review_status"] == "stale"
    target = root / proposal["target_path"]
    target.write_text(target.read_text(encoding="utf-8") + "\n作者后续添加一句说明。\n", encoding="utf-8")
    current = target.read_text(encoding="utf-8")
    monkeypatch.setattr(km, "reconcile_proposal", real_reconcile)
    km.recover()
    assert km.list_candidates(ident)["candidates"][0]["review_status"] == "applied"
    assert target.read_text(encoding="utf-8") == current
    receipt = proposal_service.apply_proposal(proposal["id"])
    assert receipt["meta"]["idempotent"]
    assert receipt["change"]["id"] == applied["change"]["id"]
    assert target.read_text(encoding="utf-8") == current


def test_legacy_model_relations_migrate_without_model_vector_or_author_writes(isolated):
    ident, root = book()
    rel = chapter(ident, "柳青在旧港给沈砚递来鱼符。", "完成")
    text = (root / rel).read_text(encoding="utf-8")
    original = (root / "设定/人物设定.md").read_text(encoding="utf-8")
    with knowledge_store.connect(ident) as conn:
        kb._init(conn)
        conn.execute("INSERT INTO kb_documents (id,rel_path,title,kind,status,chapter_number,hash) VALUES (?,?,?,?,?,?,?)",
                     ("doc", rel, "原创章", "chapter", "完成", 1, km._hash(text)))
        conn.execute("INSERT INTO kb_chunks (id,document_id,text,start,end,line_start,line_end,embedding) VALUES (?,?,?,?,?,?,?,?)",
                     ("proof", "doc", text, 0, len(text), 1, 1, "[0.3,0.7]"))
        for node_id, label in (("subject", "柳青"), ("object", "沈砚")):
            conn.execute("INSERT INTO kb_nodes (id,kind,label,source,document,evidence_ids,chapter_number) VALUES (?,?,?,?,?,?,?)",
                         (node_id, "character", label, "model", rel, '["proof"]', 1))
        conn.execute("INSERT INTO kb_edges (id,source,target,relation,origin,evidence_ids,document,quote,projection) VALUES (?,?,?,?,?,?,?,?,?)",
                     ("legacy", "subject", "object", "交付鱼符", "model", '["proof"]', rel, text, "model"))
    mapping = km.migrate_legacy(ident)
    assert mapping[0]["edge_id"] == "legacy"
    candidate = km.list_candidates(ident)["candidates"][0]
    assert candidate["review_status"] == "pending" and candidate["legacy_edge_id"] == "legacy"
    assert candidate["fingerprint"] == "legacy:unknown-model-and-prompt"
    assert not km.migrate_legacy(ident)
    assert (root / "设定/人物设定.md").read_text(encoding="utf-8") == original
    assert (root / rel).read_text(encoding="utf-8") == text
    with knowledge_store.connect(ident) as conn:
        assert conn.execute("SELECT embedding FROM kb_chunks WHERE id='proof'").fetchone()[0] == "[0.3,0.7]"


def test_rejected_target_group_does_not_cancel_independent_file_proposal(isolated, monkeypatch):
    ident, _ = book()
    rel = chapter(ident, "柳青在旧港给沈砚递来鱼符。")
    payload = {"entities": [{"name": "柳青", "kind": "character", "description": "旧港来客", "quote": "柳青在旧港给沈砚递来鱼符。"}],
               "relations": [{"subject": "柳青", "subject_kind": "character", "object": "沈砚", "object_kind": "character", "relation": "交付鱼符", "quote": "柳青在旧港给沈砚递来鱼符。"}]}
    stub(monkeypatch, payload)
    candidates = analyze(ident, rel)
    real_create = proposal_service.create_proposal
    def fail_one(**kw):
        if kw["target_path"] == "设定/关系网络.md":
            raise InvalidOperationError("关系文件已锁定")
        return real_create(**kw)
    monkeypatch.setattr(proposal_service, "create_proposal", fail_one)
    result = km.review(ident, [{"id": c["id"], "version": c["version"], "decision": "approve"} for c in candidates])
    assert len(result["proposals"]) == len(result["failures"]) == 1
    assert result["proposals"][0]["target_path"] == "设定/人物设定.md"
    states = {c["kind"]: c["review_status"] for c in km.list_candidates(ident)["candidates"]}
    assert states == {"entity": "staged", "relation": "pending"}


def test_candidate_job_api_contract_and_cross_book_access(isolated, monkeypatch):
    from fastapi.testclient import TestClient
    from workbench.backend.app import create_app
    ident, _ = book("API甲书")
    other, _ = book("API乙书")
    rel = chapter(ident, "沈砚的身份改为巡桥人。")
    stub(monkeypatch, fields())
    real_extract = km.extract
    monkeypatch.setattr(km, "extract", lambda project_id, **kwargs: real_extract(project_id, **kwargs, background=False))
    client = TestClient(create_app())
    base = f"/api/projects/{ident}/knowledge"
    assert client.post(base + "/extract", json={"types": ["invalid"]}).status_code == 422
    response = client.post(base + "/extract", json={"paths": [rel]})
    assert response.status_code == 200
    job = response.json()
    assert job["status"] == "completed"
    assert client.get(base + "/jobs").json()["jobs"][0]["id"] == job["id"]
    assert client.get(base + "/jobs/" + job["id"]).json()["candidate_count"] == 1
    assert client.get(f"/api/projects/{other}/knowledge/jobs/{job['id']}").status_code == 404
    candidates = client.get(base + "/candidates?status=pending,conflict").json()
    assert candidates["counts"]["pending"] == 1
    candidate = candidates["candidates"][0]
    proof = client.get(base + "/evidence/candidate:" + candidate["id"]).json()
    assert proof["quote"] == candidate["evidence"]["quote"]
    assert proof["document_hash"] == candidate["evidence"]["document_hash"]
    reviewed = client.post(base + "/candidates/review", json={"items": [{"id": candidate["id"], "version": candidate["version"],
        "decision": "approve", "entity_anchor": candidate["entity"]["anchor"], "value": "巡桥人", "lifecycle": "draft"}]})
    assert reviewed.status_code == 200 and reviewed.json()["proposals"][0]["diff"]
    assert reviewed.json()["candidates"][0]["review_status"] == "staged"


def test_confirmed_new_field_later_revision_supersedes_record_without_two_current_values(isolated, monkeypatch):
    ident, root = book()
    rel = "大纲/故事大纲.md"
    (root / rel).write_text("沈砚的职责是巡查东桥。", encoding="utf-8")
    stub(monkeypatch, {"fields": [{"entity": "沈砚", "entity_kind": "character", "field": "职责", "value": "巡查东桥", "quote": "沈砚的职责是巡查东桥。"}]})
    candidate = analyze(ident, rel)[0]
    first = approve(ident, candidate, lifecycle="setting_fact")["proposals"][0]
    proposal_service.apply_proposal(first["id"])
    (root / rel).write_text("沈砚的职责是巡查西桥。", encoding="utf-8")
    stub(monkeypatch, {"fields": [{"entity": "沈砚", "entity_kind": "character", "field": "职责", "value": "巡查西桥", "quote": "沈砚的职责是巡查西桥。"}]})
    candidate = next(c for c in analyze(ident, rel) if c["review_status"] in {"pending", "conflict"})
    assert candidate["current_value"] == "巡查东桥"
    second = approve(ident, candidate, lifecycle="setting_fact")["proposals"][0]
    visible = lifecycle.project_text(second["content"], preserve_offsets=False)
    assert "巡查东桥" not in visible and "巡查西桥" in visible
    proposal_service.apply_proposal(second["id"])
    assert next(e for e in km.entity_definitions(ident) if e["label"] == "沈砚")["fields"]["职责"] == "巡查西桥"


def test_unavailable_model_preparation_records_retryable_paths_without_fake_progress(isolated, monkeypatch):
    ident, _ = book()
    rel = chapter(ident, "沈砚的身份改为巡桥人。")
    monkeypatch.setattr(km, "_model_fingerprint", lambda _: (_ for _ in ()).throw(InvalidOperationError("供应商尚未配置")))
    failed = km.extract(ident, paths=[rel], background=False)
    assert failed["status"] == "failed" and failed["failed_documents"][0]["rel_path"] == rel
    assert failed["files_total"] == 1 and failed["files_completed"] == failed["chunks_completed"] == 0
    monkeypatch.setattr(km, "_model_fingerprint", lambda _: {"provider": "test", "model": "fake", "prompt_version": km.PROMPT_VERSION})
    stub(monkeypatch, fields())
    assert km.extract(ident, retry_job_id=failed["id"], background=False)["status"] == "completed"


def test_same_review_related_files_can_apply_after_only_planned_definition_additions(isolated, monkeypatch):
    ident, root = book()
    (root / "设定/人物设定.md").write_text("## 沈砚\n- 身份：守桥人\n\n## 柳青\n- 身份：舟子\n", encoding="utf-8")
    rel = chapter(ident, "沈砚的身份改为巡桥人。柳青在旧港给沈砚递来鱼符。")
    payload = fields()
    payload["relations"] = [{"subject": "柳青", "subject_kind": "character", "object": "沈砚", "object_kind": "character", "relation": "交付鱼符", "quote": "柳青在旧港给沈砚递来鱼符。"}]
    stub(monkeypatch, payload)
    candidates = analyze(ident, rel)
    response = km.review(ident, [{"id": c["id"], "version": c["version"], "decision": "approve"} for c in candidates])
    proposals = sorted(response["proposals"], key=lambda p: p["target_path"] == "设定/关系网络.md")
    assert len(proposals) == 2
    for proposal in proposals:
        result = proposal_service.apply_proposal(proposal["id"])
        assert result["knowledge"]["applied"] == 1
    assert len(km.list_candidates(ident, "applied")["candidates"]) == 2


def test_relation_endpoints_cannot_silently_rebind_after_same_name_definition_swap(isolated, monkeypatch):
    ident, root = book()
    path = root / "设定/人物设定.md"
    path.write_text("## 沈砚\n- 身份：守桥人\n\n## 沈砚\n- 身份：旧港书吏\n\n## 柳青\n- 身份：舟子\n", encoding="utf-8")
    rel = chapter(ident, "柳青在旧港给沈砚递来鱼符。")
    payload = {"relations": [{"subject": "柳青", "subject_kind": "character", "object": "沈砚", "object_kind": "character", "relation": "交付鱼符", "quote": "柳青在旧港给沈砚递来鱼符。"}]}
    stub(monkeypatch, payload)
    candidate = analyze(ident, rel)[0]
    target = next(e for e in candidate["target_entity_options"] if e["fields"]["身份"] == "守桥人")
    proposal = approve(ident, candidate, target_entity_anchor=target["anchor"])["proposals"][0]
    path.write_text("## 沈砚\n- 身份：旧港书吏\n\n## 沈砚\n- 身份：守桥人\n\n## 柳青\n- 身份：舟子\n", encoding="utf-8")
    with pytest.raises(InvalidOperationError, match="实体定义"):
        proposal_service.apply_proposal(proposal["id"])
    assert not (root / "设定/关系网络.md").exists()


def test_staging_crash_restores_approved_edits_from_durable_proposal(isolated, monkeypatch):
    ident, _ = book()
    rel = chapter(ident, "沈砚的身份改为巡桥人。")
    stub(monkeypatch, fields())
    original = analyze(ident, rel)[0]
    proposal = approve(ident, original, value="作者改为巡桥领班")["proposals"][0]
    # Simulate the book staging transaction being lost after the Proposal committed.
    with knowledge_store.connect(ident) as conn:
        km._put(conn, original)
    km.recover()
    recovered = km.list_candidates(ident)["candidates"][0]
    assert recovered["review_status"] == "staged" and recovered["proposal_id"] == proposal["id"]
    assert recovered["suggested_value"] == "作者改为巡桥领班"
    assert recovered["version"] == original["version"] + 1
    assert proposal_service.apply_proposal(proposal["id"])["knowledge"]["applied"] == 1


def test_same_file_applied_evidence_reads_verified_journal_archive_after_snapshot_pruning(isolated, monkeypatch):
    from workbench.backend.services import snapshot_service
    ident, root = book()
    rel = "设定/人物设定.md"
    path = root / rel
    path.write_text("## 沈砚\n- 身份：守桥人\n沈砚的职责是巡查东桥。\n", encoding="utf-8")
    original = path.read_text(encoding="utf-8")
    stub(monkeypatch, {"fields": [{"entity": "沈砚", "entity_kind": "character", "field": "职责", "value": "巡查东桥", "quote": "沈砚的职责是巡查东桥。"}]})
    candidate = analyze(ident, rel)[0]
    proposal = approve(ident, candidate)["proposals"][0]
    proposal_service.apply_proposal(proposal["id"])
    current_hash = km._hash(path.read_text(encoding="utf-8"))
    assert current_hash != candidate["evidence"]["document_hash"]
    snapshot_service.clear_snapshots(ident)
    evidence = km.candidate_evidence(ident, "candidate:" + candidate["id"])
    assert evidence["archived"] and evidence["archive"]["proposal_id"] == proposal["id"]
    assert evidence["quote"] == original[evidence["start"]:evidence["end"]]
    assert evidence["document_hash"] == km._hash(original)
    assert evidence["current_document_hash"] == current_hash
    # Archived read access never relaxes current-source validation or permits review.
    with pytest.raises(InvalidOperationError, match="变化"):
        km._validate_evidence(ident, candidate["evidence"])
    assert approve(ident, km._get_candidate(ident, candidate["id"]))["failures"]
    with pytest.raises(NodeNotFoundError):
        other, _ = book("依据另一本书")
        km.candidate_evidence(other, "candidate:" + candidate["id"])


def test_stale_unapplied_evidence_never_uses_unrelated_journal_archive(isolated, monkeypatch):
    ident, root = book()
    rel = "设定/人物设定.md"
    path = root / rel
    path.write_text("## 沈砚\n- 身份：守桥人\n沈砚的职责是巡查东桥。\n", encoding="utf-8")
    stub(monkeypatch, {"fields": [{"entity": "沈砚", "entity_kind": "character", "field": "职责", "value": "巡查东桥", "quote": "沈砚的职责是巡查东桥。"}]})
    candidate = analyze(ident, rel)[0]
    approve(ident, candidate)
    path.write_text("## 沈砚\n- 身份：作者已经修改的人物\n", encoding="utf-8")
    with pytest.raises(InvalidOperationError, match="变化"):
        km.candidate_evidence(ident, "candidate:" + candidate["id"])
