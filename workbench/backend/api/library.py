"""M1 收尾能力接口：模板 / 索引与检索 / 快照 / 导入导出 / 冲突 / 设置 / 日志。

错误约定与 ``api/projects.py`` 一致：服务层 ``ServiceError`` → JSON ``detail`` + 状态码。
"""

from __future__ import annotations

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from ..services import (
    chapter_service,
    conflict_service,
    index_service,
    operation_log,
    settings_service,
    snapshot_service,
    template_service,
    transfer_service,
    tree_service,
)
from ..services.project_service import get_project_dir
from ..services.errors import InvalidOperationError

router = APIRouter(prefix="/api", tags=["library"])


# ─────────────────────────── 请求模型 ───────────────────────────


class TemplateSaveIn(BaseModel):
    name: str
    project_id: int


class TemplateDuplicateIn(BaseModel):
    new_name: str


class SnapshotRestoreIn(BaseModel):
    snapshot_id: int
    expected_hash: str | None = None


class ImportIn(BaseModel):
    filename: str = Field(description="原始文件名（用于取标题与后缀校验）")
    content: str
    title: str | None = None
    as_single_chapter: bool = True


class ConflictCheckIn(BaseModel):
    rel_path: str
    base_mtime: float | None = None
    base_hash: str | None = None
    current_content: str | None = None


class ConflictResolveIn(BaseModel):
    rel_path: str
    mode: str = Field(description="keep-mine / take-external")
    content: str | None = None


class SettingsPatchIn(BaseModel):
    patch: dict


class BatchDeleteIn(BaseModel):
    rel_paths: list[str]


# ─────────────────────────── 模板 ───────────────────────────


@router.get("/templates")
def list_templates() -> list[dict]:
    return template_service.list_templates()


@router.post("/templates", status_code=201)
def save_template(payload: TemplateSaveIn) -> dict:
    """把某项目当前目录结构「另存为模板」。"""
    _row, project_dir = get_project_dir(payload.project_id)
    return template_service.save_as_template(payload.name, project_dir)


@router.post("/templates/{name}/duplicate", status_code=201)
def duplicate_template(name: str, payload: TemplateDuplicateIn) -> dict:
    return template_service.duplicate_template(name, payload.new_name)


@router.delete("/templates/{name}")
def delete_template(name: str) -> dict:
    """删除模板（内置模板会被拒绝并给出复制指引）。"""
    return template_service.delete_template(name)


# ─────────────────────────── 索引与检索 ───────────────────────────


@router.post("/projects/{project_id}/index/rebuild")
def rebuild_index(project_id: int) -> dict:
    """全量重建索引（外部改文件后调用；空文件不入索引）。"""
    result = index_service.rebuild_index(project_id)
    operation_log.log(project_id, "index-rebuild", None, result)
    return result


@router.get("/projects/{project_id}/index/stats")
def index_stats(project_id: int) -> dict:
    get_project_dir(project_id)
    return index_service.index_stats(project_id)


@router.get("/projects/{project_id}/search")
def search_documents(
    project_id: int,
    q: str = Query(min_length=1),
    limit: int = 20,
    kind: str | None = None,
) -> list[dict]:
    """项目内全文检索（FTS5；不可用时降级关键词匹配）。"""
    get_project_dir(project_id)
    return index_service.search(project_id, q, limit=limit, kind=kind)


# ─────────────────────────── 快照 ───────────────────────────


@router.get("/projects/{project_id}/snapshots")
def list_snapshots(project_id: int, rel_path: str | None = None) -> list[dict]:
    get_project_dir(project_id)
    return snapshot_service.list_snapshots(project_id, rel_path)


@router.post("/projects/{project_id}/snapshots/restore")
def restore_snapshot(project_id: int, payload: SnapshotRestoreIn) -> dict:
    get_project_dir(project_id)
    result = snapshot_service.restore_snapshot(payload.snapshot_id, project_id=project_id,
                                               expected_hash=payload.expected_hash)
    operation_log.log(project_id, "snapshot-restore", result["rel_path"], result)
    return result


# ─────────────────────────── 导入导出 ───────────────────────────


@router.get("/projects/{project_id}/export")
def export_chapters(
    project_id: int,
    scope: str = Query(default="with_title", pattern="^(body|with_title)$"),
    format: str = Query(default="md", pattern="^(md|txt)$"),
    rel_paths: str | None = Query(default=None, description="逗号分隔的章节相对路径"),
) -> dict:
    """导出章节正文（返回内容，不落盘；导出产物不含凭据）。"""
    paths = [item for item in (rel_paths or "").split(",") if item.strip()] or None
    return transfer_service.export_chapters(
        project_id, scope=scope, fmt=format, rel_paths=paths
    )


@router.post("/projects/{project_id}/import", status_code=201)
def import_document(project_id: int, payload: ImportIn) -> dict:
    """导入 MD/TXT → 草稿章节。"""
    result = transfer_service.import_document(
        project_id,
        filename=payload.filename,
        content=payload.content,
        title=payload.title,
        as_single_chapter=payload.as_single_chapter,
    )
    for item in result["created"]:
        operation_log.log(project_id, "import", item["rel_path"], item)
    return result


@router.get("/projects/{project_id}/export/package")
def export_package(project_id: int) -> dict:
    """导出完整项目包（JSON；用于备份，不含凭据）。"""
    return transfer_service.export_project_package(project_id)


@router.post("/projects/import/package", status_code=201)
def restore_package(payload: dict, new_name: str | None = None) -> dict:
    """从项目包恢复（可选 new_name 避免同名冲突）。"""
    return transfer_service.restore_project_package(payload, new_name=new_name)


# ─────────────────────────── 冲突 ───────────────────────────


@router.post("/projects/{project_id}/conflict/check")
def check_conflict(project_id: int, payload: ConflictCheckIn) -> dict:
    """检查文件是否被外部改动（mtime + hash），需要时返回行级 diff。"""
    return conflict_service.check_conflict(
        project_id,
        payload.rel_path,
        base_mtime=payload.base_mtime,
        base_hash=payload.base_hash,
        current_content=payload.current_content,
    )


@router.post("/projects/{project_id}/conflict/resolve")
def resolve_conflict(project_id: int, payload: ConflictResolveIn) -> dict:
    """解决冲突：保留我的（覆盖磁盘，外部版本进快照）或采用外部（回传磁盘内容）。"""
    get_project_dir(project_id)
    if payload.mode == "keep-mine":
        result = conflict_service.resolve_keep_mine(
            project_id, payload.rel_path, payload.content or ""
        )
        operation_log.log(project_id, "conflict-keep-mine", payload.rel_path, result)
        return result
    if payload.mode == "take-external":
        return conflict_service.resolve_take_external(project_id, payload.rel_path)
    raise InvalidOperationError("mode 必须是 keep-mine 或 take-external")


# ─────────────────────────── 批量删除 ───────────────────────────


@router.post("/projects/{project_id}/tree/delete-batch")
def delete_batch(project_id: int, payload: BatchDeleteIn) -> list[dict]:
    """批量删除：空文件物理删除、非空进回收站，逐条明示去向。"""
    return tree_service.delete_nodes(project_id, payload.rel_paths)


# ─────────────────────────── 设置与日志 ───────────────────────────


@router.get("/settings")
def get_settings() -> dict:
    return settings_service.read_settings()


@router.put("/settings")
def update_settings(payload: SettingsPatchIn) -> dict:
    return settings_service.update_settings(payload.patch)


@router.post("/settings/reset")
def reset_settings() -> dict:
    return settings_service.reset_settings()


@router.get("/projects/{project_id}/log")
def project_log(project_id: int, limit: int = 50) -> list[dict]:
    get_project_dir(project_id)
    return operation_log.recent(project_id, limit=limit)


@router.get("/trash")
def global_trash() -> list[dict]:
    """全量回收站条目（跨项目）。"""
    return chapter_service.list_trash()
