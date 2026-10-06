"""Token 计量与成本预估：记录每次引擎调用的用量，支撑仪表盘与预算告警。"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from .. import db
from .settings_service import read_settings


def record_usage(
    *,
    project_id: int | None,
    task_id: int | None = None,
    provider: str = "",
    model: str = "",
    task_type: str = "",
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    cost_estimate: float | None = None,
) -> dict:
    """记录一次用量（失败不抛错，不影响主流程）。"""
    total = int(prompt_tokens or 0) + int(completion_tokens or 0)
    cost = (
        float(cost_estimate)
        if cost_estimate is not None
        else estimate_cost(prompt_tokens, completion_tokens)
    )
    try:
        with db.get_conn() as conn:
            cursor = conn.execute(
                "INSERT INTO token_usage (project_id, task_id, provider, model, task_type,"
                " prompt_tokens, completion_tokens, total_tokens, cost_estimate)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    project_id,
                    task_id,
                    provider,
                    model,
                    task_type,
                    int(prompt_tokens or 0),
                    int(completion_tokens or 0),
                    total,
                    cost,
                ),
            )
            usage_id = int(cursor.lastrowid or 0)
    except Exception:  # noqa: BLE001 - 计量失败不阻断
        usage_id = 0
    return {"id": usage_id, "total_tokens": total, "cost_estimate": cost}


def estimate_cost(prompt_tokens: int, completion_tokens: int) -> float:
    """按设置里的单价估算成本（0 表示未配置单价，即不计价）。"""
    settings = read_settings().get("budget", {})
    price = float(settings.get("currency_per_1k_tokens") or 0.0)
    if price <= 0:
        return 0.0
    return round(
        (int(prompt_tokens or 0) + int(completion_tokens or 0)) / 1000.0 * price, 4
    )


def summary(project_id: int | None = None, days: int = 30) -> dict:
    """用量汇总：按日 / 按项目 / 按任务类型，附预算告警。"""
    days = max(1, days)
    today = date.today()
    since = (today - timedelta(days=days - 1)).isoformat()
    params: list = [since, today.isoformat()]
    where = "WHERE date(created_at) >= ? AND date(created_at) <= ?"
    if project_id is not None:
        where += " AND project_id = ?"
        params.append(project_id)

    with db.get_conn() as conn:
        by_day = conn.execute(
            "SELECT date(created_at) AS day, SUM(total_tokens) AS tokens,"
            " SUM(prompt_tokens) AS prompt, SUM(completion_tokens) AS completion,"
            " SUM(cost_estimate) AS cost, COUNT(*) AS calls"
            f" FROM token_usage {where} GROUP BY day ORDER BY day ASC",
            tuple(params),
        ).fetchall()
        by_project = conn.execute(
            "SELECT project_id, SUM(total_tokens) AS tokens, SUM(cost_estimate) AS cost,"
            " COUNT(*) AS calls"
            f" FROM token_usage {where} GROUP BY project_id ORDER BY tokens DESC",
            tuple(params),
        ).fetchall()
        by_type = conn.execute(
            "SELECT task_type, SUM(total_tokens) AS tokens, SUM(cost_estimate) AS cost,"
            " COUNT(*) AS calls"
            f" FROM token_usage {where} GROUP BY task_type ORDER BY tokens DESC",
            tuple(params),
        ).fetchall()
        totals = conn.execute(
            "SELECT COALESCE(SUM(total_tokens), 0) AS tokens,"
            " COALESCE(SUM(cost_estimate), 0) AS cost, COUNT(*) AS calls"
            f" FROM token_usage {where}",
            tuple(params),
        ).fetchone()

    today_tokens = 0
    today_str = today.isoformat()
    for row in by_day:
        if row["day"] == today_str:
            today_tokens = int(row["tokens"] or 0)

    budget = read_settings().get("budget", {})
    limit = int(budget.get("daily_token_limit") or 0)
    ratio = float(budget.get("alert_ratio") or 0.8)
    alert = bool(limit) and today_tokens >= limit * ratio

    return {
        "days": days,
        "period": {"start": since, "end": today_str},
        "project_id": project_id,
        "totals": {
            "tokens": int(totals["tokens"] or 0),
            "cost": round(float(totals["cost"] or 0), 4),
            "calls": int(totals["calls"] or 0),
            "today_tokens": today_tokens,
        },
        "by_day": [
            {
                "day": row["day"],
                "tokens": int(row["tokens"] or 0),
                "prompt": int(row["prompt"] or 0),
                "completion": int(row["completion"] or 0),
                "cost": round(float(row["cost"] or 0), 4),
                "calls": int(row["calls"] or 0),
            }
            for row in by_day
        ],
        "by_project": [
            {
                "project_id": row["project_id"],
                "tokens": int(row["tokens"] or 0),
                "cost": round(float(row["cost"] or 0), 4),
                "calls": int(row["calls"] or 0),
            }
            for row in by_project
        ],
        "by_task_type": [
            {
                "task_type": row["task_type"] or "未分类",
                "tokens": int(row["tokens"] or 0),
                "cost": round(float(row["cost"] or 0), 4),
                "calls": int(row["calls"] or 0),
            }
            for row in by_type
        ],
        "budget": {
            "daily_token_limit": limit,
            "alert_ratio": ratio,
            "currency_per_1k_tokens": float(budget.get("currency_per_1k_tokens") or 0.0),
            "alert": alert,
        },
    }


def recent(project_id: int | None = None, limit: int = 50) -> list[dict]:
    """最近用量明细（任务留痕）。"""
    sql = (
        "SELECT id, project_id, task_id, provider, model, task_type, prompt_tokens,"
        " completion_tokens, total_tokens, cost_estimate, created_at FROM token_usage"
    )
    params: list = []
    if project_id is not None:
        sql += " WHERE project_id = ?"
        params.append(project_id)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(int(limit))
    with db.get_conn() as conn:
        rows = conn.execute(sql, tuple(params)).fetchall()
    return [dict(row) for row in rows]


def purge_all() -> int:
    """清空用量记录（仪表盘重置）。"""
    with db.get_conn() as conn:
        cursor = conn.execute("DELETE FROM token_usage")
    return int(cursor.rowcount or 0)


__all__ = ["estimate_cost", "purge_all", "recent", "record_usage", "summary"]
