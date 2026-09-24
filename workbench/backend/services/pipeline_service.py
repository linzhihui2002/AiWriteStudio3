"""章节生产 8 步循环（Task 25）：LOAD → CHECK → CONTRACT → DRAFT → REVIEW →
REVISE → UPDATE → DECIDE，每步 checkpoint，支持中断后从精确步恢复。

纪律
----
- **CHECK 不过不生产**：前置依赖（大纲冻结 / 合同冻结）缺失即阻断并给提示；
- **DRAFT 产物不直接进正文**：先落 ``.workbench/drafts/``，再进 Proposal 收件箱；
- **REVIEW 双门禁**：确定性硬门 + 模型审稿；未过项生成修改指令回灌 DRAFT；
- **REVISE 最多 2 轮**，超轮转人工（不无限重试）；
- **UPDATE**：应用收件箱 → 摄取四件套；**DECIDE**：状态流转 + 质量债登记；
- 每步 checkpoint 落 ``.workbench/logs/pipeline/``，重启后可 resume。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from .. import config
from . import (
    contract_service,
    ingestion_service,
    operation_log,
    proposal_service,
    review_service,
)
from .context_service import assemble, record_assembly, to_messages
from .chapter_service import require_chapter_path
from .errors import InvalidOperationError, NodeNotFoundError
from .fs_utils import atomic_write_text, read_text, split_frontmatter
from .project_service import get_project_dir
from . import generation_service, prompt_registry_service

STEPS: tuple[str, ...] = (
    "LOAD", "CHECK", "CONTRACT", "DRAFT", "REVIEW", "REVISE", "UPDATE", "DECIDE",
)
STEP_LABELS = {
    "LOAD": "载入上下文",
    "CHECK": "前置依赖门控",
    "CONTRACT": "生成/冻结合同",
    "DRAFT": "生成正文草稿",
    "REVIEW": "双门禁审稿",
    "REVISE": "修改回灌",
    "UPDATE": "应用落盘与摄取",
    "DECIDE": "状态流转与质量债",
}


def pipeline_dir(project_id: int) -> Path:
    directory = config.logs_dir() / "pipeline" / str(project_id)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def checkpoint_path(project_id: int, chapter_rel: str) -> Path:
    name = Path(chapter_rel).name.removesuffix(".md") or "chapter"
    return pipeline_dir(project_id) / f"{name}.json"


def load_checkpoint(project_id: int, chapter_rel: str) -> dict:
    require_chapter_path(project_id, chapter_rel)
    path = checkpoint_path(project_id, chapter_rel)
    if not path.is_file():
        return {"project_id": project_id, "chapter_rel": chapter_rel, "steps": {}}
    try:
        data = json.loads(read_text(path))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {"project_id": project_id, "chapter_rel": chapter_rel, "steps": {}}
    return data if isinstance(data, dict) else {"project_id": project_id,
                                                "chapter_rel": chapter_rel, "steps": {}}


def save_checkpoint(project_id: int, chapter_rel: str, data: dict) -> Path:
    path = checkpoint_path(project_id, chapter_rel)
    data["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    return path


def list_checkpoints(project_id: int) -> list[dict]:
    directory = pipeline_dir(project_id)
    entries: list[dict] = []
    for path in sorted(directory.glob("*.json"), reverse=True):
        try:
            entries.append(json.loads(read_text(path)))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
    return entries


def _mark(data: dict, step: str, status: str, *, detail: dict | None = None) -> None:
    steps = data.setdefault("steps", {})
    entry = steps.setdefault(step, {})
    entry["status"] = status
    entry["at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    if detail:
        entry["detail"] = detail


def _first_incomplete(data: dict) -> str:
    for step in STEPS:
        status = (data.get("steps", {}).get(step) or {}).get("status")
        if status != "done":
            return step
    return STEPS[-1]


def run_pipeline(
    project_id: int,
    chapter_rel: str,
    *,
    resume: bool = True,
    auto_freeze_contract: bool = True,
    use_ai: bool = True,
    auto_apply: bool = True,
    word_budget: int | None = None,
    should_cancel=None,
) -> dict:
    """跑通 8 步循环（阻塞）。返回含每步状态与产物的结果字典。"""
    require_chapter_path(project_id, chapter_rel)
    row, project_dir = get_project_dir(project_id)
    data = load_checkpoint(project_id, chapter_rel)
    if not resume:
        data = {"project_id": project_id, "chapter_rel": chapter_rel, "steps": {}}
    data.setdefault("steps", {})
    data["status"] = "running"
    data["started_at"] = data.get("started_at") or time.strftime("%Y-%m-%dT%H:%M:%S")

    start_step = _first_incomplete(data) if resume else "LOAD"
    started_index = STEPS.index(start_step)
    events: list[dict] = []
    draft_path = config.runtime_dir() / "drafts" / f"{project_id}-{Path(chapter_rel).stem}.md"
    draft_path.parent.mkdir(parents=True, exist_ok=True)

    def cancelled() -> bool:
        return bool(should_cancel and should_cancel())

    try:
        for step in STEPS[started_index:]:
            if cancelled():
                data["status"] = "paused"
                _mark(data, step, "pending")
                save_checkpoint(project_id, chapter_rel, data)
                return {"project_id": project_id, "chapter_rel": chapter_rel,
                        "status": "paused", "resumed_from": start_step,
                        "steps": data["steps"], "events": events}

            _mark(data, step, "running")
            save_checkpoint(project_id, chapter_rel, data)

            if step == "LOAD":
                context = assemble(project_id, chapter_rel=chapter_rel,
                                   query="章节情节点与状态")
                record_assembly(project_id, chapter_rel, context)
                data["context_tokens"] = context["total_tokens"]
                events.append({"step": step, "tokens": context["total_tokens"],
                               "blocks": len(context["blocks"])})
                _mark(data, step, "done", detail={"tokens": context["total_tokens"]})

            elif step == "CHECK":
                gate = contract_service.check_prerequisites(project_id, chapter_rel)
                events.append({"step": step, "blocked": gate["blocked"],
                               "reasons": gate["reasons"]})
                if gate["blocked"]:
                    _mark(data, step, "failed", detail=gate)
                    data["status"] = "blocked"
                    save_checkpoint(project_id, chapter_rel, data)
                    return {"project_id": project_id, "chapter_rel": chapter_rel,
                            "status": "blocked", "steps": data["steps"], "events": events,
                            "blocked_reasons": gate["reasons"]}
                _mark(data, step, "done", detail={"checks": [c["key"] for c in gate["checks"]]})

            elif step == "CONTRACT":
                try:
                    contract = contract_service.get_contract(project_id, chapter_rel)
                except NodeNotFoundError:
                    contract = contract_service.generate_contract(
                        project_id, chapter_rel, use_ai=use_ai, word_budget=word_budget
                    )
                data["contract_source"] = contract.get("source", "author")
                if contract.get("status") != "frozen" and auto_freeze_contract:
                    contract = contract_service.freeze_contract(project_id, chapter_rel)
                events.append({"step": step, "hook": contract.get("hook_type"),
                               "budget": contract.get("word_budget"),
                               "frozen": contract.get("status") == "frozen"})
                _mark(data, step, "done", detail={"budget": contract.get("word_budget"),
                                                  "hook": contract.get("hook_type")})

            elif step == "DRAFT":
                ready = contract_service.check_contract_ready(project_id, chapter_rel)
                if not ready["ok"]:
                    detail = {"reason": "合同未冻结", "hint": ready["hint"]}
                    _mark(data, step, "failed", detail=detail)
                    data["status"] = "blocked"
                    save_checkpoint(project_id, chapter_rel, data)
                    events.append({"step": step, "blocked": True, "hint": ready["hint"]})
                    return {"project_id": project_id, "chapter_rel": chapter_rel,
                            "status": "blocked", "steps": data["steps"], "events": events,
                            "blocked_reasons": [ready["hint"]]}

                draft = _draft(project_id, chapter_rel, draft_path, data, use_ai=use_ai)
                events.append({"step": step, **draft})
                if not draft.get("ok"):
                    _mark(data, step, "failed", detail=draft)
                    data["status"] = "failed"
                    save_checkpoint(project_id, chapter_rel, data)
                    return {"project_id": project_id, "chapter_rel": chapter_rel,
                            "status": "failed", "steps": data["steps"], "events": events,
                            "error": draft}
                data["draft_path"] = str(draft_path)
                data["proposal_id"] = draft["proposal_id"]
                _mark(data, step, "done", detail={"proposal_id": draft["proposal_id"],
                                                  "chars": draft["chars"]})

            elif step == "REVIEW":
                review = review_service.review_chapter(
                    project_id, chapter_rel, use_ai=use_ai, register_debt=False
                ) if _has_content(project_id, chapter_rel) else None
                draft_text = _read_text(draft_path)
                hard = review_service.run_hard_gates(draft_text)
                review_payload = {
                    "verdict": "通过" if hard["passed"] and (review is None
                                                            or review["verdict"] == "通过")
                    else "不通过",
                    "hard_gates": hard,
                    "ai_review": review,
                    "round": data.get("revise_round", 0),
                }
                data["last_review"] = review_payload
                events.append({"step": step, "verdict": review_payload["verdict"],
                               "blocking": hard["blocking_gates"]})
                _mark(data, step, "done",
                      detail={"verdict": review_payload["verdict"],
                              "blocking": hard["blocking_gates"]})

            elif step == "REVISE":
                last = data.get("last_review") or {}
                rounds = int(data.get("revise_round", 0))
                if last.get("verdict") == "通过":
                    _mark(data, step, "done", detail={"rounds": rounds, "skipped": True})
                    events.append({"step": step, "skipped": True})
                elif rounds >= review_service.MAX_REVISE_ROUNDS:
                    _mark(data, step, "failed",
                          detail={"rounds": rounds, "reason": "超过 2 轮，转人工"})
                    data["status"] = "manual"
                    data["manual_reason"] = "审稿连续 2 轮未通过，转人工处理"
                    save_checkpoint(project_id, chapter_rel, data)
                    return {"project_id": project_id, "chapter_rel": chapter_rel,
                            "status": "manual", "steps": data["steps"], "events": events,
                            "manual_reason": data["manual_reason"]}
                else:
                    data["revise_round"] = rounds + 1
                    _mark(data, step, "done", detail={"rounds": rounds + 1,
                                                      "instructions": _instructions(last)})
                    events.append({"step": step, "round": rounds + 1})
                    # 回灌：重跑 DRAFT（清空后续步骤状态）
                    for later in ("DRAFT", "REVIEW", "REVISE"):
                        data["steps"][later] = {"status": "pending"}
                    data["status"] = "running"
                    save_checkpoint(project_id, chapter_rel, data)
                    return run_pipeline(
                        project_id, chapter_rel, resume=True,
                        auto_freeze_contract=auto_freeze_contract, use_ai=use_ai,
                        auto_apply=auto_apply, word_budget=word_budget,
                        should_cancel=should_cancel,
                    )

            elif step == "UPDATE":
                proposal_id = data.get("proposal_id")
                applied = None
                if auto_apply and proposal_id:
                    try:
                        applied = proposal_service.apply_proposal(int(proposal_id))
                    except Exception as exc:  # noqa: BLE001 - 已应用/冲突等
                        applied = {"error": str(exc)}
                ingested = None
                if applied and not applied.get("error"):
                    ingested = ingestion_service.ingest_chapter(
                        project_id, chapter_rel, use_ai=use_ai
                    )
                events.append({"step": step, "applied": bool(applied and not applied.get("error")),
                               "ingested": bool(ingested)})
                _mark(data, step, "done",
                      detail={"applied": bool(applied), "ingest": ingested})

            elif step == "DECIDE":
                debt = None
                last = data.get("last_review") or {}
                if last.get("verdict") != "通过":
                    debt = review_service.register_quality_debt(
                        project_id, chapter_rel,
                        {"hard_gates": last.get("hard_gates") or {},
                         "counts": (last.get("ai_review") or {}).get("counts") or {},
                         "consistency": (last.get("ai_review") or {}).get("consistency") or []},
                    )
                status = "完成" if last.get("verdict") == "通过" else "草稿"
                try:
                    from . import chapter_service

                    chapter_service.save_chapter(
                        project_id, chapter_rel,
                        read_text(Path(project_dir) / chapter_rel), status=status,
                    )
                except Exception:  # noqa: BLE001 - 状态流转失败不阻断
                    pass
                events.append({"step": step, "chapter_status": status, "debt": debt})
                _mark(data, step, "done", detail={"status": status, "debt": debt})

            save_checkpoint(project_id, chapter_rel, data)

    except Exception as exc:  # noqa: BLE001 - 统一落盘后抛出
        data["status"] = "failed"
        data["error"] = str(exc)
        save_checkpoint(project_id, chapter_rel, data)
        raise

    data["status"] = "done"
    save_checkpoint(project_id, chapter_rel, data)
    operation_log.log(project_id, "pipeline-done", chapter_rel,
                      {"events": [event.get("step") for event in events]})
    return {
        "project_id": project_id,
        "chapter_rel": chapter_rel,
        "status": "done",
        "resumed_from": start_step,
        "steps": data["steps"],
        "events": events,
        "proposal_id": data.get("proposal_id"),
        "review": data.get("last_review"),
        "revise_rounds": int(data.get("revise_round", 0)),
        "project_name": row["name"],
    }


def _has_content(project_id: int, chapter_rel: str) -> bool:
    try:
        _row, project_dir = get_project_dir(project_id)
        text = read_text(Path(project_dir) / chapter_rel)
    except (NodeNotFoundError, OSError, UnicodeDecodeError):
        return False
    return bool(split_frontmatter(text)[1].strip())


def _instructions(review: dict) -> list[str]:
    ai = review.get("ai_review") or {}
    instructions = list(ai.get("revise_instructions") or [])
    hard = review.get("hard_gates") or {}
    for gate in hard.get("gates", []):
        if gate.get("blocking"):
            instructions.append(f"硬门禁未过（{gate['key']}）：{gate.get('detail', '')[:200]}")
    if not instructions:
        instructions.append("按审稿意见逐项修订后重跑")
    return instructions[:8]


def _draft(project_id: int, chapter_rel: str, draft_path: Path, data: dict,
           *, use_ai: bool) -> dict:
    """DRAFT 步：生成草稿（含修改指令回灌）→ 写草稿文件 → 建 Proposal。"""
    prompt = prompt_registry_service.get_prompt("novel.chapter.draft")
    context = assemble(project_id, chapter_rel=chapter_rel, query="章节情节点")

    contract = {}
    try:
        contract = contract_service.get_contract(project_id, chapter_rel)
    except NodeNotFoundError:
        contract = {}

    system = prompt_registry_service.render(
        prompt["prompt_id"], {"字数预算": contract.get("word_budget")
                             or config.DEFAULT_WORD_BUDGET}
    )["body"]
    _sys, messages = to_messages(context, system=system)

    instructions: list[str] = []
    if int(data.get("revise_round", 0)) > 0 and data.get("last_review"):
        instructions = _instructions(data["last_review"])
    if instructions:
        messages.append(
            {"role": "user", "content": "【上一轮审稿意见（必须逐条修正）】\n"
             + "\n".join(f"- {item}" for item in instructions)}
        )

    if draft_path.exists():
        draft_path.unlink()

    result = generation_service.run_task(
        project_id=project_id,
        task_type="章节正文",
        system=system,
        messages=messages,
        output_path=draft_path,
        prompt_id=prompt["prompt_id"],
        prompt_version=prompt["version"],
        context_snapshot={"chapter": chapter_rel, "tokens": context["total_tokens"],
                          "revise_round": int(data.get("revise_round", 0))},
        temperature=0.85,
    )

    text = ""
    if draft_path.is_file():
        text = read_text(draft_path)
    if not text.strip():
        text = str(result.get("text") or "")
        if text.strip():
            atomic_write_text(draft_path, text)

    if not result["ok"] or not text.strip():
        return {
            "ok": False,
            "task_id": result.get("task_id"),
            "engine": result.get("engine"),
            "error_code": result.get("error_code"),
            "error_message": result.get("error_message") or "草稿为空",
        }

    body = split_frontmatter(text)[1].strip()
    proposal = proposal_service.create_proposal(
        project_id=project_id,
        kind="chapter_draft",
        title=f"{Path(chapter_rel).stem} 草稿（{len(body)} 字）",
        target_path=chapter_rel,
        content=body,
        task_id=result.get("task_id"),
        meta={
            "engine": result.get("engine"),
            "model": result.get("model"),
            "draft_path": str(draft_path),
            "revise_round": int(data.get("revise_round", 0)),
        },
    )
    return {
        "ok": True,
        "task_id": result.get("task_id"),
        "engine": result.get("engine"),
        "proposal_id": proposal["id"],
        "chars": len(body),
        "draft_path": str(draft_path),
    }


def _read_text(path: Path) -> str:
    try:
        return read_text(path)
    except (OSError, UnicodeDecodeError):
        return ""


__all__ = [
    "STEP_LABELS",
    "STEPS",
    "checkpoint_path",
    "list_checkpoints",
    "load_checkpoint",
    "pipeline_dir",
    "run_pipeline",
    "save_checkpoint",
]
