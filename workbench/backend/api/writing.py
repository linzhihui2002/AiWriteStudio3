"""创作管线接口：上下文 / 合同 / 8 步循环 / 收件箱 / 审稿 / 摄取 / 写作辅助。

写作辅助的流式接口用 SSE（``text/event-stream``）：逐块推送增量，可随时中断；
中断产物只进草稿区（不落正文），落正文必须经 Proposal 收件箱。
"""

from __future__ import annotations

import json
from typing import Literal

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..services import (
    chapter_service,
    contract_service,
    generation_service,
    ingestion_service,
    outline_service,
    pipeline_service,
    prompt_registry_service,
    proposal_service,
    review_service,
    style_service,
)
from ..engine.runtime import estimate_tokens
from ..services.context_service import assemble, build_preview, to_messages
from ..services.errors import InvalidOperationError
from ..services.project_service import get_project_dir

router = APIRouter(prefix="/api", tags=["writing"])


# ─────────────────────────── 请求模型 ───────────────────────────


class ContextPreviewIn(BaseModel):
    chapter_rel: str | None = None
    explicit: list[dict] = Field(default_factory=list)
    related_characters: list[str] = Field(default_factory=list)
    query: str = ""
    budget_tokens: int | None = None
    exclude_levels: list[int] = Field(default_factory=list)
    use_retrieval: bool = True
    retrieval_profile: str = "history"
    task_type: str = "章节正文"
    include_style: bool = True
    style_query: str | None = None
    include_prose_history: bool = True


class PipelineRunIn(BaseModel):
    chapter_rel: str
    resume: bool = True
    auto_freeze_contract: bool = True
    auto_apply: bool = True
    use_ai: bool = True
    word_budget: int | None = None


class ContractGenerateIn(BaseModel):
    chapter_rel: str
    use_ai: bool = True
    word_budget: int | None = None


class ContractUpdateIn(BaseModel):
    chapter_rel: str
    patch: dict
    confirm: bool = False


class ContractFreezeIn(BaseModel):
    chapter_rel: str
    confirm: bool = True


class ProposalApplyIn(BaseModel):
    content: str | None = None
    status: str | None = None


class ProposalContentIn(BaseModel):
    content: str


class ProposalReviewIn(BaseModel):
    use_ai: bool = True


class BatchIn(BaseModel):
    ids: list[int]


class ReviewIn(BaseModel):
    chapter_rel: str
    use_ai: bool = True


class IngestIn(BaseModel):
    chapter_rel: str
    use_ai: bool = True
    apply_states_directly: bool = False


class GenerateIn(BaseModel):
    task_type: str = "章节正文"
    chapter_rel: str | None = None
    instruction: str = ""
    related_characters: list[str] = Field(default_factory=list)
    engine: str = ""
    provider: str = ""
    model: str = ""
    agent: str = ""
    temperature: float | None = None
    max_tokens: int | None = None
    context_preview_only: bool = False
    retrieval_profile: str = ""


class StreamIn(GenerateIn):
    pass


class LocalOpIn(BaseModel):
    chapter_rel: str
    selection: str
    operation: str = Field(description="rewrite / expand / shrink / polish")
    instruction: str = ""


class GhostTextIn(BaseModel):
    chapter_rel: str
    prefix: str = ""
    suffix: str = ""


class StyleSampleIn(BaseModel):
    rel_path: str
    approved: bool = True
    note: str | None = Field(default=None, max_length=1000)


class StyleApprovalIn(BaseModel):
    approved: Literal[False] = False


# ─────────────────────────── 上下文 ───────────────────────────


@router.post("/projects/{project_id}/context/preview")
def context_preview(project_id: int, payload: ContextPreviewIn) -> dict:
    """生成上下文预览（构成 / Token / 优先级 / 可排除项），不落审计记录。"""
    assembly = assemble(
        project_id,
        chapter_rel=payload.chapter_rel,
        explicit=payload.explicit,
        related_characters=payload.related_characters,
        query=payload.query,
        budget_tokens=payload.budget_tokens,
        exclude_levels=payload.exclude_levels,
        use_retrieval=payload.use_retrieval,
        retrieval_profile=payload.retrieval_profile,
        include_style=payload.include_style and _is_writing_task(payload.task_type, payload.retrieval_profile),
        style_query=payload.style_query,
        include_prose_history=payload.include_prose_history and _is_writing_task(payload.task_type, payload.retrieval_profile),
    )
    return {
        "preview": assembly["preview"],
        "total_tokens": assembly["total_tokens"],
        "budget_tokens": assembly["budget_tokens"],
        "blocks": [
            {"level": b["level"], "title": b["title"], "source": b["source"],
              "chars": b["chars"], "tokens": b["tokens"], "mandatory": b["mandatory"],
              **({"type": "style", "style_reference": b["style_reference"]}
                 if b.get("type") == "style" else {}),
              **({"type": "prose_history", "prose_history_reference": b["prose_history_reference"]}
                 if b.get("type") == "prose_history" else {}),
              **({"retrieval": b["retrieval"]} if b.get("retrieval") else {})}
            for b in assembly["blocks"]
        ],
        "degradation": assembly["degradation"],
        "duration_ms": assembly["duration_ms"],
        "retrieval": assembly.get("retrieval", {}),
        "style_reference": assembly.get("style_reference", {}),
        "prose_history_reference": assembly.get("prose_history_reference", {}),
    }


# ─────────────────────────── 章节合同 / 门控 ───────────────────────────


@router.get("/projects/{project_id}/contracts")
def list_contracts(project_id: int) -> list[dict]:
    return contract_service.list_contracts(project_id)


@router.post("/projects/{project_id}/contracts/generate")
def generate_contract(project_id: int, payload: ContractGenerateIn) -> dict:
    return contract_service.generate_contract(
        project_id, payload.chapter_rel, use_ai=payload.use_ai,
        word_budget=payload.word_budget,
    )


@router.get("/projects/{project_id}/contracts/detail")
def get_contract(project_id: int, chapter_rel: str = Query(...)) -> dict:
    return contract_service.get_contract(project_id, chapter_rel)


@router.patch("/projects/{project_id}/contracts")
def update_contract(project_id: int, payload: ContractUpdateIn) -> dict:
    """修改合同（冻结后需 ``confirm=true``，会记录差异）。"""
    return contract_service.update_contract(
        project_id, payload.chapter_rel, payload.patch, confirm=payload.confirm
    )


@router.post("/projects/{project_id}/contracts/freeze")
def freeze_contract(project_id: int, payload: ContractFreezeIn) -> dict:
    return contract_service.freeze_contract(project_id, payload.chapter_rel,
                                            confirm=payload.confirm)


@router.get("/projects/{project_id}/gates")
def check_gates(project_id: int, chapter_rel: str = Query(...)) -> dict:
    """CHECK 门控：前置依赖（大纲冻结等）与合同就绪状态。"""
    return {
        "prerequisites": contract_service.check_prerequisites(project_id, chapter_rel),
        "contract": contract_service.check_contract_ready(project_id, chapter_rel),
    }


@router.post("/projects/{project_id}/outline/freeze")
def freeze_outline(project_id: int) -> dict:
    return outline_service.freeze(project_id)


@router.post("/projects/{project_id}/outline/unfreeze")
def unfreeze_outline(project_id: int) -> dict:
    """取消大纲冻结；取消后 CHECK 会重新要求冻结大纲。"""
    return outline_service.unfreeze(project_id)


# ─────────────────────────── 8 步循环 ───────────────────────────


@router.post("/projects/{project_id}/pipeline/run")
def run_pipeline(project_id: int, payload: PipelineRunIn) -> dict:
    """同步跑通 8 步循环（断点续跑：``resume=true``）。"""
    get_project_dir(project_id)
    return pipeline_service.run_pipeline(
        project_id,
        payload.chapter_rel,
        resume=payload.resume,
        auto_freeze_contract=payload.auto_freeze_contract,
        use_ai=payload.use_ai,
        auto_apply=payload.auto_apply,
        word_budget=payload.word_budget,
    )


@router.get("/projects/{project_id}/pipeline/checkpoints")
def list_checkpoints(project_id: int) -> list[dict]:
    return pipeline_service.list_checkpoints(project_id)


@router.get("/projects/{project_id}/pipeline/checkpoint")
def get_checkpoint(project_id: int, chapter_rel: str = Query(...)) -> dict:
    return pipeline_service.load_checkpoint(project_id, chapter_rel)


# ─────────────────────────── 收件箱 ───────────────────────────


@router.get("/proposals")
def list_proposals(project_id: int | None = None, status: str | None = "pending",
                   kind: str | None = None, limit: int = 100) -> list[dict]:
    return proposal_service.list_proposals(project_id, status=status, kind=kind, limit=limit)


@router.get("/proposals/count")
def proposals_count(project_id: int | None = None) -> dict:
    return {"pending": proposal_service.pending_count(project_id)}


@router.get("/proposals/{proposal_id}")
def get_proposal(proposal_id: int) -> dict:
    return proposal_service.get_proposal(proposal_id)


@router.put("/proposals/{proposal_id}")
def update_proposal(proposal_id: int, payload: ProposalContentIn) -> dict:
    return proposal_service.update_proposal_content(proposal_id, payload.content)


@router.post("/proposals/{proposal_id}/review")
def review_proposal(proposal_id: int, payload: ProposalReviewIn | None = None) -> dict:
    """审查收件箱内当前管线候选，仅绑定审稿结果，不应用正文。"""
    return proposal_service.review_pipeline_candidate(
        proposal_id, use_ai=payload.use_ai if payload is not None else True,
    )


@router.post("/proposals/{proposal_id}/apply")
def apply_proposal(proposal_id: int, payload: ProposalApplyIn | None = None) -> dict:
    content = payload.content if payload is not None else None
    status = payload.status if payload is not None else None
    return proposal_service.apply_proposal(proposal_id, content=content, status=status)


@router.post("/proposals/{proposal_id}/rebase-state")
def rebase_state_proposal(proposal_id: int) -> dict:
    return proposal_service.rebase_state_proposal(proposal_id)


@router.post("/proposals/{proposal_id}/discard")
def discard_proposal(proposal_id: int) -> dict:
    return proposal_service.discard_proposal(proposal_id)


@router.post("/proposals/apply-batch")
def apply_batch(payload: BatchIn) -> dict:
    return proposal_service.apply_batch(payload.ids)


@router.post("/proposals/discard-batch")
def discard_batch(payload: BatchIn) -> dict:
    return proposal_service.discard_batch(payload.ids)


# ─────────────────────────── 审稿 ───────────────────────────


@router.post("/projects/{project_id}/review")
def review_chapter(project_id: int, payload: ReviewIn) -> dict:
    return review_service.review_chapter(project_id, payload.chapter_rel,
                                         use_ai=payload.use_ai)


@router.post("/projects/{project_id}/review/deslop")
def soft_deslop(project_id: int, payload: ReviewIn) -> dict:
    """去AI味软审：模型逐行建议 → 收件箱（行级 patch，应用时校验原行）。"""
    return review_service.soft_deslop(project_id, payload.chapter_rel, use_ai=payload.use_ai)


@router.get("/projects/{project_id}/reviews")
def list_reviews(project_id: int, rel_path: str | None = None, limit: int = 50) -> list[dict]:
    return review_service.list_reviews(project_id, rel_path=rel_path, limit=limit)


@router.get("/projects/{project_id}/quality-matrix")
def quality_matrix(project_id: int) -> dict:
    """质检进度表：逐章 × 六项检查（未通过项可点击直达）。"""
    return review_service.quality_matrix(project_id)


@router.get("/projects/{project_id}/debts")
def list_debts(project_id: int, status: str | None = "open") -> list[dict]:
    return review_service.list_debts(project_id, status=status)


@router.post("/debts/{debt_id}/resolve")
def resolve_debt(debt_id: int, status: str = "resolved") -> dict:
    return review_service.resolve_debt(debt_id, status=status)


# ─────────────────────────── 摄取（四件套） ───────────────────────────


@router.post("/projects/{project_id}/ingest")
def ingest(project_id: int, payload: IngestIn) -> dict:
    return ingestion_service.ingest_chapter(
        project_id, payload.chapter_rel, use_ai=payload.use_ai,
        apply_state_directly=payload.apply_states_directly,
    )


@router.get("/projects/{project_id}/timeline")
def timeline(project_id: int) -> list[dict]:
    return ingestion_service.list_timeline(project_id)


@router.get("/projects/{project_id}/foreshadows")
def foreshadows(project_id: int) -> list[dict]:
    return ingestion_service.list_foreshadows(project_id)


@router.post("/projects/{project_id}/foreshadows/confirm")
def confirm_foreshadow(project_id: int, line: int, planned_chapter: str = "", expected_hash: str | None = None) -> dict:
    """人工确认伏笔回收（spec：回收需人工确认）。"""
    return ingestion_service.confirm_payoff(project_id, line,
                                            planned_chapter=planned_chapter, expected_hash=expected_hash)


@router.get("/projects/{project_id}/ledger")
def ledger(project_id: int) -> list[dict]:
    return ingestion_service.list_ledger(project_id)


# ─────────────────────────── 写作辅助 ───────────────────────────


def _is_writing_task(task_type: str, profile: str) -> bool:
    """Use registered task policies/capabilities; reviews never inherit samples."""
    if profile in {"review", "teardown"}:
        return False
    prompt = prompt_registry_service.find_by_task(task_type)
    if prompt:
        return prompt.get("context_policy") in {"chapter_draft", "chapter_revision"}
    from ..services import agent_service

    return any(capability.get("task_type") == task_type and capability.get("skill") == "novel-writing"
               for capability in agent_service.capability_index())


def _generation_snapshot(chapter_rel: str | None, assembly: dict) -> dict:
    return {"chapter": chapter_rel, "tokens": assembly["total_tokens"],
            "context_preview": assembly["preview"],
            "style_reference": assembly.get("style_reference", {}),
            "prose_history_reference": assembly.get("prose_history_reference", {})}


def _compact_writing_context(assembly: dict, *, fact_limit: int, fact_chars: int) -> dict:
    """Keep the existing brief fact context and the full separate style block.

    Samples must not disappear behind the first-N fact slice while an audit
    claims they were injected. Rebuild the preview for the material actually sent.
    """
    facts = [block for block in assembly["blocks"] if block.get("type") not in {"style", "prose_history"}]
    selected = []
    degradation = list(assembly["degradation"])
    for original in facts[:fact_limit]:
        block = dict(original)
        block["text"] = original["text"][:fact_chars]
        block["chars"] = len(block["text"])
        block["tokens"] = estimate_tokens(block["text"])
        if block["text"] != original["text"]:
            block["truncated"] = True
            degradation.append({"level": block["level"], "title": block["title"],
                                "source": block["source"], "reason": "局部写作保留简短事实片段",
                                "dropped_chars": original["chars"] - block["chars"]})
        selected.append(block)
    for block in facts[fact_limit:]:
        degradation.append({"level": block["level"], "title": block["title"],
                            "source": block["source"], "reason": "局部写作限制事实材料数量",
                            "dropped_tokens": block["tokens"]})
    selected.extend(block for block in assembly["blocks"] if block.get("type") in {"style", "prose_history"})
    total_tokens = sum(block["tokens"] for block in selected)
    retrieval = {**assembly.get("retrieval", {}), "hits": [dict(hit) for hit in assembly.get("retrieval", {}).get("hits", [])]}
    kept_sources = {block["source"] for block in selected if block.get("retrieval")}
    for hit in retrieval["hits"]:
        source = f"{hit['rel_path']}:{hit.get('line_start', 1)}-{hit.get('line_end', 1)}"
        hit["injected"] = source in kept_sources
    compact = {**assembly, "blocks": selected, "total_tokens": total_tokens,
               "remaining_tokens": assembly["budget_tokens"] - total_tokens,
               "degradation": degradation, "retrieval": retrieval,
               "mandatory_sources": [block["source"] for block in selected if block["mandatory"]]}
    compact["preview"] = build_preview(compact)
    return compact


def _assist_messages(project_id: int, payload: GenerateIn) -> tuple[str, list[dict], dict]:
    """构造写作辅助的消息与上下文（生成/流式共用，保证两条通道行为一致）。"""
    if payload.chapter_rel:
        chapter_service.require_chapter_path(project_id, payload.chapter_rel)
    from ..services import agent_service
    profile = payload.retrieval_profile or agent_service.retrieval_profile_for_task(payload.task_type)
    assembly = assemble(
        project_id,
        chapter_rel=payload.chapter_rel,
        related_characters=payload.related_characters,
        query=payload.instruction or "本章情节点",
        retrieval_profile=profile,
        include_style=_is_writing_task(payload.task_type, profile),
        include_prose_history=_is_writing_task(payload.task_type, profile),
    )
    prompt = prompt_registry_service.find_by_task(payload.task_type) or \
        prompt_registry_service.get_prompt("novel.chapter.continue")
    _system, messages = to_messages(assembly, system=prompt["body"])
    if payload.instruction:
        messages.append({"role": "user", "content": payload.instruction})
    return prompt["body"], messages, {
        "prompt_id": prompt["prompt_id"],
        "prompt_version": prompt["version"],
        "context": assembly,
    }


@router.post("/projects/{project_id}/generate")
def generate(project_id: int, payload: GenerateIn) -> dict:
    """同步生成（小改 / 无流式需求）；产物写入草稿区，不直接落正文。"""
    system, messages, meta = _assist_messages(project_id, payload)
    if payload.context_preview_only:
        return {"preview": meta["context"]["preview"], "dry_run": True}

    result = generation_service.run_task(
        project_id=project_id,
        task_type=payload.task_type,
        system=system,
        messages=messages,
        engine=payload.engine,
        provider=payload.provider,
        model=payload.model,
        agent=payload.agent,
        prompt_id=meta["prompt_id"],
        prompt_version=meta["prompt_version"],
        context_snapshot=_generation_snapshot(payload.chapter_rel, meta["context"]),
        temperature=payload.temperature,
        max_tokens=payload.max_tokens,
    )
    result["context_preview"] = meta["context"]["preview"]
    return result


@router.post("/projects/{project_id}/generate/stream")
def generate_stream(project_id: int, payload: StreamIn) -> StreamingResponse:
    """SSE 流式生成：可中断；中断产物进草稿区（不落正文）。"""
    system, messages, meta = _assist_messages(project_id, payload)

    def event_source():
        yield _sse({"event": "start", "preview": meta["context"]["preview"]})
        try:
            for chunk in generation_service.stream_task(
                project_id=project_id,
                task_type=payload.task_type,
                system=system,
                messages=messages,
                engine=payload.engine,
                provider=payload.provider,
                model=payload.model,
                agent=payload.agent,
                prompt_id=meta["prompt_id"],
                prompt_version=meta["prompt_version"],
                context_snapshot=_generation_snapshot(payload.chapter_rel, meta["context"]),
                temperature=payload.temperature,
                max_tokens=payload.max_tokens,
            ):
                if chunk.get("done"):
                    yield _sse({"event": "done", **chunk})
                    break
                yield _sse({"event": "delta", "text": chunk.get("delta", ""),
                            "task_id": chunk.get("task_id")})
        except Exception as exc:  # noqa: BLE001 - 流中断也要给前端一个明确事件
            yield _sse({"event": "error", "message": str(exc)})

    return StreamingResponse(event_source(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


@router.post("/projects/{project_id}/local-op")
def local_operation(project_id: int, payload: LocalOpIn) -> dict:
    """局部操作条：改写 / 扩写 / 缩写 / 润色（<500 字自动走 direct-api 轻量路径）。"""
    chapter_service.require_chapter_path(project_id, payload.chapter_rel)
    mapping = {
        "rewrite": ("改写", "保持信息不变，重写以下片段，语气与人称不变。"),
        "expand": ("扩写", "把以下片段扩写：补具体动作、物件与对话，不要注水。"),
        "shrink": ("缩写", "把以下片段压缩：保留关键动作与信息，删掉修饰与重复。"),
        "polish": ("润色", "润色以下片段：去掉套话与 AI 腔，让句子节奏更像人写的。"),
    }
    if payload.operation not in mapping:
        raise InvalidOperationError(
            f"未知局部操作：{payload.operation}（可选：{'/'.join(mapping)}）"
        )
    task_type, instruction = mapping[payload.operation]
    short = len(payload.selection) < 500

    assembly = assemble(project_id, chapter_rel=payload.chapter_rel, query="", use_retrieval=False,
                        retrieval_profile="off", include_style=True,
                        include_prose_history=True,
                        style_query=payload.instruction or payload.selection)
    assembly = _compact_writing_context(assembly, fact_limit=3, fact_chars=1200)
    _, context_messages = to_messages(assembly)
    context_text = context_messages[0]["content"]
    result = generation_service.run_task(
        project_id=project_id,
        task_type=task_type,
        system=(
            "你是中文小说的文字编辑。只输出处理后的片段正文，不要解释、不要复述要求。"
        ),
        messages=[
            {"role": "user", "content": f"{context_text}\n\n{instruction}\n\n【片段】\n{payload.selection}"
                                        + (f"\n\n【额外要求】{payload.instruction}" if payload.instruction else "")}
        ],
        engine="direct-api" if short else "",
        prompt_id="",
        prompt_version="",
        context_snapshot=_generation_snapshot(payload.chapter_rel, assembly),
        temperature=0.5,
    )
    result["short_path"] = short
    result["context_preview"] = assembly["preview"]
    return result


@router.post("/projects/{project_id}/ghost-text")
def ghost_text(project_id: int, payload: GhostTextIn) -> dict:
    """Ghost Text 候选（前端 Tab 接受 / Esc 拒绝）；产物不落盘。"""
    chapter_service.require_chapter_path(project_id, payload.chapter_rel)
    assembly = assemble(project_id, chapter_rel=payload.chapter_rel, query="", use_retrieval=False,
                        include_prose_history=True,
                        include_style=True, style_query=payload.prefix[-400:])
    assembly = _compact_writing_context(assembly, fact_limit=2, fact_chars=800)
    _, context_messages = to_messages(assembly)
    context_text = context_messages[0]["content"]
    result = generation_service.run_task(
        project_id=project_id,
        task_type="续写",
        system=(
            "你是中文小说写作助手。只输出接下来的 1-2 句正文（不超过 60 字），"
            "不要引号包裹、不要解释。"
        ),
        messages=[
            {"role": "user", "content": f"{context_text}\n\n【上文】\n{payload.prefix[-400:]}\n\n"
                                        f"【下文（如有）】\n{payload.suffix[:200]}"}
        ],
        engine="direct-api",
        context_snapshot=_generation_snapshot(payload.chapter_rel, assembly),
        temperature=0.8,
        max_tokens=120,
    )
    candidate = (result.get("text") or "").strip().strip('"“”')
    return {"candidate": candidate[:120], "ok": result["ok"],
            "task_id": result.get("task_id"), "error": result.get("error_message"),
            "context_preview": assembly["preview"]}


# ─────────────────────────── 文风指纹 ───────────────────────────


@router.post("/projects/{project_id}/style/sample")
def sample_style(project_id: int, payload: StyleSampleIn) -> dict:
    return style_service.sample(project_id, payload.rel_path, approved=payload.approved, note=payload.note)


@router.get("/projects/{project_id}/style/fingerprints")
def list_fingerprints(project_id: int, approved_only: bool = False) -> dict:
    return {
        "samples": style_service.list_fingerprints(project_id, approved_only=approved_only),
        "reference": style_service.reference_metrics(project_id),
    }


@router.patch("/projects/{project_id}/style/fingerprints/{fingerprint_id}")
def cancel_style_approval(project_id: int, fingerprint_id: int, payload: StyleApprovalIn) -> dict:
    """取消当前章样稿认可；重新认可必须显式重新采样当前正文。"""
    return style_service.cancel_approval(project_id, fingerprint_id)


@router.post("/projects/{project_id}/style/compare")
def compare_style(project_id: int, text: str = "") -> dict:
    """把候选文本与认可样本比对（软审参照，不阻断）。"""
    return {"comparison": style_service.compare(project_id, text)}


# ─────────────────────────── 提示词 ───────────────────────────


@router.get("/prompts")
def list_prompts() -> list[dict]:
    return prompt_registry_service.list_prompts()
