"""Book-bound derived storage. The client never chooses a database path."""
from __future__ import annotations

import json
import re
import shutil
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path

from .. import config, db
from .errors import InvalidOperationError
from .project_service import fetch_project_row

_LOCK = threading.RLock()
_BOOK_LOCKS: dict[str, threading.RLock] = {}


def identity(project_id: int, *, include_deleted: bool = False) -> dict:
    row = fetch_project_row(int(project_id), include_deleted=include_deleted)
    key = row["knowledge_key"]
    if not key:  # Compatibility with third-party registration using old INSERTs.
        with _LOCK, db.get_conn() as conn:
            conn.execute("UPDATE projects SET knowledge_key=? WHERE id=? AND (knowledge_key IS NULL OR knowledge_key='')",
                         (uuid.uuid4().hex, project_id))
        row = fetch_project_row(project_id, include_deleted=include_deleted)
        key = row["knowledge_key"]
    if not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{32}", key):
        raise InvalidOperationError("本书知识库标识无效，请修复项目登记")
    base = Path(config.vector_dir()) / "books"
    # Resolve both the fixed parent and descendants: symlinks/junctions must not
    # make the derived-storage root or a database escape the workbench runtime.
    runtime = Path(config.runtime_dir()).resolve()
    if base.is_symlink() or (hasattr(base, "is_junction") and base.is_junction()):
        raise InvalidOperationError("知识库根目录不能是链接")
    folder = (base / key).resolve()
    if (runtime not in folder.parents or base.resolve() not in folder.parents
            or folder != base.resolve() / key):
        raise InvalidOperationError("知识库目录越界")
    database = folder / "vectors.db"
    if database.is_symlink() or (hasattr(database, "is_junction") and database.is_junction()):
        raise InvalidOperationError("知识库文件不能是链接")
    if database.exists() and database.resolve().parent != folder:
        raise InvalidOperationError("知识库文件链接越界")
    return {"project_id": int(project_id), "knowledge_key": key,
            "project_name": row["name"], "db_path": str(database)}


def book_lock(project_id: int, *, include_deleted: bool = False) -> threading.RLock:
    ident = identity(project_id, include_deleted=include_deleted)
    with _LOCK:
        return _BOOK_LOCKS.setdefault(ident["db_path"], threading.RLock())


@contextmanager
def connect(project_id: int):
    ident = identity(project_id)
    with book_lock(project_id):
        # Revalidate under the book lock, including deletion during a queued job.
        ident = identity(project_id)
        path = Path(ident["db_path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=30000")
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables - {"book_identity", "book_preferences", "sqlite_sequence"} and "book_identity" not in tables:
                raise InvalidOperationError("未绑定书籍的旧数据库，拒绝自动关联")
            conn.execute("CREATE TABLE IF NOT EXISTS book_identity (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            stored = dict(conn.execute("SELECT key,value FROM book_identity"))
            if (tables - {"book_identity", "book_preferences", "sqlite_sequence"} and not stored):
                raise InvalidOperationError("知识库书籍绑定缺失，拒绝读取")
            if stored and (stored.get("knowledge_key") != ident["knowledge_key"]
                           or stored.get("project_id") != str(project_id)):
                raise InvalidOperationError("知识库与当前小说不匹配，已拒绝读取")
            conn.executemany("INSERT OR IGNORE INTO book_identity(key,value) VALUES (?,?)",
                             [("knowledge_key", ident["knowledge_key"]), ("project_id", str(project_id))])
            conn.execute("CREATE TABLE IF NOT EXISTS book_preferences (key TEXT PRIMARY KEY,value TEXT NOT NULL)")
            yield conn
            identity(project_id)  # A deleted project cannot commit late results.
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()


def preference(project_id: int, key: str, default=None):
    with connect(project_id) as conn:
        row = conn.execute("SELECT value FROM book_preferences WHERE key=?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def set_preference(project_id: int, key: str, value) -> None:
    with connect(project_id) as conn:
        conn.execute("INSERT OR REPLACE INTO book_preferences(key,value) VALUES (?,?)",
                     (key, json.dumps(value, ensure_ascii=False)))


def purge(project_id: int) -> None:
    """Only called by explicit project purge; failure leaves the registration."""
    ident = identity(project_id, include_deleted=True)
    with book_lock(project_id, include_deleted=True):
        folder = Path(ident["db_path"]).parent
        if folder.exists():
            # identity already checked the fully resolved absolute target.
            shutil.rmtree(folder)
