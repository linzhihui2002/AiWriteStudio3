"""Proposal 收件箱：所有 AI 写入类产出必须先进入这里，人工应用后才落盘。

原则（spec：应用前强制 diff）
- 任何 AI 产物**不得**静默写盘；创建时记录目标路径、基准内容 hash 与差异；
- 应用时：先给现有文件存快照 → 原子写 → 刷新索引 → 记录操作日志；
- 支持「应用 / 编辑后应用 / 丢弃」与批量逐条勾选。
"""

from __future__ import annotations

import json
from contextlib import nullcontext
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


def insert_proposal(conn, *, project_id: int | None, kind: str, title: str,
                    target_path: str, content: str, base: str, base_exists: bool,
                    meta: dict | None = None, task_id: int | None = None) -> int:
    """Insert using an existing transaction and an explicitly captured base.

    The caller owns version checks. This lets a completed ingestion receipt and
    its single inbox proposal become visible in the same database commit.
    """
    from .file_change_service import content_hash
    payload = json.dumps({"content": content, "base": base, "base_exists": base_exists,
                          "base_hash": content_hash(base) if base_exists else None,
                          "meta": meta or {}}, ensure_ascii=False)
    cursor = conn.execute(
        "INSERT INTO proposals (project_id,task_id,kind,title,target_path,status,payload)"
        " VALUES (?,?,?,?,?,?,?)",
        (project_id, task_id, kind if kind in PROPOSAL_KINDS else "other", title,
         target_path, STATUS_PENDING, payload),
    )
    return int(cursor.lastrowid or 0)


def _materialize_state(kind: str, target_path: str, payload: dict) -> str:
    """Use the same complete candidate for legacy read, diff and application."""
    content = str(payload.get("content") or "")
    meta = payload.get("meta") or {}
    if (kind != "state" or target_path != "状态/角色状态.md" or not meta.get("ingest")
            or meta.get("state_document_version") or meta.get("knowledge")):
        return content
    from .character_service import compose_state_document
    import re
    if (not payload.get("base_exists", bool(payload.get("base")))
            or not meta.get("character") or not meta.get("field")):
        raise InvalidOperationError("旧状态提案缺少完整基准或字段定位，请重新提取")
    fragment = re.fullmatch(r"\s*[-*]\s*([^\n：:]+)[：:]\s*([^\n]+)\s*", content)
    if not fragment or fragment.group(1).strip() != str(meta["field"]):
        raise InvalidOperationError("旧状态提案不是可定位的字段片段，请重新提取")
    return compose_state_document(str(payload["base"]), [{
        "角色": str(meta["character"]), "字段": str(meta["field"]),
        "新值": fragment.group(2).strip(),
    }])


def _validate_ingest_source(project_id: int, meta: dict) -> None:
    if not meta.get("ingest"):
        return
    from .ingestion_commit import require_history_source
    from .file_change_service import content_hash, FileConflictError
    origin = meta.get("ingestion") or {}
    chapter = str(origin.get("chapter_rel") or meta.get("chapter") or "")
    if not chapter:
        raise InvalidOperationError("状态提案缺少来源章节，请重新提取")
    row, _, source = require_history_source(project_id, chapter)
    if origin:
        if origin.get("book_key") != row["knowledge_key"] or origin.get("source_hash") != content_hash(source):
            raise FileConflictError("来源正文已变化，状态提案已过期，请重新提取", path=chapter)
    else:
        # A pre-manifest proposal has no source hash. It can only be retained
        # when its original evidence is still uniquely grounded in this chapter.
        evidence = str(meta.get("evidence") or "")
        if not evidence or source.count(evidence) != 1:
            raise InvalidOperationError("旧状态提案缺少可核验的正文依据，请重新提取")


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

    with db.get_conn() as conn:
        proposal_id = insert_proposal(conn, project_id=project_id, kind=kind, title=title,
            target_path=target_path, content=content, base=base, base_exists=base_exists,
            task_id=task_id, meta=meta)
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
    from .file_change_service import content_hash
    payload = _loads(row["payload"])
    materialization_error = ""
    try:
        content = _materialize_state(row["kind"], str(row["target_path"] or ""), payload)
    except InvalidOperationError as exc:
        content = str(payload.get("content") or "")
        materialization_error = str(exc)
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
        "base_hash": payload.get("base_hash", content_hash(base) if payload.get("base_exists", bool(base)) else None),
        "chars": len(content),
        "is_new_file": not payload.get("base_exists", bool(base)),
    }
    if include_diff:
        result["diff"] = line_diff(base, content)
    if materialization_error:
        result["meta"] = {**result["meta"], "application_error": materialization_error}
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
    from .file_change_service import project_lock
    proposal = get_proposal(proposal_id)
    with project_lock(proposal["project_id"]) if proposal["project_id"] is not None else nullcontext():
        proposal = get_proposal(proposal_id)
        if proposal["status"] != STATUS_PENDING:
            raise InvalidOperationError("只有待处理提案可以编辑内容")
        payload = _loads(_raw_payload(proposal_id))
        payload["content"] = content
        if proposal["kind"] == "state" and (payload.get("meta") or {}).get("ingest"):
            payload.setdefault("meta", {})["state_document_version"] = 1
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE proposals SET payload = ? WHERE id = ?",
                (json.dumps(payload, ensure_ascii=False), proposal_id),
            )
    return get_proposal(proposal_id)


def rebase_state_proposal(proposal_id: int) -> dict:
    """Explicitly regenerate a complete candidate against current role material.

    The source manuscript must still match the grounded extraction. This is an
    inbox action, not another ingestion or an implicit acceptance of a conflict.
    """
    from . import file_change_service as changes
    from .character_service import compose_state_document

    proposal = get_proposal(proposal_id)
    if proposal["project_id"] is None:
        raise InvalidOperationError("状态提案缺少项目上下文")
    project_id = int(proposal["project_id"])
    with changes.project_lock(project_id):
        proposal = get_proposal(proposal_id)
        payload = _loads(_raw_payload(proposal_id))
        meta = payload.get("meta") or {}
        fields = meta.get("state_changes")
        if (proposal["status"] != STATUS_PENDING or proposal["kind"] != "state"
                or proposal["target_path"] != "状态/角色状态.md" or not meta.get("ingest")
                or not meta.get("ingestion") or not isinstance(fields, list) or not fields):
            raise InvalidOperationError("这份提案不能安全重提，请重新提取当前正文")
        _validate_ingest_source(project_id, meta)
        original_candidate = compose_state_document(str(payload.get("base") or ""), fields)
        if proposal["content"] != original_candidate:
            raise InvalidOperationError("这份候选已被人工编辑，不能用原始提取值重提；请保留修改并核对当前状态材料")
        current = changes.file_state(project_id, proposal["target_path"])
        candidate = compose_state_document(current["content"] or "", fields)
        meta = {**meta, "rebased_from": proposal_id, "state_document_version": 1,
                "ingestion": {**meta["ingestion"], "target_hash": current["hash"]}}
        with db.get_conn() as conn:
            new_id = None
            if candidate != (current["content"] or ""):
                new_id = insert_proposal(conn, project_id=project_id, kind="state",
                    title=proposal["title"], target_path=proposal["target_path"], content=candidate,
                    base=current["content"] or "", base_exists=current["exists"], meta=meta)
            conn.execute("UPDATE proposals SET status=? WHERE id=?", (STATUS_DISCARDED, proposal_id))
    operation_log.log(project_id, "state-proposal-rebase", proposal["target_path"],
                      {"proposal_id": proposal_id, "new_proposal_id": new_id})
    return {"proposal_id": new_id, "already_current": new_id is None,
            "proposal": get_proposal(new_id) if new_id is not None else None}


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


def record_pipeline_review(proposal_id: int, review: dict) -> dict:
    """Bind a pipeline review to the exact candidate and contract it inspected."""
    from . import contract_service, file_change_service as changes, review_service

    proposal = get_proposal(proposal_id)
    if proposal["project_id"] is None or proposal["kind"] != "chapter_draft":
        raise InvalidOperationError("管线审稿必须绑定本书章节草稿")
    project_id = int(proposal["project_id"])
    with changes.project_lock(project_id):
        proposal = get_proposal(proposal_id)
        if proposal["status"] != STATUS_PENDING:
            raise InvalidOperationError("已处理的提案不能重新绑定候选审稿")
        payload = _loads(_raw_payload(proposal_id))
        digest = changes.content_hash(str(payload.get("content") or ""))
        if review.get("candidate_hash") != digest:
            raise InvalidOperationError("审稿正文与候选提案版本不一致")
        if changes.file_state(project_id, proposal["target_path"])["hash"] != proposal["base_hash"]:
            raise InvalidOperationError("候选生成后目标正文已变化，请核对当前稿后创建新候选")
        contract_hash = review_service.contract_fingerprint(
            contract_service.get_contract(project_id, proposal["target_path"]))
        if review.get("verdict") == "通过" and review.get("contract_hash") != contract_hash:
            raise InvalidOperationError("审稿后章节合同已变化，不能绑定通过凭据")
        meta = payload.setdefault("meta", {})
        meta["pipeline_candidate"] = {**(meta.get("pipeline_candidate") or {}),
                                      "protocol": 2, "candidate_hash": digest,
                                      "contract_hash": contract_hash}
        meta["pipeline_review"] = {
            "review_id": review.get("review_id"), "candidate_hash": digest,
            "contract_hash": review.get("contract_hash"), "verdict": review.get("verdict"),
            "ai_used": bool(review.get("ai_used")),
        }
        with db.get_conn() as conn:
            conn.execute("UPDATE proposals SET payload=? WHERE id=?",
                         (json.dumps(payload, ensure_ascii=False), proposal_id))
    return get_proposal(proposal_id)


def review_pipeline_candidate(proposal_id: int, *, use_ai: bool = True, should_cancel=None) -> dict:
    """Re-review the current inbox candidate after an author edit, without applying it."""
    from . import file_change_service as changes, review_service

    proposal = get_proposal(proposal_id)
    if (proposal["status"] != STATUS_PENDING or proposal["project_id"] is None
            or proposal["kind"] != "chapter_draft" or not proposal["meta"].get("pipeline_candidate")):
        raise InvalidOperationError("只可审查待处理的管线章节候选")
    project_id = int(proposal["project_id"])
    if changes.file_state(project_id, proposal["target_path"])["hash"] != proposal["base_hash"]:
        raise InvalidOperationError("目标正文已变化，请核对当前稿后创建新候选")
    if should_cancel and should_cancel():
        raise InvalidOperationError("候选审查已停止")
    review = review_service.review_text(
        project_id, proposal["target_path"], proposal["content"], use_ai=use_ai,
        candidate_hash=changes.content_hash(proposal["content"]), should_cancel=should_cancel)
    if should_cancel and should_cancel():
        raise InvalidOperationError("候选审查已停止，未绑定通过凭据")
    record_pipeline_review(proposal_id, review)
    return review


def _validate_pipeline_review(project_id: int, target_rel: str, content: str, meta: dict) -> None:
    if not meta.get("pipeline_candidate"):
        return
    from . import contract_service, file_change_service as changes, review_service

    review = meta.get("pipeline_review") or {}
    if (review.get("verdict") != "通过" or not review.get("ai_used")
            or review.get("candidate_hash") != changes.content_hash(content)):
        raise InvalidOperationError("当前候选版本缺少逐项通过的 AI 审稿，请重新审稿")
    contract_hash = review_service.contract_fingerprint(
        contract_service.get_contract(project_id, target_rel))
    if review.get("contract_hash") != contract_hash:
        raise InvalidOperationError("审稿后章节合同已变化，请重新审稿")


def apply_proposal(proposal_id: int, *, content: str | None = None,
                   status: str | None = None, should_cancel=None) -> dict:
    """Apply through the same version checks, gates and journal as native tools."""
    from . import file_change_service as changes
    proposal = get_proposal(proposal_id)
    knowledge_meta = (proposal.get("meta") or {}).get("knowledge")
    if proposal["status"] != STATUS_PENDING:
        # A committed knowledge review may need to repair its derived receipt
        # after a crash. Returning that receipt must never write the file again.
        if knowledge_meta and proposal["status"] == STATUS_APPLIED and proposal["project_id"] is not None:
            from . import knowledge_candidate_service as candidates
            project_id = int(proposal["project_id"])
            receipts = changes.list_changes(f"proposal:{proposal_id}", project_id)
            receipt = next((item for item in reversed(receipts) if item["status"] == "applied"), None)
            if receipt is None:
                raise InvalidOperationError("知识提案缺少已提交的变更凭据")
            review = candidates.reconcile_proposal(project_id, proposal, receipt)
            return {"proposal": proposal, "written": proposal["target_path"],
                    "change": receipt, "meta": {"change_id": receipt["id"], "idempotent": True},
                    "knowledge": review}
        raise InvalidOperationError(f"提案已处理（{proposal['status']}），不能重复应用")
    if proposal["project_id"] is None:
        raise InvalidOperationError("提案缺少项目上下文，无法应用")
    project_id = int(proposal["project_id"])
    raw_payload = _raw_payload(proposal_id)
    payload = _loads(raw_payload)
    meta = payload.get("meta") or {}
    target_rel = str(proposal["target_path"] or "")
    if not target_rel:
        raise InvalidOperationError("提案缺少目标路径")
    materialized = _materialize_state(proposal["kind"], target_rel, payload)
    new_content = content if content is not None else materialized
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
        if get_proposal(proposal_id)["status"] != STATUS_PENDING:
            raise InvalidOperationError("提案已处理，不能再次提交正文")
        if _raw_payload(proposal_id) != raw_payload:
            raise InvalidOperationError("应用期间提案内容发生变化，请重新核对后应用")
        _validate_ingest_source(project_id, meta)
        _validate_pipeline_review(project_id, target_rel, arguments["content"], meta)
        if knowledge_meta:
            from . import knowledge_candidate_service as candidates
            # A prior disk/journal commit may have succeeded before the proposal
            # status update. In that case repair it instead of revalidating the
            # old source image, which this very write may already have changed.
            receipts = changes.list_changes(arguments["run_id"], project_id)
            committed = next((item for item in receipts
                              if item["tool_call_id"] == arguments["tool_call_id"]
                              and item["status"] == "applied"), None)
            if committed is None:
                candidates.validate_proposal(project_id, {**proposal, "content": arguments["content"]})
        result = changes.apply_change(project_id, **arguments, should_cancel=should_cancel)
        if proposal["kind"] == "state" and meta.get("ingest"):
            from .character_service import sync_state_index
            sync_state_index(project_id)
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
    response = {"proposal": get_proposal(proposal_id), "written": target_rel,
                "meta": result_meta, "change": result}
    if knowledge_meta:
        try:
            response["knowledge"] = candidates.reconcile_proposal(project_id, response["proposal"], result)
        except Exception as exc:  # The committed file must not be replayed to repair a derived DB.
            response["knowledge"] = {"status": "reconciliation_pending", "error": str(exc)}
    return response


def discard_proposal(proposal_id: int) -> dict:
    from .file_change_service import project_lock
    proposal = get_proposal(proposal_id)
    with project_lock(proposal["project_id"]) if proposal["project_id"] is not None else nullcontext():
        proposal = get_proposal(proposal_id)
        if proposal["status"] != STATUS_PENDING:
            raise InvalidOperationError(f"提案已处理（{proposal['status']}）")
        with db.get_conn() as conn:
            conn.execute("UPDATE proposals SET status = ? WHERE id = ?",
                         (STATUS_DISCARDED, proposal_id))
    operation_log.log(proposal["project_id"], "proposal-discard",
                      proposal["target_path"], {"proposal_id": proposal_id})
    result = get_proposal(proposal_id)
    if (result.get("meta") or {}).get("knowledge"):
        from . import knowledge_candidate_service as candidates
        try:
            result["knowledge"] = candidates.reconcile_proposal(result["project_id"], result)
        except Exception as exc:
            result["knowledge"] = {"status": "reconciliation_pending", "error": str(exc)}
    return result


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
    "record_pipeline_review",
    "review_pipeline_candidate",
    "update_proposal_content",
]
