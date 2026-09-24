"""项目 / 文档树 / 章节 REST 接口（前缀 ``/api``）。

错误约定：服务层抛出的 :class:`ServiceError` 由 ``app.py`` 注册的异常处理器
统一转换为 ``{"detail": "..."}`` + 对应状态码（400/404/409）。
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, StrictBool

from ..services import chat_preference_service, chapter_service, project_service, tree_service

router = APIRouter(prefix="/api", tags=["projects"])


# ─────────────────────────── 请求模型 ───────────────────────────


class ProjectCreateIn(BaseModel):
    name: str = Field(min_length=1, description="书名（非法字符会被清洗为下划线）")
    genre: str = ""
    platform: str = ""
    protagonist: str = ""
    one_liner: str = ""
    template: str | None = Field(default=None, description="开书模板名或目录路径")


class ProjectPatchIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, description="改书名会重命名磁盘目录并同步索引")
    genre: str | None = None
    platform: str | None = None
    protagonist: str | None = None
    one_liner: str | None = None


class ArchiveIn(BaseModel):
    archived: bool = True


class ChatDefaultsIn(BaseModel):
    permission_mode: Literal["ask", "auto", "full"]
    discussion_only: StrictBool


class NodeCreateIn(BaseModel):
    parent_rel: str = Field(default="", description="父目录相对路径，空串表示项目根")
    name: str
    is_dir: bool = False


class NodePatchIn(BaseModel):
    rel_path: str
    new_name: str | None = None
    dst_parent_rel: str | None = Field(
        default=None, description="移动目标父目录（跨分组二次确认由前端负责）"
    )


class ChapterCreateIn(BaseModel):
    title: str | None = None


class ChapterSaveIn(BaseModel):
    rel_path: str
    content: str = ""
    status: str | None = Field(default=None, description="草稿 / 完成 / 发表")
    expected_hash: str | None = None


class ChapterMetaIn(BaseModel):
    rel_path: str
    title: str | None = Field(default=None, description="章节标题；空串表示清除")
    status: str | None = Field(default=None, description="草稿 / 完成 / 发表")


class TrashRestoreIn(BaseModel):
    trash_rel: str = Field(description="回收站条目 id，来自 GET .../trash")


# ─────────────────────────── 响应模型 ───────────────────────────


class ProjectOut(BaseModel):
    id: int
    name: str
    path: str
    created_at: str
    archived: bool
    genre: str | None = None
    platform: str | None = None
    protagonist: str | None = None
    one_liner: str | None = None
    has_cover: bool = False


class CoverStateOut(BaseModel):
    has_cover: bool


class ChatDefaultsOut(ChatDefaultsIn):
    source: Literal["book", "global"]


class ChapterOut(BaseModel):
    rel_path: str
    file_name: str
    number: int | None = None
    title: str = ""
    status: str
    word_count: int
    contract_id: str = ""
    is_empty: bool
    size: int
    mtime: float


class ChapterDetailOut(BaseModel):
    rel_path: str
    file_name: str
    content: str
    meta: dict
    body: str
    title: str = ""
    status: str
    word_count: int
    contract_id: str = ""
    is_empty: bool
    hash: str | None = None
    mtime: float | None = None


class TreeNodeOut(BaseModel):
    name: str
    rel_path: str
    type: str
    is_empty: bool = False
    size: int | None = None
    mtime: float | None = None
    file_count: int | None = None
    children: list["TreeNodeOut"] | None = None
    is_chapter: bool | None = None
    number: int | None = None
    title: str | None = None
    status: str | None = None
    word_count: int | None = None


TreeNodeOut.model_rebuild()


class TreeGroupOut(BaseModel):
    key: str
    name: str
    rel_path: str
    exists: bool
    nodes: list[TreeNodeOut]


class ProjectBriefOut(BaseModel):
    id: int
    name: str
    path: str
    archived: bool


class TreeOut(BaseModel):
    project: ProjectBriefOut
    groups: list[TreeGroupOut]
    root_nodes: list[TreeNodeOut]


class DeleteResultOut(BaseModel):
    rel_path: str
    type: str
    action: str
    trash_rel: str | None = None


class TrashEntryOut(BaseModel):
    trash_rel: str
    project_name: str
    rel_path: str | None = None
    kind: str | None = None
    deleted_at: str | None = None


class RestoreResultOut(BaseModel):
    rel_path: str
    restored_from: str


# ─────────────────────────── 项目 ───────────────────────────


@router.get("/projects", response_model=list[ProjectOut])
def list_projects(include_archived: bool = False) -> list[dict]:
    """项目列表（默认不含已归档）。"""
    return project_service.list_projects(include_archived=include_archived)


@router.post("/projects", response_model=ProjectOut, status_code=201)
def create_project(payload: ProjectCreateIn) -> dict:
    """开书：生成完整文件夹工作区（15 个初始路径）并登记到索引。"""
    return project_service.create_project(
        name=payload.name,
        genre=payload.genre,
        platform=payload.platform,
        protagonist=payload.protagonist,
        one_liner=payload.one_liner,
        template=payload.template,
    )


@router.get("/projects/{project_id}", response_model=ProjectOut)
def get_project(project_id: int) -> dict:
    return project_service.get_project(project_id)


@router.get("/projects/{project_id}/chat-defaults", response_model=ChatDefaultsOut)
def get_chat_defaults(project_id: int) -> dict:
    return chat_preference_service.get_chat_defaults(project_id)


@router.put("/projects/{project_id}/chat-defaults", response_model=ChatDefaultsOut)
def put_chat_defaults(project_id: int, payload: ChatDefaultsIn) -> dict:
    return chat_preference_service.update_chat_defaults(
        project_id, permission_mode=payload.permission_mode,
        discussion_only=payload.discussion_only,
    )


@router.patch("/projects/{project_id}", response_model=ProjectOut)
def patch_project(project_id: int, payload: ProjectPatchIn) -> dict:
    """更新项目元信息（题材/平台/主角/一句话简介）；改书名会重命名磁盘目录并同步索引与快照。"""
    if (
        payload.name is None
        and payload.genre is None
        and payload.platform is None
        and payload.protagonist is None
        and payload.one_liner is None
    ):
        raise HTTPException(status_code=400, detail="没有需要更新的字段")
    return project_service.update_project(
        project_id,
        name=payload.name,
        genre=payload.genre,
        platform=payload.platform,
        protagonist=payload.protagonist,
        one_liner=payload.one_liner,
    )


@router.put("/projects/{project_id}/cover", response_model=CoverStateOut)
async def put_cover(project_id: int, request: Request) -> dict:
    """上传封面：请求体为原始图片字节（png/jpg/webp，≤5MB）。"""
    data = await request.body()
    media_type = request.headers.get("content-type", "")
    return project_service.save_cover(project_id, data, media_type)


@router.get("/projects/{project_id}/cover")
def get_cover(project_id: int) -> FileResponse:
    """返回封面图片文件；未设置时 404。"""
    _row, directory = project_service.get_project_dir(project_id)
    cover = project_service.cover_path_of(directory)
    if cover is None:
        raise HTTPException(status_code=404, detail="尚未设置封面")
    return FileResponse(cover, media_type=project_service.cover_media_of(cover))


@router.delete("/projects/{project_id}/cover", response_model=CoverStateOut)
def delete_cover(project_id: int) -> dict:
    return project_service.delete_cover(project_id)


@router.post("/projects/{project_id}/archive", response_model=ProjectOut)
def archive_project(project_id: int, payload: ArchiveIn | None = None) -> dict:
    """归档 / 取消归档（``archived=false`` 即取消归档）。"""
    archived = payload.archived if payload is not None else True
    return project_service.set_archived(project_id, archived)


@router.post("/projects/{project_id}/unarchive", response_model=ProjectOut)
def unarchive_project(project_id: int) -> dict:
    return project_service.unarchive_project(project_id)


# ─────────────────────────── 文档树 ───────────────────────────


@router.get("/projects/{project_id}/tree", response_model=TreeOut)
def get_tree(project_id: int) -> dict:
    """项目目录树（按 备忘录/大纲/设定/状态/章节 分组）。"""
    return tree_service.get_file_tree(project_id)


@router.post("/projects/{project_id}/tree/node", response_model=TreeNodeOut, status_code=201)
def create_node(project_id: int, payload: NodeCreateIn) -> dict:
    """新建文件（默认 .md）或文件夹。"""
    return tree_service.create_node(
        project_id, payload.parent_rel, payload.name, payload.is_dir
    )


@router.patch("/projects/{project_id}/tree/node", response_model=TreeNodeOut)
def patch_node(project_id: int, payload: NodePatchIn) -> dict:
    """重命名（``new_name``）与/或移动（``dst_parent_rel``）。"""
    if payload.new_name is None and payload.dst_parent_rel is None:
        raise HTTPException(status_code=400, detail="需要提供 new_name 或 dst_parent_rel")

    node: dict | None = None
    if payload.dst_parent_rel is not None:
        node = tree_service.move_node(project_id, payload.rel_path, payload.dst_parent_rel)
    if payload.new_name is not None:
        current_rel = node["rel_path"] if node is not None else payload.rel_path
        node = tree_service.rename_node(project_id, current_rel, payload.new_name)
    return node  # type: ignore[return-value]


@router.delete("/projects/{project_id}/tree/node", response_model=DeleteResultOut)
def delete_node(project_id: int, rel_path: str = Query(...)) -> dict:
    """删除文件/文件夹（空文件物理删除，非空进回收站）。"""
    return tree_service.delete_node(project_id, rel_path)


# ─────────────────────────── 章节 ───────────────────────────


@router.get("/projects/{project_id}/chapters", response_model=list[ChapterOut])
def list_chapters(project_id: int) -> list[dict]:
    return chapter_service.list_chapters(project_id)


@router.post("/projects/{project_id}/chapters", response_model=ChapterDetailOut, status_code=201)
def create_chapter(project_id: int, payload: ChapterCreateIn | None = None) -> dict:
    """新建章节：自动取下一个可用编号（``章节/第NNNN章.md``）。"""
    title = payload.title if payload is not None else None
    return chapter_service.create_chapter(project_id, title)


@router.get("/projects/{project_id}/chapters/content", response_model=ChapterDetailOut)
def read_chapter(project_id: int, rel_path: str = Query(...)) -> dict:
    return chapter_service.read_chapter(project_id, rel_path)


@router.put("/projects/{project_id}/chapters/content", response_model=ChapterDetailOut)
def save_chapter(project_id: int, payload: ChapterSaveIn) -> dict:
    """保存章节正文（自动重算字数并回写 frontmatter）。"""
    return chapter_service.save_chapter(
        project_id, payload.rel_path, payload.content, payload.status,
        expected_hash=payload.expected_hash,
    )


@router.patch("/projects/{project_id}/chapters", response_model=ChapterDetailOut)
def update_chapter_meta(project_id: int, payload: ChapterMetaIn) -> dict:
    """只改章节标题 / 状态（不提交正文，避免与未保存的编辑冲突）。"""
    if payload.title is None and payload.status is None:
        raise HTTPException(status_code=400, detail="需要提供 title 或 status")
    return chapter_service.update_chapter_meta(
        project_id, payload.rel_path, title=payload.title, status=payload.status
    )


@router.delete("/projects/{project_id}/chapters", response_model=DeleteResultOut)
def delete_chapter(project_id: int, rel_path: str = Query(...)) -> dict:
    return chapter_service.delete_chapter(project_id, rel_path)


# ─────────────────────────── 回收站 ───────────────────────────


@router.get("/projects/{project_id}/trash", response_model=list[TrashEntryOut])
def list_trash(project_id: int) -> list[dict]:
    return chapter_service.list_trash(project_id)


@router.post("/projects/{project_id}/trash/restore", response_model=RestoreResultOut)
def restore_from_trash(project_id: int, payload: TrashRestoreIn) -> dict:
    return chapter_service.restore_from_trash(project_id, payload.trash_rel)
