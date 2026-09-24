"""M1 收尾能力单测：模板 / 索引与检索 / 快照 / 导入导出 / 冲突 / 批量删除。

全部用例在 ``tmp_path`` 下运行（monkeypatch 基目录），不污染真实 ``projects/``。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from workbench.backend import config, db
from workbench.backend.services import (
    chapter_service,
    conflict_service,
    index_service,
    project_service,
    settings_service,
    snapshot_service,
    template_service,
    transfer_service,
    tree_service,
)
from workbench.backend.services.errors import InvalidOperationError, NodeNotFoundError
from workbench.backend.services.fs_utils import (
    atomic_write_text,
    compose_document,
    read_text,
)


@pytest.fixture()
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """项目根 / 运行时 / 模板目录 / 数据库全部指向临时目录。"""
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
    return SimpleNamespace(projects=projects, runtime=runtime, templates=templates)


# ─────────────────────────── 模板 ───────────────────────────


def test_builtin_template_ready_and_protected(workspace: SimpleNamespace) -> None:
    templates = template_service.list_templates()
    builtin = [item for item in templates if item["is_builtin"]]
    assert len(builtin) == 1
    assert builtin[0]["name"] == template_service.BUILTIN_TEMPLATE_NAME
    assert Path(builtin[0]["path"]).is_dir()

    with pytest.raises(InvalidOperationError):
        template_service.delete_template(template_service.BUILTIN_TEMPLATE_NAME)


def test_save_as_template_and_open_book_with_it(workspace: SimpleNamespace) -> None:
    project = project_service.create_project(name="模板测试书")

    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO templates (name, path, is_builtin) VALUES ('书稿结构', ?, 0)",
            (str(workspace.projects / "模板测试书"),),
        )

    # 直接用目录路径开书（模板复制必须保留内容结构）
    created = project_service.create_project(
        name="按模板开书", template=str(workspace.projects / "模板测试书")
    )
    target = workspace.projects / created["name"]
    assert (target / "设定" / "人物设定.md").is_file()

    # 另存为模板
    saved = template_service.save_as_template("我的模板", path_of(project))
    assert Path(saved["path"]).is_dir()
    assert (Path(saved["path"]) / "章节").is_dir()

    with pytest.raises(InvalidOperationError):
        template_service.save_as_template("我的模板", path_of(project))

    names = {item["name"] for item in template_service.list_templates()}
    assert {"书稿结构", "我的模板"} <= names


def path_of(project: dict) -> Path:
    return Path(project["path"])


# ─────────────────────────── 索引与检索 ───────────────────────────


def test_rebuild_index_skips_empty_files(workspace: SimpleNamespace) -> None:
    project = project_service.create_project(name="索引测试书")
    project_id = project["id"]

    stats = index_service.rebuild_index(project_id)
    # 开书生成的 13 个空 md（章节目录为空）不入索引
    assert stats["indexed"] == 0
    assert stats["skipped_empty"] >= 13

    chapter = chapter_service.create_chapter(project_id, "开篇")
    chapter_service.save_chapter(
        project_id,
        chapter["rel_path"],
        compose_document({"标题": "开篇"}, "他推开木门，风灌进来，灯影摇晃。"),
    )

    stats = index_service.rebuild_index(project_id)
    assert stats["indexed"] == 1

    hits = index_service.search(project_id, "木门")
    assert [hit["rel_path"] for hit in hits] == [chapter["rel_path"]]

    # 外部新增文件 → 重建后可见
    external = workspace.projects / project["name"] / "备忘录" / "外部笔记.md"
    atomic_write_text(external, compose_document({"标题": "外部笔记"}, "外部写入的设定要点。"))
    index_service.rebuild_index(project_id)
    assert index_service.search(project_id, "设定要点")

    # 外部删除 → 重建后同步消失
    external.unlink()
    index_service.rebuild_index(project_id)
    assert index_service.search(project_id, "设定要点") == []


def test_save_chapter_refreshes_index_incrementally(workspace: SimpleNamespace) -> None:
    project = project_service.create_project(name="增量索引书")
    chapter = chapter_service.create_chapter(project["id"], "第一章")
    chapter_service.save_chapter(
        project["id"], chapter["rel_path"], "夜里的码头，缆绳绷紧。"
    )
    assert index_service.search(project["id"], "缆绳")

    # 清空正文保存 → 变回空文件，索引应移除
    chapter_service.save_chapter(project["id"], chapter["rel_path"], "   ")
    assert index_service.search(project["id"], "缆绳") == []


# ─────────────────────────── 快照 ───────────────────────────


def test_snapshot_created_on_save_and_restorable(workspace: SimpleNamespace) -> None:
    project = project_service.create_project(name="快照书")
    chapter = chapter_service.create_chapter(project["id"], "第一章")
    rel_path = chapter["rel_path"]

    chapter_service.save_chapter(project["id"], rel_path, "第一版正文")
    chapter_service.save_chapter(project["id"], rel_path, "第二版正文")

    snapshots = snapshot_service.list_snapshots(project["id"], rel_path)
    assert snapshots, "保存应产生快照"
    restored = snapshot_service.restore_snapshot(snapshots[0]["id"])
    assert restored["rel_path"] == rel_path
    assert "第一版正文" in read_text(workspace.projects / "快照书" / rel_path)


# ─────────────────────────── 导入导出 ───────────────────────────


def test_export_scopes_and_import(workspace: SimpleNamespace) -> None:
    project = project_service.create_project(name="导出书")
    project_id = project["id"]
    chapter = chapter_service.create_chapter(project_id, "楔子")
    chapter_service.save_chapter(project_id, chapter["rel_path"], "正文甲。")

    body_only = transfer_service.export_chapters(project_id, scope="body", fmt="txt")
    assert "楔子" not in body_only["content"]
    assert "正文甲。" in body_only["content"]
    assert body_only["chapter_count"] == 1

    with_title = transfer_service.export_chapters(project_id, scope="with_title", fmt="md")
    assert "# 楔子" in with_title["content"]

    imported = transfer_service.import_document(
        project_id,
        filename="导入片段.md",
        content="# 第一章 起风\n\n他站在桥上，看着水里的月亮。",
        as_single_chapter=False,
    )
    assert imported["count"] == 1
    assert imported["created"][0]["status"] == "草稿"
    detail = chapter_service.read_chapter(project_id, imported["created"][0]["rel_path"])
    assert "水里的月亮" in detail["body"]

    with pytest.raises(InvalidOperationError):
        transfer_service.import_document(project_id, filename="a.pdf", content="x")


def test_project_package_roundtrip(workspace: SimpleNamespace) -> None:
    project = project_service.create_project(name="打包书", one_liner="一句话")
    chapter = chapter_service.create_chapter(project["id"], "第一章")
    chapter_service.save_chapter(project["id"], chapter["rel_path"], "包内正文")

    package = transfer_service.export_project_package(project["id"])
    assert package["stats"]["chapters"] == 1

    restored = transfer_service.restore_project_package(package, new_name="打包书-恢复")
    target = workspace.projects / "打包书-恢复"
    assert (target / "章节" / chapter["file_name"]).is_file()
    assert restored["files_written"] > 0


# ─────────────────────────── 冲突 ───────────────────────────


def test_conflict_detection_and_resolution(workspace: SimpleNamespace) -> None:
    project = project_service.create_project(name="冲突书")
    chapter = chapter_service.create_chapter(project["id"], "第一章")
    rel_path = chapter["rel_path"]
    chapter_service.save_chapter(project["id"], rel_path, "我的版本")

    state = conflict_service.disk_state(workspace.projects / "冲突书", rel_path)
    # 模拟外部改动
    atomic_write_text(workspace.projects / "冲突书" / rel_path, "外部版本")

    checked = conflict_service.check_conflict(
        project["id"],
        rel_path,
        base_hash=state["hash"],
        current_content="我的版本",
    )
    assert checked["changed"] is True
    assert any(item["type"] == "add" and "外部版本" in item["text"] for item in checked["diff"])

    kept = conflict_service.resolve_keep_mine(project["id"], rel_path, "我的版本")
    assert kept["resolution"] == "keep-mine"
    assert "我的版本" in read_text(workspace.projects / "冲突书" / rel_path)

    # 保留我的要把外部版本先存快照
    snaps = snapshot_service.list_snapshots(project["id"], rel_path)
    assert any(item["reason"] == "external-keep-mine" for item in snaps)

    atomic_write_text(workspace.projects / "冲突书" / rel_path, "外部版本二")
    taken = conflict_service.resolve_take_external(project["id"], rel_path)
    assert taken["content"].strip() == "外部版本二"

    with pytest.raises(NodeNotFoundError):
        conflict_service.disk_state(workspace.projects / "冲突书", "章节/不存在.md")


# ─────────────────────────── 删除策略 ───────────────────────────


def test_batch_delete_mixed_and_meta_reference(workspace: SimpleNamespace) -> None:
    project = project_service.create_project(name="删除书")
    project_id = project["id"]
    project_dir = workspace.projects / "删除书"

    empty_file = project_dir / "设定" / "空设定.md"
    empty_file.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(empty_file, compose_document({"标题": "空设定"}, ""))

    chapter = chapter_service.create_chapter(project_id, "第一章")
    chapter_service.save_chapter(project_id, chapter["rel_path"], "有正文的章节")

    results = tree_service.delete_nodes(
        project_id, ["设定/空设定.md", chapter["rel_path"]]
    )
    actions = {item["rel_path"]: item["action"] for item in results}
    assert actions["设定/空设定.md"] == "deleted"
    assert actions[chapter["rel_path"]] == "trashed"

    # 被 .meta 记录引用的空文件 → 降级进回收站
    meta_dir = project_dir / ".meta"
    meta_dir.mkdir(exist_ok=True)
    referenced = project_dir / "设定" / "被引用.md"
    atomic_write_text(referenced, compose_document({"标题": "被引用"}, ""))
    atomic_write_text(
        meta_dir / "asset_cards.json",
        json.dumps({"设定/被引用.md": []}, ensure_ascii=False),
    )
    result = tree_service.delete_node(project_id, "设定/被引用.md")
    assert result["action"] == "trashed"
    assert result["reason"] == "被 .meta 记录引用（空文件降级）"


# ─────────────────────────── 设置 ───────────────────────────


def test_settings_roundtrip(workspace: SimpleNamespace) -> None:
    defaults = settings_service.read_settings()
    assert defaults["milestone"]["step"] == config.MILESTONE_STEP_DEFAULT

    updated = settings_service.update_settings({"milestone": {"step": 800}})
    assert updated["milestone"]["step"] == 800
    assert updated["milestone"]["enabled"] is True  # 未传字段保持默认

    assert settings_service.read_settings()["milestone"]["step"] == 800
    assert settings_service.reset_settings()["milestone"]["step"] == config.MILESTONE_STEP_DEFAULT