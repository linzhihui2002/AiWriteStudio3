"""M1 数据层单测：开书工作区 / 章节 CRUD / 文档树 / 按内容分策略的删除。

所有用例都在 ``tmp_path`` 下运行（monkeypatch ``config.PROJECTS_DIR`` /
``config.RUNTIME_DIR`` / ``config.DB_PATH``），**绝不污染真实 projects/**。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from workbench.backend import config, db
from workbench.backend.api import writing
from workbench.backend.services import chapter_service, contract_service, ingestion_service, pipeline_service, project_service, review_service, tree_service
from workbench.backend.services.errors import (
    InvalidChapterNameError,
    InvalidNameError,
    InvalidOperationError,
    NodeExistsError,
    ProjectExistsError,
)
from workbench.backend.services.fs_utils import (
    atomic_write_text,
    is_empty_file,
    split_frontmatter,
)

# 开书必须生成的 15 个初始路径（14 个文件 + 1 个空目录）
EXPECTED_PATHS: tuple[str, ...] = (
    "project.md",
    "备忘录/临时想法.md",
    "大纲/大纲.md",
    "大纲/章纲.md",
    "设定/世界设定.md",
    "设定/势力设定.md",
    "设定/人物设定.md",
    "设定/物品设定.md",
    "设定/技能设定.md",
    "设定/场景设定.md",
    "设定/伏笔管理.md",
    "状态/时间线.md",
    "状态/角色状态.md",
    "状态/资源账本.md",
    "章节",
)


@pytest.fixture()
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """把项目根 / 运行时目录 / 数据库全部指向临时目录。"""
    projects = tmp_path / "projects"
    runtime = tmp_path / ".workbench"
    projects.mkdir()
    runtime.mkdir()
    monkeypatch.setattr(config, "PROJECTS_DIR", projects)
    monkeypatch.setattr(config, "RUNTIME_DIR", runtime)
    monkeypatch.setattr(config, "DB_PATH", runtime / "workbench.db")
    db.init_db()
    return SimpleNamespace(projects=projects, runtime=runtime)


@pytest.fixture()
def project(workspace: SimpleNamespace) -> dict:
    return project_service.create_project(
        name="测试书名",
        genre="玄幻",
        platform="起点中文网",
        protagonist="林尘",
        one_liner="废柴少年靠搜刮系统一步步登顶。",
    )


# ─────────────────────── 开书：目录工作区 ───────────────────────


def test_create_project_initializes_all_15_paths(workspace, project) -> None:
    root = workspace.projects / "测试书名"
    for rel in EXPECTED_PATHS:
        target = root / rel
        assert target.exists(), f"缺少开书初始路径：{rel}"

    assert (root / "章节").is_dir()
    assert (root / "project.md").is_file()
    assert len(EXPECTED_PATHS) == 15

    assert project["name"] == "测试书名"
    assert project["archived"] is False
    assert project["id"] > 0
    assert project["genre"] == "玄幻"


def test_static_setting_and_dynamic_state_are_separated(workspace, project) -> None:
    root = workspace.projects / "测试书名"
    setting_dir = root / "设定"
    state_dir = root / "状态"

    assert setting_dir.is_dir() and state_dir.is_dir()
    assert setting_dir != state_dir

    setting_files = {path.name for path in setting_dir.iterdir()}
    state_files = {path.name for path in state_dir.iterdir()}
    assert setting_files == {
        "世界设定.md",
        "势力设定.md",
        "人物设定.md",
        "物品设定.md",
        "技能设定.md",
        "场景设定.md",
        "伏笔管理.md",
    }
    assert state_files == {"时间线.md", "角色状态.md", "资源账本.md"}
    assert not (setting_files & state_files)


def test_initialized_documents_are_empty_but_parseable(workspace, project) -> None:
    root = workspace.projects / "测试书名"
    for rel in project_service.WORKSPACE_FILES:
        path = root / rel
        # 模板初始化的文件按 spec 口径属于「空文件」（可被物理删除）
        assert is_empty_file(path), f"初始化文件应为空文件：{rel}"
        meta, _body = split_frontmatter(path.read_text(encoding="utf-8"))
        assert meta.get("标题") == Path(rel).stem


def test_project_md_frontmatter_and_one_liner(workspace, project) -> None:
    project_md = workspace.projects / "测试书名" / "project.md"
    text = project_md.read_text(encoding="utf-8")
    meta, body = split_frontmatter(text)

    assert meta["书名"] == "测试书名"
    assert meta["题材"] == "玄幻"
    assert meta["平台"] == "起点中文网"
    assert meta["主角"] == "林尘"
    assert meta["创建时间"]
    assert "一句话设定" in body
    assert "废柴少年靠搜刮系统一步步登顶。" in body

    detail = project_service.get_project(project["id"])
    assert detail["one_liner"] == "废柴少年靠搜刮系统一步步登顶。"


def test_project_name_sanitized_and_duplicate_rejected(workspace) -> None:
    created = project_service.create_project(name='测试/书名:第一?卷"上"')
    assert created["name"] == "测试_书名_第一_卷_上_"
    assert (workspace.projects / "测试_书名_第一_卷_上_").is_dir()

    with pytest.raises(ProjectExistsError):
        project_service.create_project(name="测试_书名_第一_卷_上_")

    # 不同原始名清洗后相同 → 同样视为重名
    with pytest.raises(ProjectExistsError):
        project_service.create_project(name='测试/书名:第一?卷"上"')

    with pytest.raises(InvalidNameError):
        project_service.create_project(name="   ")
    with pytest.raises(InvalidNameError):
        project_service.create_project(name="...")


def test_archive_and_list_projects(workspace, project) -> None:
    assert len(project_service.list_projects()) == 1
    archived = project_service.archive_project(project["id"])
    assert archived["archived"] is True
    assert project_service.list_projects() == []
    assert len(project_service.list_projects(include_archived=True)) == 1

    restored = project_service.unarchive_project(project["id"])
    assert restored["archived"] is False
    assert len(project_service.list_projects()) == 1


# ─────────────────────── 章节 ───────────────────────


def test_chapter_numbering_increments(workspace, project) -> None:
    first = chapter_service.create_chapter(project["id"])
    second = chapter_service.create_chapter(project["id"])
    third = chapter_service.create_chapter(project["id"], title="初入宗门")

    assert first["rel_path"] == "章节/第0001章.md"
    assert second["rel_path"] == "章节/第0002章.md"
    assert third["rel_path"] == "章节/第0003章.md"
    assert third["title"] == "初入宗门"

    root = workspace.projects / "测试书名" / "章节"
    assert (root / "第0001章.md").is_file()
    assert (root / "第0003章.md").is_file()

    chapters = chapter_service.list_chapters(project["id"])
    assert [item["number"] for item in chapters] == [1, 2, 3]
    assert chapters[2]["title"] == "初入宗门"


def test_save_chapter_recomputes_word_count_and_keeps_status(workspace, project) -> None:
    chapter = chapter_service.create_chapter(project["id"])
    rel_path = chapter["rel_path"]

    saved = chapter_service.save_chapter(project["id"], rel_path, "你好，世界！")
    assert saved["word_count"] == 6
    assert saved["status"] == "草稿"
    assert saved["meta"]["合同ID"] == ""

    # 原子写后磁盘内容正确，且不留临时文件
    path = workspace.projects / "测试书名" / rel_path
    text = path.read_text(encoding="utf-8")
    assert text.endswith("你好，世界！\n")
    assert not list(path.parent.glob("*.tmp"))

    # 状态可切换（完成 / 发表），字数按正文重算
    done = chapter_service.save_chapter(
        project["id"], rel_path, text, status="完成"
    )
    assert done["status"] == "完成"
    assert done["word_count"] == 6

    chapters = chapter_service.list_chapters(project["id"])
    assert chapters[0]["status"] == "完成"
    assert chapters[0]["word_count"] == 6
    assert chapters[0]["is_empty"] is False


def test_save_chapter_body_only_keeps_existing_frontmatter(workspace, project) -> None:
    chapter = chapter_service.create_chapter(project["id"], title="初入宗门")
    saved = chapter_service.save_chapter(project["id"], chapter["rel_path"], "只有正文。")

    assert saved["title"] == "初入宗门"  # 只回传正文时不清空已有元信息
    assert saved["word_count"] == 5
    assert saved["status"] == "草稿"
    assert saved["contract_id"] == ""


def test_empty_chapter_is_physically_deleted(workspace, project) -> None:
    chapter = chapter_service.create_chapter(project["id"])
    rel_path = chapter["rel_path"]
    path = workspace.projects / "测试书名" / rel_path

    # 仅 frontmatter、无正文 → 视为空文件
    assert is_empty_file(path) is True

    result = chapter_service.delete_chapter(project["id"], rel_path)
    assert result["action"] == "deleted"
    assert result["trash_rel"] is None
    assert not path.exists()
    assert chapter_service.list_trash(project["id"]) == []


def test_non_empty_chapter_goes_to_trash_and_can_be_restored(workspace, project) -> None:
    chapter = chapter_service.create_chapter(project["id"])
    rel_path = chapter["rel_path"]
    path = workspace.projects / "测试书名" / rel_path
    chapter_service.save_chapter(project["id"], rel_path, "第一章正文内容。")

    result = chapter_service.delete_chapter(project["id"], rel_path)
    assert result["action"] == "trashed"
    assert result["trash_rel"]
    assert not path.exists()

    entries = chapter_service.list_trash(project["id"])
    assert len(entries) == 1
    assert entries[0]["rel_path"] == rel_path

    restored = chapter_service.restore_from_trash(project["id"], entries[0]["trash_rel"])
    assert restored["rel_path"] == rel_path
    assert path.is_file()
    assert "第一章正文内容。" in path.read_text(encoding="utf-8")
    assert chapter_service.list_trash(project["id"]) == []


def test_rename_chapter_validates_filename_convention(workspace, project) -> None:
    chapter = chapter_service.create_chapter(project["id"])
    rel_path = chapter["rel_path"]

    renamed = tree_service.rename_node(project["id"], rel_path, "第0007章.md")
    assert renamed["rel_path"] == "章节/第0007章.md"
    assert renamed["number"] == 7

    with pytest.raises(InvalidChapterNameError):
        tree_service.rename_node(project["id"], renamed["rel_path"], "第二章.md")

    with pytest.raises(InvalidChapterNameError):
        tree_service.rename_node(project["id"], renamed["rel_path"], "第2章.md")

    # 非章节文件不受命名规范约束
    created = tree_service.create_node(project["id"], "设定", "自定义设定")
    assert created["rel_path"] == "设定/自定义设定.md"
    moved = tree_service.rename_node(project["id"], created["rel_path"], "自定义设定2.md")
    assert moved["rel_path"] == "设定/自定义设定2.md"


def test_create_node_in_chapter_dir_normalizes_name(project) -> None:
    """「章节/」下新建文件自动规范化为 第NNNN章.md，输入的名字成为章节标题。"""
    node = tree_service.create_node(project["id"], "章节", "第一章：test.txt")
    assert node["rel_path"] == "章节/第0001章.md"
    assert node["is_chapter"] is True
    assert node["title"] == "第一章：test"
    assert [item["file_name"] for item in chapter_service.list_chapters(project["id"])] == ["第0001章.md"]

    # 位数不足 → 补齐零填充，且不额外占用编号
    assert tree_service.create_node(project["id"], "章节", "第7章")["rel_path"] == "章节/第0007章.md"
    # 已规范 → 原样保留
    assert tree_service.create_node(project["id"], "章节", "第0009章.md")["rel_path"] == "章节/第0009章.md"
    # 非章节分组不受影响
    assert tree_service.create_node(project["id"], "设定", "自定义设定")["rel_path"] == "设定/自定义设定.md"


def test_update_chapter_meta_title_and_status(project) -> None:
    """章节标题与状态可单独更新，且不碰正文。"""
    chapter = chapter_service.create_chapter(project["id"], "起风")
    rel = chapter["rel_path"]

    updated = chapter_service.update_chapter_meta(
        project["id"], rel, title="雪夜归人", status="完成"
    )
    assert updated["title"] == "雪夜归人"
    assert updated["status"] == "完成"

    # 清空标题：删键而非写空串，状态不受影响
    cleared = chapter_service.update_chapter_meta(project["id"], rel, title="")
    assert cleared["title"] == ""
    assert cleared["status"] == "完成"
    assert "标题" not in cleared["meta"]
    assert chapter_service.list_chapters(project["id"])[0]["status"] == "完成"

    # 只传 status 时不动已有标题
    chapter_service.update_chapter_meta(project["id"], rel, title="归人")
    again = chapter_service.update_chapter_meta(project["id"], rel, status="发表")
    assert again["title"] == "归人"
    assert again["status"] == "发表"

    with pytest.raises(InvalidOperationError):
        chapter_service.update_chapter_meta(project["id"], rel, status="已发布")

    with pytest.raises(InvalidOperationError):
        chapter_service.update_chapter_meta(project["id"], rel, title="长" * 61)


def test_chapter_only_actions_reject_setting_file(project, workspace) -> None:
    """普通 Markdown 可在编辑器读写，但不可当作章节生产或修改章节状态。"""
    rel = "设定/世界设定.md"
    chapter = chapter_service.create_chapter(project["id"])
    with pytest.raises(InvalidOperationError):
        chapter_service.require_chapter_path(project["id"], f"章节/../{chapter['rel_path']}")
    before = (workspace.projects / project["name"] / rel).read_text(encoding="utf-8")
    assert chapter_service.read_chapter(project["id"], rel)["content"] == before

    with pytest.raises(InvalidOperationError):
        chapter_service.update_chapter_meta(project["id"], rel, status="完成")
    with pytest.raises(InvalidOperationError):
        contract_service.generate_contract(project["id"], rel, use_ai=False)
    with pytest.raises(InvalidOperationError):
        contract_service.check_prerequisites(project["id"], rel)
    with pytest.raises(InvalidOperationError):
        pipeline_service.run_pipeline(project["id"], rel, use_ai=False)
    with pytest.raises(InvalidOperationError):
        review_service.review_chapter(project["id"], rel, use_ai=False)
    with pytest.raises(InvalidOperationError):
        ingestion_service.ingest_chapter(project["id"], rel, use_ai=False)
    with pytest.raises(InvalidOperationError):
        writing.local_operation(project["id"], writing.LocalOpIn(chapter_rel=rel, selection="设定", operation="rewrite"))
    with pytest.raises(InvalidOperationError):
        writing.ghost_text(project["id"], writing.GhostTextIn(chapter_rel=rel, prefix="设定"))

    assert (workspace.projects / project["name"] / rel).read_text(encoding="utf-8") == before


# ─────────────────────── 文档树与删除分流 ───────────────────────


def test_file_tree_groups(workspace, project) -> None:
    tree = tree_service.get_file_tree(project["id"])
    groups = {group["key"]: group for group in tree["groups"]}

    assert list(groups) == ["memo", "outline", "setting", "state", "chapter"]
    assert groups["setting"]["name"] == "设定"
    assert len(groups["setting"]["nodes"]) == 7
    assert groups["chapter"]["exists"] is True
    assert groups["chapter"]["nodes"] == []
    assert [node["name"] for node in tree["root_nodes"]] == ["project.md"]


def test_create_and_move_node(workspace, project) -> None:
    folder = tree_service.create_node(project["id"], "", "素材", is_dir=True)
    assert folder["type"] == "dir"
    assert folder["rel_path"] == "素材"

    file_node = tree_service.create_node(project["id"], "素材", "笔记")
    assert file_node["rel_path"] == "素材/笔记.md"
    assert file_node["is_empty"] is True

    with pytest.raises(NodeExistsError):
        tree_service.create_node(project["id"], "素材", "笔记.md")

    moved = tree_service.move_node(project["id"], "素材/笔记.md", "大纲")
    assert moved["rel_path"] == "大纲/笔记.md"
    assert (workspace.projects / "测试书名" / "大纲" / "笔记.md").is_file()

    tree = tree_service.get_file_tree(project["id"])
    outline = next(group for group in tree["groups"] if group["key"] == "outline")
    assert {node["name"] for node in outline["nodes"]} == {"大纲.md", "章纲.md", "笔记.md"}


def test_folder_delete_split_by_content(workspace, project) -> None:
    root = workspace.projects / "测试书名"

    # 全空文件夹 → 物理删除
    tree_service.create_node(project["id"], "", "空素材", is_dir=True)
    tree_service.create_node(project["id"], "空素材", "空文件")
    empty_result = tree_service.delete_node(project["id"], "空素材")
    assert empty_result["action"] == "deleted"
    assert not (root / "空素材").exists()
    assert chapter_service.list_trash(project["id"]) == []

    # 含非空文件 → 整个文件夹进回收站
    tree_service.create_node(project["id"], "", "有料素材", is_dir=True)
    tree_service.create_node(project["id"], "有料素材", "草稿")
    atomic_write_text(root / "有料素材" / "草稿.md", "这里有真实内容。")
    trashed_result = tree_service.delete_node(project["id"], "有料素材")
    assert trashed_result["action"] == "trashed"
    assert not (root / "有料素材").exists()

    entries = chapter_service.list_trash(project["id"])
    assert len(entries) == 1
    assert entries[0]["kind"] == "dir"

    chapter_service.restore_from_trash(project["id"], entries[0]["trash_rel"])
    assert (root / "有料素材" / "草稿.md").read_text(encoding="utf-8") == "这里有真实内容。"


# ─────────────────────── 更新元信息 / 封面 ───────────────────────


def test_update_project_fields_and_preserves_body(workspace, project) -> None:
    root = workspace.projects / "测试书名"
    project_md = root / "project.md"
    project_md.write_text(
        project_md.read_text(encoding="utf-8") + "\n## 备注正文\n\n自定义内容不要丢。\n",
        encoding="utf-8",
    )

    updated = project_service.update_project(
        project["id"],
        genre="东方玄幻",
        platform="番茄小说",
        protagonist="李长歌",
        one_liner="修仙界搜刮流。",
    )
    assert updated["genre"] == "东方玄幻"
    assert updated["platform"] == "番茄小说"
    assert updated["protagonist"] == "李长歌"
    assert updated["one_liner"] == "修仙界搜刮流。"

    meta, body = split_frontmatter(project_md.read_text(encoding="utf-8"))
    assert meta["书名"] == "测试书名"
    assert meta["题材"] == "东方玄幻"
    assert meta["平台"] == "番茄小说"
    assert meta["主角"] == "李长歌"
    assert "修仙界搜刮流。" in body
    assert "自定义内容不要丢。" in body
    assert root.is_dir()


def test_update_project_rename_syncs_dir_index_and_snapshots(workspace, project) -> None:
    from workbench.backend.services import snapshot_service

    project_id = project["id"]
    chapter = chapter_service.create_chapter(project_id)
    created = snapshot_service.create_snapshot(
        project_id, "测试书名", chapter["rel_path"], "第一版内容。", reason="save"
    )
    assert created is not None
    assert (config.snapshots_dir() / "测试书名").is_dir()

    updated = project_service.update_project(project_id, name="新书名")
    assert updated["name"] == "新书名"

    new_root = workspace.projects / "新书名"
    assert new_root.is_dir()
    assert not (workspace.projects / "测试书名").exists()

    detail = project_service.get_project(project_id)
    assert detail["name"] == "新书名"
    assert Path(detail["path"]) == new_root

    meta, _body = split_frontmatter((new_root / "project.md").read_text(encoding="utf-8"))
    assert meta["书名"] == "新书名"

    # 快照目录与 snapshot_path 同步迁移，历史快照仍可读
    assert (config.snapshots_dir() / "新书名").is_dir()
    assert not (config.snapshots_dir() / "测试书名").exists()
    items = snapshot_service.list_snapshots(project_id, chapter["rel_path"])
    assert items and Path(items[0]["snapshot_path"]).is_file()


def test_update_project_rename_rejects_existing_name(workspace, project) -> None:
    project_service.create_project(name="另一本书")
    with pytest.raises(ProjectExistsError):
        project_service.update_project(project["id"], name="另一本书")
    # 磁盘已有同名目录（无索引记录）同样拒绝
    (workspace.projects / "幽灵书").mkdir()
    with pytest.raises(ProjectExistsError):
        project_service.update_project(project["id"], name="幽灵书")


def test_cover_save_replace_and_delete(workspace, project) -> None:
    project_id = project["id"]
    root = workspace.projects / "测试书名"
    assert project_service.get_project(project_id)["has_cover"] is False

    assert project_service.save_cover(project_id, b"\x89PNG-fake", "image/png") == {"has_cover": True}
    cover = project_service.cover_path_of(root)
    assert cover is not None and cover.name == "cover.png"
    assert project_service.cover_media_of(cover) == "image/png"
    assert project_service.get_project(project_id)["has_cover"] is True

    # 重新上传（换格式）→ 旧封面被替换
    project_service.save_cover(project_id, b"jpeg-bytes", "image/jpeg; charset=binary")
    cover = project_service.cover_path_of(root)
    assert cover is not None and cover.name == "cover.jpg"
    assert project_service.cover_media_of(cover) == "image/jpeg"
    assert not (root / "cover.png").exists()

    assert project_service.delete_cover(project_id) == {"has_cover": False}
    assert project_service.cover_path_of(root) is None
    assert project_service.get_project(project_id)["has_cover"] is False


def test_cover_rejects_bad_type_empty_and_oversize(workspace, project) -> None:
    project_id = project["id"]
    with pytest.raises(InvalidOperationError):
        project_service.save_cover(project_id, b"gif-data", "image/gif")
    with pytest.raises(InvalidOperationError):
        project_service.save_cover(project_id, b"", "image/png")
    with pytest.raises(InvalidOperationError):
        project_service.save_cover(
            project_id, b"x" * (project_service.MAX_COVER_BYTES + 1), "image/png"
        )
