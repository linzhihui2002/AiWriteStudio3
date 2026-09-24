"""章节服务：``章节/第NNNN章.md`` 的创建 / 读取 / 保存 / 删除 / 回收站恢复。

约定
----
- 章节文件名固定为 ``第NNNN章.md``（四位零填充，保证排序稳定；编号 ≥10000 时自然扩展位数）；
- frontmatter 至少含 ``状态``（草稿/完成/发表）、``字数``、``合同ID``（占位）；
- 保存时自动重算 ``字数``（正文非空白字符数），并保留调用方传入的 ``状态``；
- 删除按内容分策略：空文件物理删除，非空文件进 ``.workbench/trash/`` 可恢复；
- 所有写入走原子写。
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from .errors import (
    InvalidChapterNameError,
    InvalidOperationError,
    NodeExistsError,
    NodeNotFoundError,
)
from . import operation_log
from .fs_utils import (
    atomic_write_text,
    compose_document,
    count_words,
    delete_path_by_content,
    has_frontmatter,
    is_empty_document,
    read_text,
    read_trash_manifest,
    resolve_trash_entry,
    resolve_within,
    split_frontmatter,
    to_rel,
    trash_entry_payload,
    trash_root,
)
from .project_service import fetch_project_row, get_project_dir
from .snapshot_service import snapshot_file

CHAPTER_DIR = "章节"

# 四位零填充；编号超过 9999 时自然扩展为更多位
CHAPTER_FILENAME_RE = re.compile(r"^第(\d{4,})章\.md$")

CHAPTER_STATUSES: tuple[str, ...] = ("草稿", "完成", "发表")
DEFAULT_STATUS = "草稿"

PLACEHOLDER_CONTRACT_ID = ""


def chapter_filename(number: int) -> str:
    """生成章节文件名：``第0001章.md``。"""
    return f"第{int(number):04d}章.md"


def is_chapter_filename(name: str) -> bool:
    """是否为合法章节文件名（``第NNNN章.md``）。"""
    return bool(CHAPTER_FILENAME_RE.match(str(name)))


def parse_chapter_number(name: str) -> int | None:
    """从章节文件名解析编号；不合法返回 None。"""
    match = CHAPTER_FILENAME_RE.match(str(name))
    return int(match.group(1)) if match else None


def validate_chapter_filename(name: str) -> str:
    """校验章节文件名是否符合 ``第NNNN章.md`` 规范，不符则报错。"""
    if not is_chapter_filename(name):
        raise InvalidChapterNameError(
            f"章节文件必须命名为「第NNNN章.md」（四位零填充，如 第0001章.md）：{name}"
        )
    return name


# 「第N章」这类未做四位零填充的名字（可带 .md 后缀）
_CHAPTER_NUMBER_NAME_RE = re.compile(r"^第\s*(\d+)\s*章(?:\.md)?$")

_STRIPPABLE_SUFFIXES = {".md", ".txt", ".markdown"}


def normalize_chapter_create_name(project_dir: Path, name: str) -> tuple[str, str | None]:
    """把「章节/」下新建文件时输入的名字规范化为章节文件名。

    返回 ``(文件名, 标题)``：

    - 已是 ``第NNNN章.md`` → 原样保留，标题 ``None``；
    - ``第7章`` / ``第7章.md``（位数不足四位）→ 补齐为 ``第0007章.md``，标题 ``None``；
    - 其它任意名字 → 视为章节标题，文件名取下一个可用编号 ``第NNNN章.md``。
    """
    raw = str(name or "").strip()
    if is_chapter_filename(raw):
        return raw, None
    match = _CHAPTER_NUMBER_NAME_RE.match(raw)
    if match:
        return chapter_filename(int(match.group(1))), None
    path = Path(raw)
    title = path.stem if path.suffix.lower() in _STRIPPABLE_SUFFIXES else raw
    return chapter_filename(next_chapter_number(project_dir)), (title or None)


def chapter_placeholder(title: str | None = None) -> str:
    """新建章节的初始文档：frontmatter（标题/状态/字数/合同ID）+ 空正文。"""
    meta: dict = {}
    if title:
        meta["标题"] = str(title)
    meta["状态"] = DEFAULT_STATUS
    meta["字数"] = 0
    meta["合同ID"] = PLACEHOLDER_CONTRACT_ID
    return compose_document(meta, "")


def chapters_dir(project_dir: Path) -> Path:
    return Path(project_dir) / CHAPTER_DIR


def next_chapter_number(project_dir: Path) -> int:
    """取下一个可用编号 = 现有最大编号 + 1（不回收已删除编号）。"""
    directory = chapters_dir(project_dir)
    if not directory.is_dir():
        return 1
    numbers = [
        number
        for path in directory.iterdir()
        if path.is_file()
        for number in [parse_chapter_number(path.name)]
        if number is not None
    ]
    return (max(numbers) + 1) if numbers else 1


def _normalize_meta(meta: dict, body: str) -> dict:
    """规范化章节 frontmatter：状态/字数/合同ID 在前，字数按正文重算。"""
    ordered: dict = {}
    if meta.get("标题"):
        ordered["标题"] = meta["标题"]
    ordered["状态"] = str(meta.get("状态") or DEFAULT_STATUS)
    ordered["字数"] = count_words(body)
    ordered["合同ID"] = meta.get("合同ID") or PLACEHOLDER_CONTRACT_ID
    for key, value in meta.items():
        if key not in ordered:
            ordered[key] = value
    return ordered


def _chapter_info(project_dir: Path, path: Path) -> dict:
    text = read_text(path)
    meta, body = split_frontmatter(text)
    stat = path.stat()
    return {
        "rel_path": to_rel(project_dir, path),
        "file_name": path.name,
        "number": parse_chapter_number(path.name),
        "title": str(meta.get("标题") or ""),
        "status": str(meta.get("状态") or DEFAULT_STATUS),
        "word_count": count_words(body),
        "contract_id": str(meta.get("合同ID") or ""),
        "is_empty": is_empty_document(text),
        "size": stat.st_size,
        "mtime": stat.st_mtime,
    }


def create_chapter(project_id: int, title: str | None = None) -> dict:
    from .file_change_service import project_lock
    with project_lock(project_id):
        return _create_chapter_locked(project_id, title)


def _create_chapter_locked(project_id: int, title: str | None = None) -> dict:
    """新建章节：自动取下一个可用编号，写入 frontmatter（正文留空）。"""
    row, project_dir = get_project_dir(project_id)
    directory = chapters_dir(project_dir)
    directory.mkdir(parents=True, exist_ok=True)

    number = next_chapter_number(project_dir)
    filename = chapter_filename(number)

    atomic_write_text(directory / filename, chapter_placeholder(title))
    return read_chapter(project_id, f"{CHAPTER_DIR}/{filename}")


def list_chapters(project_id: int) -> list[dict]:
    """列出章节（按编号升序，含状态与字数）。"""
    _row, project_dir = get_project_dir(project_id)
    directory = chapters_dir(project_dir)
    if not directory.is_dir():
        return []

    chapters = [
        _chapter_info(project_dir, path)
        for path in directory.iterdir()
        if path.is_file() and is_chapter_filename(path.name)
    ]
    chapters.sort(key=lambda item: (item["number"] if item["number"] is not None else 0))
    return chapters


def _chapter_path(project_dir: Path, rel_path: str) -> Path:
    path = resolve_within(project_dir, rel_path)
    if not path.is_file():
        raise NodeNotFoundError(f"章节文件不存在：{rel_path}")
    return path


def require_chapter_path(project_id: int, rel_path: str) -> Path:
    """章节专属操作只接受本书章节目录下现有的规范章节文件。"""
    _row, project_dir = get_project_dir(project_id)
    path = resolve_within(project_dir, rel_path)
    canonical = f"{CHAPTER_DIR}/{path.name}"
    if (str(rel_path).replace("\\", "/") != canonical or
            path.parent != (project_dir / CHAPTER_DIR).resolve() or
            not is_chapter_filename(path.name)):
        raise InvalidOperationError(f"该操作仅适用于章节文件：{rel_path}")
    if not path.is_file():
        raise NodeNotFoundError(f"章节文件不存在：{rel_path}")
    return path


def read_chapter(project_id: int, rel_path: str) -> dict:
    from .file_change_service import project_lock
    with project_lock(project_id):
        return _read_chapter_locked(project_id, rel_path)


def _read_chapter_locked(project_id: int, rel_path: str) -> dict:
    """读取章节：返回原始内容、解析后的 frontmatter/正文与字数。"""
    _row, project_dir = get_project_dir(project_id)
    path = _chapter_path(project_dir, rel_path)
    text = read_text(path)
    from .file_change_service import content_hash
    meta, body = split_frontmatter(text)
    return {
        "rel_path": to_rel(project_dir, path),
        "file_name": path.name,
        "content": text,
        "hash": content_hash(text),
        "mtime": path.stat().st_mtime,
        "meta": meta,
        "body": body,
        "title": str(meta.get("标题") or ""),
        "status": str(meta.get("状态") or DEFAULT_STATUS),
        "word_count": count_words(body),
        "contract_id": str(meta.get("合同ID") or ""),
        "is_empty": is_empty_document(text),
    }


def save_chapter(
    project_id: int,
    rel_path: str,
    content: str,
    status: str | None = None,
    expected_hash: str | None = None,
) -> dict:
    from .file_change_service import assert_version, project_lock
    with project_lock(project_id):
        if expected_hash is not None:
            _, project_dir = get_project_dir(project_id)
            path = _chapter_path(project_dir, rel_path)
            assert_version(rel_path, read_text(path), expected_hash)
        return _save_chapter_locked(project_id, rel_path, content, status)


MAX_CHAPTER_TITLE_LENGTH = 60


def update_chapter_meta(
    project_id: int,
    rel_path: str,
    *,
    title: str | None = None,
    status: str | None = None,
) -> dict:
    """只更新章节 frontmatter 的 ``标题`` / ``状态``，不改正文。

    - ``title is None`` 不动；``title`` 为空白串表示**清除标题**（删键，避免被回填）；
    - ``status is None`` 不动；否则必须属于 ``CHAPTER_STATUSES``。
    """
    from .file_change_service import project_lock

    with project_lock(project_id):
        path = require_chapter_path(project_id, rel_path)
        meta, body = split_frontmatter(read_text(path))

        if status is not None:
            if status not in CHAPTER_STATUSES:
                raise InvalidOperationError(
                    f"非法章节状态：{status}（可选：{'/'.join(CHAPTER_STATUSES)}）"
                )
            meta["状态"] = status

        if title is not None:
            clean = str(title).strip()
            if len(clean) > MAX_CHAPTER_TITLE_LENGTH:
                raise InvalidOperationError(
                    f"章节标题过长（≤{MAX_CHAPTER_TITLE_LENGTH} 字）：{clean[:20]}…"
                )
            if clean:
                meta["标题"] = clean
            else:
                meta.pop("标题", None)

        return _save_chapter_locked(
            project_id, rel_path, compose_document(meta, body)
        )


def _save_chapter_locked(project_id: int, rel_path: str, content: str,
                         status: str | None = None) -> dict:
    """保存章节：重算字数、保留/更新状态与合同ID，原子写落盘。

    若传入内容不含 frontmatter（前端只回传正文的简化写法），则以磁盘上已有的
    frontmatter 为基底合并，避免误丢 ``标题`` / ``合同ID`` 等元信息。
    保存前对旧内容存快照；保存后增量刷新检索索引。
    """
    row, project_dir = get_project_dir(project_id)
    path = _chapter_path(project_dir, rel_path)

    meta, body = split_frontmatter(content or "")
    if not has_frontmatter(content or ""):
        existing_meta, _existing_body = split_frontmatter(read_text(path))
        existing_meta.update(meta)
        meta = existing_meta

    is_chapter = path.parent.name == CHAPTER_DIR and is_chapter_filename(path.name)
    if status is not None and is_chapter:
        if status not in CHAPTER_STATUSES:
            raise InvalidOperationError(
                f"非法章节状态：{status}（可选：{'/'.join(CHAPTER_STATUSES)}）"
            )
        meta["状态"] = status
    normalized = _normalize_meta(meta, body) if is_chapter else meta

    snapshot_file(project_id, project_dir, row["name"], rel_path, reason="save")
    atomic_write_text(path, compose_document(normalized, body) if normalized else body)
    refresh_index(project_id, project_dir, rel_path)
    return read_chapter(project_id, rel_path)


def refresh_index(project_id: int, project_dir: Path, rel_path: str) -> None:
    """增量刷新检索索引（延迟导入避免与 index_service 循环依赖）。"""
    try:
        from .index_service import index_document

        index_document(project_id, project_dir, rel_path)
    except Exception:  # noqa: BLE001 - 索引失败不阻断保存（索引可重建）
        pass


def delete_chapter(project_id: int, rel_path: str) -> dict:
    """删除章节：空文件物理删除，非空文件进回收站（可恢复）。"""
    row, project_dir = get_project_dir(project_id)
    _chapter_path(project_dir, rel_path)
    result = delete_path_by_content(project_dir, rel_path, row["name"])
    _after_delete(project_id, project_dir, rel_path, result)
    return result


def _after_delete(project_id: int, project_dir: Path, rel_path: str, result: dict) -> None:
    """删除后处理：刷新索引 + 记操作日志。"""
    operation_log.log(project_id, "delete", rel_path, result)
    try:
        from .index_service import remove_from_index, rebuild_index

        remove_from_index(project_id, rel_path)
        if result.get("type") == "dir":
            rebuild_index(project_id)
    except Exception:  # noqa: BLE001 - 索引可重建，失败不阻断
        pass


# ─────────────────────────── 回收站 ───────────────────────────


def list_trash(project_id: int | None = None) -> list[dict]:
    """列出回收站条目（可按项目过滤）。"""
    root = trash_root()
    if not root.is_dir():
        return []

    project_name: str | None = None
    if project_id is not None:
        project_name = fetch_project_row(project_id)["name"]

    names = (
        [project_name]
        if project_name is not None
        else sorted(path.name for path in root.iterdir() if path.is_dir())
    )

    entries: list[dict] = []
    for name in names:
        project_trash = root / name
        if not project_trash.is_dir():
            continue
        for entry_dir in sorted(project_trash.iterdir(), reverse=True):
            manifest = read_trash_manifest(entry_dir)
            if manifest is None:
                continue
            entries.append(
                {
                    "trash_rel": f"{name}/{entry_dir.name}",
                    "project_name": name,
                    "rel_path": manifest.get("rel_path"),
                    "kind": manifest.get("kind"),
                    "deleted_at": manifest.get("deleted_at"),
                }
            )
    return entries


def restore_from_trash(project_id: int, trash_rel: str) -> dict:
    """把回收站条目恢复到项目内原位置。"""
    row, project_dir = get_project_dir(project_id)
    entry_dir = resolve_trash_entry(trash_rel)
    if not entry_dir.is_dir():
        raise NodeNotFoundError(f"回收站条目不存在：{trash_rel}")

    manifest = read_trash_manifest(entry_dir)
    if manifest is None:
        raise InvalidOperationError(f"回收站条目缺少 entry.json：{trash_rel}")
    if manifest.get("project_name") != row["name"]:
        raise InvalidOperationError(f"回收站条目不属于当前项目：{trash_rel}")

    rel_path = str(manifest.get("rel_path") or "")
    if not rel_path:
        raise InvalidOperationError(f"回收站条目缺少原路径信息：{trash_rel}")

    payload = trash_entry_payload(entry_dir)
    if not payload.exists():
        raise NodeNotFoundError(f"回收站条目内容缺失：{trash_rel}")

    target = resolve_within(project_dir, rel_path)
    if target.exists():
        raise NodeExistsError(f"原位置已存在同名对象，无法恢复：{rel_path}")

    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(payload), str(target))
    shutil.rmtree(entry_dir, ignore_errors=True)

    refresh_index(project_id, project_dir, rel_path)
    operation_log.log(project_id, "restore", rel_path, {"from": trash_rel})

    return {"rel_path": rel_path, "restored_from": trash_rel}


__all__ = [
    "CHAPTER_DIR",
    "CHAPTER_STATUSES",
    "MAX_CHAPTER_TITLE_LENGTH",
    "chapter_filename",
    "chapter_placeholder",
    "create_chapter",
    "delete_chapter",
    "is_chapter_filename",
    "list_chapters",
    "list_trash",
    "next_chapter_number",
    "normalize_chapter_create_name",
    "parse_chapter_number",
    "read_chapter",
    "require_chapter_path",
    "restore_from_trash",
    "save_chapter",
    "update_chapter_meta",
    "validate_chapter_filename",
]
