"""项目服务：开书即生成「文件夹工作区」+ 项目档案 + 归档。

设计要点
--------
- 开书时按资产种类初始化完整目录与空 md 文件（见 :data:`WORKSPACE_DIRS` / :data:`WORKSPACE_FILES`）；
- **静态设定（``设定/``）与动态状态（``状态/``）严格分离**：前者「按需查」，后者「每章必读」，
  供后续 ContextService 区分注入策略，绝不合并；
- 初始化文件「内容为空」——frontmatter 里放一个可解析的 ``标题``，正文留空，
  因此它们天然属于「空文件」（可被物理删除，与 spec 的空文件口径一致）；
- 所有写入走原子写；失败时回滚已创建的目录；
- SQLite ``projects`` 表只是索引，事实源是 ``projects/{书名}/``。
"""

from __future__ import annotations

import re
import shutil
from datetime import datetime
from pathlib import Path

from .. import config, db
from .errors import (
    InvalidOperationError,
    ProjectExistsError,
    ProjectNotFoundError,
)
from .fs_utils import (
    atomic_write_bytes,
    atomic_write_text,
    compose_document,
    read_text,
    sanitize_project_name,
    split_frontmatter,
)

PROJECT_FILE = "project.md"

# 工作区目录（含「章节」空目录）
WORKSPACE_DIRS: tuple[str, ...] = ("备忘录", "大纲", "设定", "状态", "章节")

# 工作区初始化文件（相对项目根的 POSIX 路径）
WORKSPACE_FILES: tuple[str, ...] = (
    "备忘录/临时想法.md",
    "大纲/大纲.md",
    "大纲/章纲.md",
    "设定/世界设定.md",
    "设定/势力设定.md",
    "设定/人物设定.md",
    "设定/物品设定.md",
    "设定/技能设定.md",
    "设定/场景设定.md",
    "设定/伏笔管理.md",
    "状态/时间线.md",
    "状态/角色状态.md",
    "状态/资源账本.md",
)

# 静态设定目录（按需查）与动态状态目录（每章必读）——必须分离
STATIC_SETTING_DIR = "设定"
DYNAMIC_STATE_DIR = "状态"

_ONE_LINER_RE = re.compile(r"^##\s*一句话设定\s*\n+(.*?)(?:\n#|\Z)", re.MULTILINE | re.DOTALL)

# 封面：存进项目目录（cover.png / cover.jpg / cover.webp），随目录改名/移动自然跟随。
# 注意：备份包 export_project_package 只收文本后缀，二进制封面不进包（已知限制）。
COVER_EXTS: tuple[str, ...] = ("png", "jpg", "jpeg", "webp")
_COVER_MEDIA_BY_EXT = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
}
_COVER_EXT_BY_MEDIA = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}
MAX_COVER_BYTES = 5 * 1024 * 1024


def _now_iso() -> str:
    """本地时区 ISO 时间戳（如 2026-09-19T23:59:59+08:00）。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def projects_dir() -> Path:
    """当前的项目根目录（每次调用时读取 config，便于测试覆盖）。"""
    return Path(config.PROJECTS_DIR)


def project_dir_of(name: str) -> Path:
    """由（已清洗的）书名得到项目目录。"""
    return projects_dir() / name


# ─────────────────────────── 开书 ───────────────────────────


def _placeholder_document(rel_path: str) -> str:
    """初始化文件的占位内容：仅 frontmatter 标题 + 空正文（便于后续解析，且仍属「空文件」）。"""
    title = Path(rel_path).stem
    return compose_document({"标题": title}, "")


def _copy_template_structure(source: Path, target: Path) -> None:
    """按模板目录结构复制到目标目录（保留文件内容）。"""
    for item in sorted(source.rglob("*")):
        rel = item.relative_to(source)
        destination = target / rel
        if item.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
        else:
            atomic_write_bytes(destination, item.read_bytes())


def _resolve_template(template: str | Path) -> Path:
    """解析开书模板：优先当目录路径，其次按模板名查 ``templates/``。"""
    candidate = Path(template)
    if candidate.is_dir():
        return candidate

    from .template_service import template_path  # 延迟导入避免循环依赖

    return template_path(str(template))


def _init_workspace(target: Path, template: str | Path | None) -> None:
    """初始化目录工作区（不含 project.md）。"""
    if template is not None:
        _copy_template_structure(_resolve_template(template), target)
        return

    for rel_dir in WORKSPACE_DIRS:
        (target / rel_dir).mkdir(parents=True, exist_ok=True)
    for rel_file in WORKSPACE_FILES:
        atomic_write_text(target / rel_file, _placeholder_document(rel_file))


def _write_project_file(
    target: Path,
    *,
    name: str,
    genre: str,
    platform: str,
    protagonist: str,
    one_liner: str,
    created_at: str,
) -> None:
    meta = {
        "书名": name,
        "题材": genre or "",
        "平台": platform or "",
        "主角": protagonist or "",
        "创建时间": created_at,
    }
    body = f"# {name}\n\n## 一句话设定\n\n{(one_liner or '').strip()}\n"
    atomic_write_text(target / PROJECT_FILE, compose_document(meta, body))


def create_project(
    name: str,
    genre: str = "",
    platform: str = "",
    protagonist: str = "",
    one_liner: str = "",
    template: str | Path | None = None,
) -> dict:
    """开书：按模板初始化完整目录工作区 + ``project.md``，并写入 SQLite 索引。

    重名策略：**明确报错**（不静默加后缀）。同名项目已存在（数据库已有同名记录，
    或磁盘上 ``projects/{书名}/`` 已存在）时抛 :class:`ProjectExistsError`，
    由前端提示用户改名或先归档/删除已有项目。
    """
    safe_name = sanitize_project_name(name)
    root = projects_dir()
    root.mkdir(parents=True, exist_ok=True)
    target = root / safe_name

    with db.get_conn() as conn:
        existing = conn.execute(
            "SELECT id FROM projects WHERE name = ?", (safe_name,)
        ).fetchone()
    if existing is not None:
        raise ProjectExistsError(f"项目「{safe_name}」已存在，请换一个书名")
    if target.exists():
        raise ProjectExistsError(
            f"目录 projects/{safe_name} 已存在，请换一个书名或先清理该目录"
        )

    created_at = _now_iso()
    target.mkdir(parents=True)
    try:
        _init_workspace(target, template)
        _write_project_file(
            target,
            name=safe_name,
            genre=genre,
            platform=platform,
            protagonist=protagonist,
            one_liner=one_liner,
            created_at=created_at,
        )
        with db.get_conn() as conn:
            cursor = conn.execute(
                "INSERT INTO projects (name, path, created_at, archived) VALUES (?, ?, ?, 0)",
                (safe_name, str(target), created_at),
            )
            project_id = int(cursor.lastrowid or 0)
            row = conn.execute(
                "SELECT id, name, path, created_at, archived FROM projects WHERE id = ?",
                (project_id,),
            ).fetchone()
    except BaseException:
        shutil.rmtree(target, ignore_errors=True)
        raise

    return _row_to_project(row, genre=genre, platform=platform,
                           protagonist=protagonist, one_liner=one_liner)


# ─────────────────────────── 查询 ───────────────────────────


def _row_to_project(
    row,
    *,
    genre: str | None = None,
    platform: str | None = None,
    protagonist: str | None = None,
    one_liner: str | None = None,
    has_cover: bool = False,
) -> dict:
    return {
        "id": int(row["id"]),
        "name": row["name"],
        "path": row["path"],
        "created_at": row["created_at"],
        "archived": bool(row["archived"]),
        "genre": genre,
        "platform": platform,
        "protagonist": protagonist,
        "one_liner": one_liner,
        "has_cover": has_cover,
    }


def fetch_project_row(project_id: int):
    """取项目行；不存在时抛 :class:`ProjectNotFoundError`。"""
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT id, name, path, created_at, archived FROM projects WHERE id = ?",
            (project_id,),
        ).fetchone()
    if row is None:
        raise ProjectNotFoundError(f"项目不存在：id={project_id}")
    return row


def get_project_dir(project_id: int) -> tuple[dict, Path]:
    """返回 ``(项目行, 项目目录)``；目录缺失时抛 :class:`ProjectNotFoundError`。"""
    row = fetch_project_row(project_id)
    directory = project_dir_of(row["name"])
    if not directory.is_dir():
        raise ProjectNotFoundError(
            f"项目目录缺失：{directory}（索引与磁盘不一致，可尝试重建索引）"
        )
    return dict(row), directory


def read_project_document(project_dir: Path) -> tuple[dict, str]:
    """读取 ``project.md`` 的 frontmatter 与正文。"""
    path = Path(project_dir) / PROJECT_FILE
    if not path.is_file():
        return {}, ""
    return split_frontmatter(read_text(path))


def list_projects(include_archived: bool = False) -> list[dict]:
    """列出项目（默认过滤已归档）。"""
    sql = "SELECT id, name, path, created_at, archived FROM projects"
    params: tuple = ()
    if not include_archived:
        sql += " WHERE archived = 0"
    sql += " ORDER BY archived ASC, created_at DESC, id DESC"

    with db.get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()

    projects: list[dict] = []
    for row in rows:
        directory = project_dir_of(row["name"])
        meta, _body = read_project_document(directory)
        projects.append(
            _row_to_project(
                row,
                genre=meta.get("题材"),
                platform=meta.get("平台"),
                protagonist=meta.get("主角"),
                has_cover=cover_path_of(directory) is not None,
            )
        )
    return projects


def get_project(project_id: int) -> dict:
    """取单个项目详情（数据库字段 + project.md 元信息）。"""
    row, directory = get_project_dir(project_id)
    meta, body = read_project_document(directory)
    match = _ONE_LINER_RE.search(body)
    return _row_to_project(
        row,
        genre=meta.get("题材"),
        platform=meta.get("平台"),
        protagonist=meta.get("主角"),
        one_liner=(match.group(1).strip() if match else None),
        has_cover=cover_path_of(directory) is not None,
    )


# ─────────────────────────── 归档 ───────────────────────────


def set_archived(project_id: int, archived: bool = True) -> dict:
    """归档 / 取消归档（只改索引标记，不动磁盘文件）。"""
    fetch_project_row(project_id)
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE projects SET archived = ? WHERE id = ?",
            (1 if archived else 0, project_id),
        )
    return get_project(project_id)


def archive_project(project_id: int) -> dict:
    return set_archived(project_id, True)


def unarchive_project(project_id: int) -> dict:
    return set_archived(project_id, False)


# ─────────────────────────── 封面 ───────────────────────────


def cover_path_of(project_dir: Path) -> Path | None:
    """返回项目目录下的封面文件路径；未设置时返回 None。"""
    for ext in COVER_EXTS:
        candidate = Path(project_dir) / f"cover.{ext}"
        if candidate.is_file():
            return candidate
    return None


def cover_media_of(cover_path: Path) -> str:
    return _COVER_MEDIA_BY_EXT.get(cover_path.suffix.lower().lstrip("."), "application/octet-stream")


def _remove_covers(directory: Path) -> None:
    for ext in COVER_EXTS:
        (Path(directory) / f"cover.{ext}").unlink(missing_ok=True)


def save_cover(project_id: int, data: bytes, media_type: str) -> dict:
    """写入封面（先清旧封面再原子写）；仅支持 png/jpg/webp，≤5MB。"""
    ext = _COVER_EXT_BY_MEDIA.get((media_type or "").split(";")[0].strip().lower())
    if ext is None:
        raise InvalidOperationError("封面仅支持 png / jpg / webp 图片")
    if not data:
        raise InvalidOperationError("封面内容为空")
    if len(data) > MAX_COVER_BYTES:
        raise InvalidOperationError("封面不能超过 5MB")

    _row, directory = get_project_dir(project_id)
    _remove_covers(directory)
    atomic_write_bytes(directory / f"cover.{ext}", data)
    return {"has_cover": True}


def delete_cover(project_id: int) -> dict:
    _row, directory = get_project_dir(project_id)
    _remove_covers(directory)
    return {"has_cover": False}


# ─────────────────────────── 更新元信息 ───────────────────────────


def _rewrite_one_liner(body: str, one_liner: str) -> str:
    """重写正文「## 一句话设定」段；无该段时追加到末尾。"""
    section = f"## 一句话设定\n\n{one_liner.strip()}\n"
    if _ONE_LINER_RE.search(body):
        return _ONE_LINER_RE.sub(lambda _m: section, body, count=1)
    return body.rstrip() + f"\n\n{section}"


def update_project(
    project_id: int,
    *,
    name: str | None = None,
    genre: str | None = None,
    platform: str | None = None,
    protagonist: str | None = None,
    one_liner: str | None = None,
) -> dict:
    """更新项目元信息（project.md frontmatter + 一句话设定段）；支持改书名。

    改书名 = 重命名磁盘目录 + 同步 DB 索引 + 迁移快照目录与 snapshot_path 前缀。
    """
    row, directory = get_project_dir(project_id)
    old_name = row["name"]

    new_name = old_name
    if name is not None and name.strip() and name.strip() != old_name:
        new_name = sanitize_project_name(name)
        with db.get_conn() as conn:
            duplicate = conn.execute(
                "SELECT id FROM projects WHERE name = ? AND id != ?", (new_name, project_id)
            ).fetchone()
        if duplicate is not None:
            raise ProjectExistsError(f"项目「{new_name}」已存在，请换一个书名")
        new_dir = projects_dir() / new_name
        if new_dir.exists():
            raise ProjectExistsError(f"目录 projects/{new_name} 已存在，请换一个书名或先清理该目录")

    meta, body = read_project_document(directory)
    if name is not None:
        meta["书名"] = new_name
    if genre is not None:
        meta["题材"] = genre.strip()
    if platform is not None:
        meta["平台"] = platform.strip()
    if protagonist is not None:
        meta["主角"] = protagonist.strip()
    if one_liner is not None:
        body = _rewrite_one_liner(body, one_liner)

    if new_name != old_name:
        new_dir = projects_dir() / new_name
        directory.rename(new_dir)
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE projects SET name = ?, path = ? WHERE id = ?",
                (new_name, str(new_dir), project_id),
            )
        # 快照按项目名存放：迁移目录并同步 snapshot_path 前缀，保证历史快照可用
        old_snap = config.snapshots_dir() / old_name
        new_snap = config.snapshots_dir() / new_name
        if old_snap.is_dir() and not new_snap.exists():
            old_snap.rename(new_snap)
        old_prefix, new_prefix = str(old_snap), str(new_snap)
        with db.get_conn() as conn:
            rows = conn.execute(
                "SELECT id, snapshot_path FROM snapshots WHERE project_id = ?",
                (project_id,),
            ).fetchall()
            for snapshot in rows:
                path_text = str(snapshot["snapshot_path"])
                if path_text.startswith(old_prefix):
                    conn.execute(
                        "UPDATE snapshots SET snapshot_path = ? WHERE id = ?",
                        (new_prefix + path_text[len(old_prefix):], snapshot["id"]),
                    )
        directory = new_dir

    atomic_write_text(directory / PROJECT_FILE, compose_document(meta, body))
    return get_project(project_id)
