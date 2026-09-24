"""导入导出：章节正文的 MD/TXT 导出与导入。

- 导出：仅正文 / 正文 + 标题两种口径，MD 或 TXT；产物不含任何凭据（只取章节正文与标题）；
- 导入：MD/TXT 文本 → 生成**草稿**章节（``章节/第NNNN章.md``），自动剥离 frontmatter；
- 项目包导出/恢复（M5）：整项目 JSON 打包，见 :func:`export_project_package` /
  :func:`restore_project_package`。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .chapter_service import (
    CHAPTER_DIR,
    chapter_filename,
    create_chapter,
    list_chapters,
    next_chapter_number,
    read_chapter,
    save_chapter,
)
from .errors import InvalidOperationError, NodeNotFoundError
from .fs_utils import atomic_write_text, compose_document, count_words, read_text, split_frontmatter
from .project_service import get_project_dir, list_projects

EXPORT_FORMATS = ("md", "txt")
EXPORT_SCOPES = ("body", "with_title")

_CHARSET_TITLE_RE = re.compile(r"^\s*(?:#\s*)?第\s*[0-9一二三四五六七八九十百零两]+\s*章[^\n]*$")


def _normalize_line_endings(text: str) -> str:
    return str(text or "").replace("\r\n", "\n").replace("\r", "\n")


# ─────────────────────────── 导出 ───────────────────────────


def export_chapters(
    project_id: int,
    *,
    scope: str = "with_title",
    fmt: str = "md",
    rel_paths: list[str] | None = None,
) -> dict:
    """导出章节正文，返回 ``{"filename", "content", "chapter_count", "word_count"}``。"""
    if scope not in EXPORT_SCOPES:
        raise InvalidOperationError(f"未知导出范围：{scope}（可选：{'/'.join(EXPORT_SCOPES)}）")
    if fmt not in EXPORT_FORMATS:
        raise InvalidOperationError(f"未知导出格式：{fmt}（可选：{'/'.join(EXPORT_FORMATS)}）")

    row, project_dir = get_project_dir(project_id)
    chapters = list_chapters(project_id)
    if rel_paths:
        wanted = set(rel_paths)
        chapters = [item for item in chapters if item["rel_path"] in wanted]
    if not chapters:
        raise InvalidOperationError("没有可导出的章节（章节目录为空）")

    blocks: list[str] = []
    total_words = 0
    for item in chapters:
        detail = read_chapter(project_id, item["rel_path"])
        body = _normalize_line_endings(detail["body"]).strip()
        if not body:
            continue
        title = detail["title"] or item["file_name"].removesuffix(".md")
        heading = f"# {title}" if scope == "with_title" else ""
        if fmt == "txt" and scope == "with_title":
            heading = title
        block = f"{heading}\n\n{body}" if heading else body
        blocks.append(block)
        total_words += count_words(body)

    if not blocks:
        raise InvalidOperationError("没有可导出的正文内容（章节均为空）")

    separator = "\n\n" + ("---" if fmt == "md" else "=" * 20) + "\n\n"
    content = separator.join(blocks).strip() + "\n"
    filename = f"{row['name']}.{fmt}"
    return {
        "filename": filename,
        "content": content,
        "chapter_count": len(blocks),
        "word_count": total_words,
        "format": fmt,
        "scope": scope,
    }


def write_export_file(project_id: int, **kwargs) -> Path:
    """导出并落到 ``.workbench/exports/``，返回文件路径（供下载/归档）。"""
    from .. import config

    result = export_chapters(project_id, **kwargs)
    target = config.exports_dir() / result["filename"]
    atomic_write_text(target, result["content"])
    return target


# ─────────────────────────── 导入 ───────────────────────────


def _split_import_chunks(text: str) -> list[tuple[str, str]]:
    """把导入文本切成若干章：以「第N章」标题行或 Markdown 一级标题为界。

    返回 ``[(标题, 正文)]``；没有可识别分章标记时整体作为一章（标题为空）。
    """
    lines = _normalize_line_endings(text).split("\n")
    chunks: list[tuple[str, list[str]]] = []
    current_title = ""
    current_lines: list[str] = []

    for line in lines:
        stripped = line.strip()
        is_heading = bool(_CHARSET_TITLE_RE.match(stripped)) or (
            stripped.startswith("# ") and len(stripped) > 2
        )
        if is_heading:
            if current_lines and any(item.strip() for item in current_lines):
                chunks.append((current_title, current_lines))
            current_title = stripped.lstrip("#").strip()
            current_lines = []
            continue
        current_lines.append(line)

    if current_lines and any(item.strip() for item in current_lines):
        chunks.append((current_title, current_lines))
    if not chunks:
        chunks.append((current_title, lines))
    return [(title, "\n".join(body).strip()) for title, body in chunks]


def import_document(
    project_id: int,
    *,
    filename: str,
    content: str,
    title: str | None = None,
    as_single_chapter: bool = True,
) -> dict:
    """导入 MD/TXT → 草稿章节。

    - ``as_single_chapter=True``（默认）：整篇文本落为一章（标题取参数或文件名）；
    - ``as_single_chapter=False``：按「第N章 / # 标题」分章，逐章创建。
    """
    suffix = Path(filename or "").suffix.lower()
    if suffix and suffix not in (".md", ".txt"):
        raise InvalidOperationError(f"仅支持导入 .md / .txt 文件，收到：{suffix}")

    text = _normalize_line_endings(content)
    if not text.strip():
        raise InvalidOperationError("导入内容为空")

    # 若是工作台导出的文档（含 frontmatter），先剥离
    _meta, body_only = split_frontmatter(text)
    payload = body_only if body_only.strip() else text

    _row, project_dir = get_project_dir(project_id)
    created: list[dict] = []

    if as_single_chapter:
        chapter_title = title or Path(filename or "").stem or "导入章节"
        created.append(
            _create_draft_chapter(project_id, project_dir, chapter_title, payload.strip())
        )
    else:
        for chunk_title, chunk_body in _split_import_chunks(payload):
            created.append(
                _create_draft_chapter(
                    project_id, project_dir, chunk_title or "导入章节", chunk_body
                )
            )

    return {
        "created": created,
        "count": len(created),
        "word_count": sum(item["word_count"] for item in created),
    }


def _create_draft_chapter(
    project_id: int, project_dir: Path, title: str, body: str
) -> dict:
    """新建草稿章节并写入正文（经 chapter_service 保证 frontmatter 与字数正确）。"""
    number = next_chapter_number(Path(project_dir))
    filename = chapter_filename(number)
    directory = Path(project_dir) / CHAPTER_DIR
    directory.mkdir(parents=True, exist_ok=True)
    atomic_write_text(directory / filename, compose_document({"标题": title, "状态": "草稿"}, ""))

    rel_path = f"{CHAPTER_DIR}/{filename}"
    meta = {"标题": title, "状态": "草稿"}
    text = compose_document(meta, body)
    detail = save_chapter(project_id, rel_path, text, status="草稿")
    return {
        "rel_path": detail["rel_path"],
        "title": detail["title"],
        "status": detail["status"],
        "word_count": detail["word_count"],
    }


# ─────────────────────────── 项目包（备份 / 恢复） ───────────────────────────


def export_project_package(project_id: int) -> dict:
    """导出完整项目包（JSON 结构）：项目档案 + 全部 Markdown + 卡片缓存。

    凭据/模型配置一律不进包（见 spec：导出无凭据）。
    """
    row, project_dir = get_project_dir(project_id)
    files: dict[str, str] = {}
    for path in sorted(Path(project_dir).rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(project_dir).as_posix()
        if any(part.startswith(".") for part in rel.split("/")):
            continue
        if path.suffix.lower() not in (".md", ".txt", ".json", ".yaml", ".yml"):
            continue
        try:
            files[rel] = read_text(path)
        except (OSError, UnicodeDecodeError):
            continue

    chapters = list_chapters(project_id)
    return {
        "format": "ai-novel-workbench-project",
        "version": 1,
        "project": {
            "name": row["name"],
            "created_at": row["created_at"],
        },
        "stats": {
            "files": len(files),
            "chapters": len(chapters),
            "words": sum(item["word_count"] for item in chapters),
        },
        "files": files,
    }


def restore_project_package(payload: dict, *, new_name: str | None = None) -> dict:
    """从项目包恢复：生成同名（或指定新名）项目并写回全部文件。"""
    from .project_service import create_project  # 延迟导入避免循环

    if not isinstance(payload, dict) or payload.get("format") != "ai-novel-workbench-project":
        raise InvalidOperationError("项目包格式不正确（缺少 format 标识）")

    project_meta = payload.get("project") or {}
    files = payload.get("files") or {}
    if not isinstance(files, dict) or not files:
        raise InvalidOperationError("项目包内没有文件内容")

    name = new_name or str(project_meta.get("name") or "").strip()
    if not name:
        raise InvalidOperationError("项目包缺少项目名，且未提供 new_name")

    project = create_project(name=name, one_liner="（由项目包恢复）")
    _row, project_dir = get_project_dir(project["id"])

    written = 0
    for rel, content in files.items():
        target = Path(project_dir) / rel
        if Path(project_dir).resolve() not in target.resolve().parents:
            continue  # 拦截越界路径
        atomic_write_text(target, str(content))
        written += 1

    from .index_service import rebuild_index

    rebuild_index(project["id"])
    return {"project": project, "files_written": written}


def list_exportable_projects() -> list[dict]:
    return list_projects(include_archived=True)


__all__ = [
    "export_chapters",
    "export_project_package",
    "import_document",
    "list_exportable_projects",
    "restore_project_package",
    "write_export_file",
]