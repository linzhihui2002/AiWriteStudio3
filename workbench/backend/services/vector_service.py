"""Compatibility endpoints for the book-bound knowledge service.

local-hashing remains a diagnostic helper, never a semantic index fallback.
All persistent storage is selected through the immutable book identity.
"""
from __future__ import annotations
import hashlib
import math
import re
from pathlib import Path
from .embedding_service import (Embedder, MODEL_ID, FILES, REVISION, prepare_state,
    resolve_embedder, chunk_document, settings_for, model_directory)
from .errors import InvalidOperationError
from .fs_utils import resolve_within
from .. import config

EMBEDDER_LOCAL = "local-hashing"
EMBEDDER_ONNX = "onnx-local"
DIM_LOCAL = 512
CHUNK_CHARS = 384
CHUNK_OVERLAP = 64
INDEX_KINDS = ("chapter", "setting", "state", "outline", "teardown")
_HAN_RE = re.compile(r"[\u4e00-\u9fff]")
_WORD_RE = re.compile(r"[0-9A-Za-z_]+")

def _tokens(text: str) -> list[str]:
    """切词：中文按字 + 相邻 bigram，英文/数字按词。"""
    han = _HAN_RE.findall(text or "")
    bigrams = [han[i] + han[i + 1] for i in range(len(han) - 1)]
    words = [item.lower() for item in _WORD_RE.findall(text or "")]
    return han + bigrams + words


def _hash_index(token: str) -> tuple[int, float]:
    digest = hashlib.md5(token.encode("utf-8")).digest()
    index = int.from_bytes(digest[:4], "little") % DIM_LOCAL
    sign = 1.0 if digest[4] % 2 == 0 else -1.0
    return index, sign


def embed_local(text: str) -> list[float]:
    """local-hashing 嵌入：哈希 + 词频平方根加权 + L2 归一化。"""
    vector = [0.0] * DIM_LOCAL
    tokens = _tokens(text)
    if not tokens:
        return vector
    counts: dict[str, int] = {}
    for token in tokens:
        counts[token] = counts.get(token, 0) + 1
    for token, count in counts.items():
        index, sign = _hash_index(token)
        vector[index] += sign * math.sqrt(count)
    norm = math.sqrt(sum(value * value for value in vector))
    if norm:
        vector = [value / norm for value in vector]
    return vector



def vector_db_path(project_id: int) -> Path:
    from .knowledge_store import identity
    return Path(identity(project_id)["db_path"])


def _chunks(text: str) -> list[str]:
    return [chunk["text"] for chunk in chunk_document(text)]


def rebuild(project_id: int, *, kinds=INDEX_KINDS, background: bool = False) -> dict:
    from . import knowledge_service
    return knowledge_service.sync(project_id, background=background)


def index_document(project_id: int, rel_path: str, *, embedder=None) -> dict:
    from .project_service import get_project_dir
    from . import knowledge_service
    _, root = get_project_dir(project_id)
    resolve_within(root, rel_path)
    knowledge_service.invalidate(project_id, rel_path)
    return knowledge_service.sync(project_id, background=False)


def remove_document(project_id: int, rel_path: str) -> dict:
    from . import knowledge_service
    knowledge_service.invalidate(project_id, rel_path)
    return {"rel_path": rel_path, "chunks": 0, "removed": True}


def semantic_search(project_id: int, query: str, limit: int = 5, kinds=()) -> list[dict]:
    from . import knowledge_service
    hits = knowledge_service.search(project_id, query, mode="hybrid", limit=limit).get("hits", [])
    return [hit for hit in hits if not kinds or hit.get("kind") in kinds]


def stats(project_id: int) -> dict:
    from . import knowledge_service
    from .knowledge_store import identity
    info = knowledge_service.overview(project_id)
    counts = info.get("counts", {})
    return {**identity(project_id), **info,
            "chunks": counts.get("chunks", info.get("chunks", 0)),
            "documents": counts.get("documents", info.get("indexed_documents", 0)),
            "embedder": resolve_embedder(project_id).info()}


def vector_ready(project_id: int) -> bool:
    from . import knowledge_service
    return bool(knowledge_service.overview(project_id).get("vector_ready", False))


def model_info(project_id: int | None = None) -> dict:
    embedder = resolve_embedder(project_id)
    directory = Path(embedder.model_dir)
    files = [{"path": str(directory / name), "size_bytes": (directory / name).stat().st_size,
              "sha256": digest} for name, digest in FILES.items() if (directory / name).is_file()]
    binding = {}
    if project_id is not None:
        from .knowledge_store import preference
        override = preference(project_id, "vector", {})
        binding = {"inherited": not override or override.get("mode") == "inherit", "override": override}
    return {"active": embedder.info(), "config": settings_for(project_id), **binding,
            "default_dim": 512, "models_dir": str(config.models_dir()), "local_files": files,
            "options": [{"name": MODEL_ID, "dim": 512, "download_required": True,
                         "note": "中文本地语义模型，CPU 推理，MIT"},
                        {"name": "lexical", "download_required": False, "note": "仅关键词检索"}],
            "explicit_path_supported": True, "prepare": prepare_state(), "revision": REVISION}


def set_embedder_config(*, model_dir: str = "", provider: str = "", model: str = "",
                        mode: str | None = None, enabled: bool | None = None,
                        project_id: int | None = None) -> dict:
    from .settings_service import update_settings
    from . import knowledge_service
    chosen = mode or ("cloud" if provider and model else "local")
    if chosen not in {"local", "cloud", "lexical", "inherit"}:
        raise InvalidOperationError("嵌入方式无效")
    if chosen == "inherit" and project_id is None:
        raise InvalidOperationError("全局设置不能选择继承")
    if chosen == "local" and model and model != MODEL_ID:
        raise InvalidOperationError("本地嵌入目前仅支持固定版本 BGE 中文模型")
    if chosen == "cloud" and not (provider and model):
        raise InvalidOperationError("云嵌入须指定供应商和模型")
    values = {"mode": chosen, "model_dir": model_dir, "provider": provider,
              "model": model or MODEL_ID}
    from .. import db
    with db.get_conn() as conn:
        affected = ([project_id] if project_id is not None else
                    [row["id"] for row in conn.execute("SELECT id FROM projects WHERE deleted_at IS NULL")])
    old_fingerprints = {book: resolve_embedder(book).fingerprint for book in affected}
    if enabled is not None:
        values["enabled"] = enabled
    if project_id is not None:
        from .knowledge_store import set_preference
        set_preference(project_id, "vector", values)
        if enabled is not False:
            # Selecting a book model is an explicit activation for this book.
            set_preference(project_id, "knowledge_enabled", True)
        projects = [project_id]
    else:
        update_settings({"vector": values})
        from .. import db
        with db.get_conn() as conn:
            projects = [row["id"] for row in conn.execute("SELECT id FROM projects WHERE deleted_at IS NULL")]
    for book in projects:
        if (knowledge_service.auto_enabled(book) and
                (old_fingerprints.get(book) != resolve_embedder(book).fingerprint
                 or enabled is True)):
            knowledge_service.sync(book, background=True)
    return {"config": settings_for(project_id), "active": resolve_embedder(project_id).info(),
            "note": "模型指纹变化后，本书索引会后台重建；期间使用关键词检索"}


def drop(project_id: int | None = None) -> dict:
    from . import knowledge_service
    if project_id is None:
        raise InvalidOperationError("请指定要清理的小说，不能跨书清库")
    knowledge_service.invalidate(project_id)
    return {"project_id": project_id, "deleted": True}
