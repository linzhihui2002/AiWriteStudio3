"""Project-scoped, versioned file changes shared by chat and proposal application.

The journal owns its before-images: ordinary snapshot pruning never removes undo
data. A persisted ``applying`` row also makes an interrupted disk/DB commit visible.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Callable, Iterator

from .. import config, db
from . import operation_log
from .conflict_service import line_diff
from .errors import InvalidNameError, InvalidOperationError, NodeExistsError, NodeNotFoundError
from .fs_utils import (atomic_write_text, compose_document, has_frontmatter,
                       move_to_trash, read_text, resolve_within, split_frontmatter,
                       validate_node_name)
from .project_service import get_project_dir

_LOCKS: dict[tuple[str, int], threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()
MAX_FILE_CHARS = 400_000
OPERATIONS = {"create", "rewrite", "replace_exact", "move", "delete"}


class FileConflictError(InvalidOperationError):
    status_code = 409

    def __init__(self, message: str, *, path: str = "", expected_hash: str | None = None,
                 actual_hash: str | None = None) -> None:
        super().__init__(message)
        self.details = {"code": "file_conflict", "path": path,
                        "expected_hash": expected_hash, "actual_hash": actual_hash}


class GateRejectedError(InvalidOperationError):
    def __init__(self, result: dict) -> None:
        super().__init__("章节未通过硬门禁：" + "、".join(result["blocking_gates"]))
        self.details = {"code": "gate_rejected", "gates": result}


def content_hash(content: str | None) -> str | None:
    return None if content is None else hashlib.sha256(content.encode("utf-8")).hexdigest()


@contextmanager
def project_lock(project_id: int) -> Iterator[None]:
    key = (str(Path(config.DB_PATH).resolve()), int(project_id))
    with _LOCKS_GUARD:
        lock = _LOCKS.setdefault(key, threading.RLock())
    with lock:
        yield


def ensure_schema() -> None:
    with db.get_conn() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS workspace_changes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL, session_id INTEGER, run_id TEXT NOT NULL,
            tool_call_id TEXT NOT NULL, operation TEXT NOT NULL, path TEXT NOT NULL,
            destination TEXT, status TEXT NOT NULL DEFAULT 'applying',
            before_state TEXT NOT NULL, after_state TEXT NOT NULL,
            request_hash TEXT NOT NULL, request_json TEXT NOT NULL DEFAULT '{}',
            result TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT (datetime('now')), reverted_at TEXT,
            UNIQUE (run_id, tool_call_id)
        );
        CREATE INDEX IF NOT EXISTS idx_workspace_changes_run ON workspace_changes(run_id, id);
        """)
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(workspace_changes)")}
        if "request_json" not in columns:
            conn.execute("ALTER TABLE workspace_changes ADD COLUMN request_json TEXT NOT NULL DEFAULT '{}'")


def managed_path(project_dir: Path, rel_path: str, *, write: bool = False) -> tuple[str, Path]:
    """Only visible project documents, with no absolute/hidden/link aliases.

    章节正文为 ``章节/第NNNN章.txt``（纯正文）；其余文档保持 Markdown ``.md``。
    尚未迁移的历史 ``第NNNN章.md`` 仍可读写（由迁移任务改写为 ``.txt``）。
    """
    raw = str(rel_path or "").replace("\\", "/")
    parts = PurePosixPath(raw).parts
    if not parts or raw.startswith("/") or ":" in raw or any(
        p in {".", ".."} or p.startswith(".") for p in parts
    ):
        raise InvalidNameError("只能访问本书内的可见文档")
    for part in parts:
        validate_node_name(part)
    book_directory = Path(project_dir)
    if book_directory.is_symlink() or (hasattr(book_directory, "is_junction") and book_directory.is_junction()):
        raise InvalidNameError("不允许通过链接目录访问小说")
    root = book_directory.resolve()
    lexical = root
    for part in parts:
        lexical = lexical / part
        if lexical.is_symlink() or (hasattr(lexical, "is_junction") and lexical.is_junction()):
            raise InvalidNameError("不允许通过链接目录访问文档")
    target = resolve_within(root, raw)
    if target == root or target.is_dir():
        raise InvalidNameError("此工具只支持单个文档")
    rel = target.relative_to(root).as_posix()
    rel_parts = PurePosixPath(rel).parts
    if rel_parts[0] == "章节":
        from .chapter_service import is_chapter_name
        if is_chapter_name(target.name):
            if len(rel_parts) != 2:
                raise InvalidNameError("章节必须位于章节目录直属位置")
            return rel, target
        if write:
            if len(rel_parts) != 2:
                raise InvalidNameError("章节必须位于章节目录直属位置")
            raise InvalidNameError("章节文件名必须为「第NNNN章.txt」（如 第0001章.txt）")
    if target.suffix.lower() != ".md":
        raise InvalidNameError("此工具只支持单个 Markdown 文档")
    return rel, target


def _text(path: Path) -> str | None:
    if not path.exists():
        return None
    if not path.is_file():
        raise InvalidOperationError("目标不是文档")
    if path.stat().st_size > MAX_FILE_CHARS * 4:
        raise InvalidOperationError("文档过大，请拆分后再修改")
    value = read_text(path)
    if "\x00" in value:
        raise InvalidOperationError("不支持二进制内容")
    return value


def file_state(project_id: int, rel_path: str) -> dict:
    with project_lock(project_id):
        _, root = get_project_dir(project_id)
        rel, path = managed_path(root, rel_path)
        text = _text(path)
        return {"path": rel, "rel_path": rel, "exists": text is not None,
                "content": text, "hash": content_hash(text),
                "mtime": path.stat().st_mtime if text is not None else None}


def assert_version(path: str, content: str | None, expected_hash: str | None) -> None:
    actual = content_hash(content)
    if actual != expected_hash:
        raise FileConflictError("文件已变化，请重新读取后决定如何修改：" + path,
                                path=path, expected_hash=expected_hash, actual_hash=actual)


def _normalize(root: Path, rel: str, content: str, before: str | None,
               title: str | None, status: str | None) -> str:
    if len(content) > MAX_FILE_CHARS or "\x00" in content:
        raise InvalidOperationError("内容过长或包含二进制字符")
    if rel.startswith("章节/"):
        from .chapter_service import CHAPTER_STATUSES
        if status is not None and status not in CHAPTER_STATUSES:
            raise InvalidOperationError("非法章节状态")
        # 章节正文为纯文本：不写 frontmatter；标题/状态改由 chapters 表承载（落盘后写库）
        return split_frontmatter(content)[1]
    if rel == "project.md":
        old_meta, _ = split_frontmatter(before or "")
        meta, body = split_frontmatter(content)
        if not has_frontmatter(content):
            meta = old_meta
        if meta.get("书名") != old_meta.get("书名"):
            raise InvalidOperationError("请通过项目设置修改书名；对话可修改本书简介与其他元信息")
        return compose_document(meta, body)
    return content


def _sync_chapter_meta(project_id: int, rel: str, content: str | None,
                       title: str | None, status: str | None) -> None:
    """章节标题/状态写入 ``chapters`` 表（原先存在 frontmatter 里的元数据改由库承载）。"""
    if not rel.startswith("章节/"):
        return
    from .chapter_service import CHAPTER_STATUSES, upsert_chapter_meta

    meta, _body = split_frontmatter(content or "")
    candidate_status = status if status is not None else meta.get("状态")
    upsert_chapter_meta(
        project_id, rel,
        title=title if title is not None else meta.get("标题"),
        status=str(candidate_status) if candidate_status in CHAPTER_STATUSES else None,
    )


def _public(row: dict) -> dict:
    before = json.loads(row["before_state"])
    after = json.loads(row["after_state"])
    source = row["path"]
    destination = row["destination"]
    previous = before.get(source)
    current = after.get(destination or source)
    result = json.loads(row["result"] or "{}")
    return {"id": row["id"], "change_id": row["id"], "project_id": row["project_id"],
            "session_id": row["session_id"], "run_id": row["run_id"],
            "tool_call_id": row["tool_call_id"], "operation": row["operation"],
            "path": source, "rel_path": source, "destination": destination,
            "status": row["status"], "before_exists": previous is not None,
            "before_content": previous, "after_content": current,
            "before_hash": content_hash(previous), "after_hash": content_hash(current),
            "diff": line_diff(previous or "", current or ""),
            "created_at": row["created_at"], "reverted_at": row["reverted_at"],
            "can_revert": row["status"] == "applied", **result}


def list_changes(run_id: str | int, project_id: int | None = None) -> list[dict]:
    ensure_schema()
    with db.get_conn() as conn:
        rows = conn.execute("SELECT * FROM workspace_changes WHERE run_id=? ORDER BY id",
                            (str(run_id),)).fetchall()
    if project_id is not None and any(int(r["project_id"]) != project_id for r in rows):
        raise InvalidOperationError("变更不属于当前项目")
    return [_public(dict(row)) for row in rows]


def get_call(run_id: str | int, tool_call_id: str, project_id: int) -> dict | None:
    ensure_schema()
    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM workspace_changes WHERE run_id=? AND tool_call_id=?",
                           (str(run_id), tool_call_id)).fetchone()
    if row is None:
        return None
    if row["project_id"] != project_id:
        raise InvalidOperationError("工具调用不属于当前项目")
    return {**_public(dict(row)), "request": json.loads(row["request_json"])}


def _refresh(project_id: int, root: Path, paths: list[str]) -> None:
    from .index_service import index_document, remove_from_index
    for rel in paths:
        try:
            if (root / rel).is_file():
                index_document(project_id, root, rel)
            else:
                remove_from_index(project_id, rel)
        except Exception:
            # Index data is derived; the document and durable journal are authoritative.
            pass


def rebind_cards_after_move(project_id: int, source: str, destination: str) -> None:
    """Best-effort relocation of setting-card views after a committed move.

    Documents and the durable change journal remain authoritative. A malformed
    derived cache must not turn a completed move into an apparent file failure.
    """
    source = str(source).replace("\\", "/").rstrip("/")
    destination = str(destination).replace("\\", "/").rstrip("/")
    if not source.startswith("设定/") or not destination.startswith("设定/"):
        return
    try:
        from .asset_card_service import rebind_file

        rebind_file(project_id, source, destination)
    except Exception as exc:
        operation_log.log(project_id, "card-cache-rebind-warning", destination,
                          {"from": source, "to": destination, "error": str(exc)})


def _prepare_change(project_id: int, *, operation: str, rel_path: str,
                    content: str | None = None, expected_hash: str | None = None,
                    old_text: str | None = None, new_text: str | None = None,
                    destination: str | None = None, title: str | None = None,
                    status: str | None = None, validate_gates: bool = True) -> dict:
    """Validate and normalize without journaling, snapshots, or filesystem writes."""
    if operation not in OPERATIONS:
        raise InvalidOperationError("无效的文件变更操作")
    row, root = get_project_dir(project_id)
    rel, target = managed_path(root, rel_path, write=True)
    before_text = _text(target)
    before = {rel: before_text}
    dst = None
    gates = None
    if operation == "create":
        if before_text is not None:
            raise FileConflictError("目标已存在，不能覆盖新建：" + rel, path=rel,
                                    actual_hash=content_hash(before_text))
        if expected_hash is not None:
            raise InvalidOperationError("新建文件的基准必须为不存在")
    else:
        if before_text is None:
            raise NodeNotFoundError("文件不存在：" + rel)
        if expected_hash is None:
            raise FileConflictError("修改前必须读取文件并提供当前版本 hash", path=rel,
                                    actual_hash=content_hash(before_text))
        assert_version(rel, before_text, expected_hash)
    if operation in {"move", "delete"} and rel == "project.md":
        raise InvalidOperationError("不能移动或删除本书的 project.md")
    if operation == "move":
        dst, dst_path = managed_path(root, destination or "", write=True)
        if dst == rel or dst == "project.md" or dst_path.exists():
            raise NodeExistsError("移动目标已存在或受保护")
        before[dst] = None
        after = {rel: None, dst: before_text}
        if validate_gates and dst.startswith("章节/"):
            from .review_service import run_hard_gates
            from .writing_preference_service import chapter_word_range
            word_min, word_max = chapter_word_range(project_id)
            gates = run_hard_gates(before_text or "", project_dir=root, rel_path=dst,
                                   min_words=word_min, max_words=word_max)
    elif operation == "delete":
        after = {rel: None}
    else:
        if operation == "replace_exact":
            if not old_text or new_text is None:
                raise InvalidOperationError("替换原文不能为空，且必须提供替换内容")
            occurrences = (before_text or "").count(old_text)
            if occurrences == 0:
                raise FileConflictError("原文已变化，此修改已过期；请重新读取文件后重试", path=rel)
            if occurrences > 1:
                raise InvalidOperationError("原文匹配到多处，请补充相邻文字使其精确出现一次")
            candidate = (before_text or "").replace(old_text, new_text, 1)
        else:
            if content is None:
                raise InvalidOperationError("缺少文档内容")
            candidate = content
        candidate = _normalize(root, rel, candidate, before_text, title, status)
        if validate_gates and rel.startswith("章节/"):
            from .review_service import run_hard_gates
            from .writing_preference_service import chapter_word_range
            word_min, word_max = chapter_word_range(project_id)
            gates = run_hard_gates(candidate, project_dir=root, rel_path=rel,
                                   min_words=word_min, max_words=word_max)
        after = {rel: candidate}
    return {"row": row, "root": root, "target": target, "path": rel,
            "destination": dst, "before": before, "after": after, "gates": gates}


def preview_change(project_id: int, *, operation: str, rel_path: str,
                   content: str | None = None, expected_hash: str | None = None,
                   old_text: str | None = None, new_text: str | None = None,
                   destination: str | None = None, title: str | None = None,
                   status: str | None = None) -> dict:
    """The exact normalized candidate displayed for approval, with no side effects."""
    with project_lock(project_id):
        prepared = _prepare_change(project_id, operation=operation, rel_path=rel_path,
            content=content, expected_hash=expected_hash, old_text=old_text,
            new_text=new_text, destination=destination, title=title, status=status)
        rel, dst = prepared["path"], prepared["destination"]
        before, after = prepared["before"], prepared["after"]
        return {"project_id": project_id, "operation": operation, "path": rel,
                "rel_path": rel, "destination": dst, "before_exists": before[rel] is not None,
                "before_content": before[rel], "after_content": after.get(dst or rel),
                "before_hash": content_hash(before[rel]),
                "after_hash": content_hash(after.get(dst or rel)),
                "before_hashes": {p: content_hash(value) for p, value in before.items()},
                "after_hashes": {p: content_hash(value) for p, value in after.items()},
                "diff": line_diff(before[rel] or "", after.get(dst or rel) or ""),
                "gates": prepared["gates"]}


def apply_change(project_id: int, *, run_id: str | int, session_id: int | None,
                 tool_call_id: str, operation: str, rel_path: str,
                 content: str | None = None, expected_hash: str | None = None,
                 old_text: str | None = None, new_text: str | None = None,
                 destination: str | None = None, title: str | None = None,
                 status: str | None = None, read_only: bool = False,
                 validate_gates: bool = True,
                 approved_preview: dict | None = None,
                 source_request: dict | None = None,
                 should_cancel: Callable[[], bool] | None = None) -> dict:
    if read_only:
        raise InvalidOperationError("审稿模式只读，不能修改小说文件")
    if operation not in OPERATIONS or not str(run_id) or not tool_call_id:
        raise InvalidOperationError("缺少有效变更操作、运行编号或工具调用编号")
    ensure_schema()
    request = {"project": project_id, "session": session_id, "operation": operation,
               "path": rel_path, "content": content, "expected_hash": expected_hash,
               "old_text": old_text, "new_text": new_text, "destination": destination,
               "title": title, "status": status}
    digest = content_hash(json.dumps(request, ensure_ascii=False, sort_keys=True))
    # 自动修订的来源凭据不改变有效写入摘要或审批文本；只绑定原工具请求供严格重放。
    if source_request is not None and not isinstance(source_request, dict):
        raise InvalidOperationError("来源请求必须是对象")
    source_json = json.dumps(source_request, ensure_ascii=False, sort_keys=True)
    journal_request = {**request, "source_request": source_request} if source_request is not None else request
    with project_lock(project_id):
        if should_cancel and should_cancel():
            raise InvalidOperationError("本轮已停止，未执行文件变更")
        with db.get_conn() as conn:
            existing = conn.execute("SELECT * FROM workspace_changes WHERE run_id=? AND tool_call_id=?",
                                    (str(run_id), tool_call_id)).fetchone()
            binding = conn.execute("SELECT project_id,session_id FROM workspace_changes WHERE run_id=? LIMIT 1",
                                   (str(run_id),)).fetchone()
        if binding and (binding["project_id"] != project_id or binding["session_id"] != session_id):
            raise InvalidOperationError("运行已绑定其他项目或会话")
        if existing:
            recorded_source = json.loads(existing["request_json"]).get("source_request")
            if (existing["request_hash"] != digest or
                    json.dumps(recorded_source, ensure_ascii=False, sort_keys=True) != source_json):
                raise InvalidOperationError("重复工具调用编号的参数不一致")
            if existing["status"] == "applying":
                raise FileConflictError("先前操作的提交状态待核实，请查看本轮文件变更")
            if existing["status"] == "failed":
                raise InvalidOperationError("先前操作已失败，请重新读取后发起新的工具调用")
            if existing["status"] == "rejected":
                error = GateRejectedError(json.loads(existing["result"])["gates"])
                error.details.update(change_id=existing["id"], candidate_retained=True)
                raise error
            return {**_public(dict(existing)), "replayed": True}

        prepared = _prepare_change(project_id, operation=operation, rel_path=rel_path,
            content=content, expected_hash=expected_hash, old_text=old_text,
            new_text=new_text, destination=destination, title=title, status=status,
            validate_gates=validate_gates or approved_preview is not None)
        row, root, target = prepared["row"], prepared["root"], prepared["target"]
        rel, dst = prepared["path"], prepared["destination"]
        before, after, gates = prepared["before"], prepared["after"], prepared["gates"]
        if approved_preview is not None:
            actual = {"project_id": project_id, "operation": operation, "path": rel,
                      "destination": dst,
                      "before_hashes": {p: content_hash(value) for p, value in before.items()},
                      "after_hashes": {p: content_hash(value) for p, value in after.items()}}
            if any(approved_preview.get(key) != value for key, value in actual.items()):
                raise FileConflictError("批准的文件版本或修改内容已变化，请重新读取并请求批准", path=rel)

        # Journal is committed before any disk mutation; before-images survive pruning.
        with db.get_conn() as conn:
            cursor = conn.execute(
                "INSERT INTO workspace_changes (project_id,session_id,run_id,tool_call_id,operation,path,"
                "destination,before_state,after_state,request_hash,request_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (project_id, session_id, str(run_id), tool_call_id, operation, rel, dst,
                 json.dumps(before, ensure_ascii=False), json.dumps(after, ensure_ascii=False), digest,
                 json.dumps(journal_request, ensure_ascii=False)))
            change_id = int(cursor.lastrowid)
        if gates is not None and not gates["passed"]:
            with db.get_conn() as conn:
                conn.execute("UPDATE workspace_changes SET status='rejected',result=? WHERE id=?",
                             (json.dumps({"gates": gates, "candidate_retained": True}, ensure_ascii=False), change_id))
            operation_log.log(project_id, "file-change-rejected", rel,
                              {"run_id": str(run_id), "change_id": change_id})
            error = GateRejectedError(gates)
            error.details.update(change_id=change_id, candidate_retained=True)
            raise error
        details: dict = {}
        try:
            if should_cancel and should_cancel():
                raise InvalidOperationError("本轮已停止，未执行文件变更")
            # Recheck immediately before commit, including destination existence.
            for path, text in before.items():
                assert_version(path, _text(managed_path(root, path, write=True)[1]), content_hash(text))
            if operation == "move":
                assert dst is not None
                destination_path = managed_path(root, dst, write=True)[1]
                destination_path.parent.mkdir(parents=True, exist_ok=True)
                os.replace(target, destination_path)
            elif operation == "delete":
                details["trash_rel"] = move_to_trash(row["name"], root, rel)
            else:
                from .snapshot_service import snapshot_file
                snapshot_file(project_id, root, row["name"], rel, reason="chat-change")
                if should_cancel and should_cancel():
                    raise InvalidOperationError("本轮已停止，未执行文件变更")
                assert_version(rel, _text(target), content_hash(before[rel]))
                atomic_write_text(target, after[rel] or "")
                _sync_chapter_meta(project_id, rel, content, title, status)
            with db.get_conn() as conn:
                conn.execute("UPDATE workspace_changes SET status='applied',result=? WHERE id=?",
                             (json.dumps(details, ensure_ascii=False), change_id))
        except Exception as exc:
            # Never overwrite a concurrently edited file during failure recovery.
            current = {p: _text(managed_path(root, p, write=True)[1]) for p in before}
            commit_status = "applied" if current == after else "failed" if current == before else "conflict"
            with db.get_conn() as conn:
                conn.execute("UPDATE workspace_changes SET status=?,result=? WHERE id=?",
                             (commit_status, json.dumps({"error": str(exc), **details}, ensure_ascii=False), change_id))
            raise
        if operation == "move":
            rebind_cards_after_move(project_id, rel, dst)
        _refresh(project_id, root, list(after))
        operation_log.log(project_id, "file-change", rel,
                          {"change_id": change_id, "run_id": str(run_id), "operation": operation, **details})
        return next(c for c in list_changes(run_id, project_id) if c["id"] == change_id)


def revert(run_id: str | int, change_ids: list[int] | None = None,
           project_id: int | None = None, *, should_cancel: Callable[[], bool] | None = None) -> dict:
    """Preflight the entire undo in reverse order; later user edits are never lost."""
    ensure_schema()
    if should_cancel and should_cancel():
        raise InvalidOperationError("本轮已停止，不撤回文件修改")
    with db.get_conn() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM workspace_changes WHERE run_id=? ORDER BY id DESC", (str(run_id),))]
    if not rows:
        return {"run_id": str(run_id), "reverted": 0, "changes": []}
    pid = int(rows[0]["project_id"])
    if project_id is not None and pid != project_id:
        raise InvalidOperationError("变更不属于当前项目")
    selected = set(change_ids) if change_ids is not None else {r["id"] for r in rows}
    if selected - {r["id"] for r in rows}:
        raise InvalidOperationError("撤回列表包含其他运行的变更")
    with project_lock(pid):
        _, root = get_project_dir(pid)
        active = [r for r in rows if r["id"] in selected and r["status"] == "applied"]
        simulated: dict[str, str | None] = {}
        for row in active:
            before, after = json.loads(row["before_state"]), json.loads(row["after_state"])
            for path, expected in after.items():
                if path not in simulated:
                    simulated[path] = _text(managed_path(root, path, write=True)[1])
                assert_version(path, simulated[path], content_hash(expected))
            simulated.update(before)
        reverted = []
        for row in active:
            if should_cancel and should_cancel():
                raise InvalidOperationError("本轮已停止，不撤回文件修改")
            before, after = json.loads(row["before_state"]), json.loads(row["after_state"])
            for path, expected in after.items():
                assert_version(path, _text(managed_path(root, path, write=True)[1]), content_hash(expected))
            if should_cancel and should_cancel():
                raise InvalidOperationError("本轮已停止，不撤回文件修改")
            with db.get_conn() as conn:
                conn.execute("UPDATE workspace_changes SET status='reverting' WHERE id=?", (row["id"],))
            if row["operation"] == "move":
                source = managed_path(root, row["path"], write=True)[1]
                destination = managed_path(root, row["destination"], write=True)[1]
                source.parent.mkdir(parents=True, exist_ok=True)
                os.replace(destination, source)
            else:
                for path, content in before.items():
                    target = managed_path(root, path, write=True)[1]
                    if content is None:
                        target.unlink(missing_ok=True)
                    else:
                        atomic_write_text(target, content)
            with db.get_conn() as conn:
                conn.execute("UPDATE workspace_changes SET status='reverted',reverted_at=datetime('now') WHERE id=?",
                             (row["id"],))
            if row["operation"] == "move":
                rebind_cards_after_move(pid, row["destination"], row["path"])
            _refresh(pid, root, list(before))
            operation_log.log(pid, "file-change-revert", row["path"],
                              {"run_id": str(run_id), "change_id": row["id"]})
            reverted.append(row["id"])
        return {"run_id": str(run_id), "reverted": len(reverted), "change_ids": reverted,
                "changes": list_changes(run_id, pid)}


def recover_changes(run_id: str | int) -> list[dict]:
    """Reconcile interrupted commits without changing project files."""
    ensure_schema()
    with db.get_conn() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM workspace_changes WHERE run_id=? AND status IN ('applying','reverting') ORDER BY id",
            (str(run_id),))]
    for row in rows:
        pid = int(row["project_id"])
        with project_lock(pid):
            _, root = get_project_dir(pid)
            before, after = json.loads(row["before_state"]), json.loads(row["after_state"])
            current = {p: _text(managed_path(root, p, write=True)[1]) for p in before}
            if current == after:
                state = "applied"
            elif current == before:
                state = "reverted" if row["status"] == "reverting" else "failed"
            else:
                state = "conflict"
            with db.get_conn() as conn:
                conn.execute("UPDATE workspace_changes SET status=? WHERE id=?", (state, row["id"]))
            if row["operation"] == "move":
                if state == "applied":
                    rebind_cards_after_move(pid, row["path"], row["destination"])
                elif state == "reverted":
                    rebind_cards_after_move(pid, row["destination"], row["path"])
            _refresh(pid, root, list(before))
            operation_log.log(pid, "file-change-recovery", row["path"],
                              {"change_id": row["id"], "status": state})
    return list_changes(run_id)


def recover_pending() -> list[dict]:
    ensure_schema()
    with db.get_conn() as conn:
        run_ids = [r["run_id"] for r in conn.execute(
            "SELECT DISTINCT run_id FROM workspace_changes WHERE status IN ('applying','reverting')")]
    return [change for run_id in run_ids for change in recover_changes(run_id)]
