"""本地向量检索（M8，Task 57-60）：离线嵌入 + 本地向量库 + 增量索引 + 召回审计。

设计取舍（诚实说明）
- **默认嵌入器 ``local-hashing``**：字符 bigram 哈希 + TF-IDF 权重 → 512 维稀疏/稠密向量，
  **零下载、完全离线、确定性**，在中文文本上的召回质量足以支撑「同实体/同题材召回」；
- **可选 ``onnx-local``**：若用户提供本地 ONNX 模型目录且环境里有 ``onnxruntime``，
  则优先使用该模型（真正的语义嵌入）；不可用时**自动降级**到 ``local-hashing``，
  功能不中断（spec R11：模型下载失败降级为纯 FTS 检索的替代实现）；
- **向量库**：``.workbench/vector/vectors.db``（SQLite，BLOB 存 float32），
  与业务索引库（``.workbench/workbench.db``）共存且互不影响；数据不写入任何 DSH Home；
- 召回结果带**来源与相似度**，供上下文预览审计（Task 60）。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import time
from pathlib import Path

from .. import config
from .errors import InvalidOperationError, NodeNotFoundError
from .fs_utils import read_text, split_frontmatter
from .operation_log import log as log_operation
from .project_service import get_project_dir

EMBEDDER_LOCAL = "local-hashing"
EMBEDDER_ONNX = "onnx-local"
DIM_LOCAL = 512
CHUNK_CHARS = 400
CHUNK_OVERLAP = 60
INDEX_KINDS = ("chapter", "setting", "state", "outline", "teardown", "skill")

_HAN_RE = re.compile(r"[\u4e00-\u9fff]")
_WORD_RE = re.compile(r"[0-9A-Za-z_]+")


# ─────────────────────────── 嵌入器 ───────────────────────────


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


def _onnx_available(model_dir: str | None) -> bool:
    if not model_dir:
        return False
    path = Path(model_dir)
    if not path.is_dir():
        return False
    try:
        import onnxruntime  # noqa: F401
    except ImportError:
        return False
    return any(path.glob("*.onnx"))


class Embedder:
    """嵌入器：优先本地 ONNX 模型（若可用），否则 local-hashing。"""

    def __init__(self, model_dir: str | None = None, provider: str = "", model: str = "") -> None:
        self.model_dir = model_dir or ""
        self.cloud_provider = provider
        self.cloud_model = model
        self.name = EMBEDDER_ONNX if _onnx_available(self.model_dir) else EMBEDDER_LOCAL
        self.dim = DIM_LOCAL
        self._session = None
        self._tokenizer = None

    def info(self) -> dict:
        return {
            "name": self.name,
            "dim": self.dim,
            "model_dir": self.model_dir,
            "offline": self.name == EMBEDDER_LOCAL,
            "note": (
                "零下载的本地确定性嵌入（字符 bigram 哈希 + TF-IDF 权重）"
                if self.name == EMBEDDER_LOCAL
                else "本地 ONNX 模型嵌入"
            ),
        }

    def embed(self, texts: list[str]) -> list[list[float]]:
        if self.name == EMBEDDER_ONNX and self._load_onnx():
            try:
                return self._embed_onnx(texts)
            except Exception:  # noqa: BLE001 - 推理失败降级
                self.name = EMBEDDER_LOCAL
        if self.cloud_provider and self.cloud_model:
            try:
                from ..engine.direct_api import DirectApiEngine

                engine = DirectApiEngine()
                vectors = engine.embed(texts, provider=self.cloud_provider,
                                       model=self.cloud_model)
                if vectors:
                    self.dim = len(vectors[0])
                    return vectors
            except Exception:  # noqa: BLE001 - 云嵌入不可用时回落本地
                pass
        return [embed_local(text) for text in texts]

    # -- ONNX（可选路径） --
    def _load_onnx(self) -> bool:
        if self._session is not None:
            return True
        try:
            import onnxruntime
            from tokenizers import Tokenizer
        except ImportError:
            return False

        model_files = list(Path(self.model_dir).glob("*.onnx"))
        tokenizer_files = list(Path(self.model_dir).glob("tokenizer.json"))
        if not model_files or not tokenizer_files:
            return False
        self._session = onnxruntime.InferenceSession(str(model_files[0]))
        self._tokenizer = Tokenizer.from_file(str(tokenizer_files[0]))
        return True

    def _embed_onnx(self, texts: list[str]) -> list[list[float]]:
        assert self._session is not None and self._tokenizer is not None
        encoded = self._tokenizer.encode_batch(texts)
        import numpy as np

        input_name = self._session.get_inputs()[0].name
        batch = np.array([item.ids for item in encoded], dtype=np.int64)
        outputs = self._session.run(None, {input_name: batch})[0]
        vectors = np.asarray(outputs)
        if vectors.ndim == 3:
            vectors = vectors.mean(axis=1)
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        vectors = vectors / np.clip(norms, 1e-8, None)
        self.dim = int(vectors.shape[1])
        return vectors.tolist()


def resolve_embedder(project_id: int | None = None) -> Embedder:
    """按设置解析嵌入器（模型目录 / 是否用云嵌入）。"""
    from .settings_service import read_settings

    settings = read_settings().get("vector", {})
    model_dir = str(settings.get("model_dir") or config.models_dir())
    provider = str(settings.get("provider") or "")
    model = str(settings.get("model") or "")
    return Embedder(model_dir=model_dir, provider=provider, model=model)


# ─────────────────────────── 向量库 ───────────────────────────


def vector_db_path() -> Path:
    directory = Path(config.vector_dir())
    directory.mkdir(parents=True, exist_ok=True)
    return directory / "vectors.db"


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(vector_db_path())
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE IF NOT EXISTS vectors ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " project_id INTEGER NOT NULL,"
        " rel_path TEXT NOT NULL,"
        " kind TEXT,"
        " chunk_no INTEGER NOT NULL DEFAULT 0,"
        " chunk_hash TEXT,"
        " model TEXT,"
        " dim INTEGER,"
        " text TEXT,"
        " vector BLOB,"
        " updated_at TEXT DEFAULT (datetime('now')))"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_vectors_project ON vectors(project_id, rel_path)"
    )
    return conn


def vector_ready(project_id: int) -> bool:
    """该项目是否已有向量（决定上下文第 8 级走语义还是 FTS）。"""
    try:
        conn = _conn()
    except sqlite3.Error:
        return False
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM vectors WHERE project_id = ?", (project_id,)
        ).fetchone()
        return bool(row and row["cnt"])
    finally:
        conn.close()


def _chunks(text: str) -> list[str]:
    body = split_frontmatter(text)[1]
    paragraphs = [item.strip() for item in re.split(r"\n\s*\n", body) if item.strip()]
    chunks: list[str] = []
    buffer = ""
    for paragraph in paragraphs:
        if len(buffer) + len(paragraph) + 1 <= CHUNK_CHARS:
            buffer = f"{buffer}\n{paragraph}".strip()
            continue
        if buffer:
            chunks.append(buffer)
        buffer = paragraph
    if buffer:
        chunks.append(buffer)
    if not chunks and body.strip():
        chunks = [body.strip()[:CHUNK_CHARS]]
    return [chunk[: CHUNK_CHARS + CHUNK_OVERLAP] for chunk in chunks]


def _to_blob(vector: list[float]) -> bytes:
    import array

    return array.array("f", vector).tobytes()


def _from_blob(blob: bytes) -> list[float]:
    import array

    values = array.array("f")
    values.frombytes(blob)
    return list(values)


def index_document(project_id: int, rel_path: str, *, embedder: Embedder | None = None) -> dict:
    """把单个文档增量加入向量库（章节应用落盘后自动调用）。"""
    _row, project_dir = get_project_dir(project_id)
    path = Path(project_dir) / rel_path
    if not path.is_file():
        return remove_document(project_id, rel_path)

    text = read_text(path)
    chunks = _chunks(text)
    if not chunks:
        return remove_document(project_id, rel_path)

    embedder = embedder or resolve_embedder(project_id)
    vectors = embedder.embed(chunks)
    kind = _kind_of(rel_path)

    conn = _conn()
    try:
        conn.execute("DELETE FROM vectors WHERE project_id = ? AND rel_path = ?",
                     (project_id, rel_path))
        for index, (chunk, vector) in enumerate(zip(chunks, vectors)):
            conn.execute(
                "INSERT INTO vectors (project_id, rel_path, kind, chunk_no, chunk_hash,"
                " model, dim, text, vector) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    project_id,
                    rel_path,
                    kind,
                    index,
                    hashlib.sha256(chunk.encode("utf-8")).hexdigest()[:16],
                    embedder.name,
                    len(vector),
                    chunk,
                    _to_blob(vector),
                ),
            )
        conn.commit()
    finally:
        conn.close()
    return {"rel_path": rel_path, "chunks": len(chunks), "embedder": embedder.name}


def _kind_of(rel_path: str) -> str:
    head = rel_path.split("/", 1)[0]
    return {
        "章节": "chapter",
        "设定": "setting",
        "状态": "state",
        "大纲": "outline",
        "拆书": "teardown",
    }.get(head, "other")


def remove_document(project_id: int, rel_path: str) -> dict:
    conn = _conn()
    try:
        conn.execute("DELETE FROM vectors WHERE project_id = ? AND rel_path = ?",
                     (project_id, rel_path))
        conn.commit()
    finally:
        conn.close()
    return {"rel_path": rel_path, "chunks": 0, "removed": True}


def rebuild(project_id: int, *, kinds: tuple[str, ...] = INDEX_KINDS) -> dict:
    """全量重建向量索引（章节正文 / Story Bible / 拆书事实卡 / 技能规则文档）。"""
    from .index_service import _iter_indexable, _doc_kind

    _row, project_dir = get_project_dir(project_id)
    embedder = resolve_embedder(project_id)

    conn = _conn()
    try:
        conn.execute("DELETE FROM vectors WHERE project_id = ?", (project_id,))
        conn.commit()
    finally:
        conn.close()

    indexed = 0
    chunks_total = 0
    for path in _iter_indexable(project_dir):
        rel_path = path.relative_to(project_dir).as_posix()
        kind = _doc_kind(rel_path)
        if kinds and kind not in kinds:
            continue
        try:
            text = read_text(path)
        except (OSError, UnicodeDecodeError):
            continue
        chunks = _chunks(text)
        if not chunks:
            continue
        vectors = embedder.embed(chunks)
        conn = _conn()
        try:
            for index, (chunk, vector) in enumerate(zip(chunks, vectors)):
                conn.execute(
                    "INSERT INTO vectors (project_id, rel_path, kind, chunk_no, chunk_hash,"
                    " model, dim, text, vector) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (project_id, rel_path, kind, index,
                     hashlib.sha256(chunk.encode("utf-8")).hexdigest()[:16],
                     embedder.name, len(vector), chunk, _to_blob(vector)),
                )
            conn.commit()
        finally:
            conn.close()
        indexed += 1
        chunks_total += len(chunks)

    # 技能与规则文档（仓库级，随工作台共享）
    skill_chunks = _index_skills(project_id, embedder)
    chunks_total += skill_chunks
    indexed += 1 if skill_chunks else 0

    log_operation(project_id, "vector-rebuild", None,
                  {"documents": indexed, "chunks": chunks_total, "embedder": embedder.name})
    return {
        "project_id": project_id,
        "documents": indexed,
        "chunks": chunks_total,
        "embedder": embedder.info(),
    }


def _index_skills(project_id: int, embedder: Embedder) -> int:
    """索引技能与规则文档（``skills/*/SKILL.md`` 与编译后的规则技能）。"""
    root = Path(config.skills_dir())
    if not root.is_dir():
        return 0
    conn = _conn()
    count = 0
    try:
        conn.execute("DELETE FROM vectors WHERE project_id = ? AND kind = 'skill'",
                     (project_id,))
        for path in sorted(root.glob("*/SKILL.md")):
            try:
                text = read_text(path)
            except (OSError, UnicodeDecodeError):
                continue
            chunks = _chunks(text)
            if not chunks:
                continue
            for index, (chunk, vector) in enumerate(zip(chunks, embedder.embed(chunks))):
                conn.execute(
                    "INSERT INTO vectors (project_id, rel_path, kind, chunk_no, chunk_hash,"
                    " model, dim, text, vector) VALUES (?, ?, 'skill', ?, ?, ?, ?, ?, ?)",
                    (project_id, f"skills/{path.parent.name}/SKILL.md", index,
                     hashlib.sha256(chunk.encode("utf-8")).hexdigest()[:16],
                     embedder.name, len(vector), chunk, _to_blob(vector)),
                )
                count += 1
        conn.commit()
    finally:
        conn.close()
    return count


# ─────────────────────────── 召回 ───────────────────────────


def semantic_search(project_id: int, query: str, limit: int = 5,
                    kinds: tuple[str, ...] = ()) -> list[dict]:
    """语义召回：返回命中条目（含来源路径、片段与相似度），供上下文第 8 级与审计。"""
    text = (query or "").strip()
    if not text:
        return []

    embedder = resolve_embedder(project_id)
    query_vector = embedder.embed([text])[0]
    if not any(query_vector):
        return []

    conn = _conn()
    try:
        sql = "SELECT rel_path, kind, chunk_no, text, vector, model FROM vectors WHERE project_id = ?"
        params: list = [project_id]
        if kinds:
            placeholders = ",".join("?" for _ in kinds)
            sql += f" AND kind IN ({placeholders})"
            params.extend(kinds)
        rows = conn.execute(sql, tuple(params)).fetchall()
    finally:
        conn.close()

    scored: list[dict] = []
    for row in rows:
        vector = _from_blob(row["vector"])
        if len(vector) != len(query_vector):
            continue
        similarity = sum(a * b for a, b in zip(vector, query_vector))
        if similarity <= 0:
            continue
        scored.append(
            {
                "rel_path": row["rel_path"],
                "kind": row["kind"],
                "chunk_no": int(row["chunk_no"]),
                "snippet": re.sub(r"\s+", " ", str(row["text"] or ""))[:160],
                "score": round(float(similarity), 4),
                "source": "vector",
                "model": row["model"],
            }
        )

    scored.sort(key=lambda item: item["score"], reverse=True)
    return scored[:limit]


def stats(project_id: int) -> dict:
    """向量库统计（模型、条数、覆盖文档数）。"""
    embedder = resolve_embedder(project_id)
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS chunks, COUNT(DISTINCT rel_path) AS docs"
            " FROM vectors WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        by_kind = conn.execute(
            "SELECT kind, COUNT(*) AS chunks FROM vectors WHERE project_id = ? GROUP BY kind",
            (project_id,),
        ).fetchall()
    finally:
        conn.close()
    return {
        "project_id": project_id,
        "chunks": int(row["chunks"] or 0),
        "documents": int(row["docs"] or 0),
        "by_kind": {item["kind"]: int(item["chunks"]) for item in by_kind},
        "embedder": embedder.info(),
        "db_path": str(vector_db_path()),
        "ready": vector_ready(project_id),
    }


def set_embedder_config(*, model_dir: str = "", provider: str = "", model: str = "") -> dict:
    """切换嵌入器配置（模型目录 / 云嵌入 provider+model）。"""
    from .settings_service import update_settings

    settings = update_settings(
        {"vector": {"model_dir": model_dir, "provider": provider, "model": model}}
    )
    embedder = resolve_embedder(None)
    return {"config": settings.get("vector", {}), "active": embedder.info(),
            "note": "切换后需重建向量索引（POST /api/projects/{id}/vector/rebuild）"}


def model_info() -> dict:
    """当前嵌入器与可选模型路径说明（体积/来源/校验值明示）。"""
    directory = Path(config.models_dir())
    files: list[dict] = []
    if directory.is_dir():
        for path in sorted(directory.rglob("*")):
            if path.is_file():
                files.append(
                    {
                        "path": str(path),
                        "size_bytes": path.stat().st_size,
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    }
                )
    embedder = resolve_embedder(None)
    return {
        "active": embedder.info(),
        "default_dim": DIM_LOCAL,
        "models_dir": str(directory),
        "local_files": files,
        "options": [
            {
                "name": EMBEDDER_LOCAL,
                "dim": DIM_LOCAL,
                "download_required": False,
                "note": "零下载、离线、确定性；中文按字+bigram 哈希",
            },
            {
                "name": EMBEDDER_ONNX,
                "download_required": True,
                "note": "需自备 ONNX 模型目录（含 *.onnx 与 tokenizer.json）并安装 onnxruntime；"
                        "不可用时自动降级",
            },
        ],
        "explicit_path_supported": True,
    }


def drop(project_id: int | None = None) -> dict:
    """删除向量数据（按项目或全量），用于切换模型后重建。"""
    conn = _conn()
    try:
        if project_id is None:
            cursor = conn.execute("DELETE FROM vectors")
        else:
            cursor = conn.execute("DELETE FROM vectors WHERE project_id = ?", (project_id,))
        conn.commit()
        return {"deleted": int(cursor.rowcount or 0)}
    finally:
        conn.close()


__all__ = [
    "EMBEDDER_LOCAL",
    "EMBEDDER_ONNX",
    "Embedder",
    "drop",
    "embed_local",
    "index_document",
    "model_info",
    "rebuild",
    "remove_document",
    "resolve_embedder",
    "semantic_search",
    "set_embedder_config",
    "stats",
    "vector_db_path",
    "vector_ready",
]