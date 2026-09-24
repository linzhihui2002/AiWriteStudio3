"""开书模板：内置默认模板（不可删除）+ 用户「另存为模板」。

- 内置模板首次访问时自动生成，落 ``templates/{名称}/``，DB 标记 ``is_builtin=1``；
- 用户可在项目里调整目录结构后「另存为模板」，下次开书时选用；
- 删除内置模板一律被拒（提示可复制后修改）。
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from .. import config, db
from .errors import InvalidNameError, InvalidOperationError, NodeNotFoundError
from .fs_utils import atomic_write_text, compose_document, validate_node_name
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


__all__ = [
    "BUILTIN_TEMPLATE_NAME",
    "delete_template",
    "duplicate_template",
    "ensure_builtin_template",
    "list_templates",
    "save_as_template",
    "template_path",
    "templates_root",
]