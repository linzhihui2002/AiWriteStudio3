"""Semantic-card behaviour with mock model responses; never contact a provider."""
import copy
import json

import pytest

from workbench.backend.engine import router
from workbench.backend.services import asset_card_colors as colors
from workbench.backend.services import asset_card_service as cards
from workbench.backend.services import generation_service, project_service, prompt_registry_service, proposal_service
from workbench.backend.services.errors import InvalidOperationError
from workbench.backend.tests.test_content import workspace, _write


def proof(text, quote):
    return {"quote": quote, "start": text.index(quote)}


def obj(text, name, category="人物", fields=None, aliases=None, extent=None, source="unknown", source_quote=None):
    return {"name": name, "category": category, "evidence": proof(text, f"## {name}"),
            "extent": proof(text, extent) if extent else None, "source": source,
            "source_evidence": proof(text, source_quote) if source_quote else None,
            "fields": [{"name": key, "value": value, "source": source,
                        "source_evidence": proof(text, source_quote) if source_quote else None,
                        "evidence": proof(text, f"- {key}：{value}")} for key, value in (fields or {}).items()],
            "aliases": [{"name": alias, "evidence": proof(text, f"- 别名：{alias}")} for alias in (aliases or [])]}


@pytest.fixture
def book(workspace, monkeypatch):
    project = project_service.create_project(name="AI卡片测试")
    monkeypatch.setattr(router, "resolve_model", lambda **_: {"provider": "mock", "model": "mock"})
    return project["id"]


def parse(book, monkeypatch, raw, category="人物", force=False):
    calls = []
    def run(**kwargs):
        calls.append(kwargs)
        return {"ok": True, "text": json.dumps(raw, ensure_ascii=False)}
    monkeypatch.setattr(generation_service, "run_task", run)
    task = cards.refresh(book, category=category, force=force, background=False)
    return task, calls


def test_get_never_calls_ai_and_old_cache_is_not_entity_truth(book, workspace, monkeypatch):
    root = workspace.projects / "AI卡片测试"
    old = {"version": 1, "files": {"设定/人物设定.md": {"cards": [{"name": "姓名"}]}},
           "highlights": {"设定/人物设定.md#林玄": "highlight-2"}}
    path = _write(workspace, "AI卡片测试", cards.META_FILE, json.dumps(old, ensure_ascii=False))
    before = path.read_bytes()
    monkeypatch.setattr(generation_service, "run_task", lambda **_: pytest.fail("GET invoked AI"))
    assert cards.list_cards(book, use_ai=True)["cards"] == []
    assert cards.list_highlights(book) == []
    assert path.read_bytes() == before
    assert not (root / "设定" / "姓名.md").exists()


def test_ai_object_from_field_table_and_alias_owns_source(book, workspace, monkeypatch):
    text = "## 林玄\n| 项目 | 内容 | 来源 |\n| 姓名 | 林玄 | author |\n| 身份 | 搜玄录选中者 | author |\n- 别名：玄子\n"
    path = _write(workspace, "AI卡片测试", "设定/人物设定.md", text)
    raw = obj(text, "林玄", aliases=["玄子"], extent=text)
    row = "| 身份 | 搜玄录选中者 | author |"
    raw["fields"] = [{"name": "身份", "value": "搜玄录选中者", "source": "author", "evidence": proof(text, row)}]
    task, calls = parse(book, monkeypatch, {"objects": [raw]})
    assert task["status"] == "completed" and len(calls) == 1
    result = cards.list_cards(book)
    assert [card["name"] for card in result["cards"]] == ["林玄"]
    card = result["cards"][0]
    assert card["fields"] == {"身份": "搜玄录选中者"}
    assert card["field_meta"]["身份"]["source"] == "author"
    assert card["source"] == "unknown"  # Presence of fields is not authorship.
    assert card["aliases"] == ["玄子"]
    assert card["highlight_mode"] == "auto"
    assert path.read_text(encoding="utf-8") == text


def test_foreign_source_and_alias_are_not_attached_to_other_object():
    text = "## 林玄\n- 身份：跑货人。 来源：author\n\n## 赵伯\n- 身份：郎中。 来源：unknown\n- 别名：老赵\n"
    first = obj(text, "林玄", fields={"身份": "跑货人。 来源：author"})
    second = obj(text, "赵伯", fields={"身份": "郎中。 来源：unknown"}, source="author", source_quote="- 身份：跑货人。 来源：author")
    result = cards._validate_objects({"objects": [first, second]}, text, "设定/人物设定.md", "人物")
    assert result[1]["source"] == result[1]["field_meta"]["身份"]["source"] == "unknown"
    first["aliases"] = [{"name": "老赵", "evidence": proof(text, "## 赵伯\n- 身份：郎中。 来源：unknown\n- 别名：老赵")}]
    with pytest.raises(InvalidOperationError, match="不属于"):
        cards._validate_objects({"objects": [first, second]}, text, "设定/人物设定.md", "人物")


def test_invalid_evidence_keeps_last_good_file_result(book, workspace, monkeypatch):
    text = "## 林玄\n- 身份：跑货人\n"
    _write(workspace, "AI卡片测试", "设定/人物设定.md", text)
    raw = {"objects": [obj(text, "林玄", fields={"身份": "跑货人"}, extent=text)]}
    parse(book, monkeypatch, raw)
    before = cards.list_cards(book)["cards"]
    broken = copy.deepcopy(raw)
    broken["objects"][0]["fields"][0]["value"] = "原文没有的掌门"
    task, _ = parse(book, monkeypatch, broken, force=True)
    assert task["status"] == "failed" and task["failures"]
    assert cards.list_cards(book)["cards"] == before


def test_overwide_extent_cannot_borrow_source_or_fields_from_an_omitted_object():
    text = "## 林玄\n- 来源：author\n- 身份：跑货人\n\n## 赵伯\n- 身份：郎中\n"
    second = obj(text, "赵伯", fields={"身份": "郎中"}, extent=text,
                 source="author", source_quote="- 来源：author")
    result = cards._validate_objects({"objects": [second]}, text, "设定/人物设定.md", "人物")
    assert result[0]["source"] == result[0]["field_meta"]["身份"]["source"] == "unknown"
    second["fields"] = obj(text, "林玄", fields={"身份": "跑货人"})["fields"]
    with pytest.raises(InvalidOperationError, match="字段依据不属于"):
        cards._validate_objects({"objects": [second]}, text, "设定/人物设定.md", "人物")


def test_name_field_table_has_independent_bounds_without_a_named_heading():
    text = "| 项目 | 内容 |\n| --- | --- |\n| 姓名 | 林玄 |\n| 身份 | 跑货人 |\n| 姓名 | 赵伯 |\n| 身份 | 郎中 |\n"
    raw = {"name": "林玄", "category": "人物", "evidence": proof(text, "| 姓名 | 林玄 |"),
           "extent": proof(text, text), "fields": [{"name": "身份", "value": "跑货人", "evidence": proof(text, "| 身份 | 跑货人 |")}], "aliases": []}
    result = cards._validate_objects({"objects": [raw]}, text, "设定/人物设定.md", "人物")
    assert result[0]["fields"] == {"身份": "跑货人"}
    raw["fields"] = [{"name": "身份", "value": "郎中", "evidence": proof(text, "| 身份 | 郎中 |")}]
    with pytest.raises(InvalidOperationError, match="字段依据不属于"):
        cards._validate_objects({"objects": [raw]}, text, "设定/人物设定.md", "人物")


def test_fingerprint_reuses_content_and_changes_with_prompt_body(book, workspace, monkeypatch):
    text = "## 林玄\n- 身份：跑货人\n"
    _write(workspace, "AI卡片测试", "设定/人物设定.md", text)
    raw = {"objects": [obj(text, "林玄")]}
    parse(book, monkeypatch, raw)
    task, calls = parse(book, monkeypatch, raw)
    assert task["cached_files"] == 1 and not calls
    get_prompt = prompt_registry_service.get_prompt
    monkeypatch.setattr(prompt_registry_service, "get_prompt", lambda ident: {**get_prompt(ident), "body": get_prompt(ident)["body"] + "\n新规则"})
    task, calls = parse(book, monkeypatch, raw)
    assert task["parsed_files"] == 1 and len(calls) == 1


def test_manual_colour_off_restore_and_new_different_object(book, workspace, monkeypatch):
    text = "## 林玄\n- 身份：剑修\n"
    path = _write(workspace, "AI卡片测试", "设定/人物设定.md", text)
    parse(book, monkeypatch, {"objects": [obj(text, "林玄", fields={"身份": "剑修"})]})
    original = cards.list_cards(book)["cards"][0]
    cards.set_highlight(book, original["ref"], "highlight-1")
    parse(book, monkeypatch, {"objects": [obj(text, "林玄", fields={"身份": "剑修"})]}, force=True)
    again = cards.list_cards(book)["cards"][0]
    assert again["entity_id"] == original["entity_id"] and again["highlight"] == "highlight-1"
    cards.clear_highlight(book, again["ref"])
    assert cards.list_highlights(book)[0]["highlight_mode"] == "off"
    cards.set_highlight(book, again["ref"], "", mode="auto")
    assert cards.list_highlights(book)[0]["highlight_mode"] == "auto"
    replacement = "## 赵伯\n- 身份：剑修\n"
    path.write_text(replacement, encoding="utf-8")
    parse(book, monkeypatch, {"objects": [obj(replacement, "赵伯", fields={"身份": "剑修"})]})
    assert cards.list_cards(book)["cards"][0]["entity_id"] != original["entity_id"]


def test_explicit_name_edit_preserves_id_and_free_text(book, workspace, monkeypatch):
    text = "## 林玄\n- 身份：跑货人\n作者自由备注仍然保留。\n"
    path = _write(workspace, "AI卡片测试", "设定/人物设定.md", text)
    parse(book, monkeypatch, {"objects": [obj(text, "林玄", fields={"身份": "跑货人"}, extent=text)]})
    original = cards.list_cards(book)["cards"][0]
    cards.update_card(book, original["ref"], {"身份": "掌柜"}, name="林远")
    changed = path.read_text(encoding="utf-8")
    assert "作者自由备注仍然保留。" in changed and "## 林远" in changed
    assert cards.list_cards(book)["cards"][0]["stale"]
    with pytest.raises(InvalidOperationError, match="依据已变化"):
        cards.update_card(book, original["ref"], {"身份": "其他"})
    parse(book, monkeypatch, {"objects": [obj(changed, "林远", fields={"身份": "掌柜"}, extent=changed)]})
    assert cards.list_cards(book)["cards"][0]["entity_id"] == original["entity_id"]


def test_delete_does_not_remove_two_objects_or_stale_material(book, workspace, monkeypatch):
    text = "## 林玄\n- 身份：跑货人\n\n## 赵伯\n- 身份：郎中\n"
    path = _write(workspace, "AI卡片测试", "设定/人物设定.md", text)
    first = obj(text, "林玄", fields={"身份": "跑货人"}, extent=text)
    second = obj(text, "赵伯", fields={"身份": "郎中"}, extent=text[text.index("## 赵伯"):])
    parse(book, monkeypatch, {"objects": [first, second]})
    first_card = next(c for c in cards.list_cards(book)["cards"] if c["name"] == "林玄")
    with pytest.raises(InvalidOperationError, match="其他对象"):
        cards.delete_card(book, first_card["ref"])
    assert path.read_text(encoding="utf-8") == text
    second_card = next(c for c in cards.list_cards(book)["cards"] if c["name"] == "赵伯")
    result = cards.delete_card(book, second_card["ref"])
    proposal = proposal_service.get_proposal(result["proposal_id"])
    assert "林玄" in proposal["content"] and "赵伯" not in proposal["content"]
    path.write_text(text + "外部新文字", encoding="utf-8")
    with pytest.raises(InvalidOperationError, match="依据已变化"):
        cards.delete_card(book, second_card["ref"])


def test_legacy_manual_migrates_only_real_entity(book, workspace, monkeypatch):
    text = "## 林玄\n"
    _write(workspace, "AI卡片测试", "设定/人物设定.md", text)
    _write(workspace, "AI卡片测试", cards.META_FILE, json.dumps({"version": 1, "files": {},
           "highlights": {"设定/人物设定.md#林玄": {"color": "highlight-3", "note": "主角"}, "设定/人物设定.md#姓名": "highlight-2"}}))
    parse(book, monkeypatch, {"objects": [obj(text, "林玄")]})
    card = cards.list_cards(book)["cards"][0]
    assert card["highlight"] == "highlight-3" and card["highlight_mode"] == "manual"
    assert [c["name"] for c in cards.list_highlights(book)] == ["林玄"]


def test_many_objects_have_unique_readable_colours_in_all_themes():
    meta = {"entities": {}}
    for index in range(80):
        auto = colors.allocate(meta)
        meta["entities"][str(index)] = {"auto_colors": auto, "highlight_colors": auto}
    for theme in colors.EMPTY_COLORS:
        assert len({e["auto_colors"][theme] for e in meta["entities"].values()}) == 80
    assert all(e["auto_colors"]["light"] != e["auto_colors"]["dark"] for e in meta["entities"].values())


def test_manual_cannot_take_another_objects_reserved_auto_colour(book, workspace, monkeypatch):
    text = "## 林玄\n\n## 赵伯\n"
    _write(workspace, "AI卡片测试", "设定/人物设定.md", text)
    parse(book, monkeypatch, {"objects": [obj(text, "林玄"), obj(text, "赵伯")]})
    first, second = cards.list_cards(book)["cards"]
    cards.clear_highlight(book, first["ref"])
    with pytest.raises(InvalidOperationError, match="已被"):
        cards.set_highlight(book, second["ref"], first["highlight"])
    cards.set_highlight(book, first["ref"], "", mode="auto")
    current = cards.list_highlights(book)
    for theme in colors.EMPTY_COLORS:
        assert len({entry["highlight_colors"][theme] for entry in current}) == 2


def test_cancelled_or_changed_source_result_cannot_commit(book, workspace, monkeypatch):
    text = "## 林玄\n"
    path = _write(workspace, "AI卡片测试", "设定/人物设定.md", text)
    def run(**kwargs):
        job_id = kwargs["context_snapshot"]["job_id"]
        cards.cancel_parse_task(book, job_id)
        return {"ok": True, "text": json.dumps({"objects": [obj(text, "林玄")]})}
    monkeypatch.setattr(generation_service, "run_task", run)
    result = cards.refresh(book, category="人物", background=False)
    assert result["status"] == "cancelled" and not cards.list_cards(book)["cards"]
    def changed(**kwargs):
        path.write_text("## 林远\n", encoding="utf-8")
        return {"ok": True, "text": json.dumps({"objects": [obj(text, "林玄")]})}
    monkeypatch.setattr(generation_service, "run_task", changed)
    result = cards.refresh(book, category="人物", background=False)
    assert result["status"] == "failed" and not cards.list_cards(book)["cards"]


def test_job_creation_failure_rolls_back_active_registry(book, workspace, monkeypatch):
    original = cards._write_meta
    monkeypatch.setattr(cards, "_write_meta", lambda *_: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        cards.refresh(book, category="人物", background=False)
    assert cards._key(book) not in cards._active
    monkeypatch.setattr(cards, "_write_meta", original)
    _write(workspace, "AI卡片测试", "设定/人物设定.md", "")
    assert cards.refresh(book, category="人物", background=False)["status"] == "completed"


def test_custom_category_is_manually_parsed(book, workspace, monkeypatch):
    text = "## 青铜鱼符\n- 功能：开门\n"
    _write(workspace, "AI卡片测试", "设定/法宝设定.md", text)
    result, _ = parse(book, monkeypatch, {"objects": [obj(text, "青铜鱼符", category="法宝", fields={"功能": "开门"})]}, category="法宝")
    assert result["status"] == "completed"
    assert cards.list_cards(book, category="法宝")["count"] == 1


def test_explicit_setting_file_rename_move_and_undo_preserve_object_colour(book, workspace, monkeypatch):
    from workbench.backend.services import tree_service, file_change_service as changes
    text = "## 林玄\n- 身份：跑货人\n"
    _write(workspace, "AI卡片测试", "设定/人物设定.md", text)
    parse(book, monkeypatch, {"objects": [obj(text, "林玄", fields={"身份": "跑货人"})]})
    original = cards.list_cards(book)["cards"][0]
    cards.set_highlight(book, original["ref"], "highlight-4")
    monkeypatch.setattr(generation_service, "run_task", lambda **_: pytest.fail("moving invoked AI"))
    tree_service.rename_node(book, "设定/人物设定.md", "角色档案.md")
    renamed = cards.list_cards(book)["cards"][0]
    assert renamed["entity_id"] == original["entity_id"] and renamed["highlight"] == "highlight-4"
    tree_service.create_node(book, "设定", "扩展", is_dir=True)
    moved = changes.apply_change(book, run_id="card-move", session_id=None, tool_call_id="move",
                                 operation="move", rel_path="设定/角色档案.md", destination="设定/扩展/角色档案.md",
                                 expected_hash=changes.content_hash(text))
    current = cards.list_cards(book)["cards"][0]
    assert current["file"] == "设定/扩展/角色档案.md" and current["entity_id"] == original["entity_id"]
    assert current["evidence"]["rel_path"] == current["file"]
    changes.revert("card-move", [moved["id"]], project_id=book)
    restored = cards.list_cards(book)["cards"][0]
    assert restored["file"] == "设定/角色档案.md" and restored["highlight"] == "highlight-4"
