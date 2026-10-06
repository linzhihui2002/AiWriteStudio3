"""开书模板：内置默认模板（不可删除）+ 用户自定义模板的完整管理。

- 内置模板首次访问时自动生成，落 ``templates/{名称}/``，DB 标记 ``is_builtin=1``；
- 自定义模板可「新建空白 / 另存为 / 复制 / 重命名 / 删除 / 导入导出」；
- 内置模板只读：可浏览、复制、导出，但拒绝写入内部文件（守住「复制后修改」契约）；
- 模板内文件树的新建/改名/移动/删除为**物理操作**（模板只是骨架，不进回收站）；
- 默认模板与「题材/平台 → 模板」映射存 ``.workbench/settings.json`` 的 ``templates`` 段。
"""

from __future__ import annotations

import json
import re
import shutil
from datetime import datetime
from pathlib import Path

from .. import config, db
from . import settings_service
from .errors import (
    InvalidNameError,
    InvalidOperationError,
    NodeExistsError,
    NodeNotFoundError,
)
from .fs_utils import (
    atomic_write_text,
    compose_document,
    is_empty_file,
    read_text,
    resolve_within,
    to_rel,
    validate_node_name,
)
from .project_service import WORKSPACE_DIRS, WORKSPACE_FILES

BUILTIN_TEMPLATE_NAME = "默认模板"
BUILTIN_TEMPLATE_DESC = "工作台内置模板：备忘录/大纲/设定/状态/章节 + 空 md 文件"

# 另存为模板时忽略的目录（运行时/内部目录）
IGNORED_DIRS = {".meta", ".git", "__pycache__"}


def templates_root() -> Path:
    return config.templates_dir()


def _placeholder(rel_path: str) -> str:
    return compose_document({"标题": Path(rel_path).stem}, "")


def _register(name: str, path: Path, is_builtin: bool) -> dict:
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO templates (name, path, is_builtin) VALUES (?, ?, ?)"
            " ON CONFLICT(name) DO UPDATE SET path = excluded.path",
            (name, str(path), 1 if is_builtin else 0),
        )
        row = conn.execute(
            "SELECT id, name, path, is_builtin, created_at FROM templates WHERE name = ?",
            (name,),
        ).fetchone()
    return {
        "id": int(row["id"]),
        "name": row["name"],
        "path": row["path"],
        "is_builtin": bool(row["is_builtin"]),
        "created_at": row["created_at"],
    }


def ensure_builtin_template() -> dict:
    """确保内置默认模板存在（幂等）。"""
    root = templates_root() / BUILTIN_TEMPLATE_NAME
    for rel_dir in WORKSPACE_DIRS:
        (root / rel_dir).mkdir(parents=True, exist_ok=True)
    for rel_file in WORKSPACE_FILES:
        target = root / rel_file
        if not target.exists():
            atomic_write_text(target, _placeholder(rel_file))
    return _register(BUILTIN_TEMPLATE_NAME, root, True)


def list_templates() -> list[dict]:
    """模板列表（内置在前，其后按创建时间倒序）。"""
    ensure_builtin_template()
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT id, name, path, is_builtin, created_at FROM templates"
            " ORDER BY is_builtin DESC, created_at DESC, id DESC"
        ).fetchall()
    return [
        {
            "id": int(row["id"]),
            "name": row["name"],
            "path": row["path"],
            "is_builtin": bool(row["is_builtin"]),
            "exists": Path(row["path"]).is_dir() if row["path"] else False,
            "created_at": row["created_at"],
        }
        for row in rows
    ]


def template_path(name: str) -> Path:
    """按名称解析模板目录，不存在时报错。"""
    ensure_builtin_template()
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT path, is_builtin FROM templates WHERE name = ?", (name,)
        ).fetchone()
    if row is not None and row["path"]:
        path = Path(row["path"])
        if path.is_dir():
            return path
    fallback = templates_root() / name
    if fallback.is_dir():
        return fallback
    raise NodeNotFoundError(f"模板不存在：{name}")


def save_as_template(name: str, project_dir: Path) -> dict:
    """把项目当前目录结构「另存为模板」（跳过内部目录与章节目录下的正文）。

    章节目录仅保留空壳（不复制正文内容），避免把作品正文带进模板。
    """
    clean = validate_node_name(name)
    if clean == BUILTIN_TEMPLATE_NAME:
        raise InvalidNameError(f"「{BUILTIN_TEMPLATE_NAME}」为内置模板名，请换一个名称")

    root = templates_root() / clean
    if root.exists():
        raise InvalidOperationError(f"同名模板已存在：{clean}")

    source = Path(project_dir)
    chapter_dir = source / "章节"

    for path in sorted(source.rglob("*")):
        rel = path.relative_to(source)
        if any(part in IGNORED_DIRS for part in rel.parts):
            continue
        if path.is_dir():
            (root / rel).mkdir(parents=True, exist_ok=True)
            continue
        if chapter_dir in path.parents:
            continue  # 章节正文不进模板
        if rel.name == "project.md":
            continue  # 项目档案由开书流程重新生成
        content = path.read_text(encoding="utf-8", errors="replace")
        atomic_write_text(root / rel, _normalize_template_doc(content))

    (root / "章节").mkdir(parents=True, exist_ok=True)
    return _register(clean, root, False)


def _normalize_template_doc(text: str) -> str:
    """模板内容规范化：剔除作品专属信息（空模板只保留结构）。"""
    body = str(text or "")
    return re.sub(r"\s+$", "\n", body)


def delete_template(name: str) -> dict:
    """删除模板；内置模板拒绝删除。"""
    ensure_builtin_template()
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT id, path, is_builtin FROM templates WHERE name = ?", (name,)
        ).fetchone()
    if row is None:
        raise NodeNotFoundError(f"模板不存在：{name}")
    if row["is_builtin"]:
        raise InvalidOperationError(
            f"「{name}」是内置模板，不可删除；可复制后修改再另存为自定义模板。"
        )

    if row["path"]:
        shutil.rmtree(Path(row["path"]), ignore_errors=True)
    with db.get_conn() as conn:
        conn.execute("DELETE FROM templates WHERE name = ?", (name,))
    return {"name": name, "deleted": True}


def duplicate_template(name: str, new_name: str) -> dict:
    """复制模板（内置模板可复制后修改）。"""
    source = template_path(name)
    clean = validate_node_name(new_name)
    target = templates_root() / clean
    if target.exists():
        raise InvalidOperationError(f"同名模板已存在：{clean}")
    shutil.copytree(source, target)
    return _register(clean, target, False)


def create_blank_template(name: str) -> dict:
    """新建空白模板：按工作区骨架生成空目录 + 空 md 文件。"""
    clean = validate_node_name(name)
    if clean == BUILTIN_TEMPLATE_NAME:
        raise InvalidNameError(f"「{BUILTIN_TEMPLATE_NAME}」为内置模板名，请换一个名称")
    root = templates_root() / clean
    if root.exists():
        raise InvalidOperationError(f"同名模板已存在：{clean}")

    for rel_dir in WORKSPACE_DIRS:
        (root / rel_dir).mkdir(parents=True, exist_ok=True)
    for rel_file in WORKSPACE_FILES:
        target = root / rel_file
        if not target.exists():
            atomic_write_text(target, _placeholder(rel_file))
    return _register(clean, root, False)


def rename_template(name: str, new_name: str) -> dict:
    """重命名自定义模板（内置模板拒绝）；默认偏好引用该模板时跟随改名。"""
    ensure_builtin_template()
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT name, path, is_builtin FROM templates WHERE name = ?", (name,)
        ).fetchone()
    if row is None:
        raise NodeNotFoundError(f"模板不存在：{name}")
    if row["is_builtin"]:
        raise InvalidOperationError(
            f"「{name}」是内置模板，不可重命名；可复制后修改再重命名。"
        )

    clean = validate_node_name(new_name)
    if clean == name:
        raise InvalidOperationError("新名称与原名称相同")
    if clean == BUILTIN_TEMPLATE_NAME:
        raise InvalidNameError(f"「{BUILTIN_TEMPLATE_NAME}」为内置模板名，请换一个名称")

    source = Path(row["path"]) if row["path"] else templates_root() / name
    target = templates_root() / clean
    if target.exists():
        raise InvalidOperationError(f"同名模板已存在：{clean}")

    shutil.move(str(source), str(target))
    with db.get_conn() as conn:
        conn.execute("DELETE FROM templates WHERE name = ?", (name,))
    result = _register(clean, target, False)

    if get_template_prefs().get("default") == name:
        update_template_prefs(default=clean)
    return result


# ─────────────────────────── 默认 / 题材平台适配偏好 ───────────────────────────


def get_template_prefs() -> dict:
    """读取模板偏好（默认模板 + 题材/平台映射）；缺失或类型异常时回落空值。"""
    settings = settings_service.read_settings()
    prefs = settings.get("templates") if isinstance(settings.get("templates"), dict) else {}
    default = prefs.get("default") if isinstance(prefs.get("default"), str) else ""
    by_genre = prefs.get("by_genre") if isinstance(prefs.get("by_genre"), dict) else {}
    by_platform = prefs.get("by_platform") if isinstance(prefs.get("by_platform"), dict) else {}
    return {
        "default": default,
        "by_genre": {str(key): str(value) for key, value in by_genre.items()},
        "by_platform": {str(key): str(value) for key, value in by_platform.items()},
    }


def _write_template_prefs(prefs: dict) -> None:
    """整段替换 ``settings.templates``（空映射可被清空，不走深合并）。"""
    settings = settings_service.read_settings()
    settings["templates"] = prefs
    atomic_write_text(
        settings_service.settings_file(),
        json.dumps(settings, ensure_ascii=False, indent=2) + "\n",
    )


def update_template_prefs(
    default: str | None = None,
    by_genre: dict | None = None,
    by_platform: dict | None = None,
) -> dict:
    """更新模板偏好；引用的模板必须存在（``default=""`` 表示内置默认模板）。"""
    current = get_template_prefs()
    names = {item["name"] for item in list_templates()}

    if default is not None:
        value = str(default).strip()
        if value and value not in names:
            raise InvalidOperationError(f"默认模板不存在：{value}")
        current["default"] = value

    for key, patch in (("by_genre", by_genre), ("by_platform", by_platform)):
        if patch is None:
            continue
        if not isinstance(patch, dict):
            raise InvalidOperationError(f"{key} 必须是「键 → 模板名」的映射")
        cleaned: dict[str, str] = {}
        for raw_key, raw_value in patch.items():
            map_key = str(raw_key).strip()
            map_value = str(raw_value).strip()
            if not map_key or not map_value:
                continue
            if map_value not in names:
                raise InvalidOperationError(f"映射引用的模板不存在：{map_value}")
            cleaned[map_key] = map_value
        current[key] = cleaned

    _write_template_prefs(current)
    return current


def resolve_open_template(
    explicit: str | None, genre: str = "", platform: str = ""
) -> str:
    """解析开书用模板名：显式 → 题材映射 → 平台映射 → 默认 → 内置。"""
    ensure_builtin_template()
    names = {item["name"] for item in list_templates()}
    prefs = get_template_prefs()
    candidates = (
        explicit,
        prefs["by_genre"].get(str(genre or "").strip()),
        prefs["by_platform"].get(str(platform or "").strip()),
        prefs["default"],
    )
    for candidate in candidates:
        if candidate and str(candidate) in names:
            return str(candidate)
    return BUILTIN_TEMPLATE_NAME


# ─────────────────────────── 模板内文件树（自定义模板可写） ───────────────────────────


def _require_writable_template(name: str) -> Path:
    """定位模板目录并确认可写（内置模板只读）。"""
    root = template_path(name)
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT is_builtin FROM templates WHERE name = ?", (name,)
        ).fetchone()
    if row is not None and row["is_builtin"]:
        raise InvalidOperationError(
            f"「{name}」是内置模板（只读），请先复制为自定义模板再修改。"
        )
    return root


def _build_template_nodes(root: Path, directory: Path) -> list[dict]:
    """递归构建模板文件树节点（跳过 ``.`` 开头的内部条目）。"""
    nodes: list[dict] = []
    for entry in sorted(directory.iterdir(), key=lambda path: (not path.is_dir(), path.name)):
        if entry.name.startswith("."):
            continue
        rel = to_rel(root, entry)
        if entry.is_dir():
            nodes.append({
                "name": entry.name,
                "rel_path": rel,
                "type": "dir",
                "children": _build_template_nodes(root, entry),
            })
        else:
            stat = entry.stat()
            nodes.append({
                "name": entry.name,
                "rel_path": rel,
                "type": "file",
                "is_empty": is_empty_file(entry),
                "size": stat.st_size,
                "mtime": stat.st_mtime,
            })
    return nodes


def _template_node_payload(root: Path, path: Path) -> dict:
    """单个模板条目（用于新建/改名/移动的返回值）。"""
    rel = to_rel(root, path)
    if path.is_dir():
        return {
            "name": path.name,
            "rel_path": rel,
            "type": "dir",
            "children": _build_template_nodes(root, path),
        }
    stat = path.stat()
    return {
        "name": path.name,
        "rel_path": rel,
        "type": "file",
        "is_empty": is_empty_file(path),
        "size": stat.st_size,
        "mtime": stat.st_mtime,
    }


def list_template_tree(name: str) -> dict:
    """模板目录树（内置模板也可浏览）。"""
    root = template_path(name)
    return {"name": name, "exists": root.is_dir(), "nodes": _build_template_nodes(root, root)}


def read_template_file(name: str, rel_path: str) -> dict:
    """读取模板内某个文件内容。"""
    root = template_path(name)
    target = resolve_within(root, rel_path)
    if not target.is_file():
        raise NodeNotFoundError(f"模板文件不存在：{rel_path}")
    stat = target.stat()
    return {
        "rel_path": to_rel(root, target),
        "content": read_text(target),
        "is_empty": is_empty_file(target),
        "size": stat.st_size,
        "mtime": stat.st_mtime,
    }


def write_template_file(name: str, rel_path: str, content: str) -> dict:
    """写入模板内文件（父目录不存在时补建）；内置模板拒绝。"""
    root = _require_writable_template(name)
    target = resolve_within(root, rel_path)
    if target == root:
        raise InvalidNameError("文件路径不能为空")
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(target, str(content or ""))
    stat = target.stat()
    return {
        "rel_path": to_rel(root, target),
        "content": str(content or ""),
        "is_empty": is_empty_file(target),
        "size": stat.st_size,
        "mtime": stat.st_mtime,
    }


def create_template_node(name: str, parent_rel: str, node_name: str, is_dir: bool) -> dict:
    """在模板内新建文件（默认补 ``.md``）或目录；内置模板拒绝。"""
    root = _require_writable_template(name)
    parent = resolve_within(root, parent_rel)
    if not parent.is_dir():
        raise NodeNotFoundError(f"父目录不存在：{parent_rel or '(模板根)'}")

    clean = validate_node_name(node_name)
    if not is_dir and not clean.endswith(".md"):
        clean = f"{clean}.md"
    target = parent / clean
    if target.exists():
        raise NodeExistsError(f"同名条目已存在：{clean}")
    if is_dir:
        target.mkdir(parents=True)
    else:
        atomic_write_text(target, _placeholder(clean))
    return _template_node_payload(root, target)


def rename_template_node(name: str, rel_path: str, new_name: str) -> dict:
    """重命名模板内的文件/目录；内置模板拒绝。"""
    root = _require_writable_template(name)
    source = resolve_within(root, rel_path)
    if source == root or not source.exists():
        raise NodeNotFoundError(f"模板条目不存在：{rel_path}")

    clean = validate_node_name(new_name)
    if source.is_file() and source.suffix == ".md" and not clean.endswith(".md"):
        clean = f"{clean}.md"
    target = source.parent / clean
    if target.exists():
        raise NodeExistsError(f"同名条目已存在：{clean}")
    source.rename(target)
    return _template_node_payload(root, target)


def move_template_node(name: str, rel_path: str, dst_parent_rel: str) -> dict:
    """把模板内条目移动到另一个目录；禁止移入自身或其子目录。"""
    root = _require_writable_template(name)
    source = resolve_within(root, rel_path)
    if source == root or not source.exists():
        raise NodeNotFoundError(f"模板条目不存在：{rel_path}")
    dst_parent = resolve_within(root, dst_parent_rel)
    if not dst_parent.is_dir():
        raise NodeNotFoundError(f"目标目录不存在：{dst_parent_rel or '(模板根)'}")
    if source.is_dir() and (dst_parent == source or source in dst_parent.parents):
        raise InvalidOperationError("不能把目录移动到它自己或其子目录下")

    target = dst_parent / source.name
    if target.exists():
        raise NodeExistsError(f"目标目录已存在同名条目：{source.name}")
    shutil.move(str(source), str(target))
    return _template_node_payload(root, target)


def delete_template_node(name: str, rel_path: str) -> dict:
    """物理删除模板内的文件/目录（模板不进回收站）；内置模板拒绝。"""
    root = _require_writable_template(name)
    target = resolve_within(root, rel_path)
    if target == root or not target.exists():
        raise NodeNotFoundError(f"模板条目不存在：{rel_path}")
    kind = "dir" if target.is_dir() else "file"
    if target.is_dir():
        shutil.rmtree(target)
    else:
        target.unlink()
    return {"rel_path": to_rel(root, target), "type": kind, "deleted": True}


# ─────────────────────────── 导出 / 导入 ───────────────────────────


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def export_template(name: str) -> dict:
    """导出模板包（仅 UTF-8 文本；跳过 ``.`` 开头条目）。"""
    root = template_path(name)
    dirs: list[str] = []
    files: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if any(part.startswith(".") for part in rel.parts):
            continue
        if path.is_dir():
            dirs.append(rel.as_posix())
            continue
        try:
            files[rel.as_posix()] = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
    return {"name": name, "exported_at": _now_iso(), "dirs": dirs, "files": files}


def import_template(payload: dict, new_name: str | None = None) -> dict:
    """从模板包导入新模板（可指定 new_name 覆盖包内名称）。"""
    if not isinstance(payload, dict):
        raise InvalidOperationError("模板包格式不正确（应为 JSON 对象）")

    raw_name = new_name if new_name is not None else payload.get("name")
    clean = validate_node_name(str(raw_name or ""))
    if clean == BUILTIN_TEMPLATE_NAME:
        raise InvalidNameError(f"「{BUILTIN_TEMPLATE_NAME}」为内置模板名，请换一个名称")

    root = templates_root() / clean
    if root.exists():
        raise InvalidOperationError(f"同名模板已存在：{clean}")

    files = payload.get("files")
    if not isinstance(files, dict) or not files:
        raise InvalidOperationError("模板包不含任何文件")
    dirs = payload.get("dirs") if isinstance(payload.get("dirs"), list) else []

    root.mkdir(parents=True)
    try:
        for rel in dirs:
            if not isinstance(rel, str) or not rel.strip():
                continue
            parts = Path(rel).parts
            if any(part == ".." for part in parts):
                raise InvalidNameError(f"模板包路径越界：{rel}")
            if any(part.startswith(".") for part in parts):
                continue
            resolve_within(root, rel).mkdir(parents=True, exist_ok=True)
        for rel, content in files.items():
            if not isinstance(rel, str) or not rel.strip():
                continue
            parts = Path(rel).parts
            if any(part == ".." for part in parts):
                raise InvalidNameError(f"模板包路径越界：{rel}")
            if any(part.startswith(".") for part in parts):
                continue
            target = resolve_within(root, rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(target, str(content if content is not None else ""))
    except BaseException:
        shutil.rmtree(root, ignore_errors=True)
        raise
    return _register(clean, root, False)


__all__ = [
    "BUILTIN_TEMPLATE_NAME",
    "create_blank_template",
    "create_template_node",
    "delete_template",
    "delete_template_node",
    "duplicate_template",
    "ensure_builtin_template",
    "export_template",
    "get_template_prefs",
    "import_template",
    "list_template_tree",
    "list_templates",
    "move_template_node",
    "read_template_file",
    "rename_template",
    "rename_template_node",
    "resolve_open_template",
    "save_as_template",
    "template_path",
    "templates_root",
    "update_template_prefs",
    "write_template_file",
]