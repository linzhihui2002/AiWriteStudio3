"""模型接入与引擎路由接口：供应商 / 引擎状态 / 任务留痕 / 用量统计。"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from ..engine import router as engine_router
from ..services import (
    generation_service,
    provider_service,
    secret_store,
    settings_service,
    token_service,
)
from ..services.errors import InvalidOperationError
from ..services.project_service import get_project_dir

router = APIRouter(prefix="/api", tags=["models"])


# ─────────────────────────── 请求模型 ───────────────────────────


class ProviderModelIn(BaseModel):
    id: str
    name: str = ""
    context_window: int = Field(default=0, description="0 表示用默认值")
    max_tokens: int = Field(default=0, description="0 表示用默认值")


class ProviderIn(BaseModel):
    provider_id: str
    display_name: str = ""
    base_url: str = ""
    models: list[ProviderModelIn] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)
    api_key: str | None = Field(default=None, description="留空表示不修改已存 Key")
    timeout_seconds: int = 60
    source: str = "workbench_custom"
    enabled: bool | None = None


class DefaultTargetIn(BaseModel):
    provider_id: str = ""
    model_id: str = ""


class EnableIn(BaseModel):
    enabled: bool = True


class ModelsQueryIn(BaseModel):
    provider_id: str | None = None
    base_url: str | None = None
    api_key: str | None = None


class ImportYamlIn(BaseModel):
    yaml_text: str


class EnginesPatchIn(BaseModel):
    patch: dict


class ProjectEngineIn(BaseModel):
    default: str | None = None
    provider: str | None = None
    model: str | None = None
    overrides: dict | None = None


# ─────────────────────────── 供应商 ───────────────────────────


@router.get("/providers")
def list_providers() -> dict:
    return {
        "providers": provider_service.list_providers(),
        "capability_tags": provider_service.CAPABILITY_TAGS,
        "cipher": secret_store.cipher_mode(),
        "defaults": provider_service.get_default_target(),
    }


@router.post("/providers", status_code=201)
def upsert_provider(payload: ProviderIn) -> dict:
    """新建/更新供应商（``api_key`` 只写入安全存储，不入库、不回显）。"""
    return provider_service.upsert_provider(
        payload.provider_id,
        display_name=payload.display_name,
        base_url=payload.base_url,
        models=[item.model_dump() for item in payload.models],
        capabilities=payload.capabilities,
        api_key=payload.api_key,
        timeout_seconds=payload.timeout_seconds,
        source=payload.source,
        enabled=payload.enabled,
    )


@router.put("/providers/default")
def set_default_provider(payload: DefaultTargetIn) -> dict:
    """设置/清除全局默认模型（供应商 + 模型）；未显式指定模型时使用它。"""
    return provider_service.set_default_target(payload.provider_id, payload.model_id)


@router.delete("/providers/{provider_id}")
def delete_provider(provider_id: str) -> dict:
    return provider_service.delete_provider(provider_id)


@router.post("/providers/{provider_id}/enable")
def enable_provider(provider_id: str, payload: EnableIn) -> dict:
    """启用/停用（启用前应已通过健康检查；投影随之更新）。"""
    return provider_service.set_enabled(provider_id, payload.enabled)


@router.post("/providers/health")
def health_check(payload: ModelsQueryIn) -> dict:
    """健康检查：保存后做最小请求，成功才建议启用。"""
    if not payload.provider_id:
        raise InvalidOperationError("需要提供 provider_id")
    return provider_service.health_check(payload.provider_id)


@router.post("/providers/models")
def list_models(payload: ModelsQueryIn) -> dict:
    """按 baseURL 拉取模型列表（可用已存 Key，也可临时传入）。"""
    if payload.provider_id:
        resolved = provider_service.resolve(payload.provider_id)
        base_url = payload.base_url or resolved["base_url"]
        api_key = payload.api_key or resolved["api_key"]
    else:
        if not payload.base_url or not payload.api_key:
            raise InvalidOperationError("未提供 provider_id 时，必须同时提供 base_url 与 api_key")
        base_url, api_key = payload.base_url, payload.api_key
    models = provider_service.list_models(base_url, api_key)
    return {"base_url": base_url, "count": len(models), "models": models}


@router.post("/providers/project")
def project_to_dsh_home() -> dict:
    """把启用中的供应商单向投影到独立 dsh-home（不写 agent-default-model）。"""
    return provider_service.project_to_dsh_home()


@router.post("/providers/import")
def import_providers(payload: ImportYamlIn) -> dict:
    """从用户主动粘贴的 dsh settings.yaml 文本导入非敏感配置（Key 必须重录）。"""
    return provider_service.import_from_yaml_text(payload.yaml_text)


@router.post("/providers/import-models-md")
def import_models_md() -> dict:
    """把 ``模型API.md`` 中的明文 Key 迁入安全存储（原文件不动）。"""
    return secret_store.import_keys_from_models_md()


# ─────────────────────────── 引擎路由 ───────────────────────────


@router.get("/engines")
def engines() -> dict:
    """引擎可用性 + 决策表 + 覆盖状态。"""
    return engine_router.describe_routing()


@router.put("/engines")
def update_engines(payload: EnginesPatchIn) -> dict:
    """更新全局引擎设置（``engines.default`` / ``engines.overrides``）。"""
    settings_service.update_settings({"engines": payload.patch})
    return engine_router.describe_routing()


@router.get("/projects/{project_id}/engines")
def project_engines(project_id: int) -> dict:
    _row, project_dir = get_project_dir(project_id)
    return {
        "config": engine_router.read_project_engine_config(project_dir),
        "effective": {
            "章节正文": engine_router.resolve_route("章节正文", project_dir=project_dir),
            "结构化抽取": engine_router.resolve_route("结构化抽取", project_dir=project_dir),
        },
    }


@router.put("/projects/{project_id}/engines")
def update_project_engines(project_id: int, payload: ProjectEngineIn) -> dict:
    _row, project_dir = get_project_dir(project_id)
    patch = {key: value for key, value in payload.model_dump().items() if value is not None}
    return engine_router.write_project_engine_config(project_dir, patch)


# ─────────────────────────── 任务与用量 ───────────────────────────


@router.get("/tasks")
def list_tasks(project_id: int | None = None, limit: int = 50) -> list[dict]:
    return generation_service.list_tasks(project_id, limit=limit)


@router.get("/tasks/{task_id}")
def get_task(task_id: int) -> dict:
    return generation_service.get_task(task_id)


@router.post("/tasks/{task_id}/cancel")
def cancel_task(task_id: int, session_id: str = "") -> dict:
    return generation_service.cancel_task(task_id, session_id=session_id)


@router.get("/usage")
def usage(project_id: int | None = None, days: int = 30) -> dict:
    """Token 用量与成本（仪表盘数据源）。"""
    return token_service.summary(project_id=project_id, days=days)


@router.get("/usage/recent")
def usage_recent(project_id: int | None = None, limit: int = 50) -> list[dict]:
    return token_service.recent(project_id=project_id, limit=limit)