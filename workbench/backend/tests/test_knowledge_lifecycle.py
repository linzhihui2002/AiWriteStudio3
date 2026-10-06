"""Reviewed Markdown is the durable authority; derived projections never promote plans."""
import json

import pytest

from workbench.backend import db
from workbench.backend.services import (
    context_service, knowledge_lifecycle as lifecycle, knowledge_service as kb,
    knowledge_candidate_service as candidates, knowledge_store, vector_service,
)
from workbench.backend.tests.test_knowledge import FakeEmbedder, book, chapter, workspace


def record(ident, state, body, **extra):
    return lifecycle.render_record({"id": ident, "candidate_id": ident, "provenance": "model",
        "review_status": "approved", "reviewer": {"decision": "approved", "at": "2026-10-02"},
        "lifecycle": state, "entity_label": "陆汀", "entity_kind": "character", "field": "身份",
        "sources": [{"rel_path": "章节/第0001章.txt", "hash": "original", "quote": "陆汀在旧港修钟。", "start": 0, "end": 9}],
        **extra}, body)


def test_projection_preserves_exact_offsets_and_fails_closed():
    source = "# 原有设定\r\n- 作者：修钟匠\r\n" + record("plan", "planned", "- 未来：登上龙宫")
    source += record("event", "historical_event", "- 历史：抵达旧港", chapter_start=2, chapter_end=2)
    source += "<!-- wb-knowledge {bad json} -->\n- 未审核：隐藏密钥\n<!-- /wb-knowledge -->\n"
    projected = lifecycle.project_text(source, chapter_before=3)
    assert len(projected) == len(source)
    assert "登上龙宫" not in projected and "隐藏密钥" not in projected
    assert "抵达旧港" in projected and "作者：修钟匠" in projected
    offset = source.index("- 历史")
    assert projected[offset:offset + len("- 历史：抵达旧港")] == "- 历史：抵达旧港"
    assert "wb-knowledge" not in lifecycle.project_text(source, preserve_offsets=False)
    assert "登上龙宫" in lifecycle.project_text(source, "planning", preserve_offsets=False)
    assert "抵达旧港" not in lifecycle.project_text(source, chapter_before=2)
    assert lifecycle.approved({"review_status": "ignored", "reviewer": {"decision": "approved"}}) is False
    assert lifecycle.visible({"review_status": "approved", "lifecycle": "setting_fact", "chapter_start": 2, "chapter_end": 3}, chapter_before=5) is False
    assert "损坏记录的秘密" not in lifecycle.project_text('<!-- wb-knowledge {"id":"broken"}\n损坏记录的秘密\n')


@pytest.mark.parametrize("mode", ["keyword", "hybrid"])
def test_indexed_and_fresh_retrieval_filter_plans_but_graph_keeps_author_review(workspace, mode):
    ident, root = book("审核计划边界")
    rel = "设定/人物设定.md"
    text = "## 陆汀\n- 来源：author\n- 职业：修钟匠\n\n"
    text += record("future", "planned", "## 陆汀 · 计划与草稿\n- 身份：神秘的龙宫传人", entity_anchor="a")
    text += record("past", "historical_event", "- 经历：在旧港获赠铜钥匙", chapter_start=1, chapter_end=1)
    (root / rel).write_text(text, encoding="utf-8")
    kb.sync(ident, background=False, use_ai=True)  # legacy flag cannot call a model
    for fresh in (False, True):
        if fresh:
            text += "\n作者补充：旧港仍有钟塔。\n"
            (root / rel).write_text(text, encoding="utf-8")
        assert not any("龙宫传人" in h["text"] for h in kb.search(ident, "龙宫传人", mode=mode)["hits"])
        hits = kb.search(ident, "龙宫传人", profile="planning", mode=mode)["hits"]
        assert any(h.get("knowledge_id") == "future" and h["review_status"] == "approved" for h in hits)
        for hit in hits:
            assert text[hit["start"]:hit["end"]] == hit["text"]
            assert "wb-knowledge" not in hit["text"]
        historical = kb.search(ident, "铜钥匙", mode=mode)["hits"]
        assert any(h.get("knowledge_id") == "past" and h["provenance"] == "model" for h in historical)
    kb.sync(ident, background=False)
    node, = [n for n in kb.graph(ident)["nodes"] if n.get("knowledge_id") == "future"]
    assert node["source"] == "model" and node["review_status"] == "approved" and node["lifecycle"] == "planned"
    assert node["candidate_id"] == "future" and node["sources"][0]["quote"]
    assert node["label"] == "陆汀 · 身份：神秘的龙宫传人"
    assert "## 陆汀 · 计划与草稿" in node["description"]  # source is preserved
    evidence = kb.evidence(ident, node["source_location"]["evidence_id"])
    assert evidence["knowledge_id"] == "future"


def test_context_direct_settings_state_and_foreshadows_share_projection(workspace):
    ident, root = book("直读上下文边界")
    for rel, body in (("设定/人物设定.md", "人物设定"), ("状态/角色状态.md", "角色状态"), ("设定/伏笔管理.md", "伏笔管理")):
        (root / rel).write_text(f"# {body}\n作者旧设定仍然有效。\n"
            + record("plan" + body, "draft", "- 陆汀：尚未确认的龙宫路线")
            + record("old" + body, "historical_event", "- 陆汀：已发生的铜钥匙交接", chapter_start=2, chapter_end=2)
            + record("new" + body, "historical_event", "- 陆汀：未来章节的鲸船登船", chapter_start=5, chapter_end=5), encoding="utf-8")
    output = context_service.assemble(ident, chapter_rel="章节/第0003章.txt", use_retrieval=False,
        exclude_levels=[1, 2, 3, 7, 8], budget_tokens=20000)
    assert {4, 5, 6} <= {b["level"] for b in output["blocks"]}
    text = "\n".join(b["text"] for b in output["blocks"])
    assert "龙宫路线" not in text and "鲸船登船" not in text and "wb-knowledge" not in text
    assert "铜钥匙交接" in text and "作者旧设定" in text
    planning = context_service.assemble(ident, use_retrieval=False, retrieval_profile="planning", exclude_levels=[1, 2, 3, 7, 8], budget_tokens=20000)
    assert "龙宫路线" in "\n".join(b["text"] for b in planning["blocks"])


def test_reviewed_relation_rebuild_binds_selected_same_name_definition(workspace):
    ident, root = book("精确审核实体")
    rel = "设定/人物设定.md"
    (root / rel).write_text("## 陆汀\n- 身份：修钟匠\n\n## 陆汀\n- 身份：渡船人\n\n## 温雪\n- 身份：掌柜\n", encoding="utf-8")
    definitions = candidates.entity_definitions(ident)
    selected = [e for e in definitions if e["label"] == "陆汀"][1]
    target = next(e for e in definitions if e["label"] == "温雪")
    (root / "设定/关系网络.md").write_text(record("relation", "setting_fact", "- 陆汀 → 温雪：师徒",
        kind="relation", subject="陆汀", object="温雪", relation="师徒", subject_kind="character", object_kind="character",
        source_entity=selected["anchor"], target_entity=target["anchor"]), encoding="utf-8")
    kb.sync(ident, background=False)
    graph = kb.graph(ident)
    edge, = [e for e in graph["edges"] if e["relation"] == "师徒"]
    selected_node = next(n for n in graph["nodes"] if n["id"] == edge["source"])
    assert "渡船人" in selected_node["description"]
    assert edge["knowledge_id"] == "relation" and edge["review_status"] == "approved"
    before = edge["id"]
    kb.sync(ident, background=False, force=True)
    assert next(e for e in kb.graph(ident)["edges"] if e["relation"] == "师徒")["id"] == before


def test_reviewed_same_relation_keeps_independent_lifecycle_and_chapter_scope(workspace):
    ident, root = book("同关系不同生命周期")
    (root / "设定/人物设定.md").write_text("## 陆汀\n- 身份：修钟匠\n\n## 温雪\n- 身份：掌柜\n", encoding="utf-8")
    definitions = candidates.entity_definitions(ident)
    source = next(e for e in definitions if e["label"] == "陆汀")
    target = next(e for e in definitions if e["label"] == "温雪")
    common = {"kind": "relation", "subject": "陆汀", "object": "温雪", "relation": "同盟",
        "subject_kind": "character", "object_kind": "character", "source_entity": source["anchor"], "target_entity": target["anchor"]}
    (root / "设定/关系网络.md").write_text(
        record("event-relation", "historical_event", "- 陆汀 → 温雪：同盟", chapter_start=1, chapter_end=1, **common)
        + record("planned-relation", "planned", "- 陆汀 → 温雪：同盟", chapter_start=4, chapter_end=6, **common), encoding="utf-8")
    kb.sync(ident, background=False)
    edges = [e for e in kb.graph(ident)["edges"] if e["relation"] == "同盟"]
    assert len(edges) == 2 and len({e["id"] for e in edges}) == 2
    assert {(e["knowledge_id"], e["lifecycle"], e["chapter_start"], e["chapter_end"]) for e in edges} == {
        ("event-relation", "historical_event", 1, 1), ("planned-relation", "planned", 4, 6)}
    early_edges = [e for e in kb.graph(ident, chapter_before=3)["edges"] if e["relation"] == "同盟"]
    assert len(early_edges) == 1 and early_edges[0]["knowledge_id"] == "event-relation"
    hits = kb.search(ident, "同盟", mode="keyword")["hits"]
    assert {h.get("knowledge_id") for h in hits} == {"event-relation"}
    assert {h.get("knowledge_id") for h in kb.search(ident, "同盟", profile="planning", mode="keyword")["hits"]} == {"event-relation", "planned-relation"}


def test_external_changes_queue_only_changed_material_once_including_drafts(workspace, monkeypatch):
    ident, root = book("外部编辑检测")
    draft = chapter(ident, "陆汀在草稿中修钟。", status="草稿")
    complete = chapter(ident, "温雪保管铜钥匙。")
    queued = []
    monkeypatch.setattr(candidates, "enqueue_analysis", lambda project_id, path=None: queued.append((project_id, path)))
    kb.overview(ident)
    queued.clear()
    (root / draft).write_text("陆汀的草稿新增龙宫路线。", encoding="utf-8")
    kb.overview(ident)
    kb.overview(ident)
    assert queued == [(ident, draft)]
    queued.clear()
    with db.get_conn() as conn:
        conn.execute("UPDATE chapters SET status='发表' WHERE project_id=? AND rel_path=?", (ident, complete))
    kb.overview(ident)
    assert queued == [(ident, complete)]


def test_complete_entity_index_is_independent_of_graph_cap(workspace):
    ident, root = book("完整实体目录")
    (root / "设定/人物设定.md").write_text("\n".join(f"## 人物{index:03d}\n- 身份：修钟匠\n" for index in range(310)), encoding="utf-8")
    kb.sync(ident, background=False)
    assert kb.graph(ident)["truncated"]
    assert kb.entities(ident, kind="character", offset=300)["count"] == 310
    assert len(kb.entities(ident, kind="character", offset=300)["nodes"]) == 10
    assert kb.entities(ident, q="人物309")["nodes"][0]["label"] == "人物309"


@pytest.mark.parametrize("mode", ["keyword", "hybrid"])
def test_raw_outline_is_planning_only_in_index_and_unsynced_fallback(workspace, mode):
    ident, root = book("原始大纲边界")
    path = root / "大纲/大纲.md"
    path.write_text("第九章计划：陆汀登上鲸船。", encoding="utf-8")
    kb.sync(ident, background=False)
    graph = kb.graph(ident, document="大纲/大纲.md", include_content=True)
    assert any(n["kind"] == "document" and n["document"] == "大纲/大纲.md" for n in graph["nodes"])
    for fresh in (False, True):
        if fresh:
            path.write_text("第九章计划：陆汀登上鲸船，找到星灯。", encoding="utf-8")
        for profile in ("history", "review", "continuity", "foreshadow", "setting"):
            assert not kb.search(ident, "鲸船", profile=profile, mode=mode)["hits"]
        hits = kb.search(ident, "鲸船", profile="planning", mode=mode)["hits"]
        assert hits
        proof = kb.evidence(ident, hits[0]["evidence_id"])
        assert proof["text"] == hits[0]["text"]
        assert path.read_text(encoding="utf-8")[proof["start"]:proof["end"]] == proof["text"]


def test_scoped_reviewed_current_state_matches_applicable_chapter(workspace):
    ident, root = book("状态适用范围")
    (root / "状态/角色状态.md").write_text("# 当前角色状态\n"
        + record("state", "setting_fact", "- 陆汀位置：鲸船", chapter_start=2, chapter_end=4), encoding="utf-8")
    kb.sync(ident, background=False)
    assert kb.search(ident, "鲸船", chapter_rel="章节/第0003章.txt", mode="keyword")["hits"]
    assert not kb.search(ident, "鲸船", chapter_rel="章节/第0002章.txt", mode="keyword")["hits"]
    assert not kb.search(ident, "鲸船", chapter_rel="章节/第0006章.txt", mode="keyword")["hits"]
