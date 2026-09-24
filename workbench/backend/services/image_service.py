"""生图任务编排：任务生命周期 / 图片落盘 / 画廊记录 / 参考图暂存 / 索引重建。

架构纪律（与 :mod:`db` 一致）：**磁盘是事实源**——每张图片附带同名 sidecar
JSON（提示词 / 参数 / 模型等），``reindex()`` 可据此整体重建 image_records 表；
本库随时可删可重建。

线程模型：create_job 起守护线程执行 API 调用；SQLite 连接由 ``db.get_conn``
按调用新建，天然线程隔离。取消为尽力而为：请求发出后不可中断，完成时发现
已取消则丢弃产物。
"""

from __future__ import annotations

import base64
import json
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import httpx

from .. import config
from .. import db
from ..engine.runtime import normalize_http_error
from .errors import InvalidOperationError, NodeNotFoundError
from .fs_utils import atomic_write_bytes, atomic_write_text, resolve_within
from .image_adapters import ImageGenRequest, resolve_adapter
from .image_provider_service import resolve

# 模块别名：任务线程与请求线程各自经 db_conn() 新建连接，线程隔离安全
db_conn = db.get_conn

UPLOAD_MAX_BYTES = 5 * 1024 * 1024
UPLOAD_TYPES: dict[str, str] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
}
FORMAT_EXTS: dict[str, str] = {"png": ".png", "jpeg": ".jpg", "webp": ".webp"}
MEDIA_TYPES: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}
UPLOAD_TTL_HOURS = 24

# job_id → 线程句柄（供测试 join 与优雅等待）
_THREADS: dict[int, threading.Thread] = {}


# ─────────────────────────── 目录工具 ───────────────────────────


def _images_root() -> Path:
    root = Path(config.images_dir())
    root.mkdir(parents=True, exist_ok=True)
    return root


def _uploads_root() -> Path:
    return _images_root() / "_uploads"


# ─────────────────────────── 任务 ───────────────────────────


def _row_to_job(row) -> dict:
    try:
        params = json.loads(row["params"]) if row["params"] else {}
    except json.JSONDecodeError:
        params = {}
    return {
        "id": int(row["id"]),
        "provider_id": row["provider_id"],
        "model_id": row["model_id"],
        "mode": row["mode"] or "generate",
        "prompt": row["prompt"] or "",
        "params": params,
        "status": row["status"] or "running",
        "error": row["error"] or "",
        "duration_ms": row["duration_ms"],
        "created_at": row["created_at"],
        "finished_at": row["finished_at"],
    }


def get_job(job_id: int) -> dict:
    with db_conn() as conn:
        row = conn.execute("SELECT * FROM image_jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        raise NodeNotFoundError(f"生图任务不存在：{job_id}")
    return _row_to_job(row)


def list_jobs(active_only: bool = False, limit: int = 50) -> list[dict]:
    sql = "SELECT * FROM image_jobs"
    if active_only:
        sql += " WHERE status = 'running'"
    sql += " ORDER BY id DESC LIMIT ?"
    with db_conn() as conn:
        rows = conn.execute(sql, (int(limit),)).fetchall()
    return [_row_to_job(row) for row in rows]


def cancel_job(job_id: int) -> dict:
    job = get_job(job_id)
    if job["status"] == "running":
        with db_conn() as conn:
            conn.execute(
                "UPDATE image_jobs SET status = 'cancelled', finished_at = ? WHERE id = ?",
                (datetime.now().isoformat(timespec="seconds"), job_id),
            )
    return get_job(job_id)


def create_job(
    provider_id: str,
    prompt: str,
    *,
    size: str = "auto",
    quality: str = "auto",
    output_format: str = "png",
    background: bool = False,
    moderation: str = "auto",
    n: int = 1,
    input_upload_ids: list[str] | None = None,
) -> dict:
    """校验参数 → 落任务行 → 起后台线程。返回任务行（status=running）。"""
    if not (prompt or "").strip():
        raise InvalidOperationError("提示词不能为空")

    resolved = resolve(provider_id)  # 未配置/未启用时在此报错
    adapter = resolved["adapter"]
    caps = adapter.capabilities()

    input_images: list[tuple[str, bytes]] = []
    upload_names: list[dict] = []
    for upload_id in input_upload_ids or []:
        filename, content = load_upload(upload_id)
        input_images.append((filename, content))
        upload_names.append({"upload_id": upload_id, "filename": filename})

    background_value = str(
        caps.get("background_map", {}).get(
            "true" if background else "false", "auto"
        )
    )

    req = ImageGenRequest(
        prompt=prompt.strip(),
        model_id=resolved["model_id"],
        size=size or "auto",
        quality=quality or "auto",
        output_format=output_format or "png",
        background=background_value,
        moderation=moderation or "auto",
        n=max(1, int(n)),
        input_images=input_images,
    )
    adapter.validate(req)

    _cleanup_uploads()

    params = {
        "size": req.size,
        "quality": req.quality,
        "output_format": req.output_format,
        "background": background,
        "moderation": req.moderation,
        "n": req.n,
        "input_uploads": upload_names,
    }
    with db_conn() as conn:
        cursor = conn.execute(
            "INSERT INTO image_jobs (provider_id, model_id, mode, prompt, params)"
            " VALUES (?, ?, ?, ?, ?)",
            (
                provider_id,
                req.model_id,
                "edit" if req.is_edit else "generate",
                req.prompt,
                json.dumps(params, ensure_ascii=False),
            ),
        )
        job_id = int(cursor.lastrowid)

    thread = threading.Thread(
        target=_run_job,
        daemon=True,
        kwargs={
            "job_id": job_id,
            "req": req,
            "base_url": resolved["base_url"],
            "api_key": resolved["api_key"],
            "timeout_seconds": resolved["timeout_seconds"],
        },
        name=f"image-job-{job_id}",
    )
    _THREADS[job_id] = thread
    thread.start()
    return get_job(job_id)


def wait_job(job_id: int, timeout: float = 60.0) -> dict:
    """等待任务线程结束（测试与优雅等待用；不阻塞事件循环）。"""
    thread = _THREADS.get(job_id)
    if thread is not None:
        thread.join(timeout=timeout)
    return get_job(job_id)


def _db_status(job_id: int) -> str:
    with db_conn() as conn:
        row = conn.execute(
            "SELECT status FROM image_jobs WHERE id = ?", (job_id,)
        ).fetchone()
    return (row["status"] if row else "") or ""


def _finish_job(job_id: int, *, status: str, error: str = "", duration_ms: int = 0) -> None:
    with db_conn() as conn:
        conn.execute(
            "UPDATE image_jobs SET status = ?, error = ?, duration_ms = ?, finished_at = ?"
            " WHERE id = ?",
            (
                status,
                error,
                int(duration_ms),
                datetime.now().isoformat(timespec="seconds"),
                job_id,
            ),
        )
    _THREADS.pop(job_id, None)


def _run_job(
    job_id: int,
    req: ImageGenRequest,
    base_url: str,
    api_key: str,
    timeout_seconds: int,
) -> None:
    """后台线程：调适配器 → 取图 → 落盘 → 入画廊记录。"""
    started = time.time()
    try:
        adapter = resolve_adapter(req.model_id)
        url, payload, parts = adapter.build_request(base_url, req)
        headers = {"Authorization": f"Bearer {api_key}"}

        if payload is not None:
            headers["Content-Type"] = "application/json"
            response = httpx.post(url, headers=headers, json=payload,
                                  timeout=timeout_seconds)
        else:
            files: list[tuple[str, tuple[str, bytes, str]]] = []
            data: dict = {}
            for part in parts or []:
                if part.get("name") == "__data__":
                    data = part.get("data") or {}
                else:
                    files.append((part["name"], (
                        part.get("filename") or "image",
                        part.get("content") or b"",
                        part.get("content_type") or "application/octet-stream",
                    )))
            response = httpx.post(url, headers=headers, data=data or None,
                                  files=files or None, timeout=timeout_seconds)

        if response.status_code != 200:
            code, message = normalize_http_error(response.status_code, response.text[:300])
            _finish_job(job_id, status="failed", error=f"{code}: {message}",
                        duration_ms=int((time.time() - started) * 1000))
            return

        try:
            data_json = response.json()
        except ValueError:
            _finish_job(job_id, status="failed", error="响应不是 JSON",
                        duration_ms=int((time.time() - started) * 1000))
            return

        results = adapter.parse_response(data_json)
        if not results:
            _finish_job(job_id, status="failed", error="响应中没有图片数据",
                        duration_ms=int((time.time() - started) * 1000))
            return

        # 已取消 → 丢弃产物（不入库不落盘）
        if _db_status(job_id) == "cancelled":
            _THREADS.pop(job_id, None)
            return

        job = get_job(job_id)
        saved_rels: list[str] = []
        for item in results:
            raw = _fetch_image_bytes(item, timeout_seconds)
            rel = _save_image(
                raw,
                ext=FORMAT_EXTS.get(req.output_format, ".png"),
                meta={
                    "job_id": job_id,
                    "provider_id": job["provider_id"],
                    "model_id": req.model_id,
                    "prompt": req.prompt,
                    "params": job["params"],
                    "size": req.size,
                },
            )
            saved_rels.append(rel)

        with db_conn() as conn:
            for rel in saved_rels:
                conn.execute(
                    "INSERT INTO image_records"
                    " (job_id, provider_id, model_id, prompt, params, file_rel, size)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        job_id,
                        job["provider_id"],
                        req.model_id,
                        req.prompt,
                        json.dumps(job["params"], ensure_ascii=False),
                        rel,
                        req.size,
                    ),
                )
        _finish_job(job_id, status="succeeded",
                    duration_ms=int((time.time() - started) * 1000))
    except Exception as exc:  # noqa: BLE001 - 后台线程兜底：任何异常落到任务行
        _finish_job(job_id, status="failed", error=str(exc)[:500],
                    duration_ms=int((time.time() - started) * 1000))


def _fetch_image_bytes(item: dict, timeout: int) -> bytes:
    """从解析结果取图片字节：b64 直接解码，url 走下载。"""
    if item.get("b64"):
        return base64.b64decode(item["b64"])
    url = item.get("url")
    if not url:
        raise InvalidOperationError("图片结果缺少 b64_json 与 url")
    response = httpx.get(url, timeout=timeout)
    if response.status_code != 200:
        raise InvalidOperationError(f"图片下载失败（HTTP {response.status_code}）")
    return response.content


def _save_image(raw: bytes, *, ext: str, meta: dict) -> str:
    """图片 + sidecar JSON 落盘到 images/{YYYYMM}/，返回相对路径。"""
    if not raw:
        raise InvalidOperationError("图片内容为空")
    month_dir = _images_root() / datetime.now().strftime("%Y%m")
    month_dir.mkdir(parents=True, exist_ok=True)
    stem = uuid.uuid4().hex[:12]
    image_path = month_dir / f"{stem}{ext}"
    atomic_write_bytes(image_path, raw)
    atomic_write_text(
        month_dir / f"{stem}.json",
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n",
    )
    rel = image_path.relative_to(_images_root()).as_posix()
    return rel


# ─────────────────────────── 画廊记录 ───────────────────────────


def _row_to_record(row) -> dict:
    try:
        params = json.loads(row["params"]) if row["params"] else {}
    except json.JSONDecodeError:
        params = {}
    record_id = int(row["id"])
    return {
        "id": record_id,
        "job_id": row["job_id"],
        "provider_id": row["provider_id"] or "",
        "model_id": row["model_id"] or "",
        "prompt": row["prompt"] or "",
        "params": params,
        "file_rel": row["file_rel"],
        "size": row["size"] or "",
        "favorite": bool(row["favorite"]),
        "created_at": row["created_at"],
        "url": f"/api/images/records/{record_id}/file",
    }


def get_record(record_id: int) -> dict:
    with db_conn() as conn:
        row = conn.execute(
            "SELECT * FROM image_records WHERE id = ?", (record_id,)
        ).fetchone()
    if row is None:
        raise NodeNotFoundError(f"图片记录不存在：{record_id}")
    return _row_to_record(row)


def list_records(
    *, favorite: bool | None = None, search: str = "", limit: int = 100
) -> list[dict]:
    sql = "SELECT * FROM image_records"
    conditions: list[str] = []
    args: list[object] = []
    if favorite is not None:
        conditions.append("favorite = ?")
        args.append(1 if favorite else 0)
    if search.strip():
        conditions.append("prompt LIKE ?")
        args.append(f"%{search.strip()}%")
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(int(limit))
    with db_conn() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [_row_to_record(row) for row in rows]


def patch_record(record_id: int, *, favorite: bool | None = None,
                 prompt: str | None = None) -> dict:
    get_record(record_id)
    updates: list[str] = []
    args: list[object] = []
    if favorite is not None:
        updates.append("favorite = ?")
        args.append(1 if favorite else 0)
    if prompt is not None:
        updates.append("prompt = ?")
        args.append(prompt)
    if updates:
        with db_conn() as conn:
            conn.execute(
                f"UPDATE image_records SET {', '.join(updates)} WHERE id = ?",
                (*args, record_id),
            )
    return get_record(record_id)


def delete_record(record_id: int) -> dict:
    record = get_record(record_id)
    path = record_file_path(record_id)
    sidecar = path.with_suffix(".json")
    path.unlink(missing_ok=True)
    sidecar.unlink(missing_ok=True)
    with db_conn() as conn:
        conn.execute("DELETE FROM image_records WHERE id = ?", (record_id,))
    return {"id": record_id, "deleted": True}


def record_file_path(record_id: int) -> Path:
    """记录对应图片的绝对路径（越界拦截）。"""
    record = get_record(record_id)
    return resolve_within(_images_root(), record["file_rel"])


def reindex() -> dict:
    """扫盘读取 sidecar JSON，整体重建 image_records 表（事实源在磁盘）。"""
    root = _images_root()
    rebuilt, skipped = 0, 0
    rows: list[tuple] = []
    for sidecar in sorted(root.rglob("*.json")):
        if "_uploads" in sidecar.parts:
            continue
        image_candidates = [
            sidecar.with_suffix(ext) for ext in (".png", ".jpg", ".jpeg", ".webp")
        ]
        image_path = next((p for p in image_candidates if p.is_file()), None)
        if image_path is None:
            skipped += 1
            continue
        try:
            meta = json.loads(sidecar.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            meta = {}
        if not isinstance(meta, dict):
            meta = {}
        rel = image_path.relative_to(root).as_posix()
        rows.append(
            (
                meta.get("job_id"),
                meta.get("provider_id", ""),
                meta.get("model_id", ""),
                meta.get("prompt", ""),
                json.dumps(meta.get("params") or {}, ensure_ascii=False),
                rel,
                meta.get("size", ""),
            )
        )
        rebuilt += 1

    with db_conn() as conn:
        conn.execute("DELETE FROM image_records")
        conn.executemany(
            "INSERT INTO image_records"
            " (job_id, provider_id, model_id, prompt, params, file_rel, size)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
    return {"rebuilt": rebuilt, "skipped": skipped}


# ─────────────────────────── 参考图暂存 ───────────────────────────


def save_upload(data: bytes, media_type: str, filename: str = "") -> dict:
    """参考图 raw body 暂存：``_uploads/{upload_id}{ext}``。"""
    ext = UPLOAD_TYPES.get((media_type or "").lower().split(";")[0])
    if ext is None:
        raise InvalidOperationError(
            f"参考图仅支持 png/jpeg/webp，当前 Content-Type：{media_type!r}"
        )
    if not data:
        raise InvalidOperationError("参考图内容为空")
    if len(data) > UPLOAD_MAX_BYTES:
        raise InvalidOperationError("参考图超过 5MB 上限")

    upload_id = uuid.uuid4().hex
    uploads = _uploads_root()
    uploads.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(uploads / f"{upload_id}{ext}", data)
    safe_name = Path(filename or f"reference{ext}").name or f"reference{ext}"
    return {"upload_id": upload_id, "filename": safe_name,
            "size": len(data), "media_type": media_type}


def load_upload(upload_id: str) -> tuple[str, bytes]:
    """按 upload_id 取参考图，返回 (filename, bytes)。"""
    if not upload_id or "/" in upload_id or "\\" in upload_id:
        raise InvalidOperationError(f"参考图 upload_id 非法：{upload_id!r}")
    uploads = _uploads_root()
    matches = [p for p in uploads.glob(f"{upload_id}.*") if p.is_file()]
    if not matches:
        raise NodeNotFoundError(f"参考图不存在或已过期：{upload_id}")
    path = matches[0]
    return path.name, path.read_bytes()


def _cleanup_uploads() -> None:
    """清理超过 TTL 的参考图暂存（生成时顺带执行，防堆积）。"""
    uploads = _uploads_root()
    if not uploads.is_dir():
        return
    deadline = datetime.now() - timedelta(hours=UPLOAD_TTL_HOURS)
    for path in uploads.iterdir():
        try:
            mtime = datetime.fromtimestamp(path.stat().st_mtime)
        except OSError:
            continue
        if mtime < deadline:
            path.unlink(missing_ok=True)
