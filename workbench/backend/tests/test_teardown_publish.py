"""拆书资产库（Task 44）与发布导出模板（Task 66）单测。

覆盖 spec 要求：
- 拆书：导入目标 → 六维拆解 + 双时间线 + 事实卡 → 事实卡**均有章节依据或显式标注缺失**；
- 召回：按题材注入上下文包（第 4 级），只注入有依据的条目；
- 发布：按平台模板整理 + 提交前自检清单（字数/状态/质量债/简介/题材）。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from workbench.backend import config, db
from workbench.backend.services import (
    chapter_service,
    context_service,
    project_service,
    prompt_registry_service,
    publish_service,
    teardown_service,
)

SAMPLE = (
    "第一章 雪夜归人\n"
    "刀锋贴着门框划过，周砚翻身落地，掌心全是血。门外有人在数数，数到三就撞门。\n"
    "“二。”\n"
    "他抓起桌上的铜镜，镜面裂了一道口子。“三”字落下时，镜子已经砸向门轴。\n"
    "第二章 三日后\n"
    "三天后，城南的赌坊换了主人。周砚坐在角落里，把十二枚铜钱一枚枚排开。\n"
    "“借钱可以。”掌柜推过一张纸，“签了它，你这条命就归我。”\n"
    "他拿起笔，笔尖悬在纸面上，没有落下。\n"
) * 6


@pytest.fixture()
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    projects = tmp_path / "projects"
    runtime = tmp_path / ".workbench"
    for directory in (projects, runtime, tmp_path / "skills", tmp_path / "agents",
                      tmp_path / "rules", tmp_path / "workflows"):
        directory.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(config, "PROJECTS_DIR", projects)
    monkeypatch.setattr(config, "RUNTIME_DIR", runtime)
    monkeypatch.setattr(config, "DB_PATH", runtime / "workbench.db")
    db.init_db()
    config.ensure_runtime_dirs()
    prompt_registry_service.ensure_registry()

    from workbench.backend.engine import router as engine_router

    monkeypatch.setattr(engine_router.get_engine("dsh-headless"), "available", lambda: False)
    return SimpleNamespace(projects=projects, runtime=runtime, root=tmp_path)


# ─────────────────────────── 拆书：导入与拆解 ───────────────────────────


def test_teardown_import_and_deterministic_analyze(workspace: SimpleNamespace) -> None:
    project = project_service.create_project(name="拆书书", genre="悬疑")
    project_id = project["id"]

    imported = teardown_service.import_target(
        project_id, target_name="对标样本", content=SAMPLE, genre="悬疑",
        source_note="公开样章，仅作结构分析",
    )
    assert imported["chars"] > 0
    assert (workspace.projects / "拆书书" / "拆书" / "对标样本" / "原文.md").is_file()

    result = teardown_service.analyze(project_id, "对标样本", use_ai=False)
    assert result["source"] == "deterministic"
    assert result["fact_count"] >= 6, "六维必须齐备（缺失维度显式标注）"
    assert set(result["dimensions"]) == {item["key"] for item in teardown_service.DIMENSIONS}

    directory = workspace.projects / "拆书书" / "拆书" / "对标样本"
    for name in ("拆解.md", "双时间线.md", "事实卡.md"):
        assert (directory / name).is_file(), name

    facts_text = (directory / "事实卡.md").read_text(encoding="utf-8")
    assert "章节依据" in facts_text
    assert "依据缺失" in facts_text, "无依据条目必须显式标注，不得补写"
    assert "不补写、不编造" in facts_text

    timeline_text = (directory / "双时间线.md").read_text(encoding="utf-8")
    assert "事件时间线" in timeline_text and "揭示时间线" in timeline_text

    targets = teardown_service.list_targets(project_id)
    assert targets[0]["target"] == "对标样本"
    assert targets[0]["has_analysis"] and targets[0]["has_facts"]
    assert targets[0]["fact_count"] >= 6


def test_teardown_facts_have_evidence_or_missing_marker(workspace: SimpleNamespace) -> None:
    """spec 硬要求：事实卡均有章节依据或显式标注缺失。"""
    project = project_service.create_project(name="取证书", genre="悬疑")
    project_id = project["id"]
    teardown_service.import_target(project_id, target_name="样章", content=SAMPLE,
                                   genre="悬疑")
    teardown_service.analyze(project_id, "样章", use_ai=False)

    facts = teardown_service.list_facts(project_id)
    assert facts
    for fact in facts:
        evidence = str(fact.get("evidence") or "")
        assert evidence, f"事实卡缺少 evidence 字段：{fact}"
        assert evidence not in ("", None)
    # 确定性路径必须给出可复核的依据（首句/整篇统计/分章统计）
    evidenced = [fact for fact in facts if fact["evidence"] != "依据缺失"]
    assert evidenced, "确定性拆解至少应产出带依据的事实"


def test_teardown_recall_by_genre_and_context_injection(workspace: SimpleNamespace) -> None:
    project = project_service.create_project(name="召回书", genre="悬疑")
    project_id = project["id"]
    teardown_service.import_target(project_id, target_name="悬疑样章", content=SAMPLE,
                                   genre="悬疑")
    teardown_service.analyze(project_id, "悬疑样章", use_ai=False)
    teardown_service.import_target(project_id, target_name="科幻样章", content=SAMPLE,
                                   genre="科幻")
    teardown_service.analyze(project_id, "科幻样章", use_ai=False)

    matched = teardown_service.recall_for_genre(project_id, genre="悬疑", limit=5)
    assert matched["facts"], "同题材应召回事实"
    assert all(fact["target"] == "悬疑样章" for fact in matched["facts"])
    assert all(fact["evidence"] != "依据缺失" for fact in matched["facts"]), \
        "依据缺失的条目不得进入上下文"

    # 上下文包第 4 级出现拆书对标块
    chapter = chapter_service.create_chapter(project_id, "第一章")
    # 常规创作不自动引入参考作品；作者选择拆书场景后再注入。
    normal = context_service.assemble(project_id, chapter_rel=chapter["rel_path"])
    assert not any("拆书对标" in b["title"] for b in normal["blocks"])
    assembly = context_service.assemble(project_id, chapter_rel=chapter["rel_path"],
                                        retrieval_profile="teardown")
    teardown_blocks = [block for block in assembly["blocks"] if block["level"] == 4
                       and "拆书对标" in block["title"]]
    assert teardown_blocks, "同题材拆书事实应注入上下文第 4 级"
    assert "悬疑样章" in teardown_blocks[0]["text"]
    assert "科幻样章" not in teardown_blocks[0]["text"]

    # 无同题材命中时不注入
    other = project_service.create_project(name="无关书", genre="言情")
    other_chapter = chapter_service.create_chapter(other["id"], "第一章")
    other_assembly = context_service.assemble(other["id"],
                                             chapter_rel=other_chapter["rel_path"], retrieval_profile="teardown")
    assert not [block for block in other_assembly["blocks"]
                if "拆书对标" in block.get("title", "")]


def test_teardown_artifact_read_and_delete(workspace: SimpleNamespace) -> None:
    project = project_service.create_project(name="产物书", genre="悬疑")
    project_id = project["id"]
    teardown_service.import_target(project_id, target_name="样章", content=SAMPLE)
    teardown_service.analyze(project_id, "样章", use_ai=False)

    analysis = teardown_service.read_artifact(project_id, "样章", "analysis")
    assert "六维拆解" in analysis["content"]
    facts = teardown_service.read_artifact(project_id, "样章", "facts")
    assert "事实卡" in facts["content"]

    with pytest.raises(Exception):
        teardown_service.read_artifact(project_id, "样章", "unknown-kind")

    deleted = teardown_service.delete_target(project_id, "样章")
    assert deleted["deleted"] is True
    assert teardown_service.list_targets(project_id) == []
    assert teardown_service.list_facts(project_id) == []


# ─────────────────────────── 发布导出 ───────────────────────────


def test_publish_platforms_catalog(workspace: SimpleNamespace) -> None:
    platforms = publish_service.list_platforms()
    keys = {item["key"] for item in platforms}
    assert {"通用", "起点中文网", "番茄小说", "晋江文学城"} <= keys
    for item in platforms:
        assert item["word_range"][0] < item["word_range"][1]
        assert item["notes"], "每个平台必须给出整理注意事项"


def test_publish_export_with_checklist(workspace: SimpleNamespace) -> None:
    project = project_service.create_project(name="发布书", genre="悬疑",
                                             one_liner="少年在雪夜夺回自己的名字。")
    project_id = project["id"]

    first = chapter_service.create_chapter(project_id, "雪夜归人")
    chapter_service.save_chapter(project_id, first["rel_path"], "正文内容。" * 300,
                                 status="完成")
    second = chapter_service.create_chapter(project_id, "三日后")
    chapter_service.save_chapter(project_id, second["rel_path"], "短章。", status="草稿")

    result = publish_service.build_publish_export(project_id, platform="起点中文网")
    assert result["platform"] == "起点中文网"
    assert result["stats"]["chapters"] == 2
    assert result["stats"]["words"] > 0
    assert "第1章" in result["content"] and "第2章" in result["content"]
    assert result["filename"].endswith(".txt")
    assert "secrets.json" in str(result["checklist"]), "自检清单需说明无凭据泄漏"

    levels = {item["level"] for item in result["warnings"]}
    assert "偏短" in levels
    assert "未定稿" in levels

    checklist = {item["item"]: item for item in result["checklist"]}
    assert checklist["单章字数在建议区间"]["ok"] is False
    assert checklist["章节均已定稿（完成 / 发表）"]["ok"] is False
    assert checklist["作品简介（一句话设定）"]["ok"] is True
    assert checklist["题材与标签"]["ok"] is True

    # only_completed 只导出定稿章
    only = publish_service.build_publish_export(project_id, platform="通用",
                                                only_completed=True)
    assert only["stats"]["chapters"] == 1

    # 段落缩进：起点两格（正文非引号开头行）
    assert "　　正文内容。" in result["content"]


@pytest.mark.parametrize("status", ["完成", "发表", "completed", "published"])
def test_publish_final_states_are_exported_and_pass_status_check(
    workspace: SimpleNamespace, status: str,
) -> None:
    project = project_service.create_project(name="已定稿书", genre="悬疑")
    chapter = chapter_service.create_chapter(project["id"], "定稿章")
    chapter_service.save_chapter(project["id"], chapter["rel_path"], "正文内容。" * 500)
    # Existing metadata can contain the English legacy states consumed by the
    # knowledge corpus; public metadata mutations still use the Chinese states.
    with db.get_conn() as conn:
        conn.execute("UPDATE chapters SET status=? WHERE project_id=? AND rel_path=?",
                     (status, project["id"], chapter["rel_path"]))
    draft = chapter_service.create_chapter(project["id"], "草稿章")
    chapter_service.save_chapter(project["id"], draft["rel_path"], "草稿正文。" * 500)

    result = publish_service.build_publish_export(project["id"], only_completed=True)

    assert result["stats"]["chapters"] == 1
    assert "定稿章" in result["content"] and "草稿章" not in result["content"]
    assert not any(item["level"] == "未定稿" for item in result["warnings"])
    status_check = next(item for item in result["checklist"]
                        if item["item"] == "章节均已定稿（完成 / 发表）")
    assert status_check["ok"] is True
    assert status_check["basis"] == "全部为「完成 / 发表」"

    all_chapters = publish_service.build_publish_export(project["id"])
    drafts = [item for item in all_chapters["warnings"] if item["level"] == "未定稿"]
    assert [item["rel_path"] for item in drafts] == [draft["rel_path"]]


def test_publish_export_rejects_empty_and_unknown(workspace: SimpleNamespace) -> None:
    project = project_service.create_project(name="空书")
    with pytest.raises(Exception):
        publish_service.build_publish_export(project["id"], platform="不存在的平台")

    chapter = chapter_service.create_chapter(project["id"], "空章")
    assert chapter["is_empty"] is True
    with pytest.raises(Exception):
        publish_service.build_publish_export(project["id"], platform="通用")


def test_publish_export_no_credentials(workspace: SimpleNamespace) -> None:
    """导出内容不得包含任何凭据（spec：导出无凭据）。"""
    from workbench.backend.services import secret_store

    secret_store.set_secret("provider:leaky", "sk-leaky-abcdefghijklmn")
    project = project_service.create_project(name="凭据书", genre="悬疑")
    chapter = chapter_service.create_chapter(project["id"], "第一章")
    chapter_service.save_chapter(project["id"], chapter["rel_path"], "正文。" * 400,
                                 status="完成")

    result = publish_service.build_publish_export(project["id"])
    assert "sk-leaky" not in result["content"]
    assert "sk-leaky" not in str(result["warnings"])
    secret_store.delete_secret("provider:leaky")
