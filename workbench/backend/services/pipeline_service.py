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
import uuid
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
from .file_change_service import content_hash, file_state, project_lock
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
PIPELINE_PROTOCOL = 2


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
    # Older checkpoints cannot attest which draft their model review inspected.
    # Preserve their draft, but redo the review before any further application.
    if data.get("protocol") != PIPELINE_PROTOCOL and data.get("proposal_id"):
        old_proposal = proposal_service.get_proposal(int(data["proposal_id"]))
        if old_proposal["status"] != "pending":
            data["status"] = "blocked"
            reason = "旧检查点的提案已经处理，无法补验当时的候选稿；请从新候选开始"
            save_checkpoint(project_id, chapter_rel, data)
            return {"project_id": project_id, "chapter_rel": chapter_rel,
                    "status": "blocked", "steps": data["steps"], "events": [],
                    "blocked_reasons": [reason]}
        for pending in ("REVIEW", "REVISE", "UPDATE", "DECIDE"):
            data["steps"][pending] = {"status": "pending"}
        data.pop("last_review", None)
    data["protocol"] = PIPELINE_PROTOCOL
    if (resume and data.get("status") == "manual"
            and (data.get("last_review") or {}).get("ai_review", {}).get("ai_used") is False
            and not (data.get("last_review") or {}).get("ai_review", {}).get("contract_changed")):
        # An unavailable reviewer is recoverable without generating another draft.
        for pending in ("REVIEW", "REVISE"):
            data["steps"][pending] = {"status": "pending"}
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
                                   query="", retrieval_profile="history")
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

                draft = _draft(project_id, chapter_rel, draft_path, data, use_ai=use_ai,
                               should_cancel=should_cancel)
                events.append({"step": step, **draft})
                if not draft.get("ok"):
                    _mark(data, step, "pending" if cancelled() else "failed", detail=draft)
                    data["status"] = "paused" if cancelled() else "failed"
                    save_checkpoint(project_id, chapter_rel, data)
                    return {"project_id": project_id, "chapter_rel": chapter_rel,
                            "status": data["status"], "steps": data["steps"], "events": events,
                            "error": draft}
                data["draft_path"] = str(draft_path)
                data["proposal_id"] = draft["proposal_id"]
                data["candidate_hash"] = draft["candidate_hash"]
                data["contract_hash"] = draft["contract_hash"]
                _mark(data, step, "done", detail={"proposal_id": draft["proposal_id"],
                                                  "chars": draft["chars"]})

            elif step == "REVIEW":
                draft_text = _candidate_text(project_id, chapter_rel, draft_path, data)
                review = review_service.review_text(
                    project_id, chapter_rel, draft_text, use_ai=use_ai,
                    register_debt=False, candidate_hash=content_hash(draft_text),
                    should_cancel=should_cancel,
                )
                hard = review["hard_gates"]
                review_payload = {
                    "verdict": review["verdict"],
                    "hard_gates": hard,
                    "ai_review": review,
                    "round": data.get("revise_round", 0),
                    "candidate_hash": review["candidate_hash"],
                    "contract_hash": review["contract_hash"],
                }
                if cancelled():
                    _mark(data, step, "pending")
                    data["status"] = "paused"
                    save_checkpoint(project_id, chapter_rel, data)
                    return {"project_id": project_id, "chapter_rel": chapter_rel,
                            "status": "paused", "steps": data["steps"], "events": events}
                proposal_service.record_pipeline_review(int(data["proposal_id"]), review)
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
                elif not (last.get("ai_review") or {}).get("ai_used") or (last.get("ai_review") or {}).get("contract_changed"):
                    reason = ((last.get("ai_review") or {}).get("ai_error")
                              or "审稿合同已变化，需核对当前合同后重新生成候选")
                    _mark(data, step, "failed", detail={"rounds": rounds, "reason": reason})
                    data["status"] = "manual"
                    data["manual_reason"] = reason
                    save_checkpoint(project_id, chapter_rel, data)
                    return {"project_id": project_id, "chapter_rel": chapter_rel,
                            "status": "manual", "steps": data["steps"], "events": events,
                            "manual_reason": reason}
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
                        _candidate_text(project_id, chapter_rel, draft_path, data)
                        proposal = proposal_service.get_proposal(int(proposal_id))
                        if proposal["status"] == "applied":
                            # Recover a crash between disk commit and checkpoint.
                            current = read_text(require_chapter_path(project_id, chapter_rel))
                            if content_hash(current) != data["last_review"]["candidate_hash"]:
                                raise InvalidOperationError("已应用正文与候选审稿版本不一致")
                            applied = {"written": chapter_rel, "recovered": True}
                        else:
                            applied = proposal_service.apply_proposal(int(proposal_id), status="完成",
                                                                      should_cancel=should_cancel)
                    except Exception as exc:  # noqa: BLE001 - 已应用/冲突等
                        _mark(data, step, "pending" if cancelled() else "failed", detail={"error": str(exc)})
                        data["status"] = "paused" if cancelled() else "blocked"
                        save_checkpoint(project_id, chapter_rel, data)
                        return {"project_id": project_id, "chapter_rel": chapter_rel,
                                "status": data["status"], "steps": data["steps"], "events": events,
                                "blocked_reasons": [str(exc)]}
                ingested = None
                if applied and not applied.get("error"):
                    ingested = ingestion_service.ingest_chapter(
                        project_id, chapter_rel, use_ai=use_ai
                    )
                events.append({"step": step, "applied": bool(applied and not applied.get("error")),
                               "ingested": bool(ingested)})
                _mark(data, step, "done",
                      detail={"applied": bool(applied), "ingest": ingested,
                              "candidate_hash": (data.get("last_review") or {}).get("candidate_hash")})

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
                applied = bool((data["steps"].get("UPDATE", {}).get("detail") or {}).get("applied"))
                if applied and (content_hash(read_text(require_chapter_path(project_id, chapter_rel))) != last.get("candidate_hash")
                                or review_service.contract_fingerprint(contract_service.get_contract(
                                    project_id, chapter_rel)) != last.get("contract_hash")):
                    reason = "已应用候选的正文或合同已变化，不能复用原完成结论"
                    _mark(data, step, "failed", detail={"reason": reason})
                    data["status"] = "blocked"
                    save_checkpoint(project_id, chapter_rel, data)
                    return {"project_id": project_id, "chapter_rel": chapter_rel,
                            "status": "blocked", "steps": data["steps"], "events": events,
                            "blocked_reasons": [reason]}
                # Completion belongs to the reviewed and committed candidate;
                # an unapplied proposal must not complete the old/empty chapter.
                status = "完成" if applied and last.get("verdict") == "通过" else "待应用"
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


def _candidate_text(project_id: int, chapter_rel: str, draft_path: Path, data: dict) -> str:
    proposal = proposal_service.get_proposal(int(data["proposal_id"]))
    if proposal["project_id"] != project_id or proposal["target_path"] != chapter_rel:
        raise InvalidOperationError("候选提案不属于当前书籍或章节")
    text = str(proposal["content"] or "")
    if proposal["status"] == "pending" and file_state(project_id, chapter_rel)["hash"] != proposal["base_hash"]:
        raise InvalidOperationError("候选生成后目标正文已变化，不能把旧候选重新基于新稿应用")
    digest = content_hash(text)
    if not text.strip() or content_hash(_read_text(draft_path)) != digest:
        raise InvalidOperationError("草稿与提案版本不一致，请核对后重新生成")
    if data.get("candidate_hash") and data["candidate_hash"] != digest:
        raise InvalidOperationError("候选正文已变化，不能复用此前检查点")
    contract = contract_service.get_contract(project_id, chapter_rel)
    contract_hash = review_service.contract_fingerprint(contract)
    if data.get("contract_hash") and data["contract_hash"] != contract_hash:
        raise InvalidOperationError("章节合同已变化，请重新生成或修订后审稿")
    data["candidate_hash"] = digest
    data["contract_hash"] = contract_hash
    return text


def _instructions(review: dict) -> list[str]:
    ai = review.get("ai_review") or {}
    instructions = []
    for item in ai.get("items", []):
        if item.get("判定") != review_service.ITEM_DONE:
            instructions.append(f"合同项 {item.get('项', '')}："
                                f"{item.get('核验说明') or item.get('建议') or '尚未逐项核实'}")
    hard = review.get("hard_gates") or {}
    for gate in hard.get("gates", []):
        if gate.get("blocking"):
            instructions.append(f"硬门禁未过（{gate['key']}）：{gate.get('detail', '')[:200]}")
    for location in hard.get("locations", [])[:8]:
        instructions.append(f"第{location.get('line', '?')}行：{location.get('reason', '')}")
    instructions.extend(str(item) for item in ai.get("revise_instructions") or [])
    if not instructions:
        instructions.append("按审稿意见逐项修订后重跑")
    return instructions[:8]


def _expression_notes(review: dict) -> list[str]:
    """可选表达意见不触发修订轮次，也不挤占合同与硬门禁指令。"""
    ai = review.get("ai_review") or {}
    notes = [f"第{item.get('line', '?')}行：{item.get('reason', '')}；"
             f"原文：{item.get('text', '')[:160]}；{item.get('suggestion', '')}"
             for item in (ai.get("prose_quality") or {}).get("findings", [])[:2]]
    notes.extend(f"第{item.get('line', '?')}行：{item.get('reason', '')}；"
                 f"原文：{item.get('evidence', '')[:160]}；{item.get('suggestion', '')}"
                 for item in ai.get("prose_review", [])[:2])
    notes.extend(f"第{item.get('line', '?')}行：{item.get('reason', '')}；"
                 f"本稿原文：{item.get('text', '')[:160]}；此前来源："
                 + "、".join(f"{source.get('rel_path', '')}第{source.get('line', '?')}行"
                            for source in item.get("related_sources", [])[:2])
                 for item in (ai.get("prose_history") or {}).get("findings", [])[:2])
    return notes


def _apply_revision(text: str, response: str) -> str:
    """Apply disjoint, uniquely grounded replacements against one base image."""
    parsed = review_service._parse_json(response) or {}
    patches = parsed.get("patches")
    if not isinstance(patches, list) or not patches or len(patches) > 24:
        raise InvalidOperationError("修订结果必须包含 1–24 个精确补丁，整章重写未被接受")
    spans = []
    for patch in patches:
        if not isinstance(patch, dict):
            raise InvalidOperationError("修订补丁格式无效")
        original, replacement = patch.get("original"), patch.get("replacement")
        if not isinstance(original, str) or not original or not isinstance(replacement, str):
            raise InvalidOperationError("修订补丁必须包含精确原文与替换正文")
        if text.count(original) != 1:
            raise InvalidOperationError("修订原文缺失或出现多次，无法定位修改")
        if original == text:
            raise InvalidOperationError("修订补丁不得替换整章，必须定位具体问题片段")
        start = text.index(original)
        spans.append((start, start + len(original), replacement))
    spans.sort()
    if any(right[0] < left[1] for left, right in zip(spans, spans[1:])):
        raise InvalidOperationError("修订补丁相互重叠")
    if sum(end - start for start, end, _ in spans) >= len(text):
        raise InvalidOperationError("修订补丁不得拼接为整章替换")
    revised = text
    for start, end, replacement in reversed(spans):
        revised = revised[:start] + replacement + revised[end:]
    if revised == text:
        raise InvalidOperationError("修订没有产生有效改动")
    return revised


def _draft(project_id: int, chapter_rel: str, draft_path: Path, data: dict,
           *, use_ai: bool, should_cancel=None) -> dict:
    """DRAFT 步：生成草稿（含修改指令回灌）→ 写草稿文件 → 建 Proposal。"""
    revising = int(data.get("revise_round", 0)) > 0 and bool(data.get("last_review"))
    prompt = prompt_registry_service.get_prompt("novel.chapter.revise" if revising else "novel.chapter.draft")

    contract = {}
    try:
        contract = contract_service.get_contract(project_id, chapter_rel)
    except NodeNotFoundError:
        contract = {}

    context = assemble(project_id, chapter_rel=chapter_rel, query="", retrieval_profile="history",
                       include_style=True, include_prose_history=True, style_query="\n".join(
                           str(item) for item in contract.get("plot_points", [])))
    target_hash = file_state(project_id, chapter_rel)["hash"]

    system = prompt_registry_service.render(
        prompt["prompt_id"], {"字数预算": contract.get("word_budget")
                             or config.DEFAULT_WORD_BUDGET}
    )["body"]
    from . import agent_service, rule_service, skill_service
    try:
        skill_names = agent_service.skills_of("writer")
    except NodeNotFoundError:
        skill_names = []
    compiled = skill_service.compile_skills(skill_names)
    rules = rule_service.rules_digest(project_id)
    system = "\n\n".join(filter(None, [rules, compiled["text"], system]))
    _sys, messages = to_messages(context, system=system)

    instructions: list[str] = []
    previous = ""
    if revising:
        previous = _candidate_text(project_id, chapter_rel, draft_path, data)
        instructions = _instructions(data["last_review"])
        messages.append({"role": "user", "content": "【候选原稿，补丁只能定位此版本】\n" + previous})
    if instructions:
        messages.append(
            {"role": "user", "content": "【上一轮审稿意见（必须逐条修正）】\n"
             + "\n".join(f"- {item}" for item in instructions)}
        )
    expression_notes = _expression_notes(data["last_review"]) if revising else []
    if expression_notes:
        messages.append({"role": "user", "content": "【可选表达参考，不能扩大补丁范围】\n"
                         + "\n".join(expression_notes)
                         + "\n先核对是否承担新事实或人物意图；有意复沓可以保留。"
                         "仅有这些建议时不需要自动修订，也不影响合同通过。"})

    # Generate into a fresh artifact; failed generation cannot erase an old draft.
    output_path = draft_path.with_name(f"{draft_path.stem}-{uuid.uuid4().hex[:12]}.txt")

    result = generation_service.run_task(
        project_id=project_id,
        task_type="章节修订" if revising else "章节正文",
        system=system,
        messages=messages,
        output_path=None if revising else output_path,
        prompt_id=prompt["prompt_id"],
        prompt_version=prompt["version"],
        context_snapshot={"chapter": chapter_rel, "tokens": context["total_tokens"],
                           "revise_round": int(data.get("revise_round", 0)),
                           "base_candidate_hash": content_hash(previous) if revising else None,
                           "loaded_skills": compiled["skills"], "unavailable_skills": compiled["unavailable"],
                           "rule_hash": content_hash(rules),
                           "style_reference": context.get("style_reference", {}),
                           "prose_history_reference": context.get("prose_history_reference", {})},
        temperature=0.85,
        should_cancel=should_cancel,
    )

    text = ""
    if output_path.is_file():
        text = read_text(output_path)
    if not text.strip():
        text = str(result.get("text") or "")

    if not result["ok"] or not text.strip():
        return {
            "ok": False,
            "task_id": result.get("task_id"),
            "engine": result.get("engine"),
            "error_code": result.get("error_code"),
            "error_message": result.get("error_message") or "草稿为空",
        }

    body = split_frontmatter(text)[1].strip()
    if revising:
        body = _apply_revision(previous, str(result.get("text") or ""))
    if should_cancel and should_cancel():
        return {"ok": False, "error_code": "CANCELLED", "error_message": "修订已取消"}
    contract_hash = review_service.contract_fingerprint(contract)
    digest = content_hash(body)
    version_path = draft_path.with_name(f"{draft_path.stem}-v{int(data.get('revise_round', 0))}-{digest[:16]}.txt")
    with project_lock(project_id):
        if should_cancel and should_cancel():
            return {"ok": False, "error_code": "CANCELLED", "error_message": "候选提交已取消"}
        if review_service.contract_fingerprint(contract_service.get_contract(project_id, chapter_rel)) != contract_hash:
            raise InvalidOperationError("生成期间章节合同发生变化，候选稿未提交")
        if file_state(project_id, chapter_rel)["hash"] != target_hash:
            raise InvalidOperationError("生成期间目标正文发生变化，候选稿未重新基于新稿提交")
        atomic_write_text(version_path, body)
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
                "pipeline_candidate": {"protocol": PIPELINE_PROTOCOL, "candidate_hash": digest,
                                       "contract_hash": contract_hash},
                "style_reference": context.get("style_reference", {}),
                "prose_history_reference": context.get("prose_history_reference", {}),
            },
        )
        atomic_write_text(draft_path, body)
        data.setdefault("candidate_versions", []).append({"path": str(version_path), "hash": digest,
                                                          "round": int(data.get("revise_round", 0))})
    return {
        "ok": True,
        "task_id": result.get("task_id"),
        "engine": result.get("engine"),
        "proposal_id": proposal["id"],
        "chars": len(body),
        "draft_path": str(draft_path),
        "candidate_hash": digest,
        "contract_hash": contract_hash,
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
