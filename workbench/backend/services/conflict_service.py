"""外部修改检测与冲突解决（编辑器 ↔ 磁盘）。

场景：编辑器打开章节期间，用户在 dsh 会话或外部编辑器改了同一文件。
本模块用 **mtime + 内容 hash** 检出外部改动，产出**行级 diff**，
并提供「保留我的」/「采用外部」两种解决路径（保留我的会先给外部版本存快照）。
"""

from __future__ import annotations

import difflib
import hashlib
from pathlib import Path

from .errors import NodeNotFoundError
from .fs_utils import atomic_write_text, read_text, resolve_within
from .project_service import get_project_dir
from .snapshot_service import snapshot_file


def _hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def disk_state(project_dir: Path, rel_path: str) -> dict:
    """磁盘上某文件的状态：mtime / hash / size / 内容。"""
    path = resolve_within(Path(project_dir), rel_path)
    if not path.is_file():
        raise NodeNotFoundError(f"文件不存在：{rel_path}")
    text = read_text(path)
    stat = path.stat()
    return {
        "rel_path": rel_path,
        "mtime": stat.st_mtime,
        "size": stat.st_size,
        "hash": _hash(text),
        "content": text,
    }


def line_diff(base: str, other: str, *, base_label: str = "我的版本",
              other_label: str = "外部版本") -> list[dict]:
    """行级 diff（供前端渲染）：``[{type: equal|add|remove, line, text}]``。"""
    base_lines = str(base or "").splitlines()
    other_lines = str(other or "").splitlines()
    diff: list[dict] = []
    matcher = difflib.SequenceMatcher(a=base_lines, b=other_lines)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for offset, line in enumerate(base_lines[i1:i2]):
                diff.append({"type": "equal", "line": i1 + offset + 1, "text": line})
        elif tag == "delete":
            for offset, line in enumerate(base_lines[i1:i2]):
                diff.append({"type": "remove", "line": i1 + offset + 1, "text": line})
        elif tag == "insert":
            for offset, line in enumerate(other_lines[j1:j2]):
                diff.append({"type": "add", "line": i1 + offset + 1, "text": line})
        else:  # replace
            for offset, line in enumerate(base_lines[i1:i2]):
                diff.append({"type": "remove", "line": i1 + offset + 1, "text": line})
            for offset, line in enumerate(other_lines[j1:j2]):
                diff.append({"type": "add", "line": i1 + offset + 1, "text": line})
    return diff


def check_conflict(
    project_id: int,
    rel_path: str,
    *,
    base_mtime: float | None = None,
    base_hash: str | None = None,
    current_content: str | None = None,
) -> dict:
    """检查编辑器打开的文件是否被外部改动。

    :param base_mtime: 编辑器打开时记录的 mtime（None 表示不比较 mtime）
    :param base_hash: 编辑器打开时记录的内容 hash（优先于 mtime 判定）
    :param current_content: 编辑器当前内容（用于生成 diff；缺省只报状态）
    :return: ``{changed, reason, diff, external_content, disk}``
    """
    _row, project_dir = get_project_dir(project_id)
    disk = disk_state(project_dir, rel_path)

    changed = False
    reason = ""
    if base_hash is not None and base_hash != disk["hash"]:
        changed = True
        reason = "content"
    elif base_mtime is not None and abs(float(base_mtime) - float(disk["mtime"])) > 1e-6:
        changed = True
        reason = "mtime"

    result = {
        "changed": changed,
        "reason": reason,
        "rel_path": rel_path,
        "disk": {
            "mtime": disk["mtime"],
            "hash": disk["hash"],
            "size": disk["size"],
        },
        "external_content": disk["content"] if changed else None,
        "diff": [],
    }
    if changed and current_content is not None:
        result["diff"] = line_diff(current_content, disk["content"])
    return result


def resolve_keep_mine(project_id: int, rel_path: str, content: str) -> dict:
    """保留我的：先给外部版本存快照，再用编辑器内容覆盖磁盘。"""
    row, project_dir = get_project_dir(project_id)
    snapshot_file(project_id, project_dir, row["name"], rel_path, reason="external-keep-mine")
    target = resolve_within(Path(project_dir), rel_path)
    atomic_write_text(target, content)
    state = disk_state(project_dir, rel_path)
    return {"rel_path": rel_path, "mtime": state["mtime"], "hash": state["hash"],
            "resolution": "keep-mine"}


def resolve_take_external(project_id: int, rel_path: str) -> dict:
    """采用外部：返回磁盘内容与最新状态，编辑器以之刷新（磁盘不动）。"""
    _row, project_dir = get_project_dir(project_id)
    state = disk_state(project_dir, rel_path)
    return {
        "rel_path": rel_path,
        "content": state["content"],
        "mtime": state["mtime"],
        "hash": state["hash"],
        "resolution": "take-external",
    }


__all__ = [
    "check_conflict",
    "disk_state",
    "line_diff",
    "resolve_keep_mine",
    "resolve_take_external",
]