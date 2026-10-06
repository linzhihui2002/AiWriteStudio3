"""Original synthetic author material; parsing must never rewrite the source."""
import hashlib

import pytest

from workbench.backend.services import material_parser as parser
from workbench.backend.services import bible_service, project_service
from workbench.backend.tests.test_content import workspace, _write


@pytest.mark.parametrize("level", range(1, 7))
def test_named_property_table_is_one_entity_with_exact_field_bounds(level):
    text = f"{'#' * level} 主角：陆汀\n| 项目 | 内容 | 来源 |\n| --- | --- | --- |\n| 姓名 | 陆汀 | author |\n| 身份 | 船工 | model |\n"
    entity, = parser.parse_entities(text, "character")
    assert entity["name"] == "陆汀"
    assert entity["fields"] == {"姓名": "陆汀", "身份": "船工"}
    assert entity["field_sources"] == {"姓名": "author", "身份": "model"}
    assert entity["source"] == "unknown"  # Mixed fields never confer author approval.
    start, end = entity["field_spans"]["身份"]
    assert text[start:end] == "船工"


def test_subfields_placeholders_comments_and_examples_are_not_people():
    text = "---\n# 元数据人物\n---\n# 主角：陆汀\n- 身份：船工\n## 陆汀状态机（动态状态）\n- 位置：码头\n## 配角（待作者补充）\n待补充\n```md\n# 示例人物\n- 来源：author\n```\n<!-- aiw:managed marker -->\n"
    entity, = parser.parse_entities(text, "character")
    assert entity["name"] == "陆汀"
    assert entity["fields"]["位置"] == "码头"
    assert "示例人物" not in entity["fields"]


def test_object_table_name_column_can_follow_an_identifier():
    text = "| 界面 | 名称 | 性质 | 来源 |\n| --- | --- | --- | --- |\n| 现实侧 | 北港 | 商贸城 | author |\n| 异界 | 沧海 | 修行界 | model |\n"
    entities = parser.parse_entities(text, "world")
    assert [entity["name"] for entity in entities] == ["北港", "沧海"]
    assert [entity["source"] for entity in entities] == ["author", "model"]


def test_tracking_tables_and_lists_keep_candidate_states_and_original_spans():
    text = "---\n标题: 伏笔\n---\n| 编号 | 伏笔内容 | 埋设位置 | 计划回收 | 状态 | 来源 |\n| --- | --- | --- | --- | --- | --- |\n| F-1 | 钥匙齿缺口 | 开局 | 长线 | 待作者确认后埋设 | model |\n- [待回收] 铜铃｜埋设：第0001章｜依据：门边铜铃｜计划回收：第0003章\n- 维护说明：请按章节追加\n```md\n- [待回收] 示例｜埋设：第1章\n```\n<!-- marker -->\n"
    plan, planted = parser.parse_foreshadows(text)
    assert plan["is_planned"] and plan["record_id"] == "F-1"
    assert not planted["is_planned"] and planted["status"] == "待回收"
    assert planted["line"] + 3 == planted["source_location"]["line_start"]
    assert planted["document_hash"] == hashlib.sha256(text.encode()).hexdigest()
    for record in (plan, planted):
        start, end = record["spans"]["status"]
        assert text[start:end] == record["status"]
    start, end = planted["spans"]["planned_chapter"]
    assert text[start:end] == "第0003章"


def test_timeline_reads_dual_time_table_and_six_field_list():
    text = "| # | 节点 | 现实侧 | 九幽大千侧 | 事件 | 依据 |\n| --- | --- | --- | --- | --- | --- |\n| 1 | 故事开始前 | 当晚 | 次日 | 绑定 | 作者设定 |\n- 第0001章｜清晨｜北港｜送信｜陆汀｜依据：递信\n"
    table, line = parser.parse_timeline(text)
    assert table["chapter"] == "故事开始前" and table["event"] == "绑定"
    assert table["story_time"] == "现实侧：当晚；九幽大千侧：次日"
    assert line["location"] == "北港" and line["characters"] == "陆汀" and line["evidence"] == "递信"


def test_ledger_reads_named_and_legacy_managed_rows_but_not_resource_definition():
    text = "| 项目 | 来源 | 用途 |\n| --- | --- | --- |\n| 灵石 | 收取所得 | 交易 |\n\n| 灵石 | 第0001章 | +5 | | | 正文依据 |\n"
    row, = parser.parse_ledger(text)
    assert row["item"] == "灵石" and row["change"] == "+5" and row["evidence"] == "正文依据"


def test_bible_contract_distinguishes_source_id_path_and_required_field(workspace):
    book = project_service.create_project(name="结构资料测试")
    _write(workspace, "结构资料测试", "设定/人物设定.md", "# 主角：陆汀\n- 身份：船工\n- 来源：model\n")
    entity, = bible_service.list_entities(book["id"], "character")
    assert entity["ref"] == "character:陆汀" and entity["rel_path"] == "设定/人物设定.md"
    assert entity["source"] == "model"
    missing = bible_service.unknown_fields(book["id"])
    assert all(item["reason"] == "missing_field" and item["rel_path"] == entity["rel_path"] for item in missing)
    bible_service.write_source(book["id"], entity["ref"], "author")
    assert bible_service.list_entities(book["id"], "character")[0]["source"] == "author"
    assert bible_service.unknown_fields(book["id"])  # A source marker cannot fill missing facts.
