"""版本快照：保存 / 应用类操作前把旧内容存档到 ``.workbench/snapshots/``。

布局::

    .workbench/snapshots/{书名}/{相对路径转义}/{时间戳}-{原因}.md

- 快照是**运行时产物**（可清理），事实源始终是项目内的 Markdown；
- 相同内容重复快照时跳过（按内容 hash 去重），避免刷屏；
- 快照仅保留最近 :data:`MAX_PER_FILE` 份，超出自动清理最旧的。
"""

from __future__ import annotations

import hashlib
import shutil
from datetime import datetime
from pathlib import Path

from .. import config, db
from .errors import InvalidOperationError, NodeNotFoundError
from .fs_utils import atomic_write_text, read_text, resolve_within

MAX_PER_FILE = 20
_SAFE_NAME = str.maketrans({":": "_", "*": "_", "?": "_", '"': "_", "<": "_", ">": "_", "|": "_"})


def _now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S-%f")


def snapshots_root() -> Path:
    return config.snapshots_dir()


def _safe_reason(reason: str) -> str:
    text = "".join(ch for ch in str(reason or "save") if ch.isalnum() or ch in "-_")
    return text[:32] or "save"


def _file_snapshot_dir(project_name: str, rel_path: str) -> Path:
    safe_rel = str(rel_path).replace("\\", "/").translate(_SAFE_NAME).replace("/", "__")
    return snapshots_root() / project_name / safe_rel


def _content_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


def _latest_hash(directory: Path) -> str | None:
    if not directory.is_dir():
        return None
    entries = sorted(
        (p for p in directory.glob("*.md")), key=lambda p: p.name, reverse=True
    )
    if not entries:
        return None
    for entry in entries[:1]:
        try:
            text = read_text(entry)
        except (OSError, UnicodeDecodeError):
            return None
        marker = text.split("\n", 1)[0]
        if marker.startswith("<!-- snapshot-hash: "):
            return marker[len("<!-- snapshot-hash: "):].rstrip(" -->")
    return None


def create_snapshot(
    project_id: int,
    project_name: str,
    rel_path: str,
    content: str,
    reason: str = "save",
) -> dict | None:
    """把 ``content`` 存为快照（内容与最近一份相同时跳过，返回 None）。"""
    digest = _content_hash(content)
    directory = _file_snapshot_dir(project_name, rel_path)
    if _latest_hash(directory) == digest:
        return None

    directory.mkdir(parents=True, exist_ok=True)
    stamp = _now_stamp()
    target = directory / f"{stamp}-{_safe_reason(reason)}.md"
    body = f"<!-- snapshot-hash: {digest} -->\n{content}"
    atomic_write_text(target, body)

    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO snapshots (project_id, rel_path, snapshot_path, reason)"
            " VALUES (?, ?, ?, ?)",
            (project_id, rel_path, str(target), reason),
        )

    _prune(directory)
    return {"rel_path": rel_path, "snapshot_path": str(target), "reason": reason}


def _prune(directory: Path) -> None:
    entries = sorted(directory.glob("*.md"), key=lambda p: p.name, reverse=True)
    for stale in entries[MAX_PER_FILE:]:
        stale.unlink(missing_ok=True)


def snapshot_file(project_id: int, project_dir: Path, project_name: str,
                  rel_path: str, reason: str = "save") -> dict | None:
    """对磁盘上已有文件做快照（文件不存在时返回 None）。"""
    path = resolve_within(project_dir, rel_path)
    if not path.is_file():
        return None
    try:
        content = read_text(path)
    except (OSError, UnicodeDecodeError):
        return None
    return create_snapshot(project_id, project_name, rel_path, content, reason)


def list_snapshots(project_id: int, rel_path: str | None = None) -> list[dict]:
    """列出快照（新→旧）。"""
    sql = "SELECT id, project_id, rel_path, snapshot_path, reason, created_at FROM snapshots WHERE project_id = ?"
    params: list = [project_id]
    if rel_path:
        sql += " AND rel_path = ?"
        params.append(rel_path)
    sql += " ORDER BY id DESC LIMIT 200"
    with db.get_conn() as conn:
        rows = conn.execute(sql, tuple(params)).fetchall()
    return [
        {
            "id": int(row["id"]),
            "rel_path": row["rel_path"],
            "snapshot_path": row["snapshot_path"],
            "reason": row["reason"],
            "created_at": row["created_at"],
            "exists": Path(row["snapshot_path"]).is_file(),
        }
        for row in rows
    ]


def restore_snapshot(snapshot_id: int, *, project_id: int | None = None,
                     expected_hash: str | None = None) -> dict:
    """把某份快照写回其原路径（写回前对当前内容再存一份快照）。"""
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT id, project_id, rel_path, snapshot_path FROM snapshots WHERE id = ?",
            (snapshot_id,),
        ).fetchone()
    if row is None:
        raise NodeNotFoundError(f"快照不存在：id={snapshot_id}")
    if project_id is not None and int(row["project_id"]) != project_id:
        raise InvalidOperationError("快照不属于当前项目")

    from .project_service import get_project_dir  # 延迟导入避免循环

    project_id = int(row["project_id"])
    project_row, project_dir = get_project_dir(project_id)
    snapshot_path = Path(row["snapshot_path"]).resolve()
    archive_root = snapshots_root().resolve()
    if archive_root not in snapshot_path.parents:
        raise InvalidOperationError("快照位置超出工作台快照目录")
    if not snapshot_path.is_file():
        raise NodeNotFoundError(f"快照文件缺失：{snapshot_path}")

    from .file_change_service import assert_version, content_hash, project_lock
    with project_lock(project_id):
        rel_path = str(row["rel_path"])
        target = resolve_within(project_dir, rel_path)
        if expected_hash is not None:
            assert_version(rel_path, read_text(target) if target.is_file() else None, expected_hash)
        snapshot_file(project_id, project_dir, project_row["name"], rel_path,
                      reason="before-restore")
        text = read_text(snapshot_path)
        if text.startswith("<!-- snapshot-hash: "):
            text = text.split("\n", 1)[1] if "\n" in text else ""
        atomic_write_text(target, text)
        from .chapter_service import refresh_index
        refresh_index(project_id, project_dir, rel_path)
        return {"rel_path": rel_path, "restored_from": str(snapshot_path), "hash": content_hash(text)}


def clear_snapshots(project_id: int | None = None) -> int:
    """清理快照（按项目或全量），返回删除条数。"""
    with db.get_conn() as conn:
        if project_id is None:
            rows = conn.execute("SELECT snapshot_path FROM snapshots").fetchall()
            conn.execute("DELETE FROM snapshots")
        else:
            rows = conn.execute(
                "SELECT snapshot_path FROM snapshots WHERE project_id = ?", (project_id,)
            ).fetchall()
            conn.execute("DELETE FROM snapshots WHERE project_id = ?", (project_id,))
    removed = 0
    for row in rows:
        path = Path(row["snapshot_path"])
        if path.is_file():
            path.unlink(missing_ok=True)
            removed += 1
    return removed


def purge_all() -> None:
    """删除全部快照目录（供卸载脚本使用）。"""
    shutil.rmtree(snapshots_root(), ignore_errors=True)


__all__ = [
    "clear_snapshots",
    "create_snapshot",
    "list_snapshots",
    "purge_all",
    "restore_snapshot",
    "snapshot_file",
    "snapshots_root",
]
