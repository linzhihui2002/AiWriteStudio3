"""Durable human decisions for a running chat tool; JSON files own the facts."""
from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
import uuid

from .. import config, db
from .errors import InvalidOperationError, NodeNotFoundError
from .fs_utils import atomic_write_text

_locks: dict[tuple[str, str], threading.RLock] = {}
_locks_guard = threading.Lock()
_ACTIVE = {"queued", "starting", "running", "waiting_input"}
_TERMINAL = {"answered", "cancelled", "interrupted", "expired"}


class InteractionConflictError(InvalidOperationError):
    status_code = 409


class InteractionEndedError(InteractionConflictError):
    def __init__(self, status: str) -> None:
        super().__init__({"cancelled": "本轮已停止，未执行操作", "interrupted": "等待已中断，请重新发起请求",
                          "expired": "批准的文件版本已变化，请重新读取并请求批准"}.get(status, "交互已结束"))
        self.details = {"code": "interaction_" + status}


def ensure_schema() -> None:
    with db.get_conn() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS chat_interactions (
            id TEXT PRIMARY KEY, run_id TEXT NOT NULL, tool_call_id TEXT NOT NULL,
            kind TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL,
            response TEXT, record TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE(run_id,tool_call_id,kind)
        );
        CREATE INDEX IF NOT EXISTS idx_chat_interactions_run ON chat_interactions(run_id);
        """)


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _run(run_id: str) -> dict:
    try:
        run_id = str(uuid.UUID(str(run_id)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise InvalidOperationError("无效的对话运行编号") from exc
    with db.get_conn() as conn:
        row = conn.execute("SELECT id,session_id,project_id,status FROM chat_runs WHERE id=?", (run_id,)).fetchone()
    if row is None:
        raise NodeNotFoundError("对话任务不存在")
    return dict(row)


def _directory(run_id: str) -> Path:
    return config.runtime_dir() / "chat" / "runs" / str(uuid.UUID(run_id)) / "interactions"


def _lock(run_id: str) -> threading.RLock:
    key = (str(Path(config.DB_PATH).resolve()), run_id)
    with _locks_guard:
        return _locks.setdefault(key, threading.RLock())


def _cache(item: dict) -> None:
    raw = _json(item)
    with db.get_conn() as conn:
        old = conn.execute("SELECT record FROM chat_interactions WHERE id=?", (item["id"],)).fetchone()
        if old:
            previous = json.loads(old["record"])
            if old["record"] == raw or int(previous.get("revision", 0)) > int(item.get("revision", 0)):
                return
        conn.execute("INSERT INTO chat_interactions(id,run_id,tool_call_id,kind,payload,status,response,record,created_at,updated_at)"
                     " VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,"
                     "status=excluded.status,response=excluded.response,record=excluded.record,updated_at=excluded.updated_at"
                     " WHERE COALESCE(json_extract(excluded.record,'$.revision'),0) >= COALESCE(json_extract(chat_interactions.record,'$.revision'),0)",
                     (item["id"], item["run_id"], item["tool_call_id"], item["kind"], _json(item["payload"]),
                      item["status"], _json(item["response"]) if item.get("response") is not None else None,
                      raw, item["created_at"], item["updated_at"]))


def _save(item: dict) -> None:
    item["updated_at"] = datetime.now(timezone.utc).isoformat()
    item["revision"] = int(item.get("revision", 0)) + 1
    atomic_write_text(_directory(item["run_id"]) / (item["id"] + ".json"), _json(item))
    _cache(item)


def _load(run_id: str, interaction_id: str) -> dict:
    try:
        identity = str(uuid.UUID(str(interaction_id)))
        path = _directory(run_id) / (identity + ".json")
        item = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, TypeError, AttributeError, FileNotFoundError) as exc:
        raise NodeNotFoundError("问题或批准请求不存在") from exc
    if item.get("run_id") != run_id or item.get("id") != identity:
        raise InteractionConflictError("交互不属于本轮运行")
    return item


def _disk_items(run_id: str) -> list[dict]:
    result = []
    directory = _directory(run_id)
    if directory.is_dir():
        for path in directory.glob("*.json"):
            result.append(_load(run_id, path.stem))
    return sorted(result, key=lambda item: (item["created_at"], item["id"]))


def list_interactions(run_id: str) -> list[dict]:
    run = _run(run_id)
    ensure_schema()
    # Atomic file replacements let snapshots read without taking the responder's lock.
    items = _disk_items(run["id"])
    for item in items:
        _cache(item)
    return items


def has_task_approval(run_id: str) -> bool:
    """本次运行内作者是否已选择「本任务内不再询问」。

    任务级批准只存在于当前 run 的交互记录里，任务结束即自然失效；
    不写持久表，也不影响其他会话或后续轮次。
    """
    try:
        identity = str(uuid.UUID(str(run_id)))
    except (ValueError, TypeError, AttributeError):
        return False
    ensure_schema()
    return any(item["kind"] == "approval" and item["status"] == "answered"
               and (item.get("response") or {}).get("decision") == "approve"
               and (item.get("response") or {}).get("scope") == "task"
               for item in _disk_items(identity))


def _waiting_state(run_id: str) -> str | None:
    """Only change an active run; a cancellation can never be revived by an answer."""
    pending = any(item["status"] == "pending" for item in _disk_items(run_id))
    wanted = "waiting_input" if pending else "running"
    with db.get_conn() as conn:
        row = conn.execute("SELECT status FROM chat_runs WHERE id=?", (run_id,)).fetchone()
        if not row or row["status"] not in _ACTIVE or row["status"] == wanted:
            return None
        changed = conn.execute("UPDATE chat_runs SET status=? WHERE id=? AND status=?",
                               (wanted, run_id, row["status"])).rowcount
    return wanted if changed else None


def _emit(item: dict, status: str | None = None) -> None:
    from . import chat_run_service
    # Never hold an interaction or project lock while taking the run event lock.
    # A fast answer/finish can overtake a publisher. Re-read under the same lock
    # as run completion so an old "running" event cannot follow terminal done.
    with chat_run_service._lock:
        current = _load(item["run_id"], item["id"])
        chat_run_service.emit(item["run_id"], "interaction", interaction=current)
        if status and _run(item["run_id"])["status"] == status:
            chat_run_service.emit(item["run_id"], "status", status=status)


def _assert_active(run_id: str) -> None:
    state = _run(run_id)["status"]
    if state not in _ACTIVE:
        raise InteractionEndedError("interrupted" if state == "interrupted" else "cancelled")


def validate_questions(payload: dict) -> dict:
    questions = payload.get("questions")
    if not isinstance(questions, list) or not 1 <= len(questions) <= 3:
        raise InvalidOperationError("每次请提供 1 至 3 个问题")
    normalized, ids = [], set()
    for raw in questions:
        if not isinstance(raw, dict):
            raise InvalidOperationError("问题格式不正确")
        identity, question = raw.get("id"), raw.get("question")
        if not isinstance(identity, str) or not identity.strip() or len(identity) > 100 or identity in ids:
            raise InvalidOperationError("问题编号必须非空且不能重复")
        if not isinstance(question, str) or not question.strip() or len(question) > 2000:
            raise InvalidOperationError("问题内容必须为 1 至 2000 个字符")
        ids.add(identity)
        options = raw.get("options", [])
        if not isinstance(options, list) or len(options) > 6:
            raise InvalidOperationError("每个问题最多提供 6 个选项")
        labels, choices = set(), []
        for option in options:
            if not isinstance(option, dict) or not isinstance(option.get("label"), str):
                raise InvalidOperationError("选项须包含文字标签")
            label = option["label"].strip()
            if not label or len(label) > 200 or label in labels or label.casefold() in {"其他", "其它", "other"}:
                raise InvalidOperationError("选项不可重复或命名为其他；界面会自动提供其他输入")
            description = option.get("description", "")
            if not isinstance(description, str) or len(description) > 1000:
                raise InvalidOperationError("选项说明过长或格式不正确")
            labels.add(label)
            choices.append({"label": label, "description": description})
        header, multi = raw.get("header", ""), raw.get("multi_select", False)
        if not isinstance(header, str) or len(header) > 100 or not isinstance(multi, bool):
            raise InvalidOperationError("问题标题或多选设置格式不正确")
        normalized.append({"id": identity, "question": question.strip(), "header": header,
                           "options": choices, "multi_select": multi})
    return {"questions": normalized}


def _validate_response(item: dict, response: dict) -> dict:
    if not isinstance(response, dict):
        raise InvalidOperationError("回答必须为对象")
    if item["kind"] == "approval":
        if response.get("decision") not in {"approve", "reject"}:
            raise InvalidOperationError("请选择批准或拒绝")
        normalized = {"decision": response["decision"]}
        # scope=task 表示作者选择「本任务内不再询问」；scope=material 表示「本任务内允许
        # 改这类材料」（写域越界授权，不纳入免询范围）；scope=once 表示「仅此次批准」。
        # 三者都只在批准时生效。
        if response["decision"] == "approve" and response.get("scope") in {"task", "material", "once"}:
            normalized["scope"] = response["scope"]
        return normalized
    answers = response.get("answers")
    questions = item["payload"]["questions"]
    if not isinstance(answers, list) or len(answers) != len(questions):
        raise InvalidOperationError("请回答所有问题")
    by_id = {}
    for answer in answers:
        if not isinstance(answer, dict) or not isinstance(answer.get("id"), str) or answer["id"] in by_id:
            raise InvalidOperationError("回答编号不正确或重复")
        by_id[answer["id"]] = answer
    if set(by_id) != {q["id"] for q in questions}:
        raise InvalidOperationError("回答与当前问题不匹配")
    normalized = []
    for question in questions:
        answer = by_id[question["id"]]
        selected, custom = answer.get("selected", []), answer.get("custom", "")
        if not isinstance(selected, list) or any(not isinstance(v, str) for v in selected):
            raise InvalidOperationError("已选选项格式不正确")
        if len(set(selected)) != len(selected) or set(selected) - {o["label"] for o in question["options"]}:
            raise InvalidOperationError("回答包含重复或不存在的选项")
        if not isinstance(custom, str) or len(custom) > 12000:
            raise InvalidOperationError("其他回答格式不正确或过长")
        custom = custom.strip()
        if "custom" in answer and not custom:
            raise InvalidOperationError("选择其他时请填写回答")
        if not selected and not custom:
            raise InvalidOperationError("请选择一个选项，或填写其他回答")
        if not question["multi_select"] and len(selected) + bool(custom) > 1:
            raise InvalidOperationError("单选问题只能选择一个选项或填写其他")
        normalized.append({"id": question["id"], "selected": selected, **({"custom": custom} if custom else {})})
    return {"answers": normalized}


def _record_answer(run: dict, item: dict, response: dict) -> tuple[int, str]:
    """One ordinary author message makes a selected/custom answer visible in history."""
    questions = {q["id"]: q["question"] for q in item["payload"]["questions"]}
    lines = ["作者回答："]
    for answer in response["answers"]:
        values = [*answer["selected"], *([answer["custom"]] if answer.get("custom") else [])]
        lines.extend([questions[answer["id"]], "回答：" + "；".join(values)])
    text = "\n".join(lines)
    with db.get_conn() as conn:
        existing = conn.execute("SELECT id,content FROM chat_messages WHERE session_id=? AND role='user'"
                                " AND json_extract(meta,'$.interaction_id')=?", (run["session_id"], item["id"])).fetchone()
        if existing:
            return int(existing["id"]), existing["content"]
        cursor = conn.execute("INSERT INTO chat_messages(session_id,role,content,meta) VALUES (?,'user',?,?)",
                              (run["session_id"], text, _json({"run_id": run["id"], "interaction_id": item["id"],
                               "interaction_kind": "question", "answers": response["answers"]})))
        return int(cursor.lastrowid), text


def _capture_answer(run: dict, item: dict) -> None:
    from . import chat_memory_service, chat_service
    message_id = item.get("user_message_id")
    if message_id is None:
        return
    with db.get_conn() as conn:
        row = conn.execute("SELECT content FROM chat_messages WHERE id=?", (message_id,)).fetchone()
    try:
        if row:
            chat_memory_service.capture_user_message(run["project_id"], run["session_id"], message_id, row["content"])
    finally:
        chat_service.persist_session(run["session_id"])


def ask(run_id: str, tool_call_id: str, kind: str, payload: dict,
        should_cancel: Callable[[], bool] | None = None, wait_control=None) -> dict:
    from . import chat_run_service
    run = _run(run_id)
    run_id = run["id"]
    if kind not in {"approval", "question"} or not tool_call_id or not isinstance(payload, dict):
        raise InvalidOperationError("无效的交互类型、工具编号或请求内容")
    payload = validate_questions(payload) if kind == "question" else json.loads(_json(payload))
    digest = hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()
    identity = str(uuid.uuid5(uuid.UUID(run_id), kind + ":" + tool_call_id))
    ensure_schema()
    created = False
    # Publish the durable card and waiting state as one observable transition.
    # Snapshots/cancel/finish use this same outer lock. Always take it before
    # the per-run interaction lock, and release both before waiting for a human.
    with chat_run_service._lock, _lock(run_id):
        try:
            item = _load(run_id, identity)
        except NodeNotFoundError:
            _assert_active(run_id)
            if should_cancel and should_cancel():
                raise InteractionEndedError("cancelled")
            item = {"id": identity, "run_id": run_id, "tool_call_id": tool_call_id,
                    "kind": kind, "payload": payload, "payload_hash": digest,
                    "status": "pending", "response": None,
                    "created_at": datetime.now(timezone.utc).isoformat()}
            _save(item)
            created = True
        if item["payload_hash"] != digest:
            raise InteractionConflictError("同一工具调用编号不能用于不同的问题或批准内容")
        state = _waiting_state(run_id) if item["status"] == "pending" else None
    if created:
        _emit(item, state)
    if item["status"] == "answered":
        _assert_active(run_id)
        return item["response"]
    if item["status"] in _TERMINAL:
        raise InteractionEndedError(item["status"])
    waiting = False
    try:
        if wait_control is not None:
            wait_control.begin_wait()
            waiting = True
        pulse = threading.Event()
        while True:
            if should_cancel and should_cancel():
                interrupt_run(run_id)
                raise InteractionEndedError("cancelled")
            item = _load(run_id, identity)
            if item["status"] == "answered":
                _assert_active(run_id)
                return item["response"]
            if item["status"] in _TERMINAL:
                raise InteractionEndedError(item["status"])
            _assert_active(run_id)
            pulse.wait(0.2)
    finally:
        if waiting:
            wait_control.end_wait()


def respond(run_id: str, interaction_id: str, response: dict) -> dict:
    from . import chat_run_service
    run = _run(run_id)
    run_id = run["id"]
    ensure_schema()
    # Linearize acceptance against cancel/finish; this is the same lock order
    # used by run completion (run first, interaction second).
    with chat_run_service._lock, _lock(run_id):
        item = _load(run_id, interaction_id)
        normalized = _validate_response(item, response)
        if item["status"] == "answered":
            if item["response"] == normalized:
                return item
            raise InteractionConflictError("此问题已回答，不能提交不同的答案")
        if item["status"] != "pending":
            raise InteractionEndedError(item["status"])
        if _run(run_id)["status"] not in _ACTIVE:
            raise InteractionEndedError("cancelled")
        if item["kind"] == "question":
            item["user_message_id"], _text = _record_answer(run, item, normalized)
        item.update(status="answered", response=normalized)
        _save(item)
        state = _waiting_state(run_id)
    if item["kind"] == "question":
        try:
            _capture_answer(run, item)
        except Exception:
            # The human answer already belongs to both disk and message history.
            # Optional memory projection failures must not undo it or strand the tool.
            with _lock(run_id):
                item = _load(run_id, interaction_id)
                item["warning"] = "回答已保存；本书记忆暂未同步，下次对话仍可读取本次回答。"
                _save(item)
    _emit(item, state)
    return item


def mark_delivered(run_id: str, tool_call_id: str) -> None:
    """Called only once the vendor has durably recorded the question tool result."""
    from . import chat_service
    run = _run(run_id)
    items = [item for item in list_interactions(run["id"])
             if item["tool_call_id"] == tool_call_id and item["kind"] == "question"
             and item["status"] == "answered" and item.get("user_message_id")]
    if not items:
        return
    chat_service.persist_session(run["session_id"])
    with db.get_conn() as conn:
        conn.executemany("UPDATE chat_messages SET dsh_delivered=1 WHERE id=? AND session_id=?",
                         [(item["user_message_id"], run["session_id"]) for item in items])
    chat_service.persist_session(run["session_id"])


def expire(run_id: str, interaction_id: str) -> None:
    from . import chat_run_service
    run = _run(run_id)
    ensure_schema()
    with chat_run_service._lock, _lock(run["id"]):
        item = _load(run["id"], interaction_id)
        if item["status"] not in {"pending", "answered"}:
            return
        item["status"] = "expired"
        _save(item)
        state = _waiting_state(run["id"])
    _emit(item, state)


def interrupt_run(run_id: str, status: str = "cancelled") -> None:
    from . import chat_run_service
    if status not in {"cancelled", "interrupted"}:
        raise InvalidOperationError("无效的中断状态")
    run = _run(run_id)
    ensure_schema()
    changed = []
    with chat_run_service._lock, _lock(run["id"]):
        for item in _disk_items(run["id"]):
            if item["status"] == "pending":
                item["status"] = status
                _save(item)
                changed.append(item)
    for item in changed:
        _emit(item)
