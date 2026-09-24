"""内容层单测：设定卡片 / Story Bible / 大纲规划（全部零 AI 依赖）。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from workbench.backend import config, db
from workbench.backend.services import (
    asset_card_service,
    bible_service,
    outline_service,
    project_service,
    prompt_registry_service,
)


@pytest.fixture()
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    projects = tmp_path / "projects"
    runtime = tmp_path / ".workbench"
    templates = tmp_path / "templates"
    for directory in (projects, runtime, templates):
        directory.mkdir()
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(config, "PROJECTS_DIR", projects)
    monkeypatch.setattr(config, "RUNTIME_DIR", runtime)
    monkeypatch.setattr(config, "DB_PATH", runtime / "workbench.db")
    db.init_db()
    config.ensure_runtime_dirs()
    prompt_registry_service.ensure_registry()

    from workbench.backend.engine import router

    monkeypatch.setattr(router.get_engine("dsh-headless"), "available", lambda: False)
    return SimpleNamespace(projects=projects, runtime=runtime, root=tmp_path)


def _write(workspace: SimpleNamespace, project: str, rel: str, content: str) -> Path:
    path = workspace.projects / project / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


# ─────────────────────────── 设定卡片 ───────────────────────────


def test_cards_skeleton_and_cache(workspace: SimpleNamespace) -> None:
    project = project_service.create_project(name="卡片书")
    project_id = project["id"]
    _write(
        workspace, "卡片书", "设定/人物设定.md",
        "---\n标题: 人物设定\n---\n\n"
        "## 沈砚\n- 性格：沉默，算得清\n- 目标：把货取回来\n\n"
        "## 老周\n- 性格：油滑\n\n"
        "| 周伯 | 船老大，欠沈砚人情 |\n",
    )

    data = asset_card_service.list_cards(project_id)
    names = [card["name"] for card in data["cards"]]
    assert "沈砚" in names and "老周" in names and "周伯" in names
    assert data["count"] == 3
    assert data["counts_by_category"]["人物"] == 3
    assert data["palette"], "必须有 token 化色板"

    # 缓存指纹：内容未变 → 第二次刷新走缓存
    refreshed = asset_card_service.refresh(project_id, category="人物")
    assert refreshed["parsed_files"] == 0
    assert refreshed["cached_files"] == 1

    # 手改 md → 缓存失效重解析
    _write(
        workspace, "卡片书", "设定/人物设定.md",
        "---\n标题: 人物设定\n---\n\n## 沈砚\n- 性格：沉默\n- 弱点：轻信旧友\n",
    )
    refreshed = asset_card_service.refresh(project_id, category="人物")
    assert refreshed["parsed_files"] == 1
    data = asset_card_service.list_cards(project_id)
    assert data["count"] == 1
    assert data["cards"][0]["fields"].get("弱点") == "轻信旧友"


def test_cards_highlight_persist_and_filter(workspace: SimpleNamespace) -> None:
    project = project_service.create_project(name="高亮书")
    project_id = project["id"]
    _write(workspace, "高亮书", "设定/人物设定.md",
           "---\n标题: 人物设定\n---\n\n## 主角\n- 性格：直\n\n## 反派\n- 性格：阴\n")

    cards = asset_card_service.list_cards(project_id)["cards"]
    main = next(card for card in cards if card["name"] == "主角")
    villain = next(card for card in cards if card["name"] == "反派")

    asset_card_service.set_highlight(project_id, main["ref"], "highlight-1")
    asset_card_service.set_highlight(project_id, villain["ref"], "highlight-4")

    # 重新读取（模拟关闭重开页面）→ 高亮保持
    again = asset_card_service.list_cards(project_id)
    highlights = {card["name"]: card["highlight"] for card in again["cards"]}
    assert highlights["主角"] == "highlight-1"
    assert highlights["反派"] == "highlight-4"

    # 不写入 md
    text = (workspace.projects / "高亮书" / "设定" / "人物设定.md").read_text(encoding="utf-8")
    assert "highlight" not in text

    # 仅显示高亮筛选
    filtered = asset_card_service.list_cards(project_id, highlight_only=True)
    assert filtered["count"] == 2

    # 非法色值被拒
    with pytest.raises(Exception):
        asset_card_service.set_highlight(project_id, main["ref"], "purple-gradient")

    asset_card_service.clear_highlight(project_id, main["ref"])
    assert asset_card_service.list_cards(project_id)["cards"]
    assert len(asset_card_service.list_highlights(project_id)) == 1


def test_cards_write_back_preserves_free_text(workspace: SimpleNamespace) -> None:
    project = project_service.create_project(name="写回书")
    project_id = project["id"]
    _write(
        workspace, "写回书", "设定/势力设定.md",
        "---\n标题: 势力设定\n---\n\n"
        "## 漕帮\n- 首领：周伯\n\n"
        "这里是作者的自由书写段落，不该被改写。\n\n"
        "## 盐商\n- 首领：钱七\n",
    )

    asset_card_service.refresh(project_id, force=True)
    asset_card_service.update_card(project_id, "设定/势力设定.md#漕帮",
                                   {"首领": "周伯（已退位）", "规模": "三百人"})

    text = (workspace.projects / "写回书" / "设定" / "势力设定.md").read_text(encoding="utf-8")
    assert "周伯（已退位）" in text
    assert "规模：三百人" in text
    assert "这里是作者的自由书写段落，不该被改写。" in text
    assert "## 盐商" in text and "钱七" in text


def test_cards_add_via_proposal_and_chapter_line(workspace: SimpleNamespace) -> None:
    project = project_service.create_project(name="新增书")
    project_id = project["id"]
    _write(workspace, "新增书", "设定/物品设定.md", "---\n标题: 物品设定\n---\n\n")

    result = asset_card_service.add_card(project_id, "物品", "青铜鱼符",
                                        {"来源": "老周所赠"})
    assert result["via"] == "proposal"

    from workbench.backend.services import chapter_service, proposal_service

    proposal = proposal_service.get_proposal(result["proposal_id"])
    assert proposal["diff"], "新增卡片也必须先展示 diff"
    proposal_service.apply_proposal(result["proposal_id"])
    text = (workspace.projects / "新增书" / "设定" / "物品设定.md").read_text(encoding="utf-8")
    assert "青铜鱼符" in text and "老周所赠" in text

    chapter = chapter_service.create_chapter(project_id, "第一章")
    chapter_service.save_chapter(project_id, chapter["rel_path"],
                                 "青铜鱼符在灯下泛出暗光。青铜鱼符上刻着两个字。")
    line = asset_card_service.chapter_line(project_id, "设定/物品设定.md#青铜鱼符")
    assert line["total"] == 1
    assert line["occurrences"][0]["count"] == 2


# ─────────────────────────── Story Bible ───────────────────────────


def test_bible_overview_and_source_grading(workspace: SimpleNamespace) -> None:
    project = project_service.create_project(name="圣经书")
    project_id = project["id"]
    _write(workspace, "圣经书", "设定/世界设定.md",
           "---\n标题: 世界设定\n---\n\n## 北境\n- 三年一雪\n")
    _write(workspace, "圣经书", "设定/人物设定.md",
           "---\n标题: 人物设定\n---\n\n## 沈砚\n- 性格：沉默\n")

    overview = bible_service.overview(project_id)
    assert overview["counts"]["world"] == 1
    assert overview["counts"]["character"] == 1
    assert overview["unknown_count"] >= 1  # 来源未标注 → unknown

    unknown = bible_service.unknown_fields(project_id)
    assert any(item["name"] == "沈砚" for item in unknown)

    bible_service.write_source(project_id, "character:沈砚", "author", note="作者手写")
    assert not any(item["ref"] == "character:沈砚" for item in bible_service.unknown_fields(project_id))


def test_bible_foreshadow_kind(workspace: SimpleNamespace) -> None:
    project = project_service.create_project(name="伏笔书")
    project_id = project["id"]
    _write(workspace, "伏笔书", "设定/伏笔管理.md",
           "---\n标题: 伏笔管理\n---\n\n- [待回收] 崩断的缆绳｜埋设：第0001章｜依据：缆绳断得蹊跷\n")

    entities = bible_service.list_entities(project_id, "foreshadow")
    assert entities and entities[0]["status"] == "待回收"
    overview = bible_service.overview(project_id)
    assert overview["foreshadow_open"] == 1


# ─────────────────────────── 大纲规划 ───────────────────────────


def test_outline_flow_without_ai(workspace: SimpleNamespace) -> None:
    project = project_service.create_project(name="大纲书")
    project_id = project["id"]

    questions = outline_service.expand_ideas(project_id, idea="", use_ai=False)
    assert len(questions["questions"]) >= 3

    candidates = outline_service.generate_candidates(project_id, use_ai=False, count=3)
    assert candidates["count"] == 3
    assert all("content" in item for item in candidates["candidates"])

    locked = outline_service.lock_candidate(project_id, 0)
    assert locked["locked"]["title"]

    data = outline_service.read_outline(project_id)
    assert data["content"]
    assert data["frozen"] is False

    outline_service.freeze(project_id)
    assert outline_service.read_outline(project_id)["frozen"] is True

    with pytest.raises(Exception) as excinfo:
        outline_service.save_outline(project_id, "改后的内容")
    assert "差异确认" in str(excinfo.value)

    saved = outline_service.save_outline(project_id, "改后的内容", confirm=True)
    assert saved["content"] == "改后的内容"
    assert outline_service.list_history(project_id)


def test_outline_rolling_plan(workspace: SimpleNamespace) -> None:
    project = project_service.create_project(name="滚动书")
    project_id = project["id"]
    outline_service.save_outline(
        project_id,
        "## 第一卷 起风\n主角回到码头。\n\n## 第二卷 逆流\n货被截。\n\n"
        "## 第三卷 破局\n主角掀桌。\n\n## 第四卷 归位\n收网。\n",
    )
    result = outline_service.rolling_plan(project_id, use_ai=False)
    assert result["volumes_total"] == 4
    assert len(result["refined"]) == 2  # 只细化近 2 卷
    assert any("第三卷" in title for title in result["refined"])
    assert any("第四卷" in title for title in result["refined"])
    chapter_outline = (workspace.projects / "滚动书" / "大纲" / "章纲.md").read_text(
        encoding="utf-8"
    )
    assert "手工细化最近两卷" in chapter_outline