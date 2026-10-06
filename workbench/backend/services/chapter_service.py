"""章节服务：``章节/第NNNN章.txt`` 的创建 / 读取 / 保存 / 删除 / 回收站恢复。

约定
----
- 章节文件名固定为 ``第NNNN章.txt``（四位零填充，保证排序稳定；编号 ≥10000 时自然扩展位数）；
- 章节正文文件只存**纯正文**（不含 frontmatter、不含标题行）；
- 标题 / 状态 / 合同ID / 字数以数据库 ``chapters`` 表为唯一事实源（保存与读取路径都维护 upsert）；
- 存量 ``第NNNN章.md`` 由 :func:`migrate_chapter_files` 幂等迁移
  （存快照 → frontmatter 元数据入库 → 正文落 ``.txt`` → 删除旧 ``.md``）；
- 未迁移的历史 ``.md`` 章节仍可读取，元数据按 frontmatter 回退取值（不回写文件）；
- 删除按内容分策略：空文件物理删除，非空文件进 ``.workbench/trash/`` 可恢复；
- 所有写入走原子写。
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from .. import db
from .errors import (
    InvalidChapterNameError,
    InvalidOperationError,
    NodeExistsError,
    NodeNotFoundError,
)
from . import operation_log
from .fs_utils import (
    atomic_write_text,
    count_words,
    delete_path_by_content,
    has_frontmatter,
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
CHAPTER_SUFFIX = ".txt"
LEGACY_CHAPTER_SUFFIX = ".md"

# 四位零填充；编号超过 9999 时自然扩展为更多位
CHAPTER_FILENAME_RE = re.compile(r"^第(\d{4,})章\.txt$")
# 尚未迁移的历史章节文件名（仍可读取，由迁移任务改写为 .txt）
LEGACY_CHAPTER_FILENAME_RE = re.compile(r"^第(\d{4,})章\.md$")
# 章节文件名的两种形态（``.txt`` 为规范形态）
_CHAPTER_NAME_RE = re.compile(r"^第(\d{4,})章\.(?:txt|md)$")

CHAPTER_STATUSES: tuple[str, ...] = ("草稿", "完成", "发表")
DEFAULT_STATUS = "草稿"

PLACEHOLDER_CONTRACT_ID = ""


def chapter_filename(number: int) -> str:
    """生成章节文件名：``第0001章.txt``。"""
    return f"第{int(number):04d}章{CHAPTER_SUFFIX}"


def is_chapter_filename(name: str) -> bool:
    """是否为合法章节文件名（``第NNNN章.txt``）。"""
    return bool(CHAPTER_FILENAME_RE.match(str(name)))


def is_legacy_chapter_filename(name: str) -> bool:
    """是否为尚未迁移的历史章节文件名（``第NNNN章.md``）。"""
    return bool(LEGACY_CHAPTER_FILENAME_RE.match(str(name)))


def is_chapter_name(name: str) -> bool:
    """是否为章节文件名（``.txt`` 规范形态或未迁移的 ``.md``）。"""
    return bool(_CHAPTER_NAME_RE.match(str(name)))


def legacy_to_canonical_filename(name: str) -> str:
    """把 ``第0001章.md`` 转成规范章节文件名 ``第0001章.txt``。"""
    number = parse_chapter_number(name)
    if number is None:
        raise InvalidChapterNameError(f"不是章节文件名：{name}")
    return chapter_filename(number)


def parse_chapter_number(name: str) -> int | None:
    """从章节文件名解析编号（兼容 ``.txt`` 与未迁移的 ``.md``）；不合法返回 None。"""
    match = _CHAPTER_NAME_RE.match(str(name))
    return int(match.group(1)) if match else None


def validate_chapter_filename(name: str) -> str:
    """校验章节文件名是否符合 ``第NNNN章.txt`` 规范，不符则报错。"""
    if not is_chapter_filename(name):
        raise InvalidChapterNameError(
            f"章节文件必须命名为「第NNNN章.txt」（四位零填充，如 第0001章.txt）：{name}"
        )
    return name


# 「第N章」这类未做四位零填充的名字（可带旧后缀）
_CHAPTER_NUMBER_NAME_RE = re.compile(r"^第\s*(\d+)\s*章(?:\.(?:md|txt|markdown))?$")

_STRIPPABLE_SUFFIXES = {".md", ".txt", ".markdown"}


def normalize_chapter_create_name(project_dir: Path, name: str) -> tuple[str, str | None]:
    """把「章节/」下新建文件时输入的名字规范化为章节文件名。

    返回 ``(文件名, 标题)``：

    - 已是 ``第NNNN章.txt`` → 原样保留，标题 ``None``；
    - ``第7章`` / ``第7章.md`` / ``第7章.txt``（位数不足四位）→ 补齐为 ``第0007章.txt``，标题 ``None``；
    - 其它任意名字 → 视为章节标题，文件名取下一个可用编号 ``第NNNN章.txt``。
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


# ─────────────────────── 章节元数据（chapters 表） ───────────────────────


def fetch_chapter_meta(project_id: int, rel_path: str) -> dict | None:
    """读取 ``chapters`` 表中的章节元数据；无记录返回 None。"""
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT title, status, word_count, hash, mtime, contract_id FROM chapters"
            " WHERE project_id = ? AND rel_path = ?",
            (int(project_id), str(rel_path)),
        ).fetchone()
    return dict(row) if row is not None else None


def upsert_chapter_meta(
    project_id: int,
    rel_path: str,
    *,
    title: str | None = None,
    status: str | None = None,
    contract_id: str | None = None,
    word_count: int | None = None,
    digest: str | None = None,
    mtime: float | None = None,
) -> dict:
    """写入 ``chapters`` 表（幂等 upsert）：字段传 None 表示保留库中原值。"""
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT title, status, word_count, hash, mtime, contract_id FROM chapters"
            " WHERE project_id = ? AND rel_path = ?",
            (int(project_id), str(rel_path)),
        ).fetchone()
        current = dict(row) if row is not None else {}
        merged = {
            "title": current.get("title") if title is None else str(title),
            "status": (current.get("status") or DEFAULT_STATUS) if status is None else str(status),
            "word_count": int(current.get("word_count") or 0) if word_count is None else int(word_count),
            "hash": current.get("hash") if digest is None else digest,
            "mtime": current.get("mtime") if mtime is None else float(mtime),
            "contract_id": (
                current.get("contract_id") or ""
            ) if contract_id is None else str(contract_id),
        }
        conn.execute(
            "INSERT INTO chapters"
            " (project_id, rel_path, title, status, word_count, hash, mtime, contract_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(project_id, rel_path) DO UPDATE SET"
            " title = excluded.title, status = excluded.status,"
            " word_count = excluded.word_count, hash = excluded.hash,"
            " mtime = excluded.mtime, contract_id = excluded.contract_id",
            (
                int(project_id), str(rel_path), merged["title"], merged["status"],
                merged["word_count"], merged["hash"], merged["mtime"], merged["contract_id"],
            ),
        )
    if ((status is not None and current.get("status") != merged["status"])
            or (title is not None and current.get("title") != merged["title"])):
        from .index_service import invalidate_knowledge
        invalidate_knowledge(project_id, rel_path)
    return merged


def move_chapter_meta(project_id: int, old_rel: str, new_rel: str) -> dict | None:
    """章节改名后把 ``chapters`` 表的元数据行迁移到新路径（避免标题/状态丢失）。"""
    meta = fetch_chapter_meta(project_id, old_rel)
    from .ingestion_commit import rebind_chapter
    rebind_chapter(project_id, old_rel, new_rel)
    if meta is None:
        return None
    with db.get_conn() as conn:
        conn.execute(
            "DELETE FROM chapters WHERE project_id = ? AND rel_path = ?",
            (int(project_id), str(old_rel)),
        )
    return upsert_chapter_meta(
        project_id, new_rel,
        title=meta.get("title"), status=meta.get("status"),
        contract_id=meta.get("contract_id"), word_count=meta.get("word_count"),
        digest=meta.get("hash"), mtime=meta.get("mtime"),
    )


def set_chapter_meta(
    project_id: int,
    rel_path: str,
    *,
    title: str | None = None,
    status: str | None = None,
) -> dict:
    """只更新章节元数据（``标题`` / ``状态``），正文文件保持不变。

    ``title is None`` / ``status is None`` 表示该字段不动。
    """
    if status is not None and status not in CHAPTER_STATUSES:
        raise InvalidOperationError(
            f"非法章节状态：{status}（可选：{'/'.join(CHAPTER_STATUSES)}）"
        )
    clean = None
    if title is not None:
        clean = str(title).strip()
        if len(clean) > MAX_CHAPTER_TITLE_LENGTH:
            raise InvalidOperationError(
                f"章节标题过长（≤{MAX_CHAPTER_TITLE_LENGTH} 字）：{clean[:20]}…"
            )
    return upsert_chapter_meta(project_id, rel_path, title=clean, status=status)


def _chapter_body(text: str, path: Path) -> str:
    """章节正文：``.txt`` 整文件即正文；未迁移的 ``.md`` 剔除 frontmatter。"""
    if path.suffix.lower() == CHAPTER_SUFFIX:
        return text
    return split_frontmatter(text)[1]


def _resolve_chapter_meta(
    project_id: int, rel_path: str, path: Path, text: str, body: str
) -> dict:
    """章节元数据：``chapters`` 表为准；库中缺失时用磁盘 frontmatter 回退并回填入库。"""
    if path.suffix.lower() == CHAPTER_SUFFIX:
        disk_meta: dict = {}
    else:
        disk_meta, _disk_body = split_frontmatter(text)
    row = fetch_chapter_meta(project_id, rel_path)
    db_title = (row or {}).get("title")
    db_status = (row or {}).get("status")
    resolved = {
        "title": db_title if db_title is not None else str(disk_meta.get("标题") or ""),
        "status": (
            str(db_status) if db_status not in (None, "", "draft")
            else str(disk_meta.get("状态") or db_status or DEFAULT_STATUS)
        ),
        "contract_id": (
            str((row or {}).get("contract_id") or "")
            or str(disk_meta.get("合同ID") or "")
        ),
        "word_count": count_words(body),
    }
    return upsert_chapter_meta(
        project_id, rel_path,
        title=resolved["title"], status=resolved["status"],
        contract_id=resolved["contract_id"], word_count=resolved["word_count"],
        mtime=path.stat().st_mtime,
    )


def _chapter_info(project_id: int, project_dir: Path, path: Path) -> dict:
    text = read_text(path)
    body = _chapter_body(text, path)
    stat = path.stat()
    rel_path = to_rel(project_dir, path)
    meta = _resolve_chapter_meta(project_id, rel_path, path, text, body)
    return {
        "rel_path": rel_path,
        "file_name": path.name,
        "number": parse_chapter_number(path.name),
        "title": str(meta.get("title") or ""),
        "status": str(meta.get("status") or DEFAULT_STATUS),
        "word_count": count_words(body),
        "contract_id": str(meta.get("contract_id") or ""),
        "is_empty": body.strip() == "",
        "size": stat.st_size,
        "mtime": stat.st_mtime,
    }


def chapter_node_info(project_id: int, project_dir: Path, path: Path) -> dict:
    """文档树章节节点所需元数据（库为准，未迁移 ``.md`` 按 frontmatter 回退）。"""
    text = read_text(path)
    body = _chapter_body(text, path)
    meta = _resolve_chapter_meta(project_id, to_rel(project_dir, path), path, text, body)
    return {
        "is_chapter": True,
        "number": parse_chapter_number(path.name),
        "title": str(meta.get("title") or ""),
        "status": str(meta.get("status") or DEFAULT_STATUS),
        "word_count": count_words(body),
    }


def create_chapter(project_id: int, title: str | None = None) -> dict:
    from .file_change_service import project_lock
    with project_lock(project_id):
        return _create_chapter_locked(project_id, title)


def _create_chapter_locked(project_id: int, title: str | None = None) -> dict:
    """新建章节：自动取下一个可用编号，落空正文文件，标题写入 ``chapters`` 表。"""
    _row, project_dir = get_project_dir(project_id)
    directory = chapters_dir(project_dir)
    directory.mkdir(parents=True, exist_ok=True)

    number = next_chapter_number(project_dir)
    filename = chapter_filename(number)
    path = directory / filename

    atomic_write_text(path, "")
    upsert_chapter_meta(
        project_id, f"{CHAPTER_DIR}/{filename}",
        title=(str(title).strip() if title else ""), status=DEFAULT_STATUS,
        contract_id=PLACEHOLDER_CONTRACT_ID, word_count=0,
        mtime=path.stat().st_mtime,
    )
    return read_chapter(project_id, f"{CHAPTER_DIR}/{filename}")


def list_chapters(project_id: int) -> list[dict]:
    """列出章节（按编号升序，含状态与字数）。"""
    _row, project_dir = get_project_dir(project_id)
    directory = chapters_dir(project_dir)
    if not directory.is_dir():
        return []

    chapters = [
        _chapter_info(project_id, project_dir, path)
        for path in directory.iterdir()
        if path.is_file() and is_chapter_name(path.name)
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
    """读取章节：正文为文件全文；标题/状态/合同ID/字数以 ``chapters`` 表为准。"""
    _row, project_dir = get_project_dir(project_id)
    path = _chapter_path(project_dir, rel_path)
    text = read_text(path)
    from .file_change_service import content_hash
    body = _chapter_body(text, path)
    rel = to_rel(project_dir, path)
    word_count = count_words(body)
    in_chapter_dir = path.parent.name == CHAPTER_DIR and is_chapter_name(path.name)
    if in_chapter_dir:
        meta = _resolve_chapter_meta(project_id, rel, path, text, body)
        title = str(meta.get("title") or "")
        status = str(meta.get("status") or DEFAULT_STATUS)
        contract_id = str(meta.get("contract_id") or "")
        meta_out = {"标题": title, "状态": status, "字数": word_count, "合同ID": contract_id}
    else:
        meta_out, body = split_frontmatter(text)
        title = str(meta_out.get("标题") or "")
        status = str(meta_out.get("状态") or DEFAULT_STATUS)
        contract_id = str(meta_out.get("合同ID") or "")
        word_count = count_words(body)
    return {
        "rel_path": rel,
        "file_name": path.name,
        "content": text,
        "hash": content_hash(text),
        "mtime": path.stat().st_mtime,
        "meta": meta_out,
        "body": body,
        "title": title,
        "status": status,
        "word_count": word_count,
        "contract_id": contract_id,
        "is_empty": body.strip() == "",
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
    """只更新 ``chapters`` 表的 ``标题`` / ``状态``，正文文件字节不变。

    - ``title is None`` 不动；``title`` 为空白串表示**清除标题**；
    - ``status is None`` 不动；否则必须属于 ``CHAPTER_STATUSES``。
    """
    from .file_change_service import project_lock

    with project_lock(project_id):
        require_chapter_path(project_id, rel_path)
        set_chapter_meta(project_id, rel_path, title=title, status=status)
        return read_chapter(project_id, rel_path)


def _save_chapter_locked(project_id: int, rel_path: str, content: str,
                         status: str | None = None) -> dict:
    """保存章节：正文落盘（纯正文），元数据写库，快照 + 原子写 + 索引刷新。

    章节文件不再含 frontmatter：若调用方仍传了 frontmatter，其中的
    ``标题`` / ``状态`` / ``合同ID`` 会被并入 ``chapters`` 表而不写进正文文件。
    """
    row, project_dir = get_project_dir(project_id)
    path = _chapter_path(project_dir, rel_path)
    rel = to_rel(project_dir, path)
    is_chapter = path.parent.name == CHAPTER_DIR and is_chapter_name(path.name)

    if is_chapter:
        meta, body = split_frontmatter(content or "")
        if not has_frontmatter(content or ""):
            # 未迁移的历史 .md 仍可能带 frontmatter：作为元数据基底，但不回写文件
            meta = (
                split_frontmatter(read_text(path))[0]
                if path.suffix.lower() == LEGACY_CHAPTER_SUFFIX else {}
            )
        if status is not None:
            if status not in CHAPTER_STATUSES:
                raise InvalidOperationError(
                    f"非法章节状态：{status}（可选：{'/'.join(CHAPTER_STATUSES)}）"
                )
            meta["状态"] = status
        from .file_change_service import content_hash

        snapshot_file(project_id, project_dir, row["name"], rel, reason="save")
        atomic_write_text(path, body)
        upsert_chapter_meta(
            project_id, rel,
            title=meta.get("标题"), status=meta.get("状态"),
            contract_id=meta.get("合同ID"), word_count=count_words(body),
            digest=content_hash(body), mtime=path.stat().st_mtime,
        )
        refresh_index(project_id, project_dir, rel)
        return read_chapter(project_id, rel)

    # 非章节文档（大纲 / 设定 / 状态 / 备忘录…）继续使用 Markdown frontmatter
    from .fs_utils import compose_document

    meta, body = split_frontmatter(content or "")
    if not has_frontmatter(content or ""):
        existing_meta, _existing_body = split_frontmatter(read_text(path))
        existing_meta.update(meta)
        meta = existing_meta

    snapshot_file(project_id, project_dir, row["name"], rel_path, reason="save")
    atomic_write_text(path, compose_document(meta, body) if meta else body)
    refresh_index(project_id, project_dir, rel_path)
    return read_chapter(project_id, rel_path)


def refresh_index(project_id: int, project_dir: Path, rel_path: str) -> None:
    """增量刷新检索索引（延迟导入避免与 index_service 循环依赖）。"""
    try:
        from .index_service import index_document

        index_document(project_id, project_dir, rel_path)
    except Exception:  # noqa: BLE001 - 索引失败不阻断保存（索引可重建）
        pass


# ─────────────────────────── 幂等迁移 ───────────────────────────


MIGRATION_REASON = "migrate"


def migrate_chapter_files(project_id: int) -> dict:
    """把存量 ``章节/第NNNN章.md`` 幂等迁移为 ``第NNNN章.txt``。

    逐文件：存快照（``reason="migrate"``）→ 解析 frontmatter 写入 ``chapters`` 表 →
    正文（去掉 frontmatter，仅做换行规范化）写入 ``.txt`` → 删除旧 ``.md``。
    单个文件失败不影响其它文件；重复执行无副作用。

    :return: ``{"migrated": [...], "skipped": [...], "errors": [...]}``
    """
    from .file_change_service import project_lock

    with project_lock(project_id):
        return _migrate_chapter_files_locked(project_id)


def _migrate_chapter_files_locked(project_id: int) -> dict:
    row, project_dir = get_project_dir(project_id)
    directory = chapters_dir(project_dir)
    migrated: list[str] = []
    skipped: list[str] = []
    errors: list[dict] = []
    if not directory.is_dir():
        return {"migrated": migrated, "skipped": skipped, "errors": errors}

    for path in sorted(directory.iterdir()):
        rel = f"{CHAPTER_DIR}/{path.name}"
        try:
            if not path.is_file() or not is_chapter_name(path.name):
                continue
            if is_chapter_filename(path.name):
                skipped.append(rel)
                continue
            if _is_link(path):
                raise InvalidOperationError(f"不允许通过链接迁移章节：{rel}")
            target = directory / legacy_to_canonical_filename(path.name)
            if target.exists():
                skipped.append(rel)
                continue

            text = read_text(path)
            snapshot_file(project_id, project_dir, row["name"], rel,
                          reason=MIGRATION_REASON)
            _meta, body = split_frontmatter(text)
            # 仅做必要的换行规范化：统一 \n，并去掉 frontmatter 之后的分隔空行
            body = body.replace("\r\n", "\n").replace("\r", "\n").lstrip("\n")

            target_rel = f"{CHAPTER_DIR}/{target.name}"
            atomic_write_text(target, body)
            upsert_chapter_meta(
                project_id, target_rel,
                title=_meta.get("标题"),
                status=_migrated_status(_meta.get("状态")),
                contract_id=_migrated_text(_meta.get("合同ID")),
                word_count=count_words(body),
                mtime=target.stat().st_mtime,
            )
            path.unlink()
            from .ingestion_commit import rebind_chapter
            rebind_chapter(project_id, rel, target_rel)
            _reindex_migration(project_id, project_dir, rel, target_rel)
            migrated.append(target_rel)
        except Exception as exc:  # noqa: BLE001 - 单个文件失败不阻断其它文件
            errors.append({"rel_path": rel, "error": str(exc)})
    return {"migrated": migrated, "skipped": skipped, "errors": errors}


def _is_link(path: Path) -> bool:
    """是否为符号链接 / Windows 目录联接（迁移不得绕过目录边界）。"""
    return path.is_symlink() or (
        hasattr(path, "is_junction") and path.is_junction()
    )


def _migrated_status(value) -> str | None:
    """迁移时的状态取值：非法或缺失一律不覆盖库中已有值。"""
    text = str(value or "").strip()
    return text if text in CHAPTER_STATUSES else None


def _migrated_text(value) -> str | None:
    """迁移时的合同ID：缺失不覆盖库中已有值。"""
    text = str(value or "").strip()
    return text or None


def _reindex_migration(project_id: int, project_dir: Path, old_rel: str,
                       new_rel: str) -> None:
    """迁移后同步索引：移除旧 ``.md`` 记录并索引新的 ``.txt``。"""
    try:
        from .index_service import index_document, remove_from_index

        remove_from_index(project_id, old_rel)
        index_document(project_id, project_dir, new_rel)
    except Exception:  # noqa: BLE001 - 索引可重建，失败不阻断迁移
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


def restore_from_trash_any(trash_rel: str) -> dict:
    """跨项目恢复文件/文件夹条目：按 ``entry.json`` 的书名定位在架项目后恢复。"""
    entry_dir = resolve_trash_entry(trash_rel)
    manifest = read_trash_manifest(entry_dir)
    if manifest is None:
        raise InvalidOperationError(f"回收站条目缺少 entry.json：{trash_rel}")
    if manifest.get("kind") == "project":
        raise InvalidOperationError("这是整本书条目，请在书架回收站中恢复整本书")

    name = str(manifest.get("project_name") or "")
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM projects WHERE name = ? AND deleted_at IS NULL", (name,)
        ).fetchone()
    if row is None:
        raise InvalidOperationError(f"《{name}》已在回收站或不存在，请先恢复整本书")
    return restore_from_trash(int(row["id"]), trash_rel)


__all__ = [
    "CHAPTER_DIR",
    "CHAPTER_SUFFIX",
    "CHAPTER_STATUSES",
    "MAX_CHAPTER_TITLE_LENGTH",
    "chapter_filename",
    "chapter_node_info",
    "create_chapter",
    "delete_chapter",
    "fetch_chapter_meta",
    "is_chapter_filename",
    "is_chapter_name",
    "is_legacy_chapter_filename",
    "legacy_to_canonical_filename",
    "list_chapters",
    "list_trash",
    "migrate_chapter_files",
    "next_chapter_number",
    "normalize_chapter_create_name",
    "parse_chapter_number",
    "read_chapter",
    "require_chapter_path",
    "restore_from_trash",
    "restore_from_trash_any",
    "save_chapter",
    "set_chapter_meta",
    "update_chapter_meta",
    "upsert_chapter_meta",
    "validate_chapter_filename",
]
