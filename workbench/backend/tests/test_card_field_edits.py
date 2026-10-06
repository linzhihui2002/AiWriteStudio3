"""Field edits preserve neighboring material and require current evidence."""
import pytest

from workbench.backend.services import asset_card_service as cards
from workbench.backend.services.errors import InvalidOperationError
from workbench.backend.services.file_change_service import FileConflictError
from workbench.backend.tests.test_ai_cards import book, workspace, _write, obj, parse, proof


def test_explicit_rename_delete_and_update_preserve_other_object(book, workspace, monkeypatch):
    text = "## 林远\n- 身份：船工\n- 年龄：二十\n- 目标：寻信\n\n## 赵伯\n- 身份：郎中\n"
    path = _write(workspace, "AI卡片测试", "设定/人物设定.md", text)
    first = obj(text, "林远", fields={"身份": "船工", "年龄": "二十", "目标": "寻信"}, extent=text.split("## 赵伯")[0])
    second = obj(text, "赵伯", fields={"身份": "郎中"}, extent="## 赵伯\n- 身份：郎中\n")
    parse(book, monkeypatch, {"objects": [first, second]})
    card = next(card for card in cards.list_cards(book)["cards"] if card["name"] == "林远")
    assert card["field_meta"]["年龄"]["capabilities"]["delete"]
    cards.update_card(book, card["ref"], {}, expected_hash=card["source_hash"], field_edits=[
        {"original_name": "身份", "new_name": "职业", "value": "信使"},
        {"original_name": "年龄", "delete": True},
        {"original_name": "目标", "value": "找船"},
    ])
    assert path.read_text(encoding="utf-8") == "## 林远\n- 职业：信使\n- 目标：找船\n\n## 赵伯\n- 身份：郎中\n"


def test_property_table_deletion_preserves_source_column_and_footer(book, workspace, monkeypatch):
    text = "## 林远\n| 项目 | 内容 | 来源 |\n| --- | --- | --- |\n| 身份 | 船工 | author |\n| 目标 | 寻信 | model |\n\n自由备注：港口风大。\n"
    path = _write(workspace, "AI卡片测试", "设定/人物设定.md", text)
    raw = obj(text, "林远", extent=text)
    raw["fields"] = [{"name": key, "value": value, "evidence": proof(text, row)} for key, value, row in [("身份", "船工", "| 身份 | 船工 | author |"), ("目标", "寻信", "| 目标 | 寻信 | model |")]]
    parse(book, monkeypatch, {"objects": [raw]})
    card, = cards.list_cards(book)["cards"]
    cards.update_card(book, card["ref"], {}, expected_hash=card["source_hash"], field_edits=[{"original_name": "身份", "new_name": "职业", "value": "信使"}, {"original_name": "目标", "delete": True}])
    assert "| 职业 | 信使 | author |" in path.read_text(encoding="utf-8")
    assert "| 目标 |" not in path.read_text(encoding="utf-8")
    assert path.read_text(encoding="utf-8").endswith("自由备注：港口风大。\n")


def test_stale_hash_duplicate_names_and_structural_injection_rejected(book, workspace, monkeypatch):
    text = "## 林远\n- 身份：船工\n- 目标：寻信\n"
    path = _write(workspace, "AI卡片测试", "设定/人物设定.md", text)
    parse(book, monkeypatch, {"objects": [obj(text, "林远", fields={"身份": "船工", "目标": "寻信"}, extent=text)]})
    card, = cards.list_cards(book)["cards"]
    with pytest.raises(FileConflictError):
        cards.update_card(book, card["ref"], {}, expected_hash="stale", field_edits=[{"original_name": "身份", "delete": True}])
    for operation in ({"original_name": "身份", "new_name": "目标"}, {"original_name": "身份", "value": "船工\n## 伪人物"}):
        with pytest.raises(InvalidOperationError):
            cards.update_card(book, card["ref"], {}, field_edits=[operation])
    assert path.read_text(encoding="utf-8") == text


def test_shared_object_table_column_can_clear_one_value_but_cannot_rename(book, workspace, monkeypatch):
    text = "| 名称 | 身份 | 来源 |\n| --- | --- | --- |\n| 林远 | 船工 | author |\n| 赵伯 | 郎中 | model |\n"
    path = _write(workspace, "AI卡片测试", "设定/人物设定.md", text)
    raw = []
    for name, value, row in [("林远", "船工", "| 林远 | 船工 | author |"), ("赵伯", "郎中", "| 赵伯 | 郎中 | model |")]:
        raw.append({"name": name, "category": "人物", "evidence": proof(text, row), "fields": [{"name": "身份", "value": value, "evidence": proof(text, row)}], "aliases": []})
    parse(book, monkeypatch, {"objects": raw})
    card = next(card for card in cards.list_cards(book)["cards"] if card["name"] == "林远")
    assert card["field_meta"]["身份"]["capabilities"] == {"rename": False, "delete": True, "reason": "该列名由多个对象共用，改名请打开原材料"}
    with pytest.raises(InvalidOperationError):
        cards.update_card(book, card["ref"], {}, field_edits=[{"original_name": "身份", "new_name": "职业"}])
    cards.update_card(book, card["ref"], {}, field_edits=[{"original_name": "身份", "delete": True}])
    assert path.read_text(encoding="utf-8") == text.replace("| 林远 | 船工 |", "| 林远 |  |")


def test_prose_derived_field_cannot_delete_neighboring_sentence(book, workspace, monkeypatch):
    text = "## 林远\n林远是船工，每日为赵伯送药。\n"
    path = _write(workspace, "AI卡片测试", "设定/人物设定.md", text)
    raw = obj(text, "林远", extent=text)
    raw["fields"] = [{"name": "身份", "value": "船工", "evidence": proof(text, "林远是船工，每日为赵伯送药。")}]
    parse(book, monkeypatch, {"objects": [raw]})
    card, = cards.list_cards(book)["cards"]
    assert not card["field_meta"]["身份"]["capabilities"]["delete"]
    with pytest.raises(InvalidOperationError):
        cards.update_card(book, card["ref"], {}, field_edits=[{"original_name": "身份", "delete": True}])
    assert path.read_text(encoding="utf-8") == text
