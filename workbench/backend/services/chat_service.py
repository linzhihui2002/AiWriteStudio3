"""工作台会话与消息管理；执行由 dsh 原生会话和 chat_run_service 负责。"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Iterator

from .. import config, db
from .fs_utils import atomic_write_text
from . import (
    agent_service,
    generation_service,
    operation_log,
    routing_service,
    rule_service,
    skill_service,
)
from .errors import InvalidOperationError, NodeNotFoundError
from .project_service import get_project_dir

MAX_HISTORY_MESSAGES = 12
MAX_HISTORY_CHARS = 12000

DEFAULT_TITLE = "新对话"
TITLE_TASK_TYPE = "会话标题"
TITLE_MAX_CHARS = 24
TITLE_SOURCE_CHARS = 600
_session_lock = threading.RLock()
_title_workers: dict[tuple[str, int], threading.Thread] = {}


def _permissions(permission_mode=None, discussion_only=None, *, mode=None, auto_apply=None,
                 current: dict | None = None) -> tuple[str, bool]:
    from .settings_service import read_settings
    defaults = current or read_settings()["chat"]
    permission = permission_mode if permission_mode is not None else defaults.get("permission_mode", "auto")
    discussion = discussion_only if discussion_only is not None else defaults.get("discussion_only", False)
    if mode is not None:
        if mode not in {"read", "write"}:
            raise InvalidOperationError("对话模式必须为 write 或 read")
        if discussion_only is None:
            discussion = mode == "read"
        if permission_mode is None:
            permission = "ask" if mode == "read" or auto_apply is None else permission
    if auto_apply is not None and (not isinstance(auto_apply, (bool, int)) or auto_apply not in (0, 1)):
        raise InvalidOperationError("旧版直写设置必须为布尔值")
    if auto_apply is not None and permission_mode is None:
        permission = "auto" if auto_apply and mode != "read" else "ask"
    if not isinstance(permission, str) or permission not in {"ask", "auto", "full"}:
        raise InvalidOperationError("权限必须为请求批准、帮我批准或完全访问")
    if not isinstance(discussion, (bool, int)) or discussion not in (True, False, 0, 1):
        raise InvalidOperationError("只讨论设置必须为布尔值")
    return str(permission), bool(discussion)


def persist_session(session_id: int) -> None:
    """Snapshot under one lock; the database remains a rebuildable index."""
    with _session_lock:
        session = get_session(session_id)
        with db.get_conn() as conn:
            messages = [dict(row) for row in conn.execute("SELECT * FROM chat_messages WHERE session_id=? ORDER BY id", (session_id,))]
            project = conn.execute("SELECT path FROM projects WHERE id=?", (session["project_id"],)).fetchone()
        payload = {"version": 1, "session": session, "messages": messages,
                   "project_path": project["path"] if project else None}
        atomic_write_text(config.runtime_dir() / "chat" / "sessions" / f"{int(session_id)}.json",
                          json.dumps(payload, ensure_ascii=False))


def recover_sessions() -> None:
    """Restore absent session rows, never overwrite a live index or resurrect deletions."""
    directory = config.runtime_dir() / "chat" / "sessions"
    if not directory.is_dir():
        return
    with _session_lock:
        for path in directory.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                session = data["session"]
                messages = data.get("messages", [])
                if (data.get("version") != 1 or not isinstance(session, dict)
                        or session.get("permission_mode") not in {"ask", "auto", "full"}
                        or not isinstance(messages, list)
                        or any(not isinstance(msg, dict) or not isinstance(msg.get("id"), int)
                               or not isinstance(msg.get("role"), str) or not msg["role"]
                               or not isinstance(msg.get("content"), str) for msg in messages)):
                    continue
                if str(session["id"]) != path.stem:
                    continue
                with db.get_conn() as conn:
                    if conn.execute("SELECT 1 FROM chat_sessions WHERE id=?", (session["id"],)).fetchone():
                        continue
                    if data.get("project_path"):
                        project = conn.execute("SELECT id FROM projects WHERE path=?", (data["project_path"],)).fetchone()
                        if not project:
                            continue
                        session["project_id"] = project["id"]
                    columns = {row["name"] for row in conn.execute("PRAGMA table_info(chat_sessions)")}
                    values = {k: v for k, v in session.items() if k in columns}
                    names = list(values)
                    conn.execute(f"INSERT INTO chat_sessions ({','.join(names)}) VALUES ({','.join('?' for _ in names)})", list(values.values()))
                    for msg in messages:
                        conn.execute("INSERT OR IGNORE INTO chat_messages(id,session_id,role,content,meta,created_at,dsh_delivered)"
                                     " VALUES (?,?,?,?,?,?,?)", (msg["id"], session["id"], msg["role"], msg["content"],
                                      msg.get("meta", "{}"), msg.get("created_at"), msg.get("dsh_delivered", 0)))
            except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
                continue  # Keep the damaged archive for inspection rather than replacing it.

# 快捷卡（陪练面板）：一键触发的常用请求
QUICK_CARDS: tuple[dict, ...] = (
    {
        "key": "next-direction",
        "label": "下段方向",
        "prompt": "给我 3 个不同的下段方向，每个写：走向、需要的前置铺垫、代价、章末钩子。",
        "task_type": "灵感追问",
    },
    {
        "key": "block-guidance",
        "label": "分块写作指引",
        "prompt": "把这一章拆成 3-4 个写作块，每块写：发生什么、要带的信息、结尾落在哪里。",
        "task_type": "灵感追问",
    },
    {
        "key": "review",
        "label": "快速审稿",
        "prompt": "按合同逐项验收本章，给出完成 / 未完成 / 待核实清单与证据。",
        "task_type": "审稿",
    },
    {
        "key": "stuck",
        "label": "卡文追问",
        "prompt": "我卡住了。先问我 3 个必须先回答的问题，再给方向候选。",
        "task_type": "灵感追问",
    },
)


# ─────────────────────────── 会话 ───────────────────────────


def create_session(
    *,
    project_id: int | None = None,
    title: str = "",
    agent: str = agent_service.DEFAULT_AGENT,
    provider_id: str = "",
    model_id: str = "",
    auto_apply: bool | None = None,
    mode: str | None = None,
    permission_mode: str | None = None,
    discussion_only: bool | None = None,
) -> dict:
    defaults = None
    if project_id is not None:
        from .chat_preference_service import get_chat_defaults
        defaults = get_chat_defaults(project_id)
    permission, discussion = _permissions(permission_mode, discussion_only, mode=mode,
                                          auto_apply=auto_apply, current=defaults)
    with db.get_conn() as conn:
        cursor = conn.execute(
            "INSERT INTO chat_sessions (project_id, title, agent, provider_id, model_id,"
            " auto_apply, mode, permission_mode, discussion_only,title_source, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))",
            (project_id, title or DEFAULT_TITLE, agent, provider_id, model_id,
             int(permission != "ask"), "read" if discussion else "write", permission, int(discussion),
             "user" if title and title != DEFAULT_TITLE else "default"),
        )
        session_id = int(cursor.lastrowid or 0)
    persist_session(session_id)
    return get_session(session_id)


def get_session(session_id: int) -> dict:
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM chat_sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
    if row is None:
        raise NodeNotFoundError(f"会话不存在：id={session_id}")
    return {**dict(row), "discussion_only": bool(row["discussion_only"])}


def get_session_permission(session_id: int) -> dict:
    """实时读取会话权限，供任务执行期在每次工具调用前刷新。

    作者改权限后，当前任务的下一个工具调用即按新值判定，不需要新开对话。
    会话已被删除或读取失败时返回安全值（逐次批准 + 只讨论），绝不放宽写权限。
    """
    try:
        session = get_session(session_id)
    except (NodeNotFoundError, ValueError, TypeError):
        return {"permission_mode": "ask", "discussion_only": True}
    mode = session.get("permission_mode")
    if mode not in {"ask", "auto", "full"}:
        mode = "auto" if session.get("auto_apply") else "ask"
    return {"permission_mode": mode, "discussion_only": bool(session.get("discussion_only"))}


def list_sessions(project_id: int | None = None, limit: int = 50) -> list[dict]:
    sql = "SELECT * FROM chat_sessions"
    params: list = []
    if project_id is not None:
        sql += " WHERE project_id = ?"
        params.append(project_id)
    sql += " ORDER BY updated_at DESC, id DESC LIMIT ?"
    params.append(int(limit))
    with db.get_conn() as conn:
        rows = conn.execute(sql, tuple(params)).fetchall()
    return [{**dict(row), "discussion_only": bool(row["discussion_only"])} for row in rows]


def update_session(session_id: int, patch: dict) -> dict:
    """更新会话（标题 / Agent / 模型 / 直写开关 / Agent 钉住；下一轮生效）。"""
    current = get_session(session_id)
    patch = dict(patch)
    allowed = {"title", "agent", "provider_id", "model_id", "auto_apply", "agent_pinned", "mode",
               "permission_mode", "discussion_only", "title_source", "title_status", "title_job_id"}
    # Title provenance is server-owned, never accepted from a general patch.
    for key in ("title_source", "title_status", "title_job_id"):
        patch.pop(key, None)
    if {"permission_mode", "discussion_only", "mode", "auto_apply"} & patch.keys():
        permission, discussion = _permissions(patch.get("permission_mode"), patch.get("discussion_only"),
            mode=patch.get("mode"), auto_apply=patch.get("auto_apply"), current=current)
        patch.update(permission_mode=permission, discussion_only=int(discussion),
                     auto_apply=int(permission != "ask"), mode="read" if discussion else "write")
    if "title" in patch:
        title = str(patch["title"] or "").strip()
        if not title or len(title) > 120:
            raise InvalidOperationError("标题须为1至120个字符")
        patch.update(title=title, title_source="user", title_status="complete", title_job_id=None)
    fields: list[str] = []
    params: list = []
    for key, value in (patch or {}).items():
        if key in allowed:
            fields.append(f"{key} = ?")
            params.append(value)
    if not fields:
        return get_session(session_id)
    fields.append("updated_at = datetime('now')")
    params.append(session_id)
    with db.get_conn() as conn:
        conn.execute(f"UPDATE chat_sessions SET {', '.join(fields)} WHERE id = ?",
                     tuple(params))
    persist_session(session_id)
    return get_session(session_id)


def delete_session(session_id: int) -> dict:
    from . import chat_run_service
    # Run creation/finish also take these locks in this order.
    with chat_run_service._lock, _session_lock:
        get_session(session_id)
        if chat_run_service.active_run(session_id):
            raise InvalidOperationError("请先停止正在执行的任务，再删除对话")
        chat_run_service.close_session(session_id)
        with db.get_conn() as conn:
            conn.execute("DELETE FROM chat_messages WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM chat_sessions WHERE id = ?", (session_id,))
        (config.runtime_dir() / "chat" / "sessions" / f"{int(session_id)}.json").unlink(missing_ok=True)
    return {"id": session_id, "deleted": True}


def list_messages(session_id: int, limit: int = 200) -> list[dict]:
    get_session(session_id)
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT id, role, content, meta, created_at FROM chat_messages"
            " WHERE session_id = ? ORDER BY id DESC LIMIT ?",
            (session_id, int(limit)),
        ).fetchall()
    items: list[dict] = []
    for row in reversed(rows):
        try:
            meta = json.loads(row["meta"]) if row["meta"] else {}
        except (json.JSONDecodeError, TypeError):
            meta = {}
        items.append({"id": int(row["id"]), "role": row["role"], "content": row["content"],
                      "meta": meta, "created_at": row["created_at"]})
    return items


def _append_message(session_id: int, role: str, content: str, meta: dict | None = None) -> int:
    with db.get_conn() as conn:
        cursor = conn.execute(
            "INSERT INTO chat_messages (session_id, role, content, meta)"
            " VALUES (?, ?, ?, ?)",
            (session_id, role, content, json.dumps(meta or {}, ensure_ascii=False)),
        )
        conn.execute("UPDATE chat_sessions SET updated_at = datetime('now') WHERE id = ?",
                     (session_id,))
        message_id = int(cursor.lastrowid or 0)
    persist_session(session_id)
    return message_id


def _round_skills(agent: dict | None, routing: dict) -> list[str]:
    """本轮要注入的技能：Agent 绑定技能在前，路由命中技能在后，顺序去重。"""
    names: list[str] = []
    candidates = [*((agent or {}).get("skills") or []), str(routing.get("skill") or "")]
    for raw in candidates:
        key = str(raw or "").strip()
        if key and key not in names:
            names.append(key)
    return names


def _pick_agent(session: dict, agent_name: str, routing: dict) -> tuple[str, dict]:
    """选本轮 Agent：显式入参 > 作者钉住（agent_pinned=1） > 按意图路由 > 默认执行者。"""
    if str(agent_name or "").strip():
        key = str(agent_name).strip()
    elif int(session.get("agent_pinned") or 0):
        key = str(session.get("agent") or agent_service.DEFAULT_AGENT)
    else:
        key = str(routing.get("agent") or session.get("agent")
                  or agent_service.DEFAULT_AGENT)
    try:
        return key, agent_service.get_agent(key)
    except NodeNotFoundError:
        agent_service.ensure_builtin_agents()
        key = agent_service.DEFAULT_AGENT
        return key, agent_service.get_agent(key)


def material_dirs(write_targets) -> list[str]:
    """把写域条目归一到顶层材料目录（章节/设定/大纲/状态/备忘录），保持顺序去重。"""
    dirs: list[str] = []
    for item in write_targets or []:
        top = str(item or "").replace("\\", "/").strip("/").split("/", 1)[0]
        if top and top not in dirs:
            dirs.append(top)
    return dirs


def round_scope(project_id: int, write_targets, *, target_chapter: str | None = None):
    """本轮写域（旧同步路径与原生编排共用）：只允许写入声明范围内的材料。

    写域守卫模块不可用时返回 ``None``，由工具层按既有权限语义处理，不阻断对话。
    """
    try:
        from . import scope_guard
    except ImportError:
        return None
    kwargs: dict = {"project_id": project_id, "write_targets": list(write_targets or [])}
    if target_chapter:
        kwargs["target_chapter"] = target_chapter
    return scope_guard.build_scope(**kwargs)


def scope_label(scope) -> str:
    """写域的中文描述（如「设定/」）；守卫不可用或范围为空时返回空串。"""
    if not scope:
        return ""
    try:
        from . import scope_guard
        return str(scope_guard.describe(scope) or "")
    except Exception:  # noqa: BLE001 - 描述失败只影响展示
        return str(scope.get("label") or "")


TITLE_PROMPT = (
    "你是会话标题生成器。为下面这段小说创作对话起一个标题：\n"
    "不超过 14 个汉字，概括这次要办的事；只输出标题本身，"
    "不要引号、书名号、标点、编号或解释。"
)


def _clean_title(raw: str) -> str:
    first = next((line.strip() for line in str(raw or "").splitlines() if line.strip()), "")
    first = re.sub(r"^[#\-*\d.、）)\s]+", "", first)
    first = first.strip("「」『』《》<>\"'“”‘’ 　")
    return first[:TITLE_MAX_CHARS]


def _fallback_title(user_text: str) -> str:
    first = next((line.strip() for line in str(user_text or "").splitlines() if line.strip()), "")
    return first[:20] or DEFAULT_TITLE


def _needs_title(session: dict) -> bool:
    """仅当标题仍是默认值（或为空）时才自动生成 —— 作者改过名就不再覆盖。"""
    title = str(session.get("title") or "").strip()
    return not title or title == DEFAULT_TITLE


def generate_title(
    session_id: int,
    *,
    user_text: str = "",
    reply: str = "",
    provider: str = "",
    model: str = "",
) -> str:
    """Compatibility helper; normal chat schedules the same worker asynchronously."""
    session = get_session(session_id)
    if not _needs_title(session):
        return str(session.get("title") or "")
    schedule_title(session_id, user_text, provider=provider, model=model)
    worker = _title_workers.get((str(config.DB_PATH), session_id))
    if worker:
        worker.join(timeout=35)
    return str(get_session(session_id)["title"])


def schedule_title(session_id: int, user_text: str = "", *, provider: str = "", model: str = "",
                   force: bool = False) -> dict:
    """An independent bounded job; late replies cannot replace manual renames."""
    db_path = Path(config.DB_PATH)
    key = (str(db_path), session_id)
    with _session_lock:
        session = get_session(session_id)
        if not force and (session["title_source"] != "default" or session["title_status"] != "idle"):
            return session
        existing_worker = _title_workers.get(key)
        if session["title_status"] in {"pending", "running"} and existing_worker and existing_worker.is_alive():
            return session
        with db.get_conn() as conn:
            first = conn.execute("SELECT content FROM chat_messages WHERE session_id=? AND role='user' ORDER BY id LIMIT 1",
                                 (session_id,)).fetchone()
        source = str(first["content"] if first else user_text).strip()
        if not source:
            raise InvalidOperationError("先发送一条消息，再生成对话标题")
        job_id = str(uuid.uuid4())
        with db.get_conn() as conn:
            conn.execute("UPDATE chat_sessions SET title=?,title_source='fallback',title_status='pending',title_job_id=? WHERE id=?",
                         (_fallback_title(source), job_id, session_id))
        persist_session(session_id)
        worker = threading.Thread(target=_title_job, args=(db_path, session_id, job_id, source, provider, model),
                                  name=f"chat-title-{session_id}", daemon=True)
        _title_workers[key] = worker
        worker.start()
    return get_session(session_id)


def _title_job(db_path: Path, session_id: int, job_id: str, source: str, provider: str, model: str) -> None:
    title = ""
    try:
        if db_path != Path(config.DB_PATH):
            return
        session = get_session(session_id)
        if session.get("title_job_id") != job_id:
            return
        with db.get_conn(db_path) as conn:
            conn.execute("UPDATE chat_sessions SET title_status='running' WHERE id=? AND title_job_id=?", (session_id, job_id))
        persist_session(session_id)
        from ..engine import router
        _agent_name, agent = _pick_agent(session, "", routing_service.route(session["project_id"], source, record=False))
        project_dir = get_project_dir(session["project_id"])[1] if session["project_id"] is not None else None
        choice = router.resolve_model(provider=provider or session.get("provider_id") or agent.get("provider_id") or "",
                                      model=model or session.get("model_id") or agent.get("model_id") or "", project_dir=project_dir)
        result = generation_service.run_task(
            project_id=session["project_id"],
            task_type=TITLE_TASK_TYPE, engine="direct-api",
            system=TITLE_PROMPT,
            messages=[{"role": "user", "content": source[:TITLE_SOURCE_CHARS]}],
            provider=choice["provider"], model=choice["model"], temperature=0.3,
            max_tokens=256, timeout_seconds=30, extra={"max_retries": 0},
        )
        if result["ok"]:
            title = _clean_title(result.get("text") or "")
    except Exception:  # The chat task continues even if title generation fails.
        pass
    finally:
        with _session_lock:
            # The ID is invalidated by manual rename/deletion or another generation.
            with db.get_conn(db_path) as conn:
                conn.execute("UPDATE chat_sessions SET title=CASE WHEN ?='' THEN title ELSE ? END,"
                             " title_source=?,title_status=? WHERE id=? AND title_job_id=? AND title_source!='user'",
                             (title, title, "ai" if title else "fallback", "complete" if title else "failed", session_id, job_id))
            if db_path == Path(config.DB_PATH):
                try:
                    persist_session(session_id)
                except NodeNotFoundError:
                    pass
            key = (str(db_path), session_id)
            if _title_workers.get(key) is threading.current_thread():
                _title_workers.pop(key, None)


def recover_title_jobs() -> None:
    with _session_lock:
        with db.get_conn() as conn:
            pending = conn.execute("SELECT id FROM chat_sessions WHERE title_status IN ('pending','running')").fetchall()
        for item in pending:
            key = (str(config.DB_PATH), item["id"])
            if key in _title_workers and _title_workers[key].is_alive():
                continue
            with db.get_conn() as conn:
                conn.execute("UPDATE chat_sessions SET title_status='failed',title_job_id=NULL WHERE id=?", (item["id"],))
            persist_session(item["id"])


def quick_cards() -> list[dict]:
    return [dict(card) for card in QUICK_CARDS]


def send_message(session_id: int, text: str, *, chapter_rel: str | None = None,
                 agent_name: str = "", provider: str = "", model: str = "",
                 use_ai: bool = True) -> dict:
    """兼容旧同步接口；实际执行统一交给持久化运行服务。"""
    from . import chat_run_service
    run = chat_run_service.create_run(session_id, text=text,
        context={"active_file": chapter_rel}, agent=agent_name, provider=provider, model=model)
    for _event in chat_run_service.iter_events(run["id"]):
        pass
    result = chat_run_service.get_run(run["id"])
    return {**result, "run_id": result["id"], "reply": result["text"],
            "ok": result["status"] == "completed"}


def stream_message(session_id: int, text: str, *, chapter_rel: str | None = None,
                   agent_name: str = "", provider: str = "", model: str = "") -> Iterator[dict]:
    """兼容旧流接口；断开订阅不取消后台运行。"""
    from . import chat_run_service
    run = chat_run_service.create_run(session_id, text=text,
        context={"active_file": chapter_rel}, agent=agent_name, provider=provider, model=model)
    yield from chat_run_service.iter_events(run["id"])
