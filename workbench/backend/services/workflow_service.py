"""工作流引擎（Task 42 / FR-AWS-02）：可视化编排定义 + 批量产章 + 断点续跑。

为什么自建（见 spec §dsh 能力重叠判断）：dsh 的 workflow 是「模型编写的脚本式编排」，
**无日志化 / 无检查点 / 进程重启无法继续 / 无保存复用**，不能满足「拖拽编排 + 批量产章 +
暂停恢复」的需求，因此工作台自建工作流引擎，底层执行仍调 headless / direct-api。

- 定义落盘 ``workflows/<name>.json``（可版本化）；内置默认八步流程不可删；
- 运行态落 ``workflow_runs`` 表（检查点 = ``cursor`` + ``payload``），支持暂停/恢复/断点续跑；
- 已完成的章节不重复生成（按 run.payload.completed 记录）。
"""

from __future__ import annotations

import json
import hashlib
import threading
import time
from pathlib import Path

from .. import db
from . import generation_service, operation_log, pipeline_service, proposal_service
from .errors import InvalidNameError, InvalidOperationError, NodeNotFoundError, ServiceError
from .fs_utils import atomic_write_text, read_text
from .project_service import get_project_dir

DEFAULT_WORKFLOW_NAME = "默认八步产章"
NODE_TYPES = (
    "LOAD", "CHECK", "CONTRACT", "DRAFT", "REVIEW", "REVISE", "UPDATE", "DECIDE",
    "大纲生成", "章节正文", "审稿", "去AI味软审", "结构化抽取", "润色", "拆书",
)
STEP_NODES = ("LOAD", "CHECK", "CONTRACT", "DRAFT", "REVIEW", "REVISE", "UPDATE", "DECIDE")
_execution_lock = threading.Lock()
_executing_runs: set[tuple[str, int]] = set()


def workflows_root() -> Path:
    from .. import config

    return Path(config.workflows_dir())


# ─────────────────────────── 定义 ───────────────────────────


def default_definition() -> dict:
    """内置默认流程：对齐 8 步循环（LOAD→…→DECIDE）。"""
    nodes = [
        {
            "id": step.lower(),
            "type": step,
            "label": pipeline_service.STEP_LABELS.get(step, step),
            "engine": "dsh-headless" if step in ("DRAFT", "REVIEW", "CONTRACT") else "",
            "model": "",
            "skill": "novel-review" if step == "REVIEW" else (
                "human-linguistics" if step == "REVISE" else ""),
            "next": STEP_NODES[index + 1].lower() if index + 1 < len(STEP_NODES) else "",
        }
        for index, step in enumerate(STEP_NODES)
    ]
    return {
        "name": DEFAULT_WORKFLOW_NAME,
        "description": "工作台内置：章节生产八步循环（LOAD→CHECK→CONTRACT→DRAFT→REVIEW→REVISE→UPDATE→DECIDE）",
        "nodes": nodes,
        "batch": {"chapters": [], "stop_on_failure": False},
        "is_builtin": True,
        "version": 1,
    }


def ensure_builtin_workflow() -> dict:
    root = workflows_root()
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{DEFAULT_WORKFLOW_NAME}.json"
    definition = default_definition()
    if not path.exists():
        atomic_write_text(path, json.dumps(definition, ensure_ascii=False, indent=2) + "\n")
    _register(definition)
    return {**definition, "path": str(path)}


def _register(definition: dict) -> None:
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO workflows (name, definition, is_builtin, enabled)"
            " VALUES (?, ?, ?, 1)"
            " ON CONFLICT(name) DO UPDATE SET definition = excluded.definition,"
            " is_builtin = excluded.is_builtin",
            (definition["name"], json.dumps(definition, ensure_ascii=False),
             1 if definition.get("is_builtin") else 0),
        )


def list_workflows() -> list[dict]:
    ensure_builtin_workflow()
    items: list[dict] = []
    for path in sorted(workflows_root().glob("*.json")):
        try:
            definition = json.loads(read_text(path))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        items.append({**definition, "path": str(path)})
    return items


def get_workflow(name: str) -> dict:
    ensure_builtin_workflow()
    path = workflows_root() / f"{name}.json"
    if not path.is_file():
        raise NodeNotFoundError(f"工作流不存在：{name}")
    try:
        definition = json.loads(read_text(path))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidOperationError(f"工作流定义损坏：{name}") from exc
    return {**definition, "path": str(path)}


def validate_definition(definition: dict) -> dict:
    name = str(definition.get("name") or "").strip()
    if not name or len(name) > 40:
        raise InvalidNameError("工作流名不能为空且不超过 40 字符")
    nodes = definition.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        raise InvalidOperationError("工作流至少需要一个节点")
    seen: set[str] = set()
    cleaned: list[dict] = []
    from .agent_service import RETRIEVAL_PROFILES
    for index, node in enumerate(nodes):
        if not isinstance(node, dict):
            raise InvalidOperationError(f"节点 {index} 格式不正确")
        node_type = str(node.get("type") or "")
        if node_type not in NODE_TYPES:
            raise InvalidOperationError(
                f"未知节点类型：{node_type}（可选：{'、'.join(NODE_TYPES)}）"
            )
        node_id = str(node.get("id") or node_type.lower())
        if node_id in seen:
            raise InvalidOperationError(f"节点 id 重复：{node_id}")
        seen.add(node_id)
        profile = str(node.get("retrieval_profile") or "")
        if profile and profile not in RETRIEVAL_PROFILES:
            raise InvalidOperationError(f"节点 {node_id} 的 retrieval_profile 无效")
        cleaned.append(
            {
                "id": node_id,
                "type": node_type,
                "label": str(node.get("label") or node_type),
                "engine": str(node.get("engine") or ""),
                "model": str(node.get("model") or ""),
                "skill": str(node.get("skill") or ""),
                "next": str(node.get("next") or ""),
                "retrieval_profile": profile,
                "instruction": str(node.get("instruction") or ""),
            }
        )
    for node in cleaned:
        if node["next"] and node["next"] not in seen:
            raise InvalidOperationError(f"节点 {node['id']} 指向了不存在的后继：{node['next']}")
    return {
        "name": name,
        "description": str(definition.get("description") or ""),
        "nodes": cleaned,
        "batch": definition.get("batch") if isinstance(definition.get("batch"), dict)
        else {"chapters": [], "stop_on_failure": False},
        "is_builtin": bool(definition.get("is_builtin")),
        "version": int(definition.get("version") or 1),
    }


def save_workflow(definition: dict) -> dict:
    cleaned = validate_definition(definition)
    path = workflows_root() / f"{cleaned['name']}.json"
    existing = None
    if path.is_file():
        try:
            existing = json.loads(read_text(path))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            existing = None
    if existing is not None and existing.get("is_builtin") and not cleaned.get("is_builtin"):
        raise InvalidOperationError(
            f"「{cleaned['name']}」是内置工作流，不可覆盖；请另存为其他名称（或复制后修改）。"
        )
    if existing is not None:
        cleaned["version"] = int(existing.get("version") or 1) + 1
    else:
        cleaned["version"] = 1
    atomic_write_text(path, json.dumps(cleaned, ensure_ascii=False, indent=2) + "\n")
    _register(cleaned)
    return {**cleaned, "path": str(path)}


def duplicate_workflow(name: str, new_name: str) -> dict:
    source = get_workflow(name)
    definition = {**source, "name": new_name, "is_builtin": False}
    definition.pop("path", None)
    return save_workflow(definition)


def delete_workflow(name: str) -> dict:
    workflow = get_workflow(name)
    if workflow.get("is_builtin"):
        raise InvalidOperationError(
            f"「{name}」是内置工作流，不可删除；可复制后修改。"
        )
    Path(workflow["path"]).unlink(missing_ok=True)
    with db.get_conn() as conn:
        conn.execute("DELETE FROM workflows WHERE name = ?", (name,))
    return {"name": name, "deleted": True}


# ─────────────────────────── 运行 ───────────────────────────


def _chapters_in_range(project_id: int, start: int, end: int) -> list[str]:
    from .chapter_service import list_chapters

    return [
        item["rel_path"] for item in list_chapters(project_id)
        if item["number"] is not None and start <= item["number"] <= end
    ]


def start_run(
    workflow_name: str,
    project_id: int,
    *,
    chapters: list[str] | None = None,
    chapter_range: tuple[int, int] | None = None,
) -> dict:
    """创建运行记录（批量产章：可传章号区间或显式章节列表）。"""
    workflow = get_workflow(workflow_name)
    get_project_dir(project_id)

    targets = list(chapters or [])
    if not targets and chapter_range:
        targets = _chapters_in_range(project_id, int(chapter_range[0]), int(chapter_range[1]))
    if not targets:
        from .chapter_service import list_chapters

        chapters_all = list_chapters(project_id)
        if not chapters_all:
            raise InvalidOperationError("项目下没有章节，无法批量产章")
        targets = [chapters_all[0]["rel_path"]]

    payload = {
        "chapters": targets,
        "chapter_index": 0,
        "node_index": 0,
        "completed": [],
        "workflow": workflow["name"],
        "workflow_definition": {key: value for key, value in workflow.items() if key != "path"},
        "node_checkpoints": {},
        "node_log": [],
    }
    with db.get_conn() as conn:
        cursor = conn.execute(
            "INSERT INTO workflow_runs (workflow_id, project_id, status, cursor, payload, log,"
            " updated_at) VALUES (?, ?, 'pending', 0, ?, '', datetime('now'))",
            (1, project_id, json.dumps(payload, ensure_ascii=False)),
        )
        run_id = int(cursor.lastrowid or 0)
    operation_log.log(project_id, "workflow-start", None,
                      {"run_id": run_id, "workflow": workflow["name"],
                       "chapters": targets})
    return get_run(run_id)


def get_run(run_id: int) -> dict:
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT id, workflow_id, project_id, status, cursor, payload, log,"
            " created_at, updated_at FROM workflow_runs WHERE id = ?",
            (run_id,),
        ).fetchone()
    if row is None:
        raise NodeNotFoundError(f"工作流运行不存在：id={run_id}")
    return _row_to_run(row)


def list_runs(project_id: int | None = None, limit: int = 30) -> list[dict]:
    sql = ("SELECT id, workflow_id, project_id, status, cursor, payload, log,"
           " created_at, updated_at FROM workflow_runs")
    params: list = []
    if project_id is not None:
        sql += " WHERE project_id = ?"
        params.append(project_id)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(int(limit))
    with db.get_conn() as conn:
        rows = conn.execute(sql, tuple(params)).fetchall()
    return [_row_to_run(row) for row in rows]


def _row_to_run(row) -> dict:
    def _loads(text, fallback):
        try:
            return json.loads(text) if text else fallback
        except (json.JSONDecodeError, TypeError):
            return fallback

    payload = _loads(row["payload"], {})
    return {
        "id": int(row["id"]),
        "project_id": row["project_id"],
        "status": row["status"],
        "cursor": int(row["cursor"] or 0),
        "payload": payload,
        "log": _loads(row["log"], []),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "progress": {
            "chapter_index": int(payload.get("chapter_index", 0)),
            "chapters_total": len(payload.get("chapters") or []),
            "node_index": int(payload.get("node_index", 0)),
            "completed": payload.get("completed") or [],
        },
    }


def _update_run(run_id: int, *, status: str | None = None, payload: dict | None = None,
                log_entry: dict | None = None) -> dict:
    with db.get_conn() as conn:
        fields = ["updated_at = datetime('now')"]
        params: list = []
        if status:
            fields.append("status = ?")
            params.append(status)
        if payload is not None:
            fields.append("payload = ?")
            params.append(json.dumps(payload, ensure_ascii=False))
        if log_entry is not None:
            row = conn.execute("SELECT log FROM workflow_runs WHERE id = ?",
                               (run_id,)).fetchone()
            try:
                existing = json.loads(row["log"]) if row and row["log"] else []
            except (json.JSONDecodeError, TypeError):
                existing = []
            existing.append(log_entry)
            fields.append("log = ?")
            params.append(json.dumps(existing[-200:], ensure_ascii=False))
        params.append(run_id)
        conn.execute(f"UPDATE workflow_runs SET {', '.join(fields)} WHERE id = ?",
                     tuple(params))
    return get_run(run_id)


def pause_run(run_id: int) -> dict:
    run = get_run(run_id)
    if run["status"] not in ("running", "pending", "paused"):
        raise InvalidOperationError(f"当前状态不可暂停：{run['status']}")
    return _update_run(run_id, status="paused")


def _node_log(run_id: int, entry: dict) -> None:
    _update_run(run_id, log_entry={**entry, "at": time.strftime("%Y-%m-%dT%H:%M:%S")})


def recover_interrupted() -> None:
    """Expose stale startup records for explicit continuation; never execute."""
    from .. import config
    with _execution_lock, db.get_conn() as conn:
        live = {run_id for database, run_id in _executing_runs if database == str(config.DB_PATH)}
        rows = conn.execute("SELECT id FROM workflow_runs WHERE status='running'").fetchall()
        for row in rows:
            if row["id"] not in live:
                conn.execute("UPDATE workflow_runs SET status='interrupted',updated_at=datetime('now')"
                             " WHERE id=? AND status='running'", (row["id"],))


def execute_run(run_id: int, *, should_cancel=None, use_ai: bool = True) -> dict:
    """执行/续跑工作流（阻塞）：逐章逐节点推进，每步落检查点，可暂停/断点续跑。"""
    from .. import config
    key = (str(config.DB_PATH), int(run_id))
    with _execution_lock:
        if key in _executing_runs:
            raise InvalidOperationError("这个工作流正在执行，请等待当前执行结束")
        _executing_runs.add(key)
    try:
        return _execute_run(run_id, should_cancel=should_cancel, use_ai=use_ai)
    finally:
        with _execution_lock:
            _executing_runs.discard(key)


def _execute_run(run_id: int, *, should_cancel=None, use_ai: bool = True) -> dict:
    run = get_run(run_id)
    if run["status"] not in ("pending", "running", "paused", "failed", "interrupted"):
        raise InvalidOperationError(f"当前状态不可执行：{run['status']}")

    payload = run["payload"]
    workflow = payload.get("workflow_definition") or get_workflow(payload.get("workflow") or DEFAULT_WORKFLOW_NAME)
    payload.setdefault("workflow_definition", {key: value for key, value in workflow.items() if key != "path"})
    _update_run(run_id, status="running", payload=payload)

    chapters: list[str] = payload.get("chapters") or []
    # Legacy failures advanced the cursor before marking the chapter complete.
    # The completed set, rather than that cursor, is the recovery authority.
    completed = set(payload.get("completed") or [])
    start_chapter = next((index for index, chapter in enumerate(chapters) if chapter not in completed), len(chapters))

    def cancelled():
        return bool((should_cancel and should_cancel()) or get_run(run_id)["status"] == "paused")

    for chapter_index in range(start_chapter, len(chapters)):
        chapter_rel = chapters[chapter_index]
        if chapter_rel in (payload.get("completed") or []):
            continue
        payload["chapter_index"] = chapter_index
        if cancelled():
            _update_run(run_id, status="paused", payload=payload)
            return get_run(run_id)

        checkpoints = payload.setdefault("node_checkpoints", {}).setdefault(chapter_rel, {})
        def checkpoint_node(index, node_id, receipt):
            checkpoints[node_id] = receipt
            payload["node_index"] = index + 1 if receipt.get("ok") else index
            _update_run(run_id, payload=payload)
        try:
            step_result = _run_chapter(workflow, run["project_id"], chapter_rel,
                use_ai=use_ai, should_cancel=cancelled, checkpoints=checkpoints, checkpoint_node=checkpoint_node)
        except Exception as error:
            step_result = {"ok": False, "error": str(error)}
        if step_result.get("ok"):
            payload.setdefault("completed", []).append(chapter_rel)
            payload["chapter_index"] = chapter_index + 1
            payload["node_index"] = len(workflow["nodes"])
        _node_log(run_id, {"chapter": chapter_rel, **step_result})

        if step_result.get("status") == "paused" or cancelled():
            _update_run(run_id, status="paused", payload=payload)
            return get_run(run_id)
        if not step_result.get("ok") and workflow.get("batch", {}).get("stop_on_failure"):
            _update_run(run_id, status="failed", payload=payload)
            return get_run(run_id)

        _update_run(run_id, payload=payload)

    missing = [chapter for chapter in chapters if chapter not in (payload.get("completed") or [])]
    if missing:
        payload["chapter_index"] = chapters.index(missing[0])
        return _update_run(run_id, status="failed", payload=payload)
    _update_run(run_id, status="done", payload=payload)
    operation_log.log(run["project_id"], "workflow-done", None,
                      {"run_id": run_id, "completed": payload.get("completed")})
    return get_run(run_id)


def _run_chapter(workflow: dict, project_id: int, chapter_rel: str, *,
                 use_ai: bool, should_cancel=None, checkpoints: dict | None = None, checkpoint_node=None) -> dict:
    """按工作流节点执行单章：八步节点交给 pipeline，其他节点走通用生成。"""
    node_types = [node["type"] for node in workflow["nodes"]]
    is_pipeline = all(node in STEP_NODES for node in node_types)

    if is_pipeline:
        try:
            result = pipeline_service.run_pipeline(
                project_id, chapter_rel, resume=True, use_ai=use_ai,
                auto_apply=True, should_cancel=should_cancel,
            )
        except Exception as exc:  # noqa: BLE001 - 记录失败但不炸整批
            return {"ok": False, "error": str(exc)}
        return {
            "ok": result["status"] == "done",
            "status": result["status"],
            "steps": {name: (data or {}).get("status")
                      for name, data in (result.get("steps") or {}).items()},
            "proposal_id": result.get("proposal_id"),
        }

    # 自定义流程：逐节点生成，产物进收件箱
    produced: list[dict] = []
    for index, node in enumerate(workflow["nodes"]):
        if should_cancel and should_cancel():
            return {"ok": False, "status": "paused", "nodes": produced}
        node_hash = hashlib.sha256(json.dumps(node, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        saved = (checkpoints or {}).get(node["id"])
        if saved and saved.get("ok"):
            try:
                proposal = proposal_service.get_proposal(saved["proposal_id"])
                digest = hashlib.sha256(proposal["content"].encode("utf-8")).hexdigest()
                valid = (saved.get("node_hash") == node_hash and proposal["project_id"] == project_id
                    and saved.get("content_hash") == digest and proposal.get("status") not in {"discarded", "rejected"})
            except (KeyError, ServiceError):
                valid = False
            if not valid:
                return {"ok": False, "error": f"节点「{node['label']}」的既有提案已更改或移除，请核对后新建运行。", "nodes": produced}
            produced.append({**saved, "reused": True})
            continue
        from .context_service import assemble, to_messages

        from . import agent_service, skill_service
        profile = node.get("retrieval_profile") or (
            "off" if node["type"] == "润色" else agent_service.retrieval_profile_for_task(
                node["type"], skill=node.get("skill", "")))
        context = assemble(project_id, chapter_rel=chapter_rel,
                           query=node.get("instruction", ""), retrieval_profile=profile)
        system, messages = to_messages(context)
        selected_skill = str(node.get("skill") or "").strip()
        if selected_skill:
            compiled = skill_service.compile_skills([selected_skill])
            if compiled["unavailable"]:
                return {"ok": False, "nodes": produced,
                        "error": f"节点「{node['label']}」的技能不可用：{compiled['unavailable'][0]['error']}"}
            system = "\n\n".join(part for part in (system, compiled["text"]) if part)
        # 前序提案是该运行已核验的实际产物，续跑与首次执行使用相同交接材料。
        # 节点指令必须进入生成请求，不能仅作为检索 query。
        messages = list(messages)
        for item in produced:
            previous = proposal_service.get_proposal(item["proposal_id"])
            messages.append({"role": "user", "content":
                             f"前序节点已保存提案：{previous['title']}\n{previous['content']}"})
        messages.append({"role": "user", "content":
                         f"执行当前工作流节点「{node['label']}」。当前章节：{chapter_rel}。\n"
                         f"{node.get('instruction') or '按节点类型完成任务。'}\n"
                         "返回本节点的完整产物，供作者在收件箱中审核。"})
        result = generation_service.run_task(
            project_id=project_id,
            task_type=node["type"] if node["type"] in NODE_TYPES else "章节正文",
            system=system,
            messages=messages,
            engine=node.get("engine", ""),
            model=node.get("model", ""),
            agent="",
            context_snapshot={"chapter": chapter_rel, "node": node["id"]},
            should_cancel=should_cancel,
        )
        if should_cancel and should_cancel():
            return {"ok": False, "status": "paused", "nodes": produced}
        proposal_id = None
        if result["ok"] and result.get("text"):
            proposal = proposal_service.create_proposal(
                project_id=project_id,
                kind="chapter_draft" if node["type"] in ("DRAFT", "章节正文") else "other",
                title=f"{Path(chapter_rel).stem} · {node['label']}",
                target_path=chapter_rel if node["type"] in ("DRAFT", "章节正文") else
                f"大纲/{node['label']}.md",
                content=result["text"],
                task_id=result.get("task_id"),
                meta={"node": node["id"], "engine": result.get("engine")},
            )
            proposal_id = proposal["id"]
        receipt = {"node": node["id"], "node_hash": node_hash, "ok": bool(result["ok"] and proposal_id),
                   "proposal_id": proposal_id, "content_hash": hashlib.sha256(result.get("text", "").encode("utf-8")).hexdigest(),
                   "error": result.get("error_message") or (None if proposal_id else "节点未交付可保存内容")}
        produced.append(receipt)
        if checkpoint_node:
            checkpoint_node(index, node["id"], receipt)
        if not receipt["ok"]:
            return {"ok": False, "nodes": produced, "error": receipt["error"]}
    return {"ok": all(item["ok"] for item in produced) if produced else False,
            "nodes": produced}


__all__ = [
    "DEFAULT_WORKFLOW_NAME",
    "NODE_TYPES",
    "STEP_NODES",
    "default_definition",
    "delete_workflow",
    "duplicate_workflow",
    "ensure_builtin_workflow",
    "execute_run",
    "get_run",
    "get_workflow",
    "list_runs",
    "list_workflows",
    "pause_run",
    "recover_interrupted",
    "save_workflow",
    "start_run",
    "validate_definition",
    "workflows_root",
]
