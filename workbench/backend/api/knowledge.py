"""Book-bound knowledge workspace and evidence-grounded search/answer APIs."""
from __future__ import annotations

import json
from typing import Literal

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..services import knowledge_service, knowledge_candidate_service

router = APIRouter(prefix="/api", tags=["knowledge"])


class SyncIn(BaseModel):
    force: bool = False


class SearchIn(BaseModel):
    query: str = Field(min_length=1, max_length=12000)
    mode: Literal["hybrid", "keyword"] = "hybrid"
    profile: Literal["history", "planning", "review", "setting", "continuity", "foreshadow", "teardown"] = "history"
    chapter_rel: str | None = None
    document: str | None = None
    limit: int = Field(default=6, ge=1, le=6)


class ExtractIn(BaseModel):
    paths: list[str] | None = Field(default=None, max_length=1000)
    types: list[Literal["entity", "field", "relation"]] = Field(default_factory=lambda: ["entity", "field", "relation"], min_length=1)
    force: bool = False
    retry_job_id: str | None = None


class ReviewItem(BaseModel):
    id: str
    version: int = Field(ge=1)
    decision: Literal["approve", "ignore"]
    value: str | None = Field(default=None, max_length=2000)
    entity_anchor: str | None = None
    target_entity_anchor: str | None = None
    lifecycle: Literal["draft", "planned", "setting_fact", "historical_event"] | None = None
    chapter_start: int | None = Field(default=None, ge=1)
    chapter_end: int | None = Field(default=None, ge=1)


class ReviewIn(BaseModel):
    items: list[ReviewItem] = Field(min_length=1, max_length=100)


@router.get("/projects/{project_id}/knowledge")
def overview(project_id: int):
    return knowledge_service.overview(project_id)


@router.get("/projects/{project_id}/knowledge/graph")
def graph(project_id: int, document: str | None = None, kinds: str | None = None,
          chapter_before: int | None = Query(default=None, ge=1), center: str | None = None,
          limit: int = Query(default=300, ge=1, le=300), include_content: bool = False):
    return knowledge_service.graph(project_id, document, kinds, chapter_before, center, limit, include_content)


@router.get("/projects/{project_id}/knowledge/evidence/{evidence_id}")
def evidence(project_id: int, evidence_id: str):
    if evidence_id.startswith("candidate:"):
        return knowledge_candidate_service.candidate_evidence(project_id, evidence_id)
    return knowledge_service.evidence(project_id, evidence_id)


@router.get("/projects/{project_id}/knowledge/entities")
def entities(project_id: int, q: str = "", kind: str = "", offset: int = Query(default=0, ge=0),
             limit: int = Query(default=100, ge=1, le=300)):
    return knowledge_service.entities(project_id, q=q, kind=kind, offset=offset, limit=limit)


@router.post("/projects/{project_id}/knowledge/extract")
def extract(project_id: int, payload: ExtractIn | None = None):
    return knowledge_candidate_service.extract(project_id, **(payload or ExtractIn()).model_dump())


@router.get("/projects/{project_id}/knowledge/jobs")
def jobs(project_id: int, limit: int = Query(default=20, ge=1, le=100)):
    return knowledge_candidate_service.list_jobs(project_id, limit)


@router.get("/projects/{project_id}/knowledge/jobs/{job_id}")
def job(project_id: int, job_id: str):
    return knowledge_candidate_service.get_job(project_id, job_id)


@router.post("/projects/{project_id}/knowledge/jobs/{job_id}/cancel")
def cancel_job(project_id: int, job_id: str):
    return knowledge_candidate_service.cancel_job(project_id, job_id)


@router.get("/projects/{project_id}/knowledge/candidates")
def candidates(project_id: int, status: str | None = None):
    return knowledge_candidate_service.list_candidates(project_id, status)


@router.post("/projects/{project_id}/knowledge/candidates/review")
def review(project_id: int, payload: ReviewIn):
    return knowledge_candidate_service.review(project_id, [item.model_dump(exclude_unset=True) for item in payload.items])


@router.post("/projects/{project_id}/knowledge/sync")
def sync(project_id: int, payload: SyncIn | None = None):
    return knowledge_service.sync(project_id, background=True, force=bool(payload and payload.force))


@router.post("/projects/{project_id}/retrieval/search")
def search(project_id: int, payload: SearchIn):
    return knowledge_service.search(project_id, **payload.model_dump())


@router.post("/projects/{project_id}/knowledge/ask")
def ask(project_id: int, payload: SearchIn):
    # Validate project synchronously so a missing book produces the normal HTTP error.
    knowledge_service.knowledge_store.identity(project_id)

    def events():
        try:
            for event in knowledge_service.ask(project_id, **payload.model_dump()):
                yield f"event: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
        except Exception as exc:
            event = {"type": "error", "message": str(exc)}
            yield f"event: error\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
