"""操作日志：删除去向、索引重建、批量产章等关键动作留痕（供 UI 提示与审计）。

只写 ``operation_log`` 表（索引层，可清理）；不写项目内 Markdown。
"""

from __future__ import annotations

import json

from .. import db


def log(project_id: int | None, action: str, rel_path: str | None = None,
        detail: object | None = None) -> None:
    """记录一条操作日志（失败不抛错，不影响主流程）。"""
    payload = ""
    if detail is not None:
        payload = detail if isinstance(detail, str) else json.dumps(
            detail, ensure_ascii=False, default=str
        )
    try:
        with db.get_conn() as conn:
            conn.execute(
                "INSERT INTO operation_log (project_id, action, rel_path, detail)"
                " VALUES (?, ?, ?, ?)",
                (project_id, action, rel_path, payload),
            )
    except Exception:  # noqa: BLE001 - 日志失败不阻断业务
        pass


def recent(project_id: int | None = None, limit: int = 50) -> list[dict]:
    """最近的操作日志（新→旧）。"""
    sql = "SELECT id, project_id, action, rel_path, detail, created_at FROM operation_log"
    params: list = []
    if project_id is not None:
        sql += " WHERE project_id = ?"
        params.append(project_id)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(int(limit))
    with db.get_conn() as conn:
        rows = conn.execute(sql, tuple(params)).fetchall()
    return [
        {
            "id": int(row["id"]),
            "project_id": row["project_id"],
            "action": row["action"],
            "rel_path": row["rel_path"],
            "detail": row["detail"],
            "created_at": row["created_at"],
        }
        for row in rows
    ]


__all__ = ["log", "recent"]