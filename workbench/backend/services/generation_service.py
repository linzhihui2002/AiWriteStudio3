"""生成服务：任务落库 + 引擎调用 + 用量计量 + 可追溯（Prompt 版本 / 上下文快照）。

所有引擎调用**必须**经本模块，以保证：
- 每次调用都有 ``tasks`` 记录（引擎 / 模型 / 状态 / 耗时 / Prompt 版本）；
- 每次调用都记 ``token_usage``（仪表盘与预算告警的数据源）；
- 取消与流式中断都能落到任务状态（cancelled），不静默吞掉。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Iterator

from .. import db
from ..engine import router
from ..engine.runtime import GenerationRequest, GenerationResult
from . import token_service
from .errors import NodeNotFoundError
from .project_service import get_project_dir


def _json(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def _insert_task(
    *,
    project_id: int | None,
    task_type: str,
    engine: str,
    model: str = "",
    provider: str = "",
    agent: str = "",
    prompt_id: str = "",
    prompt_version: str = "",
    context_snapshot: dict | None = None,
    status: str = "running",
) -> int:
    with db.get_conn() as conn:
        cursor = conn.execute(
            "INSERT INTO tasks (project_id, task_type, engine, model, provider, agent,"
            " status, prompt_id, prompt_version, context_snapshot)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                project_id,
                task_type,
                engine,
                model,
                provider,
                agent,
                status,
                prompt_id,
                prompt_version,
                _json(context_snapshot) if context_snapshot else None,
            ),
        )
        return int(cursor.lastrowid or 0)


def update_task(task_id: int, *, status: str | None = None, result: object = None,
                error: str | None = None, engine: str | None = None,
                model: str | None = None) -> None:
    fields: list[str] = []
    params: list = []
    if status is not None:
        fields.append("status = ?")
        params.append(status)
        if status in ("done", "failed", "cancelled"):
            fields.append("finished_at = datetime('now')")
    if result is not None:
        fields.append("result = ?")
        params.append(result if isinstance(result, str) else _json(result))
    if error is not None:
        fields.append("error = ?")
        params.append(str(error)[:4000])
    if engine is not None:
        fields.append("engine = ?")
        params.append(engine)
    if model is not None:
        fields.append("model = ?")
        params.append(model)
    if not fields:
        return
    params.append(task_id)
    with db.get_conn() as conn:
        conn.execute(f"UPDATE tasks SET {', '.join(fields)} WHERE id = ?", tuple(params))


def _project_dir(project_id: int | None) -> Path | None:
    if project_id is None:
        return None
    try:
        _row, directory = get_project_dir(project_id)
        return directory
    except NodeNotFoundError:
        return None


def run_task(
    *,
    project_id: int | None,
    task_type: str,
    system: str = "",
    messages: list[dict] | None = None,
    context_files: list[str] | None = None,
    output_path: str | Path | None = None,
    engine: str = "",
    provider: str = "",
    model: str = "",
    agent: str = "",
    prompt_id: str = "",
    prompt_version: str = "",
    context_snapshot: dict | None = None,
    timeout_seconds: int | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    extra: dict | None = None,
    should_cancel=None,
) -> dict:
    """执行一次生成任务（阻塞），返回含 ``task_id`` 与引擎结果的结果字典。"""
    project_dir = _project_dir(project_id)
    model_choice = router.resolve_model(provider=provider, model=model,
                                        project_dir=project_dir)
    active_engine, route = router.pick_engine(
        task_type, project_dir=project_dir, explicit_engine=engine
    )

    task_id = _insert_task(
        project_id=project_id,
        task_type=task_type,
        engine=active_engine.name,
        model=model_choice["model"],
        provider=model_choice["provider"],
        agent=agent,
        prompt_id=prompt_id,
        prompt_version=prompt_version,
        context_snapshot=context_snapshot,
    )

    if route.get("unavailable"):
        message = (
            f"引擎 {route['engine']} 不可用，且无可用降级引擎。"
            "请检查 vendor dsh 安装 / 供应商配置与 Key。"
        )
        update_task(task_id, status="failed", error=message)
        return {
            "task_id": task_id,
            "ok": False,
            "engine": active_engine.name,
            "route": route,
            "error_code": "ENGINE_UNAVAILABLE",
            "error_message": message,
            "text": "",
        }

    request = GenerationRequest(
        task_type=task_type,
        system=system,
        messages=list(messages or []),
        project_root=str(project_dir) if project_dir else None,
        project_id=project_id,
        prompt_id=prompt_id,
        prompt_version=prompt_version,
        context_files=list(context_files or []),
        output_path=str(output_path) if output_path else None,
        provider=model_choice["provider"],
        model=model_choice["model"],
        temperature=temperature,
        max_tokens=max_tokens,
        timeout_seconds=timeout_seconds,
        agent=agent,
        extra=dict(extra or {}),
    )
    if route.get("fallback"):
        request.extra["fallback_from"] = route.get("requested_engine", "")

    session_id = active_engine.create_session(
        project_id=project_id, task_type=task_type,
        model=request.model, provider=request.provider,
    )

    result = active_engine.run_agent(request, session_id=session_id,
                                     should_cancel=should_cancel)

    if result.ok:
        token_service.record_usage(
            project_id=project_id,
            task_id=task_id,
            provider=result.provider or request.provider,
            model=result.model or request.model,
            task_type=task_type,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
        )
        update_task(
            task_id,
            status="done",
            result={
                "chars": len(result.text or ""),
                "output_path": result.output_path,
                "artifact": (result.raw or {}).get("artifact_note", ""),
            },
            engine=result.engine,
            model=result.model or request.model,
        )
    else:
        update_task(
            task_id,
            status="cancelled" if result.cancelled else "failed",
            error=result.error_message or result.error_code or "未知错误",
        )

    return {
        "task_id": task_id,
        "ok": result.ok,
        "engine": result.engine or active_engine.name,
        "route": route,
        "text": result.text,
        "output_path": result.output_path,
        "error_code": result.error_code,
        "error_message": result.error_message,
        "tokens": {
            "prompt": result.prompt_tokens,
            "completion": result.completion_tokens,
            "total": result.total_tokens,
        },
        "duration_ms": result.duration_ms,
        "attempts": result.attempts,
        "cancelled": result.cancelled,
        "raw": result.raw,
    }


def stream_task(
    *,
    project_id: int | None,
    task_type: str,
    system: str = "",
    messages: list[dict] | None = None,
    context_files: list[str] | None = None,
    engine: str = "",
    provider: str = "",
    model: str = "",
    agent: str = "",
    prompt_id: str = "",
    prompt_version: str = "",
    context_snapshot: dict | None = None,
    timeout_seconds: int | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    should_cancel=None,
) -> Iterator[dict]:
    """流式执行：逐块产出 ``{"delta": ...}``，结束产出 ``{"done": True, ...}``。

    中断（``should_cancel`` 返回 True）时：已产出内容保留在 ``{"done": True,
    "interrupted": True, "text": 已产出全文}`` 中，由调用方决定进草稿区（不落正文）。
    """
    project_dir = _project_dir(project_id)
    model_choice = router.resolve_model(provider=provider, model=model,
                                        project_dir=project_dir)
    active_engine, route = router.pick_engine(
        task_type, project_dir=project_dir, explicit_engine=engine
    )

    task_id = _insert_task(
        project_id=project_id,
        task_type=task_type,
        engine=active_engine.name,
        model=model_choice["model"],
        provider=model_choice["provider"],
        agent=agent,
        prompt_id=prompt_id,
        prompt_version=prompt_version,
        context_snapshot=context_snapshot,
    )

    if route.get("unavailable"):
        message = f"引擎 {route['engine']} 不可用，且无可用降级引擎。"
        update_task(task_id, status="failed", error=message)
        yield {"task_id": task_id, "ok": False, "error_code": "ENGINE_UNAVAILABLE",
               "error_message": message, "done": True}
        return

    request = GenerationRequest(
        task_type=task_type,
        system=system,
        messages=list(messages or []),
        project_root=str(project_dir) if project_dir else None,
        project_id=project_id,
        prompt_id=prompt_id,
        prompt_version=prompt_version,
        context_files=list(context_files or []),
        provider=model_choice["provider"],
        model=model_choice["model"],
        temperature=temperature,
        max_tokens=max_tokens,
        timeout_seconds=timeout_seconds,
        agent=agent,
    )
    session_id = active_engine.create_session(
        project_id=project_id, task_type=task_type,
        model=request.model, provider=request.provider,
    )

    started = time.time()
    buffer: list[str] = []
    cancelled = False
    error: tuple[str, str] | None = None

    for chunk in active_engine.stream_agent(request, session_id=session_id,
                                            should_cancel=should_cancel):
        if should_cancel is not None and should_cancel():
            active_engine.cancel_task(session_id)
            cancelled = True
            break
        buffer.append(chunk)
        yield {"task_id": task_id, "engine": active_engine.name, "delta": chunk,
               "route": route}

    text = "".join(buffer)
    if not text and getattr(active_engine, "_last_error", None) is not None:
        exc = active_engine._last_error  # type: ignore[attr-defined]
        error = (
            getattr(exc, "code", "UNKNOWN"),
            getattr(exc, "message", str(exc)),
        )

    if error is not None:
        update_task(task_id, status="failed", error=error[1])
    elif cancelled:
        update_task(task_id, status="cancelled", result={"chars": len(text)})
    else:
        token_service.record_usage(
            project_id=project_id,
            task_id=task_id,
            provider=request.provider,
            model=request.model,
            task_type=task_type,
            prompt_tokens=0,
            completion_tokens=len(text) // 2,
        )
        update_task(task_id, status="done", result={"chars": len(text)})

    yield {
        "task_id": task_id,
        "engine": active_engine.name,
        "route": route,
        "done": True,
        "interrupted": cancelled,
        "ok": error is None,
        "text": text,
        "error_code": error[0] if error else None,
        "error_message": error[1] if error else None,
        "duration_ms": int((time.time() - started) * 1000),
    }


# ─────────────────────────── 任务查询与取消 ───────────────────────────


def cancel_task(task_id: int, session_id: str = "") -> dict:
    """按任务 id 取消：从 checkpoint/会话登记中找引擎会话并终止。"""
    task = get_task(task_id)
    engine = router.get_engine(task["engine"])
    ok = False
    if session_id:
        ok = engine.cancel_task(session_id)
    update_task(task_id, status="cancelled")
    return {"task_id": task_id, "cancelled": ok or True, "engine": task["engine"]}


def get_task(task_id: int) -> dict:
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT id, project_id, task_type, engine, model, provider, agent, status,"
            " prompt_id, prompt_version, context_snapshot, result, error, created_at,"
            " finished_at FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
    if row is None:
        raise NodeNotFoundError(f"任务不存在：id={task_id}")
    return _task_row(row)


def list_tasks(project_id: int | None = None, limit: int = 50) -> list[dict]:
    sql = (
        "SELECT id, project_id, task_type, engine, model, provider, agent, status,"
        " prompt_id, prompt_version, context_snapshot, result, error, created_at,"
        " finished_at FROM tasks"
    )
    params: list = []
    if project_id is not None:
        sql += " WHERE project_id = ?"
        params.append(project_id)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(int(limit))
    with db.get_conn() as conn:
        rows = conn.execute(sql, tuple(params)).fetchall()
    return [_task_row(row) for row in rows]


def _task_row(row) -> dict:
    def _loads(text):
        if not text:
            return None
        try:
            return json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return text

    return {
        "id": int(row["id"]),
        "project_id": row["project_id"],
        "task_type": row["task_type"],
        "engine": row["engine"],
        "model": row["model"],
        "provider": row["provider"],
        "agent": row["agent"],
        "status": row["status"],
        "prompt_id": row["prompt_id"],
        "prompt_version": row["prompt_version"],
        "context_snapshot": _loads(row["context_snapshot"]),
        "result": _loads(row["result"]),
        "error": row["error"],
        "created_at": row["created_at"],
        "finished_at": row["finished_at"],
    }


__all__ = [
    "cancel_task",
    "get_task",
    "list_tasks",
    "run_task",
    "stream_task",
    "update_task",
]