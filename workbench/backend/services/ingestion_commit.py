"""Durable, book-scoped commit receipts for automatic chapter trackers.

Visible author files use the existing versioned change journal. The one internal
summary file has before/after images in this receipt; it is never exposed to the
ordinary file tool. A stopped commit is reconciled before another extraction.
"""
from __future__ import annotations

import json
import threading
import uuid
from pathlib import Path

from .. import config, db
from . import file_change_service as changes
from .errors import InvalidOperationError, NodeNotFoundError, ProjectNotFoundError
from .fs_utils import atomic_write_text, read_text, resolve_within
from .project_service import get_project_dir

SUMMARY_PATH = ".meta/summaries.json"
REVISION = "grounded-trackers-v1"
_FINAL = {"完成", "发表", "completed", "published"}
_ACTIVE: set[tuple[str, str]] = set()
_GUARD = threading.Lock()


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def ensure_schema() -> None:
    with db.get_conn() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS ingestion_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT UNIQUE NOT NULL,
            project_id INTEGER NOT NULL, book_key TEXT NOT NULL, chapter_rel TEXT NOT NULL,
            source_hash TEXT NOT NULL, revision TEXT NOT NULL, use_ai INTEGER NOT NULL,
            status TEXT NOT NULL, plan_json TEXT NOT NULL DEFAULT '{}',
            result_json TEXT NOT NULL DEFAULT '{}', error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            finished_at TEXT
        );
        CREATE INDEX IF NOT EXISTS ingestion_runs_chapter
            ON ingestion_runs(book_key,chapter_rel,id);
        CREATE UNIQUE INDEX IF NOT EXISTS ingestion_one_active_chapter
            ON ingestion_runs(book_key,chapter_rel)
            WHERE status IN ('preparing','applying');
        """)


def _active_key(run_id: str) -> tuple[str, str]:
    return str(Path(config.DB_PATH).resolve()), run_id


def _row(run_id: str) -> dict:
    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM ingestion_runs WHERE run_id=?", (run_id,)).fetchone()
    if row is None:
        raise InvalidOperationError("提取提交记录不存在")
    return dict(row)


def require_history_source(project_id: int, chapter_rel: str) -> tuple[dict, Path, str]:
    """Automatic history may only be grounded in a currently finalized chapter."""
    from .chapter_service import fetch_chapter_meta, require_chapter_path
    row, root = get_project_dir(project_id)
    path = require_chapter_path(project_id, chapter_rel)
    meta = fetch_chapter_meta(project_id, chapter_rel) or {}
    if meta.get("status") not in _FINAL:
        raise InvalidOperationError("只有「完成」或「发表」章节可以更新自动历史记录，请先核对正文并设置状态")
    return row, root, read_text(path)


def _source(project_id: int, chapter_rel: str) -> tuple[dict, Path, str]:
    return require_history_source(project_id, chapter_rel)


def _assert_source(run: dict) -> None:
    row, _, text = _source(run["project_id"], run["chapter_rel"])
    if row["knowledge_key"] != run["book_key"] or changes.content_hash(text) != run["source_hash"]:
        raise changes.FileConflictError("正文在提取期间已变化，请重新提取当前版本", path=run["chapter_rel"])


def summary_state(project_id: int) -> dict:
    _, root = get_project_dir(project_id)
    # Check the lexical intermediate path too: resolve_within alone accepts an
    # in-book alias, but internal persistence must not follow links of any kind.
    lexical = root
    for part in SUMMARY_PATH.split("/"):
        lexical /= part
        if lexical.is_symlink() or (hasattr(lexical, "is_junction") and lexical.is_junction()):
            raise InvalidOperationError("摘要文件不能通过链接访问")
    target = resolve_within(root, SUMMARY_PATH)
    if target.exists() and not target.is_file():
        raise InvalidOperationError("摘要目标不是文件")
    text = read_text(target) if target.is_file() else None
    return {"path": target, "content": text, "hash": changes.content_hash(text)}


def summary_plan(project_id: int, chapter_rel: str, summary: str) -> dict:
    before = summary_state(project_id)["content"]
    try:
        data = json.loads(before) if before else {}
    except (ValueError, TypeError) as exc:
        raise InvalidOperationError("历史摘要格式损坏，提取不会覆盖它") from exc
    if not isinstance(data, dict):
        raise InvalidOperationError("历史摘要必须是对象，提取不会覆盖它")
    if summary:
        data[chapter_rel] = summary
    after = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    return {"before": before, "after": after}


def _write_summary(project_id: int, plan: dict) -> None:
    state = summary_state(project_id)
    changes.assert_version(SUMMARY_PATH, state["content"], changes.content_hash(plan["before"]))
    state["path"].parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(state["path"], plan["after"])


def latest(project_id: int, chapter_rel: str) -> dict | None:
    ensure_schema()
    row, _ = get_project_dir(project_id)
    with db.get_conn() as conn:
        value = conn.execute("SELECT * FROM ingestion_runs WHERE book_key=? AND chapter_rel=?"
                             " AND status='committed' ORDER BY id DESC LIMIT 1",
                             (row["knowledge_key"], chapter_rel)).fetchone()
    return dict(value) if value else None


def source_is_current(run: dict) -> bool:
    """Check receipt eligibility without rewriting its author-owned projections."""
    try:
        _assert_source(run)
    except (OSError, InvalidOperationError, NodeNotFoundError, ProjectNotFoundError, changes.FileConflictError):
        return False
    return True


def summary_is_current(project_id: int, chapter_rel: str, summary: str) -> bool:
    """Versioned summaries require their receipt; unversioned legacy ones remain compatible."""
    chapter_rel = str(chapter_rel).replace("\\", "/")
    with changes.project_lock(project_id):
        try:
            require_history_source(project_id, chapter_rel)
        except (OSError, InvalidOperationError, NodeNotFoundError, ProjectNotFoundError):
            return False
        run = latest(project_id, chapter_rel)
        if run is None:
            # Old manually maintained summaries have no extraction receipt.
            # Retain compatibility while still requiring a finalized source.
            return True
        if not source_is_current(run):
            return False
        try:
            result = json.loads(run["result_json"])
            return result.get("summary") == summary
        except (ValueError, TypeError, AttributeError):
            return False


def _finish(run: dict, plan: dict) -> dict:
    """One DB commit publishes the receipt and the grouped inbox proposal."""
    from . import proposal_service
    result = dict(plan["result"])
    proposal_ids = []
    with db.get_conn() as conn:
        if conn.execute("SELECT status FROM ingestion_runs WHERE run_id=?", (run["run_id"],)).fetchone()[0] == "committed":
            return json.loads(_row(run["run_id"])["result_json"])
        state = plan.get("state_proposal")
        if state:
            proposal_ids.append(proposal_service.insert_proposal(
                conn, project_id=run["project_id"], kind="state", title=state["title"],
                target_path="状态/角色状态.md", content=state["content"],
                base=state["base"], base_exists=state["base_exists"], meta=state["meta"],
            ))
        # Old pending state candidates no longer describe the current source.
        for proposal in conn.execute("SELECT id,payload FROM proposals WHERE project_id=?"
                                     " AND kind='state' AND status='pending'", (run["project_id"],)).fetchall():
            payload = json.loads(proposal["payload"])
            origin = (payload.get("meta") or {}).get("ingestion") or {}
            if (origin.get("chapter_rel") == run["chapter_rel"]
                    and origin.get("run_id") != run["run_id"]):
                payload.setdefault("meta", {})["superseded_by"] = run["run_id"]
                conn.execute("UPDATE proposals SET status='discarded',payload=? WHERE id=?",
                             (_json(payload), proposal["id"]))
        result["proposals_created"] = proposal_ids
        result["proposal_ids"] = proposal_ids
        conn.execute("UPDATE ingestion_runs SET status='committed',result_json=?,finished_at=datetime('now')"
                     " WHERE run_id=?", (_json(result), run["run_id"]))
    return result


def _rollback(run: dict, plan: dict) -> None:
    """Undo our writes only; author edits turn a stopped commit into a conflict."""
    failures = []
    changes.recover_changes(run["run_id"])
    receipt = changes.list_changes(run["run_id"], run["project_id"])
    if any(item["status"] == "conflict" for item in receipt):
        failures.append("托管材料出现人工修改")
    else:
        try:
            changes.revert(run["run_id"], project_id=run["project_id"])
        except Exception as exc:
            failures.append(str(exc))
    summary = plan.get("summary")
    if summary:
        try:
            current = summary_state(run["project_id"])
            if current["content"] == summary["after"]:
                if summary["before"] is None:
                    current["path"].unlink(missing_ok=True)
                else:
                    atomic_write_text(current["path"], summary["before"])
            elif current["content"] != summary["before"]:
                failures.append("历史摘要出现人工修改")
        except Exception as exc:
            failures.append(str(exc))
    with db.get_conn() as conn:
        conn.execute("UPDATE ingestion_runs SET status=?,error=?,finished_at=datetime('now') WHERE run_id=?",
                     ("conflict" if failures else "rolled_back", "；".join(failures), run["run_id"]))
    if failures:
        raise changes.FileConflictError("提取提交未完成，保留已变化的人工材料：" + "；".join(failures))


def recover_ingestion(run_id: str) -> None:
    run = _row(run_id)
    with changes.project_lock(run["project_id"]):
        if run["status"] not in {"preparing", "applying"}:
            return
        plan = json.loads(run["plan_json"])
        if run["status"] == "applying" and plan:
            changes.recover_changes(run_id)
            receipts = {item["rel_path"]: item for item in changes.list_changes(run_id, run["project_id"])}
            all_written = all(receipts.get(item["path"], {}).get("status") == "applied"
                              and changes.file_state(run["project_id"], item["path"])["content"] == item["after"]
                              for item in plan["files"])
            try:
                _assert_source(run)
                all_written = all_written and summary_state(run["project_id"])["content"] == plan["summary"]["after"]
            except Exception:
                all_written = False
            if all_written:
                _finish(run, plan)
                return
        _rollback(run, plan)


def claim(project_id: int, chapter_rel: str, use_ai: bool, revision: str) -> tuple[dict | None, dict | None, str]:
    ensure_schema()
    with changes.project_lock(project_id):
        row, _, text = _source(project_id, chapter_rel)
        with db.get_conn() as conn:
            pending = conn.execute("SELECT * FROM ingestion_runs WHERE book_key=? AND chapter_rel=?"
                                   " AND status IN ('preparing','applying')",
                                   (row["knowledge_key"], chapter_rel)).fetchone()
        if pending:
            with _GUARD:
                live = _active_key(pending["run_id"]) in _ACTIVE
            if live:
                raise changes.FileConflictError("本章正在提取，请等待本次操作完成", path=chapter_rel)
            recover_ingestion(pending["run_id"])
        previous = latest(project_id, chapter_rel)
        digest = changes.content_hash(text)
        if (previous and previous["source_hash"] == digest and previous["revision"] == revision
                and bool(previous["use_ai"]) == bool(use_ai)):
            result = json.loads(previous["result_json"])
            result.update(replayed=True, timeline_added=0, ledger_added=0, foreshadow_added=0,
                          memory_added=0, proposals_created=[], proposal_ids=[])
            # Replaying creates nothing, but retained/rebased pending candidates
            # must remain reachable from the extraction report.
            with db.get_conn() as conn:
                for item in conn.execute("SELECT id,payload FROM proposals WHERE project_id=?"
                                         " AND kind='state' AND status='pending'", (project_id,)).fetchall():
                    payload = json.loads(item["payload"])
                    origin = (payload.get("meta") or {}).get("ingestion") or {}
                    if origin.get("run_id") == previous["run_id"]:
                        result["proposal_ids"].append(item["id"])
            result["counts"] = {name: {"added": 0, "updated": 0, "skipped": len(items), "rejected": 0}
                                for name, items in json.loads(previous["plan_json"]).get("records", {}).items()}
            return None, result, text
        run_id = "ingest:" + uuid.uuid4().hex
        with db.get_conn() as conn:
            conn.execute("INSERT INTO ingestion_runs(run_id,project_id,book_key,chapter_rel,source_hash,revision,use_ai,status)"
                         " VALUES (?,?,?,?,?,?,?,'preparing')",
                         (run_id, project_id, row["knowledge_key"], chapter_rel, digest, revision, int(use_ai)))
        with _GUARD:
            _ACTIVE.add(_active_key(run_id))
        return _row(run_id), None, text


def commit_ingestion(run: dict, plan: dict) -> dict:
    with changes.project_lock(run["project_id"]):
        _assert_source(run)
        with db.get_conn() as conn:
            conn.execute("UPDATE ingestion_runs SET status='applying',plan_json=? WHERE run_id=?",
                         (_json(plan), run["run_id"]))
        for item in plan["files"]:
            _assert_source(run)
            changes.apply_change(run["project_id"], run_id=run["run_id"], session_id=None,
                tool_call_id=item["path"], operation="rewrite" if item["before"] is not None else "create",
                rel_path=item["path"], expected_hash=changes.content_hash(item["before"]), content=item["after"])
        _assert_source(run)
        _write_summary(run["project_id"], plan["summary"])
        _assert_source(run)
        return _finish(run, plan)


def abandon(run: dict) -> None:
    current = _row(run["run_id"])
    with changes.project_lock(run["project_id"]):
        if current["status"] in {"preparing", "applying"}:
            _rollback(current, json.loads(current["plan_json"]))


def release(run: dict) -> None:
    with _GUARD:
        _ACTIVE.discard(_active_key(run["run_id"]))


def rebind_chapter(project_id: int, old_rel: str, new_rel: str) -> None:
    """Called by the chapter path migration; title-only edits need no rebinding."""
    with changes.project_lock(project_id), db.get_conn() as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ingestion_runs'").fetchone():
            return
        runs = conn.execute("SELECT * FROM ingestion_runs WHERE project_id=? AND chapter_rel=?",
                            (project_id, old_rel)).fetchall()
        from .ingestion_records import _id
        for value in runs:
            run = dict(value)
            plan, result = json.loads(run["plan_json"]), json.loads(run["result_json"])
            for entries in plan.get("records", {}).values():
                for entry in entries:
                    track = next(key for key, items in plan["records"].items() if entries is items)
                    entry["key"] = _id(run["book_key"], new_rel, track, entry["subject"], entry.get("field"),
                                       entry["value"], entry.get("event_quote", entry["evidence"]), entry.get("occurrence", 0))
            for track, projection in plan.get("projections", {}).items():
                projection["records"] = plan.get("records", {}).get(track, [])
            if plan.get("state_proposal"):
                meta = plan["state_proposal"]["meta"]
                meta["chapter"] = new_rel
                meta["ingestion"]["chapter_rel"] = new_rel
            if plan.get("result"):
                plan["result"]["chapter_rel"] = new_rel
            result["chapter_rel"] = new_rel
            conn.execute("UPDATE ingestion_runs SET chapter_rel=?,revision=?,plan_json=?,result_json=? WHERE id=?",
                         (new_rel, run["revision"] + ":path-rebound", _json(plan), _json(result), run["id"]))
        if runs:
            for proposal in conn.execute("SELECT id,payload FROM proposals WHERE project_id=? AND kind='state'",
                                         (project_id,)).fetchall():
                payload = json.loads(proposal["payload"])
                meta = payload.get("meta") or {}
                if (meta.get("ingestion") or {}).get("chapter_rel") == old_rel:
                    meta["ingestion"]["chapter_rel"] = new_rel
                    meta["chapter"] = new_rel
                    conn.execute("UPDATE proposals SET payload=? WHERE id=?", (_json(payload), proposal["id"]))
            # Context reads canonical chapter paths too. Preserve every other
            # summary and refuse to silently merge two different manuscripts.
            summary = summary_state(project_id)
            if summary["content"]:
                data = json.loads(summary["content"])
                if not isinstance(data, dict):
                    raise InvalidOperationError("历史摘要格式损坏，无法迁移章节引用")
                if old_rel in data:
                    if new_rel in data and data[new_rel] != data[old_rel]:
                        raise changes.FileConflictError("目标章节已有不同摘要，无法合并", path=new_rel)
                    data[new_rel] = data.pop(old_rel)
                    _write_summary(project_id, {"before": summary["content"], "after":
                        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"})
