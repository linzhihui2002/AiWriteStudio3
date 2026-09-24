"""SQLite 索引服务：扫描磁盘 → 重建索引（章节表 / 文档表 / FTS）。

原则
----
- **SQLite 只是索引**：随时可清空重建，事实源是项目内 Markdown；
- **空文件不入索引**（spec：剔除 frontmatter 后正文为空则跳过）；
- 外部（如 dsh 会话）增删改文件后，调用 :func:`rebuild_index` 即可与磁盘一致；
- FTS5 不可用时检索自动降级为 LIKE 关键词匹配（功能不中断）。
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from .. import db
from .chapter_service import (
    CHAPTER_DIR,
    is_chapter_filename,
    parse_chapter_number,
)
from .fs_utils import count_words, is_empty_document, read_text, split_frontmatter, to_rel
from .project_service import get_project_dir

# 参与索引的文件类型（内部目录如 .meta/ 一律跳过）
INDEXABLE_SUFFIXES = (".md", ".txt")
IGNORED_NAMES = {"project.md"}


def _doc_kind(rel_path: str) -> str:
    """文档分类：chapter / outline / setting / state / memo / other。"""
    head = rel_path.split("/", 1)[0]
    return {
        "章节": "chapter",
        "大纲": "outline",
        "设定": "setting",
        "状态": "state",
        "备忘录": "memo",
        "拆书": "teardown",
    }.get(head, "other")


def _iter_indexable(project_dir: Path):
    for path in sorted(project_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(project_dir)
        if any(part.startswith(".") for part in rel.parts):
            continue
        if path.suffix.lower() not in INDEXABLE_SUFFIXES:
            continue
        if path.name in IGNORED_NAMES:
            continue
        yield path


def _file_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def rebuild_index(project_id: int) -> dict:
    """全量重建：扫描项目目录，重写 chapters / index_docs / FTS。"""
    row, project_dir = get_project_dir(project_id)
    conn = db.get_conn()
    with conn as conn:
        chapters = conn.execute(
            "SELECT id, rel_path FROM chapters WHERE project_id = ?", (project_id,)
        ).fetchall()
        existing_chapters = {r["rel_path"]: int(r["id"]) for r in chapters}
        conn.execute("DELETE FROM index_docs WHERE project_id = ?", (project_id,))
        try:
            conn.execute("DELETE FROM docs_fts WHERE project_id = ?", (project_id,))
        except Exception:  # noqa: BLE001 - FTS 表可能不存在
            pass

        indexed = 0
        skipped_empty = 0
        chapter_paths: set[str] = set()

        for path in _iter_indexable(project_dir):
            rel_path = to_rel(project_dir, path)
            try:
                text = read_text(path)
            except (OSError, UnicodeDecodeError):
                continue
            if is_empty_document(text):
                skipped_empty += 1
                continue

            _meta, body = split_frontmatter(text)
            stat = path.stat()
            digest = _file_hash(text)
            kind = _doc_kind(rel_path)
            word_count = count_words(body)

            conn.execute(
                "INSERT INTO index_docs"
                " (project_id, kind, rel_path, hash, mtime, word_count)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (project_id, kind, rel_path, digest, stat.st_mtime, word_count),
            )
            try:
                conn.execute(
                    "INSERT INTO docs_fts (project_id, rel_path, kind, body)"
                    " VALUES (?, ?, ?, ?)",
                    (project_id, rel_path, kind, body),
                )
            except Exception:  # noqa: BLE001 - FTS 不可用时仅跳过
                pass
            indexed += 1

            if kind == "chapter" and path.parent.name == CHAPTER_DIR and is_chapter_filename(path.name):
                chapter_paths.add(rel_path)
                meta, body = split_frontmatter(text)
                conn.execute(
                    "INSERT INTO chapters"
                    " (project_id, rel_path, status, word_count, hash, mtime)"
                    " VALUES (?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT(project_id, rel_path) DO UPDATE SET"
                    " status = excluded.status, word_count = excluded.word_count,"
                    " hash = excluded.hash, mtime = excluded.mtime",
                    (
                        project_id,
                        rel_path,
                        str(meta.get("状态") or "草稿"),
                        word_count,
                        digest,
                        stat.st_mtime,
                    ),
                )

        # 磁盘上已不存在的章节记录 → 清理
        stale = [rel for rel in existing_chapters if rel not in chapter_paths]
        for rel in stale:
            conn.execute(
                "DELETE FROM chapters WHERE project_id = ? AND rel_path = ?",
                (project_id, rel),
            )

    return {
        "project_id": project_id,
        "project_name": row["name"],
        "indexed": indexed,
        "skipped_empty": skipped_empty,
        "removed_chapters": len(stale),
    }


def index_document(project_id: int, project_dir: Path, rel_path: str) -> dict | None:
    """增量索引单个文档（保存后调用）；空文件将从索引移除。"""
    path = Path(project_dir) / rel_path
    if not path.is_file():
        with db.get_conn() as conn:
            conn.execute(
                "DELETE FROM index_docs WHERE project_id = ? AND rel_path = ?",
                (project_id, rel_path),
            )
            conn.execute(
                "DELETE FROM chapters WHERE project_id = ? AND rel_path = ?",
                (project_id, rel_path),
            )
            _fts_delete(conn, project_id, rel_path)
        return None

    try:
        text = read_text(path)
    except (OSError, UnicodeDecodeError):
        return None

    if is_empty_document(text):
        with db.get_conn() as conn:
            conn.execute(
                "DELETE FROM index_docs WHERE project_id = ? AND rel_path = ?",
                (project_id, rel_path),
            )
            _fts_delete(conn, project_id, rel_path)
        return None

    meta, body = split_frontmatter(text)
    stat = path.stat()
    digest = _file_hash(text)
    kind = _doc_kind(rel_path)
    word_count = count_words(body)

    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO index_docs (project_id, kind, rel_path, hash, mtime, word_count)"
            " VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(project_id, rel_path) DO UPDATE SET"
            " hash = excluded.hash, mtime = excluded.mtime,"
            " word_count = excluded.word_count, indexed_at = datetime('now')",
            (project_id, kind, rel_path, digest, stat.st_mtime, word_count),
        )
        _fts_delete(conn, project_id, rel_path)
        try:
            conn.execute(
                "INSERT INTO docs_fts (project_id, rel_path, kind, body) VALUES (?, ?, ?, ?)",
                (project_id, rel_path, kind, body),
            )
        except Exception:  # noqa: BLE001
            pass
        if kind == "chapter":
            conn.execute(
                "INSERT INTO chapters"
                " (project_id, rel_path, status, word_count, hash, mtime)"
                " VALUES (?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(project_id, rel_path) DO UPDATE SET"
                " status = excluded.status, word_count = excluded.word_count,"
                " hash = excluded.hash, mtime = excluded.mtime",
                (project_id, rel_path, str(meta.get("状态") or "草稿"), word_count,
                 digest, stat.st_mtime),
            )

    # 增量向量索引（仅当该项目已建过向量库时；章节应用落盘后自动入索引）
    _vector_refresh(project_id, rel_path)

    return {
        "rel_path": rel_path,
        "kind": kind,
        "word_count": word_count,
        "hash": digest,
    }


def _vector_refresh(project_id: int, rel_path: str) -> None:
    """把文档同步进向量库（未启用向量检索时静默跳过）。"""
    try:
        from .vector_service import index_document as vector_index, vector_ready

        if vector_ready(project_id):
            vector_index(project_id, rel_path)
    except Exception:  # noqa: BLE001 - 向量索引失败不影响主流程
        pass


def _fts_delete(conn, project_id: int, rel_path: str) -> None:
    try:
        conn.execute(
            "DELETE FROM docs_fts WHERE project_id = ? AND rel_path = ?",
            (project_id, rel_path),
        )
    except Exception:  # noqa: BLE001
        pass


def remove_from_index(project_id: int, rel_path: str) -> None:
    """把某路径从索引中彻底移除（文件被删除后调用）。"""
    with db.get_conn() as conn:
        conn.execute(
            "DELETE FROM index_docs WHERE project_id = ? AND rel_path = ?",
            (project_id, rel_path),
        )
        conn.execute(
            "DELETE FROM chapters WHERE project_id = ? AND rel_path = ?",
            (project_id, rel_path),
        )
        _fts_delete(conn, project_id, rel_path)


_FTS_TOKEN_RE = re.compile(r"[0-9A-Za-z_]+|[\u4e00-\u9fff]")


def _fts_query(text: str) -> str:
    """把用户输入转成 FTS5 查询串（中文按字拆分 + 英文按词，全部作为 AND 词）。"""
    tokens = _FTS_TOKEN_RE.findall(str(text or ""))
    if not tokens:
        return ""
    return " ".join(f'"{token}"' for token in tokens[:24])


def search(project_id: int, query: str, limit: int = 20, kind: str | None = None) -> list[dict]:
    """检索项目文档：优先 FTS5，不可用时降级 LIKE。返回含命中片段的结果。"""
    text = str(query or "").strip()
    if not text:
        return []

    results: list[dict] = []
    with db.get_conn() as conn:
        fts = db.fts_available(conn)
        if fts:
            fts_query = _fts_query(text)
            if fts_query:
                try:
                    sql = (
                        "SELECT rel_path, kind, snippet(docs_fts, 3, '[', ']', '…', 12) AS snippet"
                        " FROM docs_fts WHERE project_id = ? AND docs_fts MATCH ?"
                    )
                    params: list = [project_id, fts_query]
                    if kind:
                        sql += " AND kind = ?"
                        params.append(kind)
                    sql += " LIMIT ?"
                    params.append(limit)
                    rows = conn.execute(sql, tuple(params)).fetchall()
                    results = [
                        {
                            "rel_path": row["rel_path"],
                            "kind": row["kind"],
                            "snippet": row["snippet"],
                            "score": 1.0,
                            "source": "fts",
                        }
                        for row in rows
                    ]
                except Exception:  # noqa: BLE001 - FTS 语法问题 → 降级
                    results = []

        if not results:
            pattern = f"%{text}%"
            sql = (
                "SELECT d.rel_path, d.kind, f.body FROM index_docs d"
                " LEFT JOIN docs_fts f ON f.project_id = d.project_id AND f.rel_path = d.rel_path"
                " WHERE d.project_id = ? AND (d.rel_path LIKE ? OR f.body LIKE ?)"
            )
            params = [project_id, pattern, pattern]
            if kind:
                sql += " AND d.kind = ?"
                params.append(kind)
            sql += " LIMIT ?"
            params.append(limit)
            try:
                rows = conn.execute(sql, tuple(params)).fetchall()
            except Exception:  # noqa: BLE001
                rows = conn.execute(
                    "SELECT rel_path, kind, NULL AS body FROM index_docs"
                    " WHERE project_id = ? AND rel_path LIKE ? LIMIT ?",
                    (project_id, pattern, limit),
                ).fetchall()
            for row in rows:
                body = row["body"] or ""
                index = body.find(text)
                snippet = body[max(0, index - 20): index + 60] if index >= 0 else ""
                results.append(
                    {
                        "rel_path": row["rel_path"],
                        "kind": row["kind"],
                        "snippet": snippet,
                        "score": 0.5,
                        "source": "like",
                    }
                )
    return results


def index_stats(project_id: int) -> dict:
    """索引统计：文档数、字数、按分类条数、空文件跳过数（重建时的口径）。"""
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT kind, COUNT(*) AS cnt, COALESCE(SUM(word_count), 0) AS words"
            " FROM index_docs WHERE project_id = ? GROUP BY kind",
            (project_id,),
        ).fetchall()
    by_kind = {row["kind"]: {"count": int(row["cnt"]), "words": int(row["words"])} for row in rows}
    return {
        "project_id": project_id,
        "documents": sum(item["count"] for item in by_kind.values()),
        "words": sum(item["words"] for item in by_kind.values()),
        "by_kind": by_kind,
        "fts": db.fts_available(),
    }


__all__ = [
    "index_document",
    "index_stats",
    "rebuild_index",
    "remove_from_index",
    "search",
]