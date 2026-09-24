"""Proposal 收件箱：所有 AI 写入类产出必须先进入这里，人工应用后才落盘。

原则（spec：应用前强制 diff）
- 任何 AI 产物**不得**静默写盘；创建时记录目标路径、基准内容 hash 与差异；
- 应用时：先给现有文件存快照 → 原子写 → 刷新索引 → 记录操作日志；
- 支持「应用 / 编辑后应用 / 丢弃」与批量逐条勾选。
"""

from __future__ import annotations

import json
from pathlib import Path

from .. import db
from . import operation_log
from .conflict_service import line_diff
from .errors import InvalidOperationError, NodeNotFoundError
from .fs_utils import atomic_write_text, read_text, resolve_within
from .project_service import get_project_dir
from .snapshot_service import snapshot_file

PROPOSAL_KINDS = (
    "chapter_draft",   # 章节正文
    "outline",         # 大纲
    "setting",         # 设定条目
    "state",           # 状态/四件套
    "card",            # 设定卡片字段
    "contract",        # 章节合同
    "foreshadow",      # 伏笔
    "deslop",          # 去AI味行级建议（patch 应用）
    "rule",            # 规则
    "skill",           # 技能
    "agent",           # Agent 定义
    "other",
)

STATUS_PENDING = "pending"
STATUS_APPLIED = "applied"
STATUS_DISCARDED = "discarded"


def _loads(text: str | None) -> dict:
    if not text:
        return {}
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def create_proposal(
    *,
    project_id: int | None,
    kind: str,
    title: str,
    target_path: str,
    content: str,
    task_id: int | None = None,
    meta: dict | None = None,
) -> dict:
    """创建一条待处理提案（记录基准内容，供应用前 diff）。"""
    if kind not in PROPOSAL_KINDS:
        kind = "other"

    base = ""
    base_exists = False
    if project_id is not None and target_path:
        try:
            _row, project_dir = get_project_dir(project_id)
            path = resolve_within(Path(project_dir), target_path)
            if path.is_file():
                base = read_text(path)
                base_exists = True
        except (NodeNotFoundError, OSError, UnicodeDecodeError):
            base = ""

    from .file_change_service import content_hash
    payload = json.dumps(
        {"content": content, "base": base, "base_exists": base_exists,
         "base_hash": content_hash(base) if base_exists else None, "meta": meta or {}},
        ensure_ascii=False,
    )
    with db.get_conn() as conn:
        cursor = conn.execute(
            "INSERT INTO proposals (project_id, task_id, kind, title, target_path, status, payload)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (project_id, task_id, kind, title, target_path, STATUS_PENDING, payload),
        )
        proposal_id = int(cursor.lastrowid or 0)
    operation_log.log(project_id, "proposal-create", target_path,
                      {"proposal_id": proposal_id, "kind": kind})
    return get_proposal(proposal_id)


def get_proposal(proposal_id: int) -> dict:
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT id, project_id, task_id, kind, title, target_path, status, payload,"
            " created_at, applied_at FROM proposals WHERE id = ?",
            (proposal_id,),
        ).fetchone()
    if row is None:
        raise NodeNotFoundError(f"提案不存在：id={proposal_id}")
    return _row_to_proposal(row, include_diff=True)


def _row_to_proposal(row, *, include_diff: bool = False) -> dict:
    payload = _loads(row["payload"])
    content = str(payload.get("content") or "")
    base = str(payload.get("base") or "")
    result = {
        "id": int(row["id"]),
        "project_id": row["project_id"],
        "task_id": row["task_id"],
        "kind": row["kind"],
        "title": row["title"],
        "target_path": row["target_path"],
        "status": row["status"],
        "created_at": row["created_at"],
        "applied_at": row["applied_at"],
        "meta": payload.get("meta") or {},
        "content": content,
        "chars": len(content),
        "is_new_file": not payload.get("base_exists", bool(base)),
    }
    if include_diff:
        result["diff"] = line_diff(base, content)
    return result


def list_proposals(
    project_id: int | None = None,
    status: str | None = STATUS_PENDING,
    kind: str | None = None,
    limit: int = 100,
) -> list[dict]:
    sql = (
        "SELECT id, project_id, task_id, kind, title, target_path, status, payload,"
        " created_at, applied_at FROM proposals WHERE 1=1"
    )
    params: list = []
    if project_id is not None:
        sql += " AND project_id = ?"
        params.append(project_id)
    if status:
        sql += " AND status = ?"
        params.append(status)
    if kind:
        sql += " AND kind = ?"
        params.append(kind)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(int(limit))
    with db.get_conn() as conn:
        rows = conn.execute(sql, tuple(params)).fetchall()
    return [_row_to_proposal(row) for row in rows]


def update_proposal_content(proposal_id: int, content: str) -> dict:
    """编辑提案内容（应用前人工精修）。"""
    proposal = get_proposal(proposal_id)
    if proposal["status"] != STATUS_PENDING:
        raise InvalidOperationError("只有待处理提案可以编辑内容")
    payload = _loads(_raw_payload(proposal_id))
    payload["content"] = content
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE proposals SET payload = ? WHERE id = ?",
            (json.dumps(payload, ensure_ascii=False), proposal_id),
        )
    return get_proposal(proposal_id)


def find_tool_proposal(project_id: int, run_id: str | int, tool_call_id: str) -> dict | None:
    """Resolve retries even before a staged proposal has a file-change journal row."""
    with db.get_conn() as conn:
        rows = conn.execute("SELECT id,payload FROM proposals WHERE project_id=? ORDER BY id DESC",
                            (project_id,)).fetchall()
    for row in rows:
        recorded = (_loads(row["payload"]).get("meta") or {}).get("file_change") or {}
        if str(recorded.get("run_id")) == str(run_id) and recorded.get("tool_call_id") == tool_call_id:
            return get_proposal(int(row["id"]))
    return None


def _raw_payload(proposal_id: int) -> str:
    with db.get_conn() as conn:
        row = conn.execute("SELECT payload FROM proposals WHERE id = ?",
                           (proposal_id,)).fetchone()
    return row["payload"] if row is not None else ""


def apply_proposal(proposal_id: int, *, content: str | None = None,
                   status: str | None = None) -> dict:
    """Apply through the same version checks, gates and journal as native tools."""
    from . import file_change_service as changes
    proposal = get_proposal(proposal_id)
    if proposal["status"] != STATUS_PENDING:
        raise InvalidOperationError(f"提案已处理（{proposal['status']}），不能重复应用")
    if proposal["project_id"] is None:
        raise InvalidOperationError("提案缺少项目上下文，无法应用")
    project_id = int(proposal["project_id"])
    payload = _loads(_raw_payload(proposal_id))
    meta = payload.get("meta") or {}
    target_rel = str(proposal["target_path"] or "")
    if not target_rel:
        raise InvalidOperationError("提案缺少目标路径")
    new_content = content if content is not None else str(payload.get("content") or "")
    base = str(payload.get("base") or "")
    # Old proposals still have a base image; only genuinely missing/empty legacy
    # bases require consulting disk existence. Their content is never silently rebased.
    base_exists = payload.get("base_exists")
    if base_exists is None:
        base_exists = bool(base) or changes.file_state(project_id, target_rel)["exists"]
    expected = payload.get("base_hash", changes.content_hash(base) if base_exists else None)
    recorded = meta.get("file_change") or {}
    operation = str(recorded.get("operation") or ("rewrite" if base_exists else "create"))
    arguments = {
        "run_id": recorded.get("run_id") or f"proposal:{proposal_id}",
        "session_id": recorded.get("session_id", meta.get("session_id")),
        "tool_call_id": recorded.get("tool_call_id") or f"proposal:{proposal_id}",
        "operation": operation, "rel_path": target_rel,
        "expected_hash": recorded.get("expected_hash", expected),
        "content": new_content,
        "destination": recorded.get("destination"),
        "old_text": recorded.get("old_text"), "new_text": recorded.get("new_text"),
        "title": recorded.get("title") or meta.get("title"), "status": status,
    }
    if recorded and operation == "replace_exact" and content is not None:
        arguments["new_text"] = content
    elif proposal["kind"] == "deslop" and meta.get("patch"):
        patch = meta["patch"]
        original = str(patch.get("original") or "")
        if not original:
            raise InvalidOperationError("行级建议必须包含精确原文")
        arguments.update(operation="replace_exact", old_text=original,
                         new_text=str(patch.get("replacement") or ""))
    elif proposal["kind"] == "setting" and meta.get("append"):
        joiner = "" if base.endswith("\n") else "\n"
        arguments["content"] = f"{base}{joiner}\n{new_content}\n"
    elif meta.get("frontmatter"):
        from .fs_utils import compose_document
        arguments["content"] = compose_document(meta["frontmatter"], new_content)
    # A rejected candidate can be edited and submitted again. Retry the same
    # revision idempotently, but give a corrected revision its own journal row.
    revision = changes.content_hash(json.dumps(
        {key: value for key, value in arguments.items() if key != "tool_call_id"},
        ensure_ascii=False, sort_keys=True))
    arguments["tool_call_id"] = f"{arguments['tool_call_id']}:apply:{revision}"
    with changes.project_lock(project_id):
        result = changes.apply_change(project_id, **arguments)
        result_meta = {"change_id": result["id"], "run_id": result["run_id"],
                       "before_hash": result["before_hash"], "after_hash": result["after_hash"]}
        if target_rel.startswith("章节/") and operation not in {"move", "delete"}:
            from .chapter_service import read_chapter
            detail = read_chapter(project_id, target_rel)
            result_meta.update(word_count=detail["word_count"], status=detail["status"])
        if proposal["kind"] == "setting" and meta.get("append"):
            result_meta["appended"] = True
        if proposal["kind"] == "deslop":
            result_meta["applied"] = True
        with db.get_conn() as conn:
            conn.execute("UPDATE proposals SET status=?, applied_at=datetime('now') WHERE id=?",
                         (STATUS_APPLIED, proposal_id))
        operation_log.log(project_id, "proposal-apply", target_rel,
                          {"proposal_id": proposal_id, **result_meta})
    return {"proposal": get_proposal(proposal_id), "written": target_rel,
            "meta": result_meta, "change": result}


def discard_proposal(proposal_id: int) -> dict:
    proposal = get_proposal(proposal_id)
    if proposal["status"] != STATUS_PENDING:
        raise InvalidOperationError(f"提案已处理（{proposal['status']}）")
    with db.get_conn() as conn:
        conn.execute("UPDATE proposals SET status = ? WHERE id = ?",
                     (STATUS_DISCARDED, proposal_id))
    operation_log.log(proposal["project_id"], "proposal-discard",
                      proposal["target_path"], {"proposal_id": proposal_id})
    return get_proposal(proposal_id)


def _apply_line_patch(target: Path, patch: dict) -> dict:
    """行级替换补丁：``patch = {"line": 12, "original": "...", "replacement": "..."}``。

    ``line`` 是**正文（不含 frontmatter）**行号——与软审时发给模型的行号口径一致；
    这里自动换算为文件行号，避免误改元信息。
    """
    import re

    line_no = int(patch.get("line") or 0)
    original = str(patch.get("original") or "")
    replacement = str(patch.get("replacement") or "")
    if line_no < 1 or not replacement:
        raise InvalidOperationError("行级建议缺少行号或替换内容")

    text = read_text(target)
    lines = text.splitlines()

    match = re.match(r"^---[ \t]*\r?\n.*?\r?\n---[ \t]*(?:\r?\n|$)", text, re.DOTALL)
    offset = 0
    if match:
        offset = match.group(0).count("\n")

    file_index = offset + line_no - 1
    if file_index >= len(lines):
        raise InvalidOperationError(
            f"行号越界：正文第 {line_no} 行（文件共 {len(lines)} 行）"
        )
    current = lines[file_index]
    needle = original.strip()

    if needle and needle in current:
        lines[file_index] = current.replace(needle, replacement, 1)
    elif needle:
        # 行号漂移兜底：按原句在文件中定位（仍找不到则判为过期建议）
        located = next(
            (index for index, line in enumerate(lines) if needle in line and index >= offset),
            None,
        )
        if located is None:
            raise InvalidOperationError(
                "原行内容已变化，建议已过期（请重新做去AI味软审）"
            )
        lines[located] = lines[located].replace(needle, replacement, 1)
        file_index = located
    else:
        lines[file_index] = replacement

    atomic_write_text(target, "\n".join(lines) + ("\n" if text.endswith("\n") else ""))
    return {"line": line_no, "file_line": file_index + 1, "applied": True}


def apply_batch(proposal_ids: list[int]) -> dict:
    """批量应用（逐条勾选）：逐条返回成功/失败，不因单条失败中断。"""
    results: list[dict] = []
    for proposal_id in proposal_ids:
        try:
            results.append({"id": proposal_id, "ok": True, **apply_proposal(proposal_id)})
        except Exception as exc:  # noqa: BLE001 - 单条失败不影响其余
            results.append({"id": proposal_id, "ok": False, "error": str(exc)})
    return {
        "applied": sum(1 for item in results if item["ok"]),
        "failed": sum(1 for item in results if not item["ok"]),
        "results": results,
    }


def discard_batch(proposal_ids: list[int]) -> dict:
    results: list[dict] = []
    for proposal_id in proposal_ids:
        try:
            discard_proposal(proposal_id)
            results.append({"id": proposal_id, "ok": True})
        except Exception as exc:  # noqa: BLE001
            results.append({"id": proposal_id, "ok": False, "error": str(exc)})
    return {"discarded": sum(1 for item in results if item["ok"]), "results": results}


def pending_count(project_id: int | None = None) -> int:
    sql = "SELECT COUNT(*) AS cnt FROM proposals WHERE status = ?"
    params: list = [STATUS_PENDING]
    if project_id is not None:
        sql += " AND project_id = ?"
        params.append(project_id)
    with db.get_conn() as conn:
        row = conn.execute(sql, tuple(params)).fetchone()
    return int(row["cnt"] or 0)


__all__ = [
    "PROPOSAL_KINDS",
    "apply_batch",
    "apply_proposal",
    "create_proposal",
    "discard_batch",
    "discard_proposal",
    "get_proposal",
    "list_proposals",
    "pending_count",
    "update_proposal_content",
]
