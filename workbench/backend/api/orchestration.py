"""编排侧接口：技能 / Agent / 规则 / 意图路由 / 工作流 / 对话。"""

from __future__ import annotations

import json
import asyncio

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..services import (
    agent_service,
    chat_service,
    chat_run_service,
    rule_service,
    routing_service,
    skill_service,
    workflow_service,
)
from ..services.errors import InvalidOperationError

router = APIRouter(prefix="/api", tags=["orchestration"])


# ─────────────────────────── 请求模型 ───────────────────────────


class SkillCreateIn(BaseModel):
    name: str
    description: str
    body: str
    source: str = "workbench_custom"


class SkillUpdateIn(BaseModel):
    content: str


class SkillImportIn(BaseModel):
    content: str
    name: str | None = None


class SkillGenerateIn(BaseModel):
    description: str
    name: str = ""
    save: bool = False


class SkillDuplicateIn(BaseModel):
    new_name: str


class AgentIn(BaseModel):
    name: str
    title: str = ""
    description: str = ""
    system_prompt: str = ""
    skills: list[str] = Field(default_factory=list)
    provider_id: str = ""
    model_id: str = ""
    tools: list[str] = Field(default_factory=list)
    params: dict = Field(default_factory=dict)


class AgentPatchIn(BaseModel):
    patch: dict


class AgentDuplicateIn(BaseModel):
    new_name: str


class AgentModelIn(BaseModel):
    provider_id: str
    model_id: str


class AgentDraftIn(BaseModel):
    description: str
    name: str = ""


class RuleIn(BaseModel):
    name: str
    body: str
    keys: list[str] = Field(default_factory=list)
    priority: int = 100
    confirmed: bool = False
    enabled: bool = True


class RouteIn(BaseModel):
    text: str
    project_id: int | None = None
    agent: str = ""


class WorkflowIn(BaseModel):
    definition: dict


class WorkflowRunIn(BaseModel):
    workflow: str = workflow_service.DEFAULT_WORKFLOW_NAME
    project_id: int
    chapters: list[str] = Field(default_factory=list)
    chapter_start: int | None = None
    chapter_end: int | None = None
    execute: bool = True
    use_ai: bool = True


class ChatSessionIn(BaseModel):
    project_id: int | None = None
    title: str = ""
    agent: str = agent_service.DEFAULT_AGENT
    provider_id: str = ""
    model_id: str = ""
    auto_apply: bool | None = None
    mode: str | None = None
    permission_mode: str | None = None
    discussion_only: bool | None = None
    agent_pinned: bool = False


class ChatPatchIn(BaseModel):
    patch: dict


class ChatSendIn(BaseModel):
    text: str
    chapter_rel: str | None = None
    agent: str = ""
    provider: str = ""
    model: str = ""


class ChatRunIn(BaseModel):
    text: str
    client_request_id: str = ""
    continue_from_run_id: str = ""
    context: dict = Field(default_factory=dict)
    agent: str = ""
    provider: str = ""
    model: str = ""


class ChatRevertIn(BaseModel):
    change_ids: list[int] | None = None


class ChatInteractionIn(BaseModel):
    response: dict


class ChatMemoryIn(BaseModel):
    content: str


# ─────────────────────────── 技能 ───────────────────────────


@router.get("/skills")
def list_skills() -> dict:
    return {"skills": skill_service.list_skills(),
            "budget_bytes": skill_service.SKILL_BUDGET_BYTES,
            "target_root": str(skill_service.dsh_skills_root())}


@router.get("/skills/{name}")
def get_skill(name: str) -> dict:
    return skill_service.get_skill(name)


@router.post("/skills", status_code=201)
def create_skill(payload: SkillCreateIn) -> dict:
    return skill_service.create_skill(payload.name, payload.description, payload.body,
                                      source=payload.source)


@router.put("/skills/{name}")
def update_skill(name: str, payload: SkillUpdateIn) -> dict:
    return skill_service.update_skill(name, payload.content)


@router.post("/skills/import", status_code=201)
def import_skill(payload: SkillImportIn) -> dict:
    return skill_service.import_skill(payload.content, name=payload.name)


@router.post("/skills/{name}/duplicate", status_code=201)
def duplicate_skill(name: str, payload: SkillDuplicateIn) -> dict:
    return skill_service.duplicate_skill(name, payload.new_name)


@router.post("/skills/{name}/enable")
def enable_skill(name: str, enabled: bool = True) -> dict:
    return skill_service.set_enabled(name, enabled)


@router.delete("/skills/{name}")
def delete_skill(name: str) -> dict:
    return skill_service.delete_skill(name)


@router.post("/skills/sync")
def sync_skills(prune: bool = True) -> dict:
    """同步 skills/ → .dsh/skills（含体积校验）。"""
    return skill_service.sync_skills(prune=prune)


@router.post("/skills/generate")
def generate_skill(payload: SkillGenerateIn) -> dict:
    """AI 辅助生成 SKILL.md 草案（save=true 时直接保存）。"""
    return skill_service.generate_skill_draft(payload.description, name=payload.name,
                                              save=payload.save)


# ─────────────────────────── Agent ───────────────────────────


@router.get("/agents")
def list_agents(include_builtin: bool = False) -> dict:
    return {
        "agents": agent_service.list_agents(include_builtin=include_builtin),
        "builtin_count": len(agent_service.BUILTIN_AGENTS),
        "tool_whitelist": list(agent_service.TOOL_WHITELIST),
    }


@router.get("/agents/{name}")
def get_agent(name: str) -> dict:
    return agent_service.get_agent(name)


@router.post("/agents", status_code=201)
def create_agent(payload: AgentIn) -> dict:
    return agent_service.create_agent(
        payload.name, title=payload.title, description=payload.description,
        system_prompt=payload.system_prompt, skills=payload.skills,
        provider_id=payload.provider_id, model_id=payload.model_id,
        tools=payload.tools, params=payload.params,
    )


@router.patch("/agents/{name}")
def update_agent(name: str, payload: AgentPatchIn) -> dict:
    return agent_service.update_agent(name, payload.patch)


@router.post("/agents/{name}/duplicate", status_code=201)
def duplicate_agent(name: str, payload: AgentDuplicateIn) -> dict:
    return agent_service.duplicate_agent(name, payload.new_name)


@router.post("/agents/{name}/model")
def set_agent_model(name: str, payload: AgentModelIn) -> dict:
    """给 Agent 配模型（未配置时按 任务级 → 项目级 → 全局 回退）。"""
    return agent_service.set_agent_model(name, payload.provider_id, payload.model_id)


@router.delete("/agents/{name}")
def delete_agent(name: str) -> dict:
    return agent_service.delete_agent(name)


@router.post("/agents/draft")
def draft_agent(payload: AgentDraftIn) -> dict:
    """AI 辅助创建 Agent：生成草案供确认后保存。"""
    return agent_service.draft_agent(payload.description, name=payload.name)


# ─────────────────────────── 规则 ───────────────────────────


@router.get("/rules")
def list_rules(project_id: int | None = None) -> dict:
    return rule_service.list_rules(project_id)


@router.get("/rules/resolved")
def resolved_rules(project_id: int | None = None) -> dict:
    """按优先级链解析（作者确认 > 作品级 > 全局 > 技能内置）+ 冲突说明。"""
    return rule_service.resolve_rules(project_id)


@router.post("/rules", status_code=201)
def upsert_rule(payload: RuleIn, project_id: int | None = None) -> dict:
    """新增/更新规则（带 project_id 即作品级，否则全局级）。"""
    if project_id is not None:
        return rule_service.upsert_project_rule(
            project_id, payload.name, payload.body, keys=payload.keys,
            priority=payload.priority, confirmed=payload.confirmed, enabled=payload.enabled,
        )
    return rule_service.upsert_global_rule(
        payload.name, payload.body, keys=payload.keys, priority=payload.priority,
        confirmed=payload.confirmed, enabled=payload.enabled,
    )


@router.delete("/rules/{name}")
def delete_rule(name: str, scope: str = "global", project_id: int | None = None) -> dict:
    return rule_service.delete_rule(scope, name, project_id=project_id)


@router.post("/rules/compile")
def compile_rules(project_id: int | None = None) -> dict:
    """编译规则到 .dsh/skills/workbench-rules/SKILL.md。"""
    return rule_service.compile_rules(project_id)


# ─────────────────────────── 意图路由 ───────────────────────────


@router.get("/routing/intents")
def routing_intents() -> dict:
    return routing_service.describe()


@router.post("/routing/route")
def routing_route(payload: RouteIn) -> dict:
    """只看路由决策（不执行、不落盘）。"""
    return routing_service.route(payload.project_id, payload.text,
                                 override_agent=payload.agent, record=False)


# ─────────────────────────── 工作流 ───────────────────────────


@router.get("/workflows")
def list_workflows() -> dict:
    return {"workflows": workflow_service.list_workflows(),
            "node_types": list(workflow_service.NODE_TYPES)}


@router.get("/workflows/{name}")
def get_workflow(name: str) -> dict:
    return workflow_service.get_workflow(name)


@router.post("/workflows", status_code=201)
def save_workflow(payload: WorkflowIn) -> dict:
    return workflow_service.save_workflow(payload.definition)


@router.post("/workflows/{name}/duplicate", status_code=201)
def duplicate_workflow(name: str, new_name: str) -> dict:
    return workflow_service.duplicate_workflow(name, new_name)


@router.delete("/workflows/{name}")
def delete_workflow(name: str) -> dict:
    return workflow_service.delete_workflow(name)


@router.post("/workflows/run", status_code=201)
def start_workflow_run(payload: WorkflowRunIn) -> dict:
    """批量产章：可传章号区间或章节列表；execute=true 时同步执行。"""
    chapter_range = None
    if payload.chapter_start is not None and payload.chapter_end is not None:
        chapter_range = (payload.chapter_start, payload.chapter_end)
    run = workflow_service.start_run(
        payload.workflow, payload.project_id,
        chapters=payload.chapters, chapter_range=chapter_range,
    )
    if not payload.execute:
        return run
    return workflow_service.execute_run(run["id"], use_ai=payload.use_ai)


@router.get("/workflows/runs/list")
def list_runs(project_id: int | None = None, limit: int = 30) -> list[dict]:
    return workflow_service.list_runs(project_id, limit=limit)


@router.get("/workflows/runs/{run_id}")
def get_run(run_id: int) -> dict:
    return workflow_service.get_run(run_id)


@router.post("/workflows/runs/{run_id}/pause")
def pause_run(run_id: int) -> dict:
    return workflow_service.pause_run(run_id)


@router.post("/workflows/runs/{run_id}/resume")
def resume_run(run_id: int, use_ai: bool = True) -> dict:
    """断点续跑：已完成章节不重复生成。"""
    return workflow_service.execute_run(run_id, use_ai=use_ai)


# ─────────────────────────── 对话 ───────────────────────────


@router.get("/chat/sessions")
def list_sessions(project_id: int | None = None, limit: int = 50) -> list[dict]:
    return chat_service.list_sessions(project_id, limit=limit)


@router.post("/chat/sessions", status_code=201)
def create_session(payload: ChatSessionIn) -> dict:
    session = chat_service.create_session(
        project_id=payload.project_id, title=payload.title, agent=payload.agent,
        provider_id=payload.provider_id, model_id=payload.model_id,
        auto_apply=payload.auto_apply, mode=payload.mode,
        permission_mode=payload.permission_mode, discussion_only=payload.discussion_only,
    )
    if payload.agent_pinned:
        return chat_service.update_session(session["id"], {"agent_pinned": 1})
    return session


@router.get("/chat/sessions/{session_id}")
def get_session(session_id: int) -> dict:
    return {**chat_service.get_session(session_id),
            "messages": chat_service.list_messages(session_id),
            "active_run": chat_run_service.active_run(session_id)}


@router.patch("/chat/sessions/{session_id}")
def patch_session(session_id: int, payload: ChatPatchIn) -> dict:
    """改标题 / 换 Agent / 对话内切模型（下一轮生效）。"""
    return chat_service.update_session(session_id, payload.patch)


@router.delete("/chat/sessions/{session_id}")
def delete_session(session_id: int) -> dict:
    return chat_service.delete_session(session_id)


@router.get("/chat/quick-cards")
def quick_cards() -> list[dict]:
    return chat_service.quick_cards()


@router.post("/chat/sessions/{session_id}/runs", status_code=201)
def create_chat_run(session_id: int, payload: ChatRunIn) -> dict:
    return chat_run_service.create_run(session_id, **payload.model_dump())


@router.get("/chat/runs/{run_id}")
def get_chat_run(run_id: str) -> dict:
    return chat_run_service.get_run(run_id)


@router.get("/chat/runs/{run_id}/events")
def chat_run_events(run_id: str, request: Request, after: int = Query(0, ge=0)) -> StreamingResponse:
    chat_run_service.get_run(run_id)
    last_event_id = request.headers.get("last-event-id", "")
    if last_event_id.isdigit():
        after = max(after, int(last_event_id))

    async def stream():
        cursor = after
        idle = 0
        while not await request.is_disconnected():
            events = chat_run_service.events_after(run_id, cursor)
            for event in events:
                cursor = event["seq"]
                yield f"id: {cursor}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
            if not events and chat_run_service._row(run_id)["status"] in chat_run_service.TERMINAL:
                if not chat_run_service.events_after(run_id, cursor):
                    break
            idle = 0 if events else idle + 1
            if idle >= 100:
                yield ": keep-alive\n\n"
                idle = 0
            await asyncio.sleep(0.1)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/chat/runs/{run_id}/cancel")
def cancel_chat_run(run_id: str) -> dict:
    return chat_run_service.cancel(run_id)


@router.post("/chat/runs/{run_id}/interactions/{interaction_id}/respond")
def respond_chat_interaction(run_id: str, interaction_id: str, payload: ChatInteractionIn) -> dict:
    from ..services import chat_interaction_service
    return chat_interaction_service.respond(run_id, interaction_id, payload.response)


@router.post("/chat/sessions/{session_id}/title/regenerate")
def regenerate_chat_title(session_id: int) -> dict:
    return chat_service.schedule_title(session_id, force=True)


@router.get("/projects/{project_id}/chat-memory")
def get_chat_memory(project_id: int) -> dict:
    from ..services import chat_memory_service
    return {"entries": chat_memory_service.list_entries(project_id)}


@router.patch("/projects/{project_id}/chat-memory/{memory_id}")
def update_chat_memory(project_id: int, memory_id: str, payload: ChatMemoryIn) -> dict:
    from ..services import chat_memory_service
    return chat_memory_service.update_entry(project_id, memory_id, payload.content)


@router.delete("/projects/{project_id}/chat-memory/{memory_id}")
def delete_chat_memory(project_id: int, memory_id: str) -> dict:
    from ..services import chat_memory_service
    return chat_memory_service.delete_entry(project_id, memory_id)


@router.post("/chat/runs/{run_id}/revert")
def revert_chat_run(run_id: str, payload: ChatRevertIn) -> dict:
    return chat_run_service.revert(run_id, payload.change_ids)


@router.post("/chat/sessions/{session_id}/send")
def send_message(session_id: int, payload: ChatSendIn) -> dict:
    """一轮对话（同步）。"""
    return chat_service.send_message(
        session_id, payload.text, chapter_rel=payload.chapter_rel,
        agent_name=payload.agent, provider=payload.provider, model=payload.model,
    )


@router.post("/chat/sessions/{session_id}/stream")
def stream_message(session_id: int, payload: ChatSendIn) -> StreamingResponse:
    """一轮对话（SSE 流式，可中断）。"""

    def event_source():
        for chunk in chat_service.stream_message(
            session_id, payload.text, chapter_rel=payload.chapter_rel,
            agent_name=payload.agent, provider=payload.provider, model=payload.model,
        ):
            yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"

    return StreamingResponse(event_source(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


__all__ = ["router"]
