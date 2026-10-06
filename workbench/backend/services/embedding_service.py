"""Explicit embedding spaces, verified local model preparation and CPU inference.

No manuscript is transmitted by local mode. Failure never changes providers.
"""
from __future__ import annotations

import hashlib
import bisect
import json
import math
import re
import threading
from pathlib import Path

import httpx

from .. import config
from .errors import InvalidOperationError
from .fs_utils import atomic_write_text, split_frontmatter
from .settings_service import read_settings, update_settings

MODEL_ID = "BAAI/bge-small-zh-v1.5"
REVISION = "46fbe35fd4374a00fee7de77dfddaeb6dd6a2c59"
REPOSITORY = "Qdrant/bge-small-zh-v1.5"
QUERY_PREFIX = "为这个句子生成表示以用于检索相关文章："
FILES = {
    "model_optimized.onnx": "1294ea4b6331115a353d81f96b85e8c8d7fdcc284453d5b2fab5b016230aad38",
    "tokenizer.json": "48cea5d44424912a6fd1ea647bf4fe50b55ab8b1e5879c3275f80e339e8fae26",
    "tokenizer_config.json": "e6f3b96db926a37d4039995fbf5ad17de158dfb8f6343d607e4dbaad18d75f5a",
    "special_tokens_map.json": "b6d346be366a7d1d48332dbc9fdf3bf8960b5d879522b7799ddba59e76237ee3",
    "config.json": "9088751d39abbf86ec3d19ffca92ad62ad19075f7e59712e6c71217fa125d1d3",
    "vocab.txt": "45bbac6b341c319adc98a532532882e91a9cefc0329aa57bac9ae761c27b291c",
}
_LOCK = threading.RLock()
_SESSIONS: dict[str, tuple] = {}
_VERIFIED: dict[tuple, str] = {}
_PREPARER: threading.Thread | None = None
_STOP = threading.Event()


def model_directory() -> Path:
    return Path(config.models_dir()) / "bge-small-zh-v1.5"


def _sha(path: Path) -> str:
    stat = path.stat()
    key = (str(path.resolve()), stat.st_size, stat.st_mtime_ns)
    with _LOCK:
        if key in _VERIFIED:
            return _VERIFIED[key]
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for data in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(data)
    value = digest.hexdigest()
    with _LOCK:
        _VERIFIED[key] = value
    return value


def settings_for(project_id: int | None = None) -> dict:
    values = read_settings().get("vector", {})
    result = {"mode": "local", "model_dir": "", "provider": "", "model": MODEL_ID,
              "enabled": False, **values}
    if "mode" not in values and values.get("provider") and values.get("model"):
        result["mode"] = "cloud"  # Preserve an existing explicit cloud selection.
    if project_id is not None:
        from .knowledge_store import preference
        override = preference(project_id, "vector", {})
        if override and override.get("mode") != "inherit":
            result.update(override)
    return result


class Embedder:
    def __init__(self, model_dir: str | None = None, provider: str = "", model: str = MODEL_ID,
                 mode: str = "local", project_id: int | None = None):
        configured = Path(model_dir) if model_dir else model_directory()
        # Compatibility: former settings pointed at the parent models directory.
        if configured == Path(config.models_dir()):
            configured = model_directory()
        self.directory = configured
        self.model_dir = str(configured)
        self.cloud_provider, self.cloud_model = provider, model
        self.project_id, self.mode = project_id, mode
        self.name = MODEL_ID if mode == "local" else (f"cloud:{provider}:{model}" if mode == "cloud" else "lexical")
        self.dim = 512 if mode == "local" else 0
        self._tokenizer = None
        self._session = None
        self.error = ""
        self.ready = False
        self._fingerprint = hashlib.sha256(json.dumps(
            {"mode": mode, "provider": provider, "model": model, "dir": str(configured)},
            sort_keys=True).encode()).hexdigest()
        if mode == "local":
            try:
                manifest = json.loads((configured / "manifest.json").read_text(encoding="utf-8"))
                if manifest.get("model") != MODEL_ID or manifest.get("revision") != REVISION:
                    raise ValueError("模型包不是已支持的固定 BGE 制品")
                for name, expected in FILES.items():
                    if _sha(configured / name) != expected:
                        raise ValueError(f"模型文件校验失败：{name}")
                self._fingerprint = hashlib.sha256(json.dumps(
                    {"model": MODEL_ID, "files": FILES, "pooling": "cls", "normalization": "l2",
                     "query_prefix": QUERY_PREFIX, "chunk_version": "paragraph-sentence384-overlap64-v2"},
                    sort_keys=True).encode()).hexdigest()
                import onnxruntime  # noqa: F401
                import tokenizers  # noqa: F401
                import numpy  # noqa: F401
                self.ready = True
            except (OSError, ValueError, ImportError) as exc:
                self.error = f"本地语义模型未就绪：{exc}"
        elif mode == "cloud":
            if not provider or not model:
                self.error = "云嵌入须显式选择供应商和模型"
            else:
                try:
                    from .provider_service import get_provider
                    configured_provider = get_provider(provider)
                    if not configured_provider.get("enabled"):
                        raise ValueError("嵌入供应商已停用")
                    self._fingerprint = hashlib.sha256(json.dumps({
                        "mode": "cloud", "provider": provider, "model": model,
                        "base_url": configured_provider.get("base_url", "").rstrip("/"),
                        "normalization": "l2", "chunk_version": "paragraph-sentence384-overlap64-v2"},
                        sort_keys=True).encode()).hexdigest()
                    self.ready = True
                except Exception as exc:
                    self.error = f"云嵌入配置不可用：{exc}"
        else:
            self.error = "当前选择仅关键词检索"

    @property
    def fingerprint(self) -> str:
        return self._fingerprint

    def info(self) -> dict:
        return {"name": self.name, "model": self.cloud_model if self.mode == "cloud" else MODEL_ID,
                "mode": self.mode, "dim": self.dim, "model_dir": self.model_dir,
                "offline": self.mode != "cloud", "ready": self.ready, "fingerprint": self.fingerprint,
                "error": self.error, "note": self.error or ("本机 CPU 中文语义嵌入" if self.mode == "local" else "已显式选择云嵌入")}

    def tokenizer(self):
        if self.mode != "local" or not self.ready:
            return None
        if self._tokenizer is None:
            from tokenizers import Tokenizer
            self._tokenizer = Tokenizer.from_file(str(self.directory / "tokenizer.json"))
        return self._tokenizer

    def _load(self):
        if not self.ready:
            raise InvalidOperationError(self.error)
        with _LOCK:
            if self.fingerprint not in _SESSIONS:
                import onnxruntime as ort
                options = ort.SessionOptions()
                options.intra_op_num_threads = 2
                options.inter_op_num_threads = 1
                session = ort.InferenceSession(str(self.directory / "model_optimized.onnx"),
                                               sess_options=options, providers=["CPUExecutionProvider"])
                _SESSIONS[self.fingerprint] = (session,)
            self._session = _SESSIONS[self.fingerprint][0]

    def _embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if not self.ready:
            raise InvalidOperationError(self.error)
        if self.mode == "cloud":
            from ..engine.direct_api import DirectApiEngine
            from . import generation_service
            task_id = generation_service._insert_task(project_id=self.project_id, task_type="embedding",
                engine="direct-api", model=self.cloud_model, provider=self.cloud_provider,
                context_snapshot={"texts": len(texts), "fingerprint": self.fingerprint})
            try:
                vectors = DirectApiEngine().embed(texts, provider=self.cloud_provider, model=self.cloud_model)
                if len(vectors) != len(texts) or not vectors or not vectors[0]:
                    raise ValueError("嵌入接口返回数量或维度无效")
                self.dim = len(vectors[0])
                result = []
                for vector in vectors:
                    if len(vector) != self.dim or not all(math.isfinite(x) for x in vector):
                        raise ValueError("嵌入接口返回非法向量")
                    norm = math.sqrt(sum(x * x for x in vector))
                    if not norm:
                        raise ValueError("嵌入接口返回零向量")
                    result.append([x / norm for x in vector])
                generation_service.update_task(task_id, status="done", result={"count": len(result), "dim": self.dim})
                return result
            except Exception as exc:
                generation_service.update_task(task_id, status="failed", error=str(exc))
                raise InvalidOperationError(f"云嵌入失败，已保留词法检索：{exc}") from exc
        self._load()
        import numpy as np
        tokenizer = self.tokenizer()
        tokenizer.enable_truncation(max_length=512)
        tokenizer.enable_padding(pad_id=0, pad_token="[PAD]")
        result = []
        # Bounded batches prevent a large full-book rebuild from allocating all
        # token hidden states at once.
        for offset in range(0, len(texts), 8):
            encoded = tokenizer.encode_batch(texts[offset:offset + 8])
            feeds = {"input_ids": np.asarray([e.ids for e in encoded], dtype=np.int64),
                     "attention_mask": np.asarray([e.attention_mask for e in encoded], dtype=np.int64),
                     "token_type_ids": np.asarray([e.type_ids for e in encoded], dtype=np.int64)}
            expected = {entry.name for entry in self._session.get_inputs()}
            if expected - feeds.keys():
                raise InvalidOperationError("不支持的 ONNX 输入契约")
            outputs = self._session.run(None, {key: value for key, value in feeds.items() if key in expected})[0]
            vectors = outputs[:, 0, :] if outputs.ndim == 3 else outputs
            if vectors.ndim != 2 or vectors.shape[1] != 512 or not np.isfinite(vectors).all():
                raise InvalidOperationError("ONNX 输出维度或数值无效")
            norms = np.linalg.norm(vectors, axis=1, keepdims=True)
            vectors = vectors / np.clip(norms, 1e-8, None)
            result.extend(vectors.tolist())
        return result

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._embed(texts)

    def embed_query(self, text: str) -> list[float]:
        return self._embed([QUERY_PREFIX + text if self.mode == "local" else text])[0]

    def embed(self, texts: list[str]) -> list[list[float]]:
        return self.embed_documents(texts)


def resolve_embedder(project_id: int | None = None) -> Embedder:
    values = settings_for(project_id)
    return Embedder(model_dir=values.get("model_dir"), provider=values.get("provider", ""),
                    model=values.get("model") or MODEL_ID, mode=values.get("mode", "local"), project_id=project_id)


def chunk_document(text: str, embedder: Embedder | None = None) -> list[dict]:
    """Prefer paragraph/sentence boundaries, retaining offsets and long tails."""
    body = split_frontmatter(text)[1]
    body_start = len(text) - len(body)
    if not body.strip():
        return []
    tokenizer = embedder.tokenizer() if embedder and hasattr(embedder, "tokenizer") else None
    if tokenizer:
        tokenizer.no_truncation()
        tokenizer.no_padding()
        offsets = tokenizer.encode(body, add_special_tokens=False).offsets
        offsets = [(start, end) for start, end in offsets if end > start]
    else:
        offsets = [(i, i + 1) for i in range(len(body))]
    result = []
    starts = [start for start, _ in offsets]
    boundaries = [bisect.bisect_left(starts, match.end())
                  for match in re.finditer(r"\n+|[。！？!?；;][”’\"']*", body)]
    position = 0
    while position < len(offsets):
        stop = min(position + 384, len(offsets))
        if stop < len(offsets):
            candidates = [boundary for boundary in boundaries if position + 192 <= boundary <= stop]
            if candidates:
                stop = candidates[-1]
        start = body_start + (offsets[position][0] if position else 0)
        end = len(text) if stop == len(offsets) else body_start + offsets[stop][0]
        raw = text[start:end]
        left = len(raw) - len(raw.lstrip())
        right = len(raw.rstrip())
        if right > left:
            begin, finish = start + left, start + right
            result.append({"text": text[begin:finish], "start": begin, "end": finish,
                           "line_start": text.count("\n", 0, begin) + 1,
                           "line_end": text.count("\n", 0, max(begin, finish - 1)) + 1})
        if stop == len(offsets):
            break
        position = stop - 64
    return result


def _prepare_state_path() -> Path:
    return Path(config.models_dir()) / "prepare.json"


def prepare_state() -> dict:
    try:
        value = json.loads(_prepare_state_path().read_text(encoding="utf-8"))
        if value.get("status") == "running" and not (_PREPARER and _PREPARER.is_alive()):
            value.update(status="interrupted", error="下载被中断，可以继续准备模型")
        return value
    except (OSError, ValueError):
        return {"status": "idle", "progress": 0}


def _save_prepare(value: dict):
    atomic_write_text(_prepare_state_path(), json.dumps(value, ensure_ascii=False))


def _prepare(enable: bool) -> dict:
    directory = model_directory()
    directory.mkdir(parents=True, exist_ok=True)
    state = {"status": "running", "progress": 0, "model": MODEL_ID, "error": "", "enable": enable}
    _save_prepare(state)
    try:
        for index, (filename, expected) in enumerate(FILES.items()):
            path = directory / filename
            if path.is_file() and _sha(path) == expected:
                continue
            state.update(file=filename, progress=round(index / len(FILES), 2))
            _save_prepare(state)
            temporary = directory / (filename + ".partial")
            digest = hashlib.sha256()
            with httpx.stream("GET", f"https://huggingface.co/{REPOSITORY}/resolve/{REVISION}/{filename}",
                              follow_redirects=True, timeout=httpx.Timeout(45, connect=20)) as response:
                response.raise_for_status()
                total = int(response.headers.get("content-length", 0))
                received = 0
                with temporary.open("wb") as handle:
                    for data in response.iter_bytes(1024 * 1024):
                        if _STOP.is_set():
                            raise InterruptedError("模型准备已停止")
                        handle.write(data)
                        digest.update(data)
                        received += len(data)
                        state.update(bytes=received, total_bytes=total)
                        _save_prepare(state)
            if digest.hexdigest() != expected:
                temporary.unlink(missing_ok=True)
                raise ValueError(f"下载文件 SHA-256 不符：{filename}")
            temporary.replace(path)
        # Keep the copyright/permission alongside the model. The upstream
        # publisher's license is retrieved from its official repository.
        if not (directory / "LICENSE").is_file():
            license_response = httpx.get("https://raw.githubusercontent.com/FlagOpen/FlagEmbedding/master/LICENSE",
                                        follow_redirects=True, timeout=25)
            license_response.raise_for_status()
            atomic_write_text(directory / "LICENSE", license_response.text)
        atomic_write_text(directory / "manifest.json", json.dumps({"model": MODEL_ID, "repository": REPOSITORY,
            "revision": REVISION, "files": FILES, "license": "MIT", "pooling": "cls", "normalize": "l2"}, indent=2))
        embedder = Embedder()
        embedder.embed_query("本地模型准备检查")
        if enable:
            update_settings({"vector": {"mode": "local", "model": MODEL_ID, "enabled": True,
                                        "model_dir": str(directory), "provider": ""}})
            from . import knowledge_service
            from .. import db
            with db.get_conn() as conn:
                projects = [row["id"] for row in conn.execute("SELECT id FROM projects WHERE deleted_at IS NULL")]
            for project_id in projects:
                if settings_for(project_id).get("mode") == "local":
                    knowledge_service.sync(project_id, background=True)
        state.update(status="ready", progress=1, error="", model_dir=str(directory))
    except Exception as exc:
        state.update(status="interrupted" if isinstance(exc, InterruptedError) else "failed", error=str(exc))
    _save_prepare(state)
    return state


def prepare(*, background: bool = True, enable: bool = True) -> dict:
    global _PREPARER
    with _LOCK:
        if _PREPARER and _PREPARER.is_alive():
            return prepare_state()
        _STOP.clear()
        if not background:
            return _prepare(enable)
        _save_prepare({"status": "running", "progress": 0, "model": MODEL_ID, "enable": enable})
        _PREPARER = threading.Thread(target=_prepare, args=(enable,), daemon=True, name="knowledge-model-prepare")
        _PREPARER.start()
    return prepare_state()


def shutdown():
    _STOP.set()


def recover():
    """Resume a previously requested model preparation after process restart."""
    state = prepare_state()
    if state.get("status") in {"running", "interrupted"}:
        prepare(background=True, enable=bool(state.get("enable", True)))
