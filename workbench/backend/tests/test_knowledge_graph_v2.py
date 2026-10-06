"""Graph-v2 projections use original, temporary story material; never a model API."""
import hashlib
import json

import pytest

from workbench.backend.services import generation_service, knowledge_service as kb, vector_service
from workbench.backend.services import knowledge_candidate_service as candidates
from workbench.backend.tests.test_knowledge import FakeEmbedder, book, chapter, seed_legacy_relation, workspace


def write(root, relative, text):
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return text


def sync(ident, *, ai=False):
    result = kb.sync(ident, background=False, use_ai=ai)
    with kb._connect(ident) as conn:
        jobs = conn.execute("SELECT status,error FROM kb_jobs").fetchall()
    assert all(row["status"] == "done" for row in jobs), [dict(row) for row in jobs]
    return result


def entities(ident, kind="character", **kwargs):
    return [node for node in kb.graph(ident, **kwargs)["nodes"] if node["kind"] == kind]


@pytest.mark.parametrize("level", range(1, 7))
def test_all_markdown_heading_levels_have_grounded_definitions(workspace, level):
    ident, root = book(f"标题级别{level}")
    text = write(root, "设定/人物设定.md", f"{'#' * level} 陆汀\n- 身份：修钟匠\n- 来源：author\n")
    sync(ident)
    node, = entities(ident)
    assert node["label"] == "陆汀"
    assert node["source"] == "author"
    assert node["description"] == text.rstrip()
    definitions = [edge for edge in kb.graph(ident)["edges"] if edge["relation"] == "定义"]
    assert len(definitions) == 1
    assert definitions[0]["target"] == node["id"]
    assert definitions[0]["evidence_ids"]


def test_foreshadow_numbered_bold_entries_remain_plans_with_exact_locations(workspace):
    ident, root = book("伏笔意向")
    relative = "设定/伏笔管理.md"
    text = write(root, relative,
                 "# 伏笔管理\n\n## 意向登记\n尚无正文章节，以下为计划。\n"
                 "1. **缺角铜铃**：第1章门边听见旧铃，来源：model。\n"
                 "2. **空白航海图**：第1章码头发现折痕，来源：unknown。\n")
    chapter(ident, "陆汀在码头修理钟摆。")
    sync(ident)
    nodes = entities(ident, "foreshadow")
    assert {node["label"] for node in nodes} == {"缺角铜铃", "空白航海图"}
    graph = kb.graph(ident)
    assert {edge["relation"] for edge in graph["edges"] if edge["source"] in {node["id"] for node in nodes}} >= {"计划于章节"}
    for node in nodes:
        assert node["planned"] is True
        position = node["source_location"]
        assert text[position["start"]:position["end"]] == node["description"]
        assert "\n" not in node["description"]
        assert position["line_start"] == position["line_end"]


@pytest.mark.parametrize("preamble", ["", "以下伏笔均为计划，尚未进入埋设状态。\n"])
def test_foreshadow_plans_do_not_leak_to_siblings_and_completed_state_overrides_preamble(workspace, preamble):
    ident, root = book("伏笔独立状态")
    write(root, "设定/伏笔管理.md", "# 伏笔管理\n" + preamble
          + "1. **旧钥匙**：计划第1章出现。\n"
          + "2. **断剑缺口**：第1章已埋设，第6章已回收，原计划第8章回收。\n"
          + "3. **铜盒暗格**：第1章门边露出一道划痕。\n")
    chapter(ident, "陆汀在门边看见断剑缺口和铜盒上的划痕。")
    sync(ident)
    nodes = {node["label"]: node for node in entities(ident, "foreshadow")}
    assert nodes["旧钥匙"]["planned"] is True
    assert nodes["断剑缺口"]["planned"] is False
    assert nodes["铜盒暗格"]["planned"] is bool(preamble)
    relations = {edge["relation"] for edge in kb.graph(ident)["edges"]
                 if edge["source"] == nodes["断剑缺口"]["id"]}
    assert "登记于章节" in relations
    assert "计划于章节" not in relations


@pytest.mark.parametrize("fence", ["```", "~~~~"])
def test_frontmatter_and_fenced_tables_and_lists_never_become_definitions(workspace, fence):
    ident, root = book("示例材料隔离")
    text = ("---\n| 名称 | 状态 |\n| 元数据铜牌 | 计划 |\n"
            "1. **元数据钥匙**：计划登记。\n---\n# 伏笔管理\n"
            f"{fence}markdown\n| 名称 | 状态 |\n| 示例铜牌 | 计划 |\n"
            "1. **示例钥匙**：计划登记。\n"
            # A different marker or too-short marker cannot close the fence.
            + ("~~~\n" if fence == "```" else "~~~\n````\n")
            + "2. **围栏内断剑**：仍为示例。\n"
            + f"{fence}\n"
            + "| 名称 | 状态 |\n| --- | --- |\n| 缺角铜铃 | 第1章已埋设 |\n"
            + "\n1. **空白航海图**：第1章已回收。\n")
    write(root, "设定/伏笔管理.md", text)
    sync(ident)
    nodes = {node["label"]: node for node in entities(ident, "foreshadow")}
    assert set(nodes) == {"缺角铜铃", "空白航海图"}
    for node in nodes.values():
        assert node["planned"] is False
        location = node["source_location"]
        assert text[location["start"]:location["end"]] == node["description"]
        assert location["line_start"] == text.count("\n", 0, location["start"]) + 1


@pytest.mark.parametrize(("field", "expected"), [
    ("- 来源：author", "author"),
    ("- 来源：model（author 未给材料）", "model"),
    ("- 来源：unknown", "unknown"),
    ("- 来源：author / model", "unknown"),
    ("- 身份：船工", "unknown"),
])
def test_file_source_is_not_inferred_author_confirmation(workspace, field, expected):
    ident, root = book("来源分级")
    write(root, "设定/人物设定.md", f"# 陆汀\n{field}\n")
    sync(ident)
    node, = entities(ident)
    assert node["source"] == expected
    definitions = [edge for edge in kb.graph(ident)["edges"] if edge["relation"] == "定义"]
    assert definitions[0]["origin"] == expected


def test_bible_source_metadata_overrides_file_source(workspace):
    ident, root = book("作者确认")
    write(root, "设定/人物设定.md", "# 陆汀\n- 来源：model\n")
    write(root, ".meta/bible_sources.json", json.dumps({"character:陆汀": {"source": "author"}}, ensure_ascii=False))
    sync(ident)
    assert entities(ident)[0]["source"] == "author"


def test_generic_placeholders_fields_frontmatter_and_fenced_headings_are_not_entities(workspace):
    ident, root = book("排除占位")
    write(root, "设定/人物设定.md",
          "---\n# 假元数据人物\n---\n# 人物设定\n## 待建人物\n待补充\n"
          "```markdown\n# 示例人物\n```\n## 陆汀\n- 身份：修钟匠\n"
          "### 基本信息\n年龄未知\n### 外貌\n左手有茧\n")
    sync(ident)
    assert [node["label"] for node in entities(ident)] == ["陆汀"]


def test_same_name_definitions_keep_distinct_roles_and_do_not_guess_mentions(workspace):
    ident, root = book("同名人物")
    write(root, "设定/人物设定.md",
          "# 陆汀\n- 身份：港口修钟匠\n- 来源：author\n\n"
          "# 陆汀\n- 身份：北山守林员\n- 来源：model\n")
    rel = chapter(ident, "陆汀在岸边等候天亮。")
    sync(ident)
    nodes = entities(ident)
    assert len(nodes) == 2
    assert len({node["id"] for node in nodes}) == 2
    assert all(node["ambiguous"] for node in nodes)
    assert {node["source"] for node in nodes} == {"author", "model"}
    assert any("修钟匠" in node["description"] for node in nodes)
    assert any("守林员" in node["description"] for node in nodes)
    graph = kb.graph(ident, document=rel)
    assert not any(node["kind"] == "character" for node in graph["nodes"])


def test_model_sourced_same_name_cards_remain_distinct_definitions(workspace):
    ident, root = book("模型同名卡片")
    write(root, "设定/人物设定.md",
          "# 陆汀\n- 身份：修钟匠\n- 来源：model\n\n"
          "# 陆汀\n- 身份：守林员\n- 来源：model\n")
    sync(ident)
    nodes = entities(ident)
    assert len(nodes) == 2
    assert len({node["id"] for node in nodes}) == 2
    assert all(node["source"] == "model" and node["ambiguous"] for node in nodes)
    assert {node["source_location"]["line_start"] for node in nodes} == {1, 5}


def test_empty_parent_groups_are_not_people_and_field_subsections_belong_to_card(workspace):
    ident, root = book("嵌套人物卡")
    text = write(root, "设定/人物设定.md",
                 "# 海港居民\n\n## 陆汀\n### 基本信息\n- 身份：修钟匠\n"
                 "### 目标\n修好大钟，归还铜钥匙。\n### 来源\n- 来源：author\n\n"
                 "## 温雪\n- 身份：灯塔管理员\n")
    sync(ident)
    nodes = {node["label"]: node for node in entities(ident)}
    assert set(nodes) == {"陆汀", "温雪"}
    card = nodes["陆汀"]
    assert card["description"] == text[text.index("## 陆汀"):text.index("## 温雪")].rstrip()
    assert card["source"] == "author"
    assert "归还铜钥匙" in card["description"]
    assert "灯塔管理员" not in card["description"]


def test_definition_location_selects_its_section_in_original_file(workspace):
    ident, root = book("小节定位")
    relative = "设定/人物设定.md"
    text = write(root, relative,
                 "---\ncategory: character\n---\n# 人物设定\n\n"
                 "## 陆汀\n- 身份：修钟匠\n- 目标：修好港口大钟\n\n"
                 "## 温雪\n- 身份：灯塔管理员\n")
    sync(ident)
    nodes = {node["label"]: node for node in entities(ident)}
    node = nodes["陆汀"]
    location = node["source_location"]
    expected_start = text.index("## 陆汀")
    expected_end = text.index("## 温雪")
    assert node["description"] == text[expected_start:expected_end].rstrip()
    assert location["start"] == expected_start
    assert location["end"] == expected_start + len(node["description"])
    assert location["rel_path"] == relative
    assert location["document_hash"] == hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert location["line_start"] == text.count("\n", 0, expected_start) + 1
    assert location["line_end"] == text.count("\n", 0, location["end"] - 1) + 1
    proof = kb.evidence(ident, location["evidence_id"])
    assert proof["hash"] == location["document_hash"]
    assert proof["start"] <= location["start"] < proof["end"]


def test_document_membership_includes_mentions_and_defaults_hide_content_nodes(workspace):
    ident, root = book("正文提及")
    relative = "设定/人物设定.md"
    write(root, relative, "# 陆汀\n- 身份：修钟匠\n")
    rel = chapter(ident, "陆汀把新刻度写在钟面上。")
    sync(ident)
    graph = kb.graph(ident)
    node = entities(ident)[0]
    assert {relative, rel} <= set(node["documents"])
    assert node["id"] in graph["document_nodes"][relative]
    assert node["id"] in graph["document_nodes"][rel]
    assert not any(node["kind"] == "content" for node in graph["nodes"])
    focused = kb.graph(ident, document=rel)
    assert node["id"] in {item["id"] for item in focused["nodes"]}
    assert any(edge["target"] == node["id"] and edge["relation"] == "提及" for edge in focused["edges"])
    assert any(node["kind"] == "content" for node in kb.graph(ident, include_content=True)["nodes"])


def test_document_mentions_preserve_all_matching_chunks_instead_of_last_chunk(workspace):
    ident, root = book("连续提及证据")
    write(root, "设定/人物设定.md", "# 陆汀\n- 身份：修钟匠\n")
    rel = chapter(ident, "\n\n".join(f"陆汀在第{number}次检修时拆开铜盖，逐项记下钟摆偏差与发条温度。" for number in range(90)))
    sync(ident)
    graph = kb.graph(ident, document=rel)
    declared, = [node for node in graph["nodes"] if node["kind"] == "character"]
    source, = [node for node in graph["nodes"] if node["kind"] == "chapter"]
    mention, = [edge for edge in graph["edges"] if edge["source"] == source["id"]
                and edge["target"] == declared["id"] and edge["relation"] == "提及"]
    with kb._connect(ident) as conn:
        expected = {row["id"] for row in conn.execute(
            "SELECT c.id,c.text FROM kb_chunks c JOIN kb_documents d ON c.document_id=d.id WHERE d.rel_path=?",
            (rel,)) if "陆汀" in row["text"]}
    assert len(expected) >= 3
    assert set(mention["evidence_ids"]) == expected
    assert expected <= set(declared["evidence_ids"])
    assert declared["id"] in graph["document_nodes"][rel]


def test_declared_short_name_matches_only_the_card_with_that_name_field(workspace):
    ident, root = book("显式名称归属")
    write(root, "设定/人物设定.md",
          "# 陆汀（本体）\n- 名称：陆汀（正式名，作者）\n- 身份：修钟匠\n\n"
          "# 陆汀（临时身份）\n- 名称：待定\n- 身份：渡船过客\n")
    rel = chapter(ident, "陆汀给钟摆换上了一根细绳。")
    sync(ident)
    all_cards = {node["label"]: node for node in entities(ident)}
    assert set(all_cards) == {"陆汀（本体）", "陆汀（临时身份）"}
    matched = kb.graph(ident, document=rel)
    assert {node["label"] for node in matched["nodes"] if node["kind"] == "character"} == {"陆汀（本体）"}
    assert rel in all_cards["陆汀（本体）"]["documents"]
    assert rel not in all_cards["陆汀（临时身份）"]["documents"]
    assert all_cards["陆汀（本体）"]["id"] in matched["document_nodes"][rel]


def test_qualified_title_without_a_name_field_does_not_guess_a_short_name(workspace):
    ident, root = book("标题不推断简称")
    write(root, "设定/人物设定.md", "# 陆汀（本体）\n- 身份：修钟匠\n")
    rel = chapter(ident, "陆汀独自留在钟楼。")
    sync(ident)
    assert entities(ident)[0]["label"] == "陆汀（本体）"
    assert not entities(ident, document=rel)


def test_duplicate_explicit_short_names_do_not_auto_assign_a_chapter_mention(workspace):
    ident, root = book("显式同名歧义")
    write(root, "设定/人物设定.md",
          "# 陆汀（本体）\n- 名称：陆汀（作者确认）\n- 身份：修钟匠\n\n"
          "# 陆汀（临时身份）\n- 名称：陆汀\n- 身份：守林员\n")
    rel = chapter(ident, "陆汀把斗篷挂在门后。")
    sync(ident)
    assert len(entities(ident)) == 2
    assert not entities(ident, document=rel)
    assert all(rel not in node["documents"] for node in entities(ident))


def set_model_relation(monkeypatch):
    from workbench.backend.engine import router
    monkeypatch.setattr(router.get_engine("direct-api"), "available", lambda: True)
    calls = []

    def extract(**kwargs):
        calls.append(kwargs)
        return {"ok": True, "text": json.dumps({"relations": [{
            "subject": "陆汀", "subject_kind": "character", "relation": "交给",
            "object": "温雪", "object_kind": "character", "quote": "陆汀把铜钥匙交给温雪。",
        }]}, ensure_ascii=False)}

    monkeypatch.setattr(generation_service, "run_task", extract)
    return calls


def test_changed_author_card_is_hidden_before_sync_even_with_bound_live_model_evidence(workspace, monkeypatch):
    ident, root = book("失效设定不可借正文存活")
    relative = "设定/人物设定.md"
    write(root, relative, "# 陆汀\n- 身份：旧钟塔的修钟匠\n- 来源：author\n")
    rel = chapter(ident, "陆汀把铜钥匙交给温雪。")
    sync(ident, ai=True)
    seed_legacy_relation(ident, rel)
    original, = [node for node in entities(ident) if node["label"] == "陆汀"]
    assert original["source"] == "author"
    # This reproduces the dangerous state: the stored author node itself has
    # both its setting evidence and live chapter evidence from a model edge.
    with kb._connect(ident) as conn:
        row = conn.execute("SELECT evidence_ids FROM kb_nodes WHERE id=?", (original["id"],)).fetchone()
        source_paths = set()
        for evidence_id in json.loads(row["evidence_ids"]):
            source_paths.add(conn.execute(
                "SELECT d.rel_path FROM kb_chunks c JOIN kb_documents d ON c.document_id=d.id WHERE c.id=?",
                (evidence_id,)).fetchone()[0])
    assert {relative, rel} <= source_paths
    old_counts = kb.overview(ident)["counts"]
    write(root, relative, "# 程泊\n- 身份：新来的织网工\n- 来源：author\n")
    graph = kb.graph(ident, include_content=True)
    overview = kb.overview(ident)
    assert original["id"] not in {node["id"] for node in graph["nodes"]}
    assert all(node["document"] != relative for node in graph["nodes"])
    assert all("旧钟塔的修钟匠" not in node["description"] for node in graph["nodes"])
    assert not any(original["id"] in {edge["source"], edge["target"]} for edge in graph["edges"])
    assert overview["counts"]["nodes"] == len(graph["nodes"]) < old_counts["nodes"]
    assert overview["counts"]["edges"] == len(graph["edges"]) < old_counts["edges"]
    assert next(doc for doc in overview["documents"] if doc["rel_path"] == relative)["index_status"] == "stale"
    assert not any(edge.get("projection") == "model" for edge in graph["edges"])


def test_v1_projection_upgrade_reuses_vectors_and_grounded_model_relations(workspace, monkeypatch):
    ident, root = book("仅升级图谱")
    write(root, "设定/人物设定.md", "# 陆汀\n- 身份：修钟匠\n- 来源：author\n")
    rel = chapter(ident, "陆汀把铜钥匙交给温雪。")
    sync(ident, ai=True)
    source_before = (root / rel).read_bytes()
    seed_legacy_relation(ident, rel)
    with kb._connect(ident) as conn:
        chunks_before = [tuple(row) for row in conn.execute("SELECT * FROM kb_chunks ORDER BY id")]
        conn.execute("UPDATE kb_meta SET value=? WHERE key='graph_version'", (json.dumps("grounded-graph-v1"),))
        conn.execute("UPDATE kb_meta SET value=? WHERE key='index_version'", (json.dumps(kb._VECTOR_VERSION + ":grounded-graph-v1"),))
        conn.execute("ALTER TABLE kb_edges DROP COLUMN projection")

    class NoEncoding(FakeEmbedder):
        def embed_documents(self, texts):
            pytest.fail("a graph-only upgrade must not re-embed source documents")

    monkeypatch.setattr(vector_service, "resolve_embedder", lambda *_: NoEncoding())
    monkeypatch.setattr(generation_service, "run_task", lambda **_: pytest.fail("a graph-only upgrade must not regenerate relations"))
    graph = kb.graph(ident)
    assert graph["graph_version"] == kb._GRAPH_VERSION
    assert graph["graph_version"] != "grounded-graph-v1"
    assert {node["label"] for node in graph["nodes"]} >= {"陆汀", "温雪"}
    relation, = [edge for edge in graph["edges"] if edge["origin"] == "model"]
    assert relation["relation"] == "交给"
    assert relation["quote"] == "陆汀把铜钥匙交给温雪。"
    assert relation["review_status"] == "pending" and relation["candidate_id"]
    assert (root / rel).read_bytes() == source_before
    sync(ident, ai=True)
    with kb._connect(ident) as conn:
        assert [tuple(row) for row in conn.execute("SELECT * FROM kb_chunks ORDER BY id")] == chunks_before
    assert len([edge for edge in kb.graph(ident)["edges"] if edge["relation"] == "交给"]) == 1


def test_model_provenance_card_deletion_does_not_leave_card_projection(workspace, monkeypatch):
    ident, root = book("删除模型卡片")
    relative = "设定/人物设定.md"
    write(root, relative, "# 陆汀\n- 身份：修钟匠\n- 来源：model\n\n# 旧船主\n- 来源：model\n")
    rel = chapter(ident, "陆汀把铜钥匙交给温雪。")
    sync(ident, ai=True)
    seed_legacy_relation(ident, rel)
    assert "旧船主" in {node["label"] for node in kb.graph(ident)["nodes"]}
    original, = candidates.list_candidates(ident, "pending")["candidates"]
    assert original["entity"]["document"] == relative
    proof_before = dict(original["evidence"])
    chapter_before = (root / rel).read_bytes()
    with kb._connect(ident) as conn:
        doc_id = conn.execute("SELECT id FROM kb_documents WHERE rel_path=?", (rel,)).fetchone()[0]
        vectors_before = [tuple(row) for row in conn.execute("SELECT * FROM kb_chunks WHERE document_id=? ORDER BY id", (doc_id,))]
    (root / relative).unlink()
    sync(ident, ai=True)
    graph = kb.graph(ident)
    assert "旧船主" not in {node["label"] for node in graph["nodes"]}
    assert not any(node["document"] == relative for node in graph["nodes"])
    # The quote remains true, but its chosen entity definition was deleted.
    # Do not silently turn that identity into a new chapter-only model entity.
    assert not any(edge["origin"] == "model" for edge in graph["edges"])
    stale, = candidates.list_candidates(ident, "stale")["candidates"]
    assert stale["id"] == original["id"] and stale["legacy_edge_id"] == original["legacy_edge_id"]
    assert stale["entity"]["anchor"] == original["entity"]["anchor"]
    assert stale["entity"]["document"] == relative and "实体定义" in stale["stale_reason"]
    assert stale["evidence"] == proof_before and stale["suggested_value"] == "交给"
    proof = candidates.candidate_evidence(ident, "candidate:" + stale["id"])
    assert proof["status"] == "stale" and proof["rel_path"] == rel
    assert proof["text"] == "陆汀把铜钥匙交给温雪。"
    assert (root / rel).read_text(encoding="utf-8")[proof["start"]:proof["end"]] == proof["text"]
    assert proof["document_hash"] == proof_before["document_hash"]
    result = candidates.review(ident, [{"id": stale["id"], "version": stale["version"], "decision": "approve"}])
    assert not result["proposals"] and result["failures"]
    assert "仅待审核" in result["failures"][0]["error"]
    assert (root / rel).read_bytes() == chapter_before and not (root / relative).exists()
    for node in graph["nodes"]:
        assert relative not in node["documents"]
        assert node["source_location"]["rel_path"] != relative
    with kb._connect(ident) as conn:
        assert not conn.execute("SELECT 1 FROM kb_edges WHERE document=?", (relative,)).fetchone()
        assert not conn.execute("SELECT 1 FROM kb_nodes WHERE document=?", (relative,)).fetchone()
        assert [tuple(row) for row in conn.execute("SELECT * FROM kb_chunks WHERE document_id=? ORDER BY id", (doc_id,))] == vectors_before


def test_graph_caps_300_nodes_and_never_returns_dangling_edges(workspace):
    ident, root = book("大书局部图")
    write(root, "设定/人物设定.md", "\n".join(f"# 海员{number:03d}\n- 职责：维护第{number}号钟表\n" for number in range(325)))
    sync(ident)
    graph = kb.graph(ident, limit=10000, include_content=True)
    assert graph["count"] == len(graph["nodes"]) == 300
    assert graph["total"] > 300
    assert graph["truncated"] is True
    ids = {node["id"] for node in graph["nodes"]}
    assert all(edge["source"] in ids and edge["target"] in ids for edge in graph["edges"])
    assert all(set(members) <= ids for members in graph["document_nodes"].values())


def test_graph_v2_identity_and_membership_are_isolated_between_matching_books(workspace):
    first, left = book("同名甲书")
    second, right = book("同名乙书")
    for ident, root in ((first, left), (second, right)):
        write(root, "设定/人物设定.md", "# 陆汀\n- 身份：修钟匠\n")
        chapter(ident, "陆汀擦净塔楼上的钟面。")
        sync(ident)
    graphs = [kb.graph(ident) for ident in (first, second)]
    assert graphs[0]["knowledge_key"] != graphs[1]["knowledge_key"]
    assert graphs[0]["project_id"] == first and graphs[1]["project_id"] == second
    assert {node["id"] for node in graphs[0]["nodes"]}.isdisjoint(node["id"] for node in graphs[1]["nodes"])
    assert {edge["id"] for edge in graphs[0]["edges"]}.isdisjoint(edge["id"] for edge in graphs[1]["edges"])
    source = entities(first)[0]["source_location"]
    with pytest.raises(Exception, match="失效"):
        kb.evidence(second, source["evidence_id"])
    assert kb.graph(second, center=entities(first)[0]["id"])["nodes"] == []
