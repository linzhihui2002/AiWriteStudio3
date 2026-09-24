"""生图工坊接口：图片供应商 / 参考图上传 / 生成任务 / 画廊记录。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from ..services import image_provider_service, image_service
from ..services.errors import InvalidOperationError
from ..services.image_service import MEDIA_TYPES

router = APIRouter(prefix="/api/images", tags=["images"])


# ─────────────────────────── 请求模型 ───────────────────────────


class ImageProviderIn(BaseModel):
    provider_id: str
    display_name: str = ""
    model_id: str
    base_url: str = ""
    api_key: str | None = Field(default=None, description="留空表示不修改已存 Key")
    timeout_seconds: int = image_provider_service.DEFAULT_TIMEOUT
    enabled: bool | None = None


class ImageEnableIn(BaseModel):
    enabled: bool = True


class ImageHealthIn(BaseModel):
    provider_id: str


class ImageGenerateIn(BaseModel):
    provider_id: str
    prompt: str
    size: str = "auto"
    quality: str = "auto"
    output_format: str = "png"
    background: bool = False
    moderation: str = "auto"
    n: int = 1
    input_upload_ids: list[str] = Field(default_factory=list)


class ImageRecordPatchIn(BaseModel):
    favorite: bool | None = None
    prompt: str | None = None


# ─────────────────────────── 供应商 ───────────────────────────


@router.get("/providers")
def list_image_providers() -> dict:
    return {"providers": image_provider_service.list_providers()}


@router.post("/providers", status_code=201)
def upsert_image_provider(payload: ImageProviderIn) -> dict:
    return image_provider_service.upsert_provider(
        payload.provider_id,
        display_name=payload.display_name,
        model_id=payload.model_id,
        base_url=payload.base_url,
        api_key=payload.api_key,
        timeout_seconds=payload.timeout_seconds,
        enabled=payload.enabled,
    )


@router.delete("/providers/{provider_id}")
def delete_image_provider(provider_id: str) -> dict:
    return image_provider_service.delete_provider(provider_id)


@router.post("/providers/{provider_id}/enable")
def enable_image_provider(provider_id: str, payload: ImageEnableIn) -> dict:
    return image_provider_service.set_enabled(provider_id, payload.enabled)


@router.post("/providers/health")
def health_check_image_provider(payload: ImageHealthIn) -> dict:
    if not payload.provider_id:
        raise InvalidOperationError("需要提供 provider_id")
    return image_provider_service.health_check(payload.provider_id)


# ─────────────────────────── 参考图上传 ───────────────────────────


@router.put("/uploads")
async def upload_reference_image(request: Request) -> dict:
    """参考图上传：请求体为原始图片字节（png/jpeg/webp，≤5MB）。"""
    data = await request.body()
    media_type = request.headers.get("content-type", "")
    filename = request.query_params.get("filename", "")
    return image_service.save_upload(data, media_type, filename)


# ─────────────────────────── 生成任务 ───────────────────────────


@router.post("/generate")
def generate_images(payload: ImageGenerateIn) -> dict:
    """发起生成：有 input_upload_ids 即走垫图/编辑（images/edits）。

    任务在后台线程执行，前端轮询 ``GET /jobs?active=1`` 跟踪进度。
    """
    job = image_service.create_job(
        payload.provider_id,
        payload.prompt,
        size=payload.size,
        quality=payload.quality,
        output_format=payload.output_format,
        background=payload.background,
        moderation=payload.moderation,
        n=payload.n,
        input_upload_ids=payload.input_upload_ids,
    )
    return {"job": job}


@router.get("/jobs")
def list_image_jobs(active: bool = False, limit: int = 50) -> dict:
    return {"jobs": image_service.list_jobs(active_only=active, limit=limit)}


@router.get("/jobs/{job_id}")
def get_image_job(job_id: int) -> dict:
    return {"job": image_service.get_job(job_id)}


@router.post("/jobs/{job_id}/cancel")
def cancel_image_job(job_id: int) -> dict:
    """取消任务（尽力而为：请求已发出时，完成阶段会丢弃产物）。"""
    return {"job": image_service.cancel_job(job_id)}


# ─────────────────────────── 画廊记录 ───────────────────────────


@router.get("/records")
def list_image_records(
    favorite: bool | None = None, search: str = "", limit: int = 100
) -> dict:
    return {
        "records": image_service.list_records(
            favorite=favorite, search=search, limit=limit
        )
    }


@router.patch("/records/{record_id}")
def patch_image_record(record_id: int, payload: ImageRecordPatchIn) -> dict:
    return {"record": image_service.patch_record(
        record_id, favorite=payload.favorite, prompt=payload.prompt
    )}


@router.delete("/records/{record_id}")
def delete_image_record(record_id: int) -> dict:
    return image_service.delete_record(record_id)


@router.get("/records/{record_id}/file")
def read_image_record_file(record_id: int) -> FileResponse:
    """返回图片文件。"""
    path = image_service.record_file_path(record_id)
    media_type = MEDIA_TYPES.get(path.suffix.lower())
    if media_type is None:
        raise HTTPException(status_code=415, detail=f"不支持的图片格式：{path.suffix}")
    return FileResponse(path, media_type=media_type)


@router.post("/reindex")
def reindex_image_records() -> dict:
    """扫盘重建画廊索引（事实源 = 磁盘图片 + sidecar JSON）。"""
    return image_service.reindex()
