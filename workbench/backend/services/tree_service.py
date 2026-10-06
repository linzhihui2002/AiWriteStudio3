"""文档树服务：项目目录树读取 + 文件/文件夹的新建、重命名、删除、移动。

- 树与磁盘一一对应，按资产分组返回：备忘录 / 大纲 / 设定 / 状态 / 章节；
- 新建文件默认补 ``.md`` 后缀；章节目录下的文件固定为 ``第NNNN章.txt``（纯正文，元数据在库）；
- 删除复用「按内容分策略」：空文件（含空文件夹）物理删除，非空进回收站；
- 内部目录（``.`` 开头的如 ``.meta/``）不在树中展示，但仍可通过显式路径操作。
"""

from __future__ import annotations

import shutil
from pathlib import Path

from .errors import (
    InvalidOperationError,
    NodeExistsError,
    NodeNotFoundError,
)
from . import operation_log
from .fs_utils import (
    atomic_write_text,
    directory_is_all_empty,
    delete_path_by_content,
    is_empty_file,
    resolve_within,
    to_rel,
    validate_node_name,
)
from .chapter_service import (
    CHAPTER_DIR,
    chapter_node_info,
    is_chapter_name,
    move_chapter_meta,
    normalize_chapter_create_name,
    upsert_chapter_meta,
    validate_chapter_filename,
)
from .project_service import get_project_dir

# 分组顺序即前端左栏展示顺序；key 供前端做分组样式与二次确认策略
GROUP_DEFS: tuple[tuple[str, str], ...] = (
    ("memo", "备忘录"),
    ("outline", "大纲"),
    ("setting", "设定"),
    ("state", "状态"),
    ("chapter", "章节"),
)

DEFAULT_FILE_SUFFIX = ".md"


def _visible_entries(directory: Path) -> list[Path]:
    """目录下可见条目（跳过 ``.`` 开头的内部目录/文件），目录在前、按名称排序。"""
    if not directory.is_dir():
        return []
    entries = [path for path in directory.iterdir() if not path.name.startswith(".")]
    return sorted(entries, key=lambda path: (not path.is_dir(), path.name))


def _file_node(project_id: int, project_dir: Path, path: Path) -> dict:
    stat = path.stat()
    node = {
        "name": path.name,
        "rel_path": to_rel(project_dir, path),
        "type": "file",
        "size": stat.st_size,
        "mtime": stat.st_mtime,
        "is_empty": is_empty_file(path),
    }

    if path.parent.name == CHAPTER_DIR and is_chapter_name(path.name):
        node.update(chapter_node_info(project_id, project_dir, path))
    return node


def _build_node(project_id: int, project_dir: Path, path: Path) -> dict:
    """递归构建树节点。"""
    if path.is_dir():
        return {
            "name": path.name,
            "rel_path": to_rel(project_dir, path),
            "type": "dir",
            "is_empty": directory_is_all_empty(path),
            "file_count": sum(1 for child in path.rglob("*") if child.is_file()),
            "children": [
                _build_node(project_id, project_dir, child)
                for child in _visible_entries(path)
            ],
        }
    return _file_node(project_id, project_dir, path)


def get_file_tree(project_id: int) -> dict:
    """返回项目目录树：按分组（备忘录/大纲/设定/状态/章节）+ 根目录散落条目。"""
    row, project_dir = get_project_dir(project_id)

    groups: list[dict] = []
    grouped_names: set[str] = set()
    for key, label in GROUP_DEFS:
        directory = project_dir / label
        grouped_names.add(label)
        groups.append(
            {
                "key": key,
                "name": label,
                "rel_path": label,
                "exists": directory.is_dir(),
                "nodes": [
                    _build_node(project_id, project_dir, child)
                    for child in _visible_entries(directory)
                ],
            }
        )

    root_nodes = [
        _build_node(project_id, project_dir, child)
        for child in _visible_entries(project_dir)
        if child.name not in grouped_names
    ]

    return {
        "project": {
            "id": int(row["id"]),
            "name": row["name"],
            "path": row["path"],
            "archived": bool(row["archived"]),
        },
        "groups": groups,
        "root_nodes": root_nodes,
    }


def _require_existing(project_dir: Path, rel_path: str) -> Path:
    path = resolve_within(project_dir, rel_path)
    if not path.exists():
        raise NodeNotFoundError(f"对象不存在：{rel_path}")
    return path


def create_node(project_id: int, parent_rel: str, name: str, is_dir: bool = False) -> dict:
    """在 ``parent_rel`` 下新建文件（默认 ``.md``）或文件夹。

    「章节/」直属文件自动规范化为 ``第NNNN章.txt``（纯正文，标题写入 ``chapters`` 表）：
    已合规的名字原样保留，``第7章`` / ``第7章.md`` 这类补齐零填充，
    其余一律把输入当作章节标题并取下一个可用编号。
    """
    _row, project_dir = get_project_dir(project_id)

    parent = resolve_within(project_dir, parent_rel or "")
    if not parent.is_dir():
        raise NodeNotFoundError(f"父目录不存在：{parent_rel or '(项目根)'}")

    clean_name = validate_node_name(name)
    in_chapter_dir = parent.name == CHAPTER_DIR and parent.parent == Path(project_dir).resolve()

    title: str | None = None
    if not is_dir and in_chapter_dir:
        clean_name, title = normalize_chapter_create_name(project_dir, clean_name)
    elif not is_dir and not Path(clean_name).suffix:
        clean_name += DEFAULT_FILE_SUFFIX

    target = parent / clean_name
    if target.exists():
        raise NodeExistsError(f"同名对象已存在：{to_rel(project_dir, target)}")

    if is_dir:
        target.mkdir(parents=True)
    elif in_chapter_dir:
        atomic_write_text(target, "")
        upsert_chapter_meta(
            project_id, f"{CHAPTER_DIR}/{clean_name}",
            title=(title or ""), status="草稿", word_count=0,
        )
    else:
        atomic_write_text(target, "")
    return _build_node(project_id, project_dir, target)


def rename_node(project_id: int, rel_path: str, new_name: str) -> dict:
    """重命名；章节目录下的文件必须保持 ``第NNNN章.txt`` 命名规范。"""
    _row, project_dir = get_project_dir(project_id)
    source = _require_existing(project_dir, rel_path)
    if source == project_dir.resolve():
        raise InvalidOperationError("不能重命名项目根目录")

    clean_name = validate_node_name(new_name)
    if source.is_file() and source.parent.name == CHAPTER_DIR:
        validate_chapter_filename(clean_name)

    if clean_name == source.name:
        return _build_node(project_id, project_dir, source)

    target = source.parent / clean_name
    if target.exists():
        raise NodeExistsError(f"同名对象已存在：{to_rel(project_dir, target)}")
    source.rename(target)
    if source.parent.name == CHAPTER_DIR and is_chapter_name(target.name):
        move_chapter_meta(project_id, str(rel_path).replace("\\", "/"),
                          to_rel(project_dir, target))
    operation_log.log(project_id, "rename", to_rel(project_dir, target),
                      {"from": rel_path})
    from .file_change_service import rebind_cards_after_move

    rebind_cards_after_move(project_id, to_rel(project_dir, source), to_rel(project_dir, target))
    _rebuild_index(project_id)
    return _build_node(project_id, project_dir, target)


def delete_node(project_id: int, rel_path: str) -> dict:
    """删除文件/文件夹：空文件（或内容全空的文件夹）物理删除，否则进回收站。"""
    row, project_dir = get_project_dir(project_id)
    try:
        result = delete_path_by_content(project_dir, rel_path, row["name"])
    except FileNotFoundError as exc:
        raise NodeNotFoundError(str(exc)) from exc

    operation_log.log(project_id, "delete", rel_path, result)
    _refresh_index_after_change(project_id, rel_path, result.get("type") == "dir")
    return result


def delete_nodes(project_id: int, rel_paths: list[str]) -> list[dict]:
    """批量删除：逐条按内容分策略处理并返回各自去向（混合选择分别处理）。"""
    results: list[dict] = []
    for rel_path in rel_paths:
        try:
            results.append(delete_node(project_id, rel_path))
        except (NodeNotFoundError, InvalidOperationError) as exc:
            results.append(
                {
                    "rel_path": rel_path,
                    "type": "unknown",
                    "action": "failed",
                    "trash_rel": None,
                    "reason": exc.message,
                }
            )
    return results


def _refresh_index_after_change(project_id: int, rel_path: str, is_dir: bool) -> None:
    """增删改后刷新索引（索引可重建，失败不阻断业务）。"""
    try:
        from .index_service import rebuild_index, remove_from_index

        if is_dir:
            rebuild_index(project_id)
        else:
            remove_from_index(project_id, rel_path)
    except Exception:  # noqa: BLE001
        pass


def _rebuild_index(project_id: int) -> None:
    """整项目重建索引（重命名/移动这类会改变路径的操作使用）。"""
    try:
        from .index_service import rebuild_index

        rebuild_index(project_id)
    except Exception:  # noqa: BLE001
        pass


def move_node(project_id: int, src_rel: str, dst_parent_rel: str) -> dict:
    """把文件/文件夹移动到目标父目录下。"""
    _row, project_dir = get_project_dir(project_id)
    source = _require_existing(project_dir, src_rel)
    if source == project_dir.resolve():
        raise InvalidOperationError("不能移动项目根目录")

    destination_dir = resolve_within(project_dir, dst_parent_rel or "")
    if not destination_dir.is_dir():
        raise NodeNotFoundError(f"目标目录不存在：{dst_parent_rel or '(项目根)'}")

    if source.is_dir() and (
        destination_dir == source or source in destination_dir.parents
    ):
        raise InvalidOperationError("不能把文件夹移动到其自身或其子目录下")

    target = destination_dir / source.name
    if target.exists():
        raise NodeExistsError(f"目标目录下已存在同名对象：{to_rel(project_dir, target)}")

    shutil.move(str(source), str(target))
    operation_log.log(project_id, "move", to_rel(project_dir, target),
                      {"from": src_rel})
    from .file_change_service import rebind_cards_after_move

    rebind_cards_after_move(project_id, to_rel(project_dir, source), to_rel(project_dir, target))
    _rebuild_index(project_id)
    return _build_node(project_id, project_dir, target)


__all__ = [
    "GROUP_DEFS",
    "create_node",
    "delete_node",
    "delete_nodes",
    "get_file_tree",
    "move_node",
    "rename_node",
]
