"""Dedicated fact tools do not turn plans or stale trackers into history."""
import pytest

from workbench.backend.services import (
    chat_tools, chapter_service, ingestion_service, knowledge_lifecycle,
)
from workbench.backend.tests.test_ingestion_commit import book


@pytest.mark.parametrize("kind", ["setting", "state", "foreshadow"])
def test_dedicated_readers_apply_knowledge_lifecycle(book, kind):
    record = lambda ident, lifecycle, body, chapter=1: knowledge_lifecycle.render_record(
        {"id": ident, "review_status": "approved", "lifecycle": lifecycle, "chapter_start": chapter}, body)
    text = "# 陆衡\n作者已确认的基础材料。\n" + record("history", "historical_event", "已发生的铜钥匙旧事。")
    text += record("plan", "planned", "计划中的白玉书信。")
    text += record("draft", "draft", "草稿中的银币事件。")
    text += record("future", "historical_event", "后章才发生的茶园事件。", 3)
    ctx = chat_tools.ToolContext(project_id=book.id, chapter_rel="章节/第0002章.txt")
    if kind == "setting":
        category = next(key for key, value in chat_tools.BASE_CATEGORIES.items() if value == "设定/人物设定.md")
        rel = chat_tools.BASE_CATEGORIES[category]
        read = lambda: chat_tools.tool_read_setting({"category": category}, ctx)
    elif kind == "state":
        rel = chat_tools.STATE_FILE
        read = lambda: chat_tools.tool_read_character_state({}, ctx)
    else:
        rel = chat_tools.FORESHADOW_FILE
        read = lambda: chat_tools.tool_list_foreshadows({}, ctx)
    (book.root / rel).write_text(text, encoding="utf-8")
    result = read()
    assert result["ok"]
    assert "基础材料" in result["text"] and "铜钥匙旧事" in result["text"]
    assert "白玉书信" not in result["text"] and "银币事件" not in result["text"]
    assert "后章才发生" not in result["text"]
    assert "计划中的白玉书信" in chat_tools._read_rel(ctx, rel)


def test_foreshadow_tool_hides_withdrawn_source_tracker(book):
    ingestion_service.ingest_chapter(book.id, book.rel, use_ai=False)
    ctx = chat_tools.ToolContext(project_id=book.id, chapter_rel="章节/第0002章.txt")
    assert "铜钥匙" in chat_tools.tool_list_foreshadows({}, ctx)["text"]
    chapter_service.set_chapter_meta(book.id, book.rel, status="草稿")
    assert "铜钥匙" not in chat_tools.tool_list_foreshadows({}, ctx)["text"]
    assert "铜钥匙" in chat_tools._read_rel(ctx, chat_tools.FORESHADOW_FILE)
