"""内容侧接口：Story Bible / 大纲规划 / 设定卡片 / 角色成长。"""

from __future__ import annotations

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from ..services import (
    asset_card_service,
    bible_service,
    character_service,
    outline_service,
)
from ..services.project_service import get_project_dir

router = APIRouter(prefix="/api", tags=["content"])


# ─────────────────────────── 请求模型 ───────────────────────────


class OutlineSaveIn(BaseModel):
    content: str
    confirm: bool = False


class OutlineCandidateIn(BaseModel):
    idea: str = ""
    count: int = 3
    use_ai: bool = True


class OutlineLockIn(BaseModel):
    candidate_id: str | None = None
    index: int | None = None
    title: str | None = None
    confirm: bool = True


class OutlineExpandIn(BaseModel):
    idea: str = ""
    use_ai: bool = True


class OutlineCandidateRefineIn(BaseModel):
    message: str = Field(min_length=1, max_length=20000)


class SourceMarkIn(BaseModel):
    ref: str
    source: str
    note: str = ""


class CardQueryIn(BaseModel):
    category: str | None = None
    query: str = ""
    highlight_only: bool = False
    use_ai: bool = False


class CardRefreshIn(BaseModel):
    category: str | None = None
    force: bool = False
    use_ai: bool = True


class CardHighlightIn(BaseModel):
    ref: str
    color: str = ""
    mode: str | None = None


class CardFieldEditIn(BaseModel):
    original_name: str
    new_name: str | None = None
    value: str | None = None
    delete: bool = False


class CardUpdateIn(BaseModel):
    ref: str
    fields: dict = Field(default_factory=dict)
    name: str | None = None
    field_edits: list[CardFieldEditIn] = Field(default_factory=list)
    expected_hash: str | None = None


class CardAddIn(BaseModel):
    category: str
    name: str
    fields: dict = Field(default_factory=dict)
    via_proposal: bool = True


class CardDeleteIn(BaseModel):
    ref: str
    via_proposal: bool = True


class CardGenerateIn(BaseModel):
    category: str
    name: str
    hint: str = ""


class CardExportIn(BaseModel):
    category: str | None = None
    format: str = "md"


class StateChangeIn(BaseModel):
    character: str
    field: str
    value: str
    source: str = "author"
    chapter: str = ""
    evidence: str = ""


class MemoryIn(BaseModel):
    character: str
    know_what: str
    when_known: str = ""
    source_event: str = ""
    source: str = "author"


class CharacterFieldIn(BaseModel):
    character: str
    field: str
    value: str


class VectorModelIn(BaseModel):
    model_dir: str = ""
    provider: str = ""
    model: str = ""
    mode: str | None = None
    enabled: bool | None = None


class TeardownImportIn(BaseModel):
    target_name: str
    content: str
    genre: str = ""
    source_note: str = ""


class TeardownAnalyzeIn(BaseModel):
    target: str
    use_ai: bool = True
    extra_note: str = ""


class PublishExportIn(BaseModel):
    platform: str = "通用"
    only_completed: bool = False


# ─────────────────────────── Story Bible ───────────────────────────


@router.get("/projects/{project_id}/bible")
def bible_overview(project_id: int) -> dict:
    return bible_service.overview(project_id)


@router.get("/projects/{project_id}/bible/unknown")
def bible_unknown(project_id: int) -> list[dict]:
    """来源 unknown 的条目（审稿按待核实处理）。"""
    return bible_service.unknown_fields(project_id)


@router.get("/projects/{project_id}/bible/{kind}")
def bible_entities(project_id: int, kind: str) -> dict:
    entities = bible_service.list_entities(project_id, kind)
    return {
        "kind": kind,
        "label": bible_service.KIND_LABELS.get(kind, kind),
        "entities": entities,
        "count": len(entities),
    }


@router.post("/projects/{project_id}/bible/source")
def mark_source(project_id: int, payload: SourceMarkIn) -> dict:
    """标注条目来源：author / model / unknown。"""
    return bible_service.write_source(project_id, payload.ref, payload.source,
                                      note=payload.note)


# ─────────────────────────── 大纲规划 ───────────────────────────


@router.get("/projects/{project_id}/outline")
def read_outline(project_id: int) -> dict:
    return outline_service.read_outline(project_id)


@router.put("/projects/{project_id}/outline")
def save_outline(project_id: int, payload: OutlineSaveIn) -> dict:
    """保存大纲（已冻结时需 confirm=true，走差异确认）。"""
    return outline_service.save_outline(project_id, payload.content, confirm=payload.confirm)


@router.post("/projects/{project_id}/outline/expand")
def expand_outline(project_id: int, payload: OutlineExpandIn) -> dict:
    """灵感扩展：定向追问（先问清关键问题，再谈结构）。"""
    return outline_service.expand_ideas(project_id, idea=payload.idea, use_ai=payload.use_ai)


@router.post("/projects/{project_id}/outline/candidates")
def outline_candidates(project_id: int, payload: OutlineCandidateIn) -> dict:
    """追加不同的大纲候选，返回包含已有候选的完整列表。"""
    return outline_service.generate_candidates(
        project_id, idea=payload.idea, count=payload.count, use_ai=payload.use_ai
    )


@router.post("/projects/{project_id}/outline/candidates/{candidate_id}/refine")
def refine_outline_candidate(project_id: int, candidate_id: str,
                             payload: OutlineCandidateRefineIn) -> dict:
    """对话优化当前候选，保存新版本并保留原版。"""
    return outline_service.refine_candidate(project_id, candidate_id, message=payload.message)


@router.post("/projects/{project_id}/outline/lock")
def lock_outline(project_id: int, payload: OutlineLockIn) -> dict:
    """确认锁定某个候选为正式大纲。"""
    target: int | str = payload.candidate_id or payload.title or (payload.index or 0)
    return outline_service.lock_candidate(project_id, target, confirm=payload.confirm)


@router.post("/projects/{project_id}/outline/rolling")
def rolling_plan(project_id: int, use_ai: bool = True) -> dict:
    """滚动规划：只细化近 2 卷。"""
    return outline_service.rolling_plan(project_id, use_ai=use_ai)


@router.get("/projects/{project_id}/outline/history")
def outline_history(project_id: int) -> list[dict]:
    return outline_service.list_history(project_id)


# ─────────────────────────── 设定卡片 ───────────────────────────


@router.get("/projects/{project_id}/cards")
def list_cards(project_id: int, category: str | None = None, query: str = "",
               highlight_only: bool = False, use_ai: bool = False) -> dict:
    """设定卡片列表（分类 / 搜索 / 仅高亮 / 条数统计）。"""
    return asset_card_service.list_cards(
        project_id, category=category, query=query,
        highlight_only=highlight_only, use_ai=use_ai,
    )


@router.get("/projects/{project_id}/cards/categories")
def card_categories(project_id: int) -> list[dict]:
    return asset_card_service.categories(project_id)


@router.post("/projects/{project_id}/cards/refresh")
def refresh_cards(project_id: int, payload: CardRefreshIn) -> dict:
    """手动触发异步 AI 解析；普通读取不会调用模型。"""
    return asset_card_service.refresh(project_id, category=payload.category,
                                      force=payload.force, use_ai=payload.use_ai)


@router.get("/projects/{project_id}/cards/tasks/{task_id}")
def card_parse_task(project_id: int, task_id: str) -> dict:
    return asset_card_service.get_parse_task(project_id, task_id)


@router.post("/projects/{project_id}/cards/tasks/{task_id}/cancel")
def cancel_card_parse_task(project_id: int, task_id: str) -> dict:
    return asset_card_service.cancel_parse_task(project_id, task_id)


@router.post("/projects/{project_id}/cards/highlight")
def set_highlight(project_id: int, payload: CardHighlightIn) -> dict:
    """指派/清除高亮色（token 色板；持久化不写 md）。"""
    return asset_card_service.set_highlight(project_id, payload.ref, payload.color, mode=payload.mode)


@router.get("/projects/{project_id}/cards/highlights")
def list_highlights(project_id: int) -> list[dict]:
    return asset_card_service.list_highlights(project_id)


@router.get("/projects/{project_id}/cards/chapter-line")
def card_chapter_line(project_id: int, ref: str = Query(...)) -> dict:
    """定位章节线：该卡片在各章的出场位置。"""
    return asset_card_service.chapter_line(project_id, ref)


@router.patch("/projects/{project_id}/cards")
def update_card(project_id: int, payload: CardUpdateIn) -> dict:
    """卡片字段编辑（diff 写回 Markdown，不破坏自由书写内容）。"""
    return asset_card_service.update_card(project_id, payload.ref, payload.fields, name=payload.name,
                                         field_edits=[edit.model_dump(exclude_none=True) for edit in payload.field_edits], expected_hash=payload.expected_hash)


@router.post("/projects/{project_id}/cards", status_code=201)
def add_card(project_id: int, payload: CardAddIn) -> dict:
    """新增卡片（默认经收件箱）。"""
    return asset_card_service.add_card(project_id, payload.category, payload.name,
                                       payload.fields, via_proposal=payload.via_proposal)


@router.delete("/projects/{project_id}/cards")
def delete_card(project_id: int, ref: str = Query(...), via_proposal: bool = True) -> dict:
    return asset_card_service.delete_card(project_id, ref, via_proposal=via_proposal)


@router.post("/projects/{project_id}/cards/generate")
def generate_card(project_id: int, payload: CardGenerateIn) -> dict:
    """AI 智能生成卡片 → 收件箱（应用前展示 diff）。"""
    return asset_card_service.generate_card(project_id, category=payload.category,
                                            name=payload.name, hint=payload.hint)


@router.post("/projects/{project_id}/cards/export")
def export_cards(project_id: int, payload: CardExportIn) -> dict:
    return asset_card_service.export_cards(project_id, category=payload.category,
                                           fmt=payload.format)


# ─────────────────────────── 角色成长 ───────────────────────────


@router.get("/projects/{project_id}/characters")
def list_characters(project_id: int) -> list[dict]:
    characters = character_service.list_characters(project_id)
    states = character_service.list_states(project_id)
    for character in characters:
        character["state"] = states.get(character["name"], {})
    return characters


@router.get("/projects/{project_id}/characters/states")
def list_states(project_id: int, character: str | None = None) -> dict:
    return {
        "states": character_service.list_states(project_id),
        "index": character_service.list_state_index(project_id, character),
    }


@router.post("/projects/{project_id}/characters/states")
def apply_state(project_id: int, payload: StateChangeIn) -> dict:
    """写回角色状态（带来源标记与章节依据）。"""
    result = character_service.apply_state_change(
        project_id, character=payload.character, field=payload.field,
        value=payload.value, source=payload.source, chapter=payload.chapter,
        evidence=payload.evidence,
    )
    character_service.sync_state_index(project_id)
    return result


@router.put("/projects/{project_id}/characters/field")
def update_field(project_id: int, payload: CharacterFieldIn) -> dict:
    """更新深度设定字段（性格/价值观/说话风格/关系网络/秘密与认知边界…）。"""
    return character_service.update_character_field(
        project_id, character=payload.character, field=payload.field, value=payload.value
    )


@router.get("/projects/{project_id}/characters/memory")
def list_memory(project_id: int, character: str | None = None) -> list[dict]:
    return character_service.list_memory(project_id, character)


@router.post("/projects/{project_id}/characters/memory", status_code=201)
def add_memory(project_id: int, payload: MemoryIn) -> dict:
    return character_service.add_memory(
        project_id, character=payload.character, know_what=payload.know_what,
        when_known=payload.when_known, source_event=payload.source_event,
        source=payload.source,
    )


@router.get("/projects/{project_id}/characters/{name}/growth")
def growth(project_id: int, name: str) -> dict:
    """成长弧线：心理/关系/能力随章节的变化点（可跳转触发章节）。"""
    get_project_dir(project_id)
    return character_service.growth_curve(project_id, name)


# ─────────────────────────── 向量检索（M8） ───────────────────────────


@router.get("/vector/model")
def vector_model_info() -> dict:
    """嵌入模型信息（来源/体积/校验值/可选路径）。"""
    from ..services import vector_service

    return vector_service.model_info()


@router.put("/vector/model")
def set_vector_model(payload: VectorModelIn) -> dict:
    """切换嵌入器（本地模型目录 / 云嵌入 provider+model）。"""
    from ..services import vector_service

    return vector_service.set_embedder_config(
        model_dir=payload.model_dir, provider=payload.provider, model=payload.model,
        mode=payload.mode, enabled=payload.enabled,
    )


@router.post("/vector/model/prepare")
def prepare_vector_model() -> dict:
    from ..services.embedding_service import prepare
    return prepare(background=True, enable=True)


@router.get("/projects/{project_id}/vector/model")
def book_vector_model(project_id: int) -> dict:
    from ..services.vector_service import model_info
    return model_info(project_id)


@router.put("/projects/{project_id}/vector/model")
def set_book_vector_model(project_id: int, payload: VectorModelIn) -> dict:
    from ..services.vector_service import set_embedder_config
    return set_embedder_config(project_id=project_id, **payload.model_dump())


@router.get("/projects/{project_id}/vector/stats")
def vector_stats(project_id: int) -> dict:
    from ..services import vector_service

    get_project_dir(project_id)
    return vector_service.stats(project_id)


@router.post("/projects/{project_id}/vector/rebuild")
def vector_rebuild(project_id: int, background: bool = False) -> dict:
    """重建本书知识索引；保留同步调用，可选择后台执行。"""
    from ..services import vector_service

    get_project_dir(project_id)
    return vector_service.rebuild(project_id, background=background)


@router.post("/projects/{project_id}/vector/index")
def vector_index_document(project_id: int, rel_path: str = Query(...)) -> dict:
    """增量索引单个文档（章节应用后自动调用，也可手工触发）。"""
    from ..services import vector_service

    get_project_dir(project_id)
    return vector_service.index_document(project_id, rel_path)


@router.get("/projects/{project_id}/vector/search")
def vector_search(project_id: int, q: str = Query(min_length=1), limit: int = 5) -> dict:
    """语义召回（返回命中条目与相似度，供审计）。"""
    from ..services import vector_service

    get_project_dir(project_id)
    hits = vector_service.semantic_search(project_id, q, limit=limit)
    return {"query": q, "hits": hits, "count": len(hits),
            "ready": vector_service.vector_ready(project_id)}


# ─────────────────────────── 拆书资产库（M4 / Task 44） ───────────────────────────


@router.get("/projects/{project_id}/teardown")
def list_teardown_targets(project_id: int) -> dict:
    """已导入的拆解目标（含题材、字数、是否已拆解、事实卡条数）。"""
    from ..services import teardown_service

    get_project_dir(project_id)
    return {
        "targets": teardown_service.list_targets(project_id),
        "dimensions": [item["key"] for item in teardown_service.DIMENSIONS],
    }


@router.post("/projects/{project_id}/teardown/import", status_code=201)
def import_teardown_target(project_id: int, payload: TeardownImportIn) -> dict:
    """导入拆解目标文本（落 `拆书/{目标}/原文.md`）。"""
    from ..services import teardown_service

    return teardown_service.import_target(
        project_id, target_name=payload.target_name, content=payload.content,
        genre=payload.genre, source_note=payload.source_note,
    )


@router.post("/projects/{project_id}/teardown/analyze")
def analyze_teardown(project_id: int, payload: TeardownAnalyzeIn) -> dict:
    """六维拆解 + 双时间线 + 事实卡（事实卡强制附章节依据，缺则标注「依据缺失」）。"""
    from ..services import teardown_service

    return teardown_service.analyze(project_id, payload.target, use_ai=payload.use_ai,
                                    extra_note=payload.extra_note)


@router.get("/projects/{project_id}/teardown/facts")
def list_teardown_facts(project_id: int, target: str | None = None) -> dict:
    from ..services import teardown_service

    facts = teardown_service.list_facts(project_id, target)
    return {"facts": facts, "count": len(facts),
            "missing_evidence": sum(1 for item in facts
                                    if str(item.get("evidence") or "") in ("", "依据缺失"))}


@router.get("/projects/{project_id}/teardown/recall")
def recall_teardown(project_id: int, genre: str = "", limit: int = 6) -> dict:
    """按题材召回拆解事实（只返回有章节依据的条目）。"""
    from ..services import teardown_service

    return teardown_service.recall_for_genre(project_id, genre=genre, limit=limit)


@router.get("/projects/{project_id}/teardown/artifact")
def read_teardown_artifact(project_id: int, target: str = Query(...),
                           kind: str = Query("facts")) -> dict:
    """读取拆解产物：source / analysis（六维）/ timeline（双时间线）/ facts（事实卡）。"""
    from ..services import teardown_service

    return teardown_service.read_artifact(project_id, target, kind)


@router.delete("/projects/{project_id}/teardown")
def delete_teardown_target(project_id: int, target: str = Query(...)) -> dict:
    from ..services import teardown_service

    return teardown_service.delete_target(project_id, target)


# ─────────────────────────── 发布导出（M5 / Task 66） ───────────────────────────


@router.get("/publish/platforms")
def list_publish_platforms() -> list[dict]:
    """平台清单（自定整理规则：标题格式/缩进/建议字数/注意事项）。"""
    from ..services import publish_service

    return publish_service.list_platforms()


@router.post("/projects/{project_id}/publish/export")
def build_publish_export(project_id: int, payload: PublishExportIn) -> dict:
    """按平台模板整理发布稿：内容 + 统计 + 提交前自检清单 + 告警（无凭据）。"""
    from ..services import publish_service

    get_project_dir(project_id)
    return publish_service.build_publish_export(
        project_id, platform=payload.platform, only_completed=payload.only_completed
    )
