"""Grounding, book isolation and indexing lifecycle; no model/network calls."""
from pathlib import Path
from types import SimpleNamespace
from threading import Event
import hashlib
import json
import time

import pytest

from workbench.backend import config, db
from workbench.backend.services import (chapter_service, generation_service, knowledge_service as kb,
                                       knowledge_store, project_service, vector_service)
from workbench.backend.services import knowledge_candidate_service as candidates


class FakeEmbedder:
    fingerprint = "test-semantic-v1"

    def info(self):
        return {"name": "test", "offline": True, "available": True}

    def embed_query(self, text):
        # Deliberately small fixed space: unit tests verify routing, not semantic quality.
        return [1.0, 0.0] if "鱼符" in text or "信物" in text else [0.0, 1.0]

    def embed_documents(self, texts):
        return [self.embed_query(text) for text in texts]


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    candidates.shutdown()
    runtime, projects = tmp_path / ".workbench", tmp_path / "projects"
    runtime.mkdir()
    projects.mkdir()
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(config, "PROJECTS_DIR", projects)
    monkeypatch.setattr(config, "RUNTIME_DIR", runtime)
    monkeypatch.setattr(config, "DB_PATH", runtime / "workbench.db")
    db.init_db()
    config.ensure_runtime_dirs()
    monkeypatch.setattr(vector_service, "resolve_embedder", lambda *_: FakeEmbedder())
    monkeypatch.setattr(generation_service, "run_task", lambda **_: pytest.fail("unexpected model invocation"))
    # Existing lifecycle hooks are tested explicitly without racing a background worker.
    monkeypatch.setattr(kb, "enqueue", lambda *args, **kwargs: None)
    monkeypatch.setattr(candidates, "enqueue", lambda *args, **kwargs: None)
    yield SimpleNamespace(root=tmp_path, runtime=runtime, projects=projects)
    candidates.shutdown()
    kb.shutdown()


def book(name):
    project = project_service.create_project(name=name)
    return project["id"], project_service.get_project_dir(project["id"])[1]


def chapter(project_id, text, status="完成"):
    row = chapter_service.create_chapter(project_id, "测试章节")
    chapter_service.save_chapter(project_id, row["rel_path"], text, status=status)
    return row["rel_path"]


def seed_legacy_relation(project_id, rel, subject="陆汀", target="温雪", relation="交给", quote="陆汀把铜钥匙交给温雪。"):
    """Reproduce a pre-review DB without calling the retired sync extraction."""
    doc = next(doc for doc in kb._corpus(project_id) if doc["rel_path"] == rel)
    with kb._connect(project_id) as conn:
        piece = conn.execute("SELECT * FROM kb_chunks WHERE document_id=? AND text LIKE ?", (doc["id"], "%" + quote + "%")).fetchone()
        assert piece
        endpoints = []
        for label in (subject, target):
            node = kb._node(conn, "character", label, rel, piece["id"], "model", doc["chapter_number"])
            if not conn.execute("SELECT 1 FROM kb_node_details WHERE node_id=?", (node,)).fetchone():
                start = doc["text"].index(quote)
                kb._details(conn, node, doc, start, start + len(quote), piece["id"])
            endpoints.append(node)
        kb._edge(conn, *endpoints, relation, "model", [piece["id"]], rel, quote, projection="model")


def test_books_have_distinct_databases_and_no_cross_book_evidence(workspace):
    a, _ = book("甲书")
    b, _ = book("乙书")
    chapter(a, "沈砚将青铜鱼符交给柳青，作为入门信物。")
    chapter(b, "柳青在雪岭收到白玉书信。")
    for ident in (a, b):
        kb.sync(ident, background=False, use_ai=False)
    assert knowledge_store.identity(a)["db_path"] != knowledge_store.identity(b)["db_path"]
    assert knowledge_store.identity(a)["knowledge_key"] != knowledge_store.identity(b)["knowledge_key"]
    assert kb.search(a, "青铜鱼符", mode="keyword")["hits"]
    assert not kb.search(b, "青铜鱼符", mode="keyword")["hits"]


def test_status_and_future_chapter_filters_and_explicit_draft(workspace):
    ident, _ = book("状态边界")
    early = chapter(ident, "沈砚收藏青铜鱼符。")
    later = chapter(ident, "柳青拿走青铜鱼符。", "发表")
    draft = chapter(ident, "草稿里银色鱼符尚未定稿。", "草稿")
    kb.sync(ident, background=False, use_ai=False)
    hits = kb.search(ident, "鱼符", chapter_rel=later, mode="keyword")["hits"]
    assert {hit["rel_path"] for hit in hits} == {early}
    overview = kb.overview(ident)
    draft_row = next(doc for doc in overview["documents"] if doc["rel_path"] == draft)
    assert draft_row["index_status"] == "excluded"
    assert draft_row["index_label"] == "未纳入历史索引"
    assert overview["counts"]["indexed_documents"] == 2
    assert overview["counts"]["eligible_documents"] == 2
    assert overview["counts"]["excluded_documents"] == 1
    selected = kb.search(ident, "银色鱼符", document=draft, mode="keyword")["hits"]
    assert selected and selected[0]["status"] == "explicit"
    assert selected[0]["source_tier"] == selected[0]["provenance"] == "unknown"
    # Caller-supplied review text is not an approved historical fact.
    hits = kb.search(ident, "银色鱼符", document={"rel_path": draft, "text": "银色鱼符"}, mode="keyword")["hits"]
    assert all(hit["rel_path"] != draft for hit in hits)


def test_stale_content_immediately_invalidates_graph_and_evidence(workspace):
    ident, root = book("新鲜度")
    rel = chapter(ident, "沈砚收藏青铜鱼符。")
    kb.sync(ident, background=False, use_ai=False)
    old = kb.search(ident, "青铜鱼符", mode="keyword")["hits"][0]
    assert kb.graph(ident)["nodes"]
    (root / rel).write_text("这段正文已经改成茶园采收。", encoding="utf-8")
    assert not kb.search(ident, "青铜鱼符", mode="keyword")["hits"]
    assert not kb.graph(ident)["nodes"]
    with pytest.raises(Exception, match="失效"):
        kb.evidence(ident, old["id"])
    assert kb.search(ident, "茶园采收", mode="keyword")["hits"]  # fresh lexical fallback
    (root / rel).unlink()
    assert not kb.search(ident, "茶园采收", mode="keyword")["hits"]


def test_demoted_chapter_and_cleared_document_never_recalled(workspace):
    ident, root = book("撤销发布")
    rel = chapter(ident, "沈砚收藏青铜鱼符。")
    setting = root / "设定/物品设定.md"
    setting.write_text("## 鱼符\n- 来历：旧港凭证\n", encoding="utf-8")
    kb.sync(ident, background=False, use_ai=False)
    with db.get_conn() as conn:
        conn.execute("UPDATE chapters SET status='草稿' WHERE project_id=?", (ident,))
    setting.write_text("", encoding="utf-8")
    assert not kb.search(ident, "鱼符", mode="keyword")["hits"]
    assert not kb.graph(ident)["nodes"]


def test_model_space_fingerprint_mismatch_degrades_to_keywords(workspace, monkeypatch):
    ident, _ = book("模型空间")
    chapter(ident, "沈砚收藏青铜鱼符。")
    kb.sync(ident, background=False, use_ai=False)
    embedder = FakeEmbedder()
    embedder.fingerprint = "same-dimension-different-model"
    monkeypatch.setattr(vector_service, "resolve_embedder", lambda *_: embedder)
    result = kb.search(ident, "信物")
    assert not result["hits"]
    assert result["degraded_reason"]
    assert kb.search(ident, "鱼符")["hits"][0]["source"] == "keyword"


def test_graph_uses_real_setting_entities_and_evidence_bounds(workspace):
    ident, root = book("真实图")
    (root / "设定/人物设定.md").write_text("## 沈砚\n- 目标：修复旧港\n\n| 名称 | 说明 |\n| 柳青 | 信使 |\n", encoding="utf-8")
    rel = chapter(ident, "沈砚将青铜鱼符交给柳青。\n柳青赶往旧港。")
    kb.sync(ident, background=False, use_ai=False)
    data = kb.graph(ident)
    assert {"沈砚", "柳青"} <= {node["label"] for node in data["nodes"]}
    assert any(edge["relation"] == "提及" for edge in data["edges"])
    assert not any(edge["relation"] == "师徒" for edge in data["edges"])
    for hit in kb.search(ident, "青铜鱼符", mode="keyword")["hits"]:
        text = (root / hit["rel_path"]).read_text(encoding="utf-8")
        assert text[hit["start"]:hit["end"]] == hit["text"]
        assert hit["line_start"] >= 1 and hit["line_end"] >= hit["line_start"]
    assert kb.graph(ident, document=rel, limit=1)["truncated"]


def test_ai_extraction_rejects_fabricated_quote_and_keeps_model_source(workspace, monkeypatch):
    ident, _ = book("抽取依据")
    rel = chapter(ident, "沈砚将青铜鱼符交给柳青。")
    from workbench.backend.engine import router
    monkeypatch.setattr(router.get_engine("direct-api"), "available", lambda: True)
    payload = '{"relations":[{"subject":"沈砚","subject_kind":"character","relation":"交给","object":"柳青","object_kind":"character","quote":"沈砚将青铜鱼符交给柳青。"},{"subject":"沈砚","relation":"师父","object":"柳青","quote":"沈砚是柳青的师父。"}]}'
    monkeypatch.setattr(generation_service, "run_task", lambda **_: {"ok": True, "text": payload})
    monkeypatch.setattr(candidates, "_model_fingerprint", lambda *_: {"provider": "test", "model": "test"})
    kb.sync(ident, background=False)
    assert not any(edge["origin"] == "model" for edge in kb.graph(ident)["edges"])
    job = candidates.extract(ident, paths=[rel], types=["relation"], background=False)
    assert job["status"] == "completed" and job["candidate_count"] == 1
    edges = kb.graph(ident)["edges"]
    accepted = [edge for edge in edges if edge["origin"] == "model"]
    assert len(accepted) == 1 and accepted[0]["relation"] == "交给"
    assert accepted[0]["quote"] == "沈砚将青铜鱼符交给柳青。"
    assert accepted[0]["evidence_ids"]
    assert accepted[0]["review_status"] == "pending" and accepted[0]["candidate_id"]
    proof = candidates.candidate_evidence(ident, accepted[0]["evidence_ids"][0])
    assert proof["text"] == accepted[0]["quote"]


def test_extraction_failure_preserves_base_graph_and_error(workspace, monkeypatch):
    ident, _ = book("失败兜底")
    rel = chapter(ident, "沈砚将青铜鱼符交给柳青。")
    from workbench.backend.engine import router
    monkeypatch.setattr(router.get_engine("direct-api"), "available", lambda: True)
    monkeypatch.setattr(generation_service, "run_task", lambda **_: {"ok": False, "error_message": "模型离线"})
    monkeypatch.setattr(candidates, "_model_fingerprint", lambda *_: {"provider": "test", "model": "test"})
    kb.sync(ident, background=False)
    job = candidates.extract(ident, paths=[rel], background=False)
    assert kb.graph(ident)["nodes"]
    assert job["status"] == "failed" and "模型离线" in job["failed_documents"][0]["error"]
    assert "关系抽取失败" not in kb.overview(ident)["degraded_reason"]
    assert kb.search(ident, "鱼符", mode="keyword")["hits"]


def test_incremental_idempotence_and_deleted_documents(workspace):
    ident, root = book("增量")
    rel = chapter(ident, "沈砚收藏青铜鱼符。")
    kb.sync(ident, background=False, use_ai=False)
    before = kb.overview(ident)["counts"]
    kb.sync(ident, background=False, use_ai=False)
    assert kb.overview(ident)["counts"] == before
    (root / rel).unlink()
    kb.sync(ident, background=False, use_ai=False)
    assert kb.overview(ident)["counts"]["chunks"] == 0
    assert not kb.graph(ident)["nodes"]


def test_no_raw_teardown_or_outside_links_in_corpus(workspace):
    ident, root = book("参考边界")
    (root / "拆书/第三方").mkdir(parents=True)
    (root / "拆书/第三方/原文.md").write_text("第三方青铜鱼符", encoding="utf-8")
    assert not kb.search(ident, "鱼符", mode="keyword")["hits"]
    outside = workspace.root / "outside.txt"
    outside.write_text("外部鱼符", encoding="utf-8")
    try:
        (root / "设定/链接.txt").symlink_to(outside)
    except OSError:
        pytest.skip("symlink privilege unavailable")
    assert not kb.search(ident, "鱼符", mode="keyword")["hits"]


def test_answer_no_evidence_makes_no_model_call(workspace):
    ident, _ = book("无依据")
    events = list(kb.ask(ident, "未存在的鱼符"))
    assert events[0]["type"] == "evidence"
    assert events[-1]["type"] == "done"
    assert "无法确定" in events[1]["text"]


def test_result_limits_and_one_document_cap(workspace):
    ident, _ = book("检索预算")
    chapter(ident, "青铜鱼符凭证。\n" * 600)
    kb.sync(ident, background=False, use_ai=False)
    hits = kb.search(ident, "鱼符", mode="keyword", limit=100)["hits"]
    assert len(hits) <= 2


def test_ids_are_book_bound_even_with_identical_paths_and_text(workspace):
    a, _ = book("相同甲")
    b, _ = book("相同乙")
    for ident in (a, b):
        chapter(ident, "沈砚收藏青铜鱼符。")
        kb.sync(ident, background=False, use_ai=False)
    left = kb.search(a, "鱼符", mode="keyword")["hits"][0]
    right = kb.search(b, "鱼符", mode="keyword")["hits"][0]
    assert left["id"] != right["id"]
    assert {node["id"] for node in kb.graph(a)["nodes"]}.isdisjoint({node["id"] for node in kb.graph(b)["nodes"]})
    with pytest.raises(Exception, match="失效"):
        kb.evidence(b, left["id"])


def test_exact_entity_priority_and_default_graph_does_not_dump_chunks(workspace):
    ident, root = book("精确实体")
    (root / "设定/人物设定.md").write_text("## 柳青\n- 职业：旧港信使\n", encoding="utf-8")
    chapter(ident, "沈砚讨论信使任务，旧港生意尚未恢复。")
    rel = chapter(ident, "柳青将一封书信放入木盒。")
    kb.sync(ident, background=False, use_ai=False)
    hits = kb.search(ident, "柳青的信使任务", mode="keyword")["hits"]
    assert hits[0]["rel_path"] == "设定/人物设定.md"
    data = kb.graph(ident)
    assert not any(node["kind"] == "content" for node in data["nodes"])
    assert not any(node["kind"] == "content" for node in kb.graph(ident, document=rel)["nodes"])
    assert any(node["kind"] == "content" for node in kb.graph(ident, document=rel, include_content=True)["nodes"])


def test_free_memo_and_empty_frontmatter_not_indexed(workspace):
    ident, root = book("语料纪律")
    (root / "备忘录").mkdir(exist_ok=True)
    (root / "备忘录/便签.md").write_text("鱼符也许可以改成玩笑，作者备忘非事实。", encoding="utf-8")
    (root / "设定/物品设定.md").write_text("---\n标题: 青铜鱼符\n---\n", encoding="utf-8")
    kb.sync(ident, background=False, use_ai=False)
    assert not kb.search(ident, "鱼符", mode="keyword")["hits"]
    assert not any(doc["rel_path"].startswith("备忘录/") for doc in kb.overview(ident)["documents"])


def test_teardown_evidence_is_a_real_fact_card_span_and_summaries_are_not_evidence(workspace, monkeypatch):
    ident, root = book("可回读出处")
    rel = chapter(ident, "沈砚在旧港收起鱼符。")
    meta = root / ".meta"
    meta.mkdir(exist_ok=True)
    (meta / "summaries.json").write_text(
        '{"' + rel + '":"隐藏摘要没有可定位的原文行"}', encoding="utf-8")
    card = root / "拆书/样本/事实卡.md"
    card.parent.mkdir(parents=True, exist_ok=True)
    card.write_text(
        "# 样本事实卡\n"
        "| 节奏 | 旧港潮汐暗号 | 第2章 | 原文摘录 |\n"
        "| 情绪 | 悬峰灵石黑匣 | 依据缺失 | 无 |\n", encoding="utf-8")
    from workbench.backend.services import teardown_service
    monkeypatch.setattr(teardown_service, "list_facts", lambda *_: [
        {"target": "样本", "conclusion": "旧港潮汐暗号", "evidence": "第2章", "quote": "原文摘录"},
        {"target": "样本", "conclusion": "悬峰灵石黑匣", "evidence": "依据缺失", "quote": ""},
    ])
    kb.sync(ident, background=False, use_ai=False)
    assert all("#" not in doc["rel_path"] for doc in kb.overview(ident)["documents"])
    assert not kb.search(ident, "隐藏摘要没有可定位的原文行", mode="keyword")["hits"]
    assert not kb.search(ident, "悬峰灵石黑匣", mode="keyword", profile="teardown")["hits"]
    hit = kb.search(ident, "旧港潮汐暗号", mode="keyword", profile="teardown")["hits"][0]
    assert hit["rel_path"] == "拆书/样本/事实卡.md"
    original = card.read_text(encoding="utf-8")
    assert original[hit["start"]:hit["end"]] == hit["text"]
    assert hit["document_hash"] == hashlib.sha256(original.encode()).hexdigest()
    assert hit["source_tier"] == "reference" and hit["provenance"] == "unknown"
    assert hit["line_start"] == hit["line_end"] == 2
    assert kb.evidence(ident, hit["id"])["rel_path"] == hit["rel_path"]


def test_present_state_cannot_prove_past_chapter(workspace):
    ident, root = book("时态边界")
    chapter(ident, "柳青在旧港等待归船。")
    target = chapter(ident, "沈砚乘船离开旧港。")
    (root / "状态/角色状态.md").write_text("## 柳青\n- 当前位置：雪岭\n", encoding="utf-8")
    kb.sync(ident, background=False, use_ai=False)
    assert kb.search(ident, "雪岭", mode="keyword")["hits"]
    assert not kb.search(ident, "雪岭", mode="keyword", chapter_rel=target)["hits"]


def test_retrieval_rank_source_is_distinct_from_material_provenance(workspace):
    ident, root = book("证据分级")
    chapter(ident, "沈砚收藏青铜鱼符。")
    (root / "设定/物品设定.md").write_text("## 鱼符\n- 来历：旧港\n", encoding="utf-8")
    kb.sync(ident, background=False, use_ai=False)
    hits = kb.search(ident, "鱼符", mode="keyword")["hits"]
    tiers = {hit["kind"]: (hit["source"], hit["source_tier"], hit["provenance"]) for hit in hits}
    assert tiers["chapter"][0] in {"keyword", "hybrid"}
    assert tiers["chapter"][1:] == ("author", "author")
    assert tiers["setting"][0] in {"keyword", "hybrid"}
    assert tiers["setting"][1:] == ("unknown", "unknown")


def test_explicit_draft_evidence_can_be_opened_without_indexing(workspace):
    ident, _ = book("草稿回读")
    rel = chapter(ident, "银色鱼符暂为草稿。", "草稿")
    hit = kb.search(ident, "银色鱼符", document=rel, mode="keyword")["hits"][0]
    assert kb.evidence(ident, hit["id"])["status"] == "explicit"
    assert kb.overview(ident)["counts"]["chunks"] == 0


def test_enable_gate_and_recover_durable_jobs(workspace, monkeypatch):
    ident, _ = book("恢复任务")
    assert not kb.auto_enabled(ident)
    knowledge_store.set_preference(ident, "knowledge_enabled", True)
    assert kb.auto_enabled(ident)
    with knowledge_store.connect(ident) as conn:
        kb._init(conn)
        conn.execute("INSERT INTO kb_jobs(id,status) VALUES ('resume','running')")
    calls = []
    monkeypatch.setattr(kb, "enqueue", lambda project_id, **_: calls.append(project_id))
    kb.recover()
    assert calls == [ident]
    assert kb.overview(ident)["job"]["status"] == "interrupted"


def test_sse_stream_is_real_and_marks_changed_evidence_invalid(workspace, monkeypatch):
    ident, root = book("流式回答")
    rel = chapter(ident, "沈砚收藏青铜鱼符。")
    kb.sync(ident, background=False, use_ai=False)
    from workbench.backend.engine import router
    monkeypatch.setattr(router.get_engine("direct-api"), "available", lambda: True)

    def stream(**kwargs):
        assert kwargs["context_snapshot"]["evidence_ids"]
        yield {"delta": "沈砚收藏"}
        yield {"delta": "青铜鱼符。[1]"}
        (root / rel).write_text("原文已变化。", encoding="utf-8")
        yield {"done": True, "ok": True}

    monkeypatch.setattr(generation_service, "stream_task", stream)
    events = list(kb.ask(ident, "鱼符", mode="keyword"))
    assert [event["type"] for event in events] == ["evidence", "delta", "delta", "error", "done"]
    assert events[-2]["invalidate_answer"]


def test_out_of_range_answer_citation_is_rejected(workspace, monkeypatch):
    ident, _ = book("引用校验")
    chapter(ident, "沈砚收藏青铜鱼符。")
    kb.sync(ident, background=False, use_ai=False)
    from workbench.backend.engine import router
    monkeypatch.setattr(router.get_engine("direct-api"), "available", lambda: True)
    monkeypatch.setattr(generation_service, "stream_task", lambda **_: iter([{"delta": "错误引用[9]"}, {"done": True, "ok": True}]))
    events = list(kb.ask(ident, "鱼符", mode="keyword"))
    assert any(event.get("invalidate_answer") for event in events)


def test_uncited_factual_answer_and_partial_stream_are_invalidated(workspace, monkeypatch):
    ident, _ = book("回答验证")
    chapter(ident, "沈砚收藏青铜鱼符。")
    kb.sync(ident, background=False, use_ai=False)
    from workbench.backend.engine import router
    monkeypatch.setattr(router.get_engine("direct-api"), "available", lambda: True)
    monkeypatch.setattr(generation_service, "stream_task", lambda **_: iter([
        {"delta": "沈砚收藏青铜鱼符。"}, {"done": True, "ok": True}]))
    events = list(kb.ask(ident, "鱼符", mode="keyword"))
    assert any(event.get("invalidate_answer") and "缺少" in event["message"] for event in events)
    monkeypatch.setattr(generation_service, "stream_task", lambda **_: iter([
        {"delta": "沈砚"}, {"done": True, "ok": False, "error_message": "连接中断"}]))
    events = list(kb.ask(ident, "鱼符", mode="keyword"))
    assert any(event.get("invalidate_answer") and "连接中断" in event["message"] for event in events)
    monkeypatch.setattr(generation_service, "stream_task", lambda **_: iter([
        {"delta": "证据不足，无法确定。"}, {"done": True, "ok": True}]))
    events = list(kb.ask(ident, "鱼符", mode="keyword"))
    assert not any(event["type"] == "error" for event in events)


def test_overview_never_counts_stale_rows_and_vector_requires_current_space(workspace, monkeypatch):
    ident, root = book("显示状态")
    rel = chapter(ident, "沈砚收藏青铜鱼符。")
    before = kb.overview(ident)
    assert before["counts"]["eligible_documents"] == 1
    assert before["counts"]["indexed_documents"] == 0
    assert before["documents"][0]["index_status"] == "pending"
    assert not before["vector_ready"]
    kb.sync(ident, background=False, use_ai=False)
    indexed = kb.overview(ident)
    assert indexed["counts"]["documents"] == 1
    assert indexed["counts"]["chunks"] == 1
    assert indexed["counts"]["nodes"] > 0
    assert indexed["index_version"] == kb._INDEX_VERSION
    assert indexed["vector_ready"]
    embedder = FakeEmbedder()
    embedder.fingerprint = "changed-model-with-same-dimension"
    monkeypatch.setattr(vector_service, "resolve_embedder", lambda *_: embedder)
    assert not kb.overview(ident)["vector_ready"]
    (root / rel).write_text("茶园正在采收。", encoding="utf-8")
    stale = kb.overview(ident)
    assert stale["documents"][0]["index_status"] == "stale"
    assert stale["counts"]["indexed_documents"] == 0
    assert stale["counts"]["nodes"] == stale["counts"]["edges"] == 0
    assert not stale["vector_ready"]


def test_new_sync_fences_old_worker_after_cancel_and_restore(workspace, monkeypatch):
    ident, root = book("竞态隔离")
    rel = chapter(ident, "旧章写着青铜鱼符。")
    started, release = Event(), Event()
    calls = 0

    def delayed_embed(texts):
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            assert release.wait(10)
        return [[1.0, 0.0] for _ in texts]

    embedder = FakeEmbedder()
    embedder.embed_documents = delayed_embed
    monkeypatch.setattr(vector_service, "resolve_embedder", lambda *_: embedder)
    # The old worker is blocked outside the per-book DB lock while embedding.
    kb.sync(ident, background=True, use_ai=False)
    assert started.wait(10)
    kb.cancel(ident)
    (root / rel).write_text("新章改成白玉书信。", encoding="utf-8")
    kb.sync(ident, background=True, use_ai=False)
    release.set()
    for _ in range(100):
        view = kb.overview(ident)
        if view["job"] and view["job"]["status"] == "done" and view["counts"]["indexed_documents"] == 1:
            break
        time.sleep(0.05)
    else:
        pytest.fail("replacement sync did not finish")
    assert not kb.search(ident, "青铜鱼符", mode="keyword")["hits"]
    assert kb.search(ident, "白玉书信", mode="keyword")["hits"]
    assert kb.overview(ident)["vector_ready"]


def test_api_search_and_sse_contract(workspace):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from workbench.backend.api.knowledge import router
    ident, _ = book("接口契约")
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    response = client.get(f"/api/projects/{ident}/knowledge")
    assert response.status_code == 200 and response.json()["knowledge_key"]
    assert client.post(f"/api/projects/{ident}/retrieval/search", json={"query": "鱼符", "mode": "keyword"}).status_code == 200
    stream = client.post(f"/api/projects/{ident}/knowledge/ask", json={"query": "鱼符", "mode": "keyword"})
    assert stream.status_code == 200
    assert '"type": "evidence"' in stream.text and '"type": "done"' in stream.text
    assert client.post(f"/api/projects/{ident}/retrieval/search", json={"query": "鱼符", "limit": 9}).status_code == 422


def test_same_named_entities_are_distinct_by_definition_source(workspace):
    ident, root = book("同名消歧")
    (root / "设定/人物设定.md").write_text("## 沈砚\n- 身份：旧港信使\n", encoding="utf-8")
    (root / "设定/人物古代设定.md").write_text("## 沈砚\n- 身份：古代将军\n", encoding="utf-8")
    rel = chapter(ident, "沈砚在窗前等人。")
    kb.sync(ident, background=False, use_ai=False)
    data = kb.graph(ident)
    people = [node for node in data["nodes"] if node["kind"] == "character" and node["label"] == "沈砚"]
    assert len(people) == 2 and len({node["id"] for node in people}) == 2
    assert {node["document"] for node in people} == {"设定/人物设定.md", "设定/人物古代设定.md"}
    assert all(node["source"] == "unknown" for node in people)
    # An unqualified name mention cannot be attached to both definitions.
    assert not any(edge["document"] == rel and edge["relation"] == "提及" for edge in data["edges"])


def test_alias_requires_declared_field_and_never_inferred_from_cooccurrence(workspace):
    ident, root = book("别名依据")
    (root / "设定/人物设定.md").write_text("## 沈砚\n- 别名：阿砚、小沈\n\n## 柳青\n- 职业：信使\n", encoding="utf-8")
    chapter(ident, "沈砚与柳青站在院子里，老周和小周还未到场。")
    kb.sync(ident, background=False, use_ai=False)
    edges = kb.graph(ident)["edges"]
    aliases = [edge for edge in edges if edge["relation"] == "别名"]
    assert len(aliases) == 2
    assert all(edge["quote"] == "- 别名：阿砚、小沈" and edge["evidence_ids"] for edge in aliases)
    assert not any(edge["relation"] in {"朋友", "亲属", "兄弟"} for edge in edges)


def test_model_exact_unique_declared_identity_keeps_provenance(workspace, monkeypatch):
    ident, root = book("统一身份")
    (root / "设定/人物设定.md").write_text("## 沈砚\n- 职业：信使\n\n## 柳青\n- 职业：掌柜\n", encoding="utf-8")
    (root / ".meta").mkdir(exist_ok=True)
    (root / ".meta/bible_sources.json").write_text('{"character:沈砚":{"source":"author"},"character:柳青":{"source":"unknown"}}', encoding="utf-8")
    rel = chapter(ident, "沈砚将青铜鱼符交给柳青。")
    from workbench.backend.engine import router
    monkeypatch.setattr(router.get_engine("direct-api"), "available", lambda: True)
    payload = '{"relations":[{"subject":"沈砚","subject_kind":"character","relation":"交给","object":"柳青","object_kind":"character","quote":"沈砚将青铜鱼符交给柳青。"}]}'
    monkeypatch.setattr(generation_service, "run_task", lambda **_: {"ok": True, "text": payload})
    monkeypatch.setattr(candidates, "_model_fingerprint", lambda *_: {"provider": "test", "model": "test"})
    kb.sync(ident, background=False)
    job = candidates.extract(ident, paths=[rel], types=["relation"], background=False)
    assert job["status"] == "completed"
    nodes = kb.graph(ident)["nodes"]
    assert len([node for node in nodes if node["label"] == "沈砚"]) == 1
    assert next(node for node in nodes if node["label"] == "沈砚")["source"] == "author"
    assert next(node for node in nodes if node["label"] == "柳青")["source"] == "unknown"
    relation = next(edge for edge in kb.graph(ident)["edges"] if edge["origin"] == "model")
    assert relation["quote"] and relation["review_status"] == "pending"


def test_current_state_is_labeled_current_and_keeps_source_quote(workspace):
    ident, root = book("当前状态")
    (root / "设定/人物设定.md").write_text("## 沈砚\n- 职业：信使\n", encoding="utf-8")
    (root / "状态/角色状态.md").write_text("## 沈砚\n- 伤势：左臂包扎\n- 位置：旧港\n", encoding="utf-8")
    kb.sync(ident, background=False, use_ai=False)
    data = kb.graph(ident)
    states = [node for node in data["nodes"] if node["kind"] == "state"]
    assert len(states) == 2
    links = [edge for edge in data["edges"] if edge["relation"] == "当前状态"]
    assert len(links) == 2 and all(edge["quote"] and edge["evidence_ids"] for edge in links)
    assert not any(edge["relation"] == "受伤于第1章" for edge in data["edges"])


def test_duplicate_same_file_headings_keep_distinct_definitions(workspace):
    ident, root = book("同页同名")
    (root / "设定/人物设定.md").write_text("## 沈砚\n- 身份：信使\n\n## 沈砚\n- 身份：古代将军\n", encoding="utf-8")
    kb.sync(ident, background=False, use_ai=False)
    people = [node for node in kb.graph(ident)["nodes"] if node["label"] == "沈砚"]
    assert len(people) == 2
    assert all(node["ambiguous"] for node in people)
