"""图片模型供应商（image_providers）：CRUD / 解析 / 健康检查。

与 LLM 供应商（:mod:`provider_service`）的关键差异：
1. **不投影 dsh-home**——图片模型不参与 dsh 引擎路由；
2. 密钥仍走 :mod:`secret_store`（DPAPI 加密），``secret_ref = image-provider:{id}``；
3. 健康检查 ``GET /models`` 连通即视为可用（部分图片网关的 /models
   不含图片模型，``model_matched`` 仅作提示不作失败条件）。
"""

from __future__ import annotations

import httpx

from .. import db
from .errors import InvalidOperationError, NodeNotFoundError
from .image_adapters import resolve_adapter
from .secret_store import delete_secret, get_secret, mask, set_secret

DEFAULT_TIMEOUT = 600
DEFAULT_BASE_URL = "https://api.openai.com/v1"

PROVIDER_COLUMNS = (
    "id, provider_id, display_name, model_id, base_url, secret_ref,"
    " timeout_seconds, enabled, health, created_at"
)


def _row_to_provider(row) -> dict:
    adapter = resolve_adapter(row["model_id"] or "")
    caps = adapter.capabilities()
    secret_ref = row["secret_ref"] or f"image-provider:{row['provider_id']}"
    return {
        "id": int(row["id"]),
        "provider_id": row["provider_id"],
        "display_name": row["display_name"] or row["provider_id"],
        "model_id": row["model_id"] or "",
        "base_url": row["base_url"] or "",
        "secret_ref": secret_ref,
        "timeout_seconds": int(row["timeout_seconds"] or DEFAULT_TIMEOUT),
        "enabled": bool(row["enabled"]),
        "health": row["health"] or "",
        "has_secret": bool(get_secret(secret_ref)),
        "masked_secret": mask(get_secret(secret_ref)),
        "created_at": row["created_at"],
        "adapter_key": caps.get("adapter_key", ""),
        "capabilities": caps,
    }


# ─────────────────────────── CRUD ───────────────────────────


def list_providers() -> list[dict]:
    with db.get_conn() as conn:
        rows = conn.execute(
            f"SELECT {PROVIDER_COLUMNS} FROM image_providers"
            " ORDER BY enabled DESC, id ASC"
        ).fetchall()
    return [_row_to_provider(row) for row in rows]


def get_provider(provider_id: str) -> dict:
    with db.get_conn() as conn:
        row = conn.execute(
            f"SELECT {PROVIDER_COLUMNS} FROM image_providers WHERE provider_id = ?",
            (provider_id,),
        ).fetchone()
    if row is None:
        raise NodeNotFoundError(f"图片供应商不存在：{provider_id}")
    return _row_to_provider(row)


def upsert_provider(
    provider_id: str,
    *,
    display_name: str = "",
    model_id: str = "",
    base_url: str = "",
    api_key: str | None = None,
    timeout_seconds: int = DEFAULT_TIMEOUT,
    enabled: bool | None = None,
) -> dict:
    """新建/更新供应商；传入 ``api_key`` 时写入安全存储（不落库、不回显）。"""
    if not provider_id.strip():
        raise InvalidOperationError("provider_id 不能为空")
    if not model_id.strip():
        raise InvalidOperationError("model_id 不能为空（如 gpt-image-2）")

    # 提前校验模型可匹配（未知模型回落 OpenAI 兼容兜底，不阻断）
    resolve_adapter(model_id)

    secret_ref = f"image-provider:{provider_id}"
    if api_key:
        set_secret(secret_ref, api_key)

    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO image_providers"
            " (provider_id, display_name, model_id, base_url, secret_ref,"
            "  timeout_seconds, enabled)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(provider_id) DO UPDATE SET"
            " display_name = excluded.display_name, model_id = excluded.model_id,"
            " base_url = excluded.base_url, secret_ref = excluded.secret_ref,"
            " timeout_seconds = excluded.timeout_seconds,"
            " enabled = CASE WHEN ? IS NULL THEN image_providers.enabled ELSE ? END",
            (
                provider_id,
                display_name or provider_id,
                model_id,
                base_url,
                secret_ref,
                int(timeout_seconds),
                1 if enabled else 0,
                None if enabled is None else int(bool(enabled)),
                None if enabled is None else int(bool(enabled)),
            ),
        )
    return get_provider(provider_id)


def set_enabled(provider_id: str, enabled: bool) -> dict:
    get_provider(provider_id)
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE image_providers SET enabled = ? WHERE provider_id = ?",
            (1 if enabled else 0, provider_id),
        )
    return get_provider(provider_id)


def delete_provider(provider_id: str) -> dict:
    get_provider(provider_id)
    with db.get_conn() as conn:
        conn.execute(
            "DELETE FROM image_providers WHERE provider_id = ?", (provider_id,)
        )
    delete_secret(f"image-provider:{provider_id}")
    return {"provider_id": provider_id, "deleted": True}


def set_health(provider_id: str, health: str) -> None:
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE image_providers SET health = ? WHERE provider_id = ?",
            (health, provider_id),
        )


# ─────────────────────────── 解析与探测 ───────────────────────────


def resolve(provider_id: str) -> dict:
    """解析供应商为可调用连接信息（含明文 Key，仅生成线程内部使用）。"""
    provider = get_provider(provider_id)
    secret_ref = provider["secret_ref"] or f"image-provider:{provider_id}"
    api_key = get_secret(secret_ref)
    if not api_key:
        raise InvalidOperationError(
            f"图片供应商「{provider['display_name']}」尚未配置 API Key"
            "（请在生图工坊「模型配置」中填写）"
        )
    if not provider["enabled"]:
        raise InvalidOperationError(
            f"图片供应商「{provider['display_name']}」未启用，请先在模型配置中启用"
        )
    adapter = resolve_adapter(provider["model_id"])
    return {
        "provider_id": provider_id,
        "display_name": provider["display_name"],
        "base_url": (provider["base_url"] or DEFAULT_BASE_URL).rstrip("/"),
        "api_key": api_key,
        "model_id": provider["model_id"],
        "timeout_seconds": provider["timeout_seconds"],
        "adapter": adapter,
        "capabilities": adapter.capabilities(),
    }


def health_check(provider_id: str) -> dict:
    """健康检查：``GET {base_url}/models`` 200 即连通。"""
    provider = get_provider(provider_id)
    secret_ref = provider["secret_ref"] or f"image-provider:{provider_id}"
    api_key = get_secret(secret_ref)
    if not api_key:
        set_health(provider_id, "credentials-missing")
        return {"ok": False, "kind": "credentials", "reason": "尚未配置 API Key"}

    base_url = (provider["base_url"] or DEFAULT_BASE_URL).rstrip("/")
    url = f"{base_url}/models"
    try:
        response = httpx.get(
            url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=min(30, provider["timeout_seconds"]),
        )
    except httpx.HTTPError as exc:
        set_health(provider_id, "unreachable")
        return {"ok": False, "kind": "network", "reason": f"无法连接 {url}：{exc}"}

    if response.status_code != 200:
        set_health(provider_id, "unreachable")
        return {
            "ok": False,
            "kind": "network",
            "reason": f"健康检查失败（HTTP {response.status_code}）：{response.text[:200]}",
        }

    model_ids: list[str] = []
    try:
        payload = response.json()
        items = payload.get("data") if isinstance(payload, dict) else payload
        for item in items or []:
            if isinstance(item, dict) and item.get("id"):
                model_ids.append(str(item["id"]))
            elif isinstance(item, str):
                model_ids.append(item)
    except ValueError:
        pass  # 非 JSON 响应仍视为连通

    target = provider["model_id"]
    matched = (not target) or (target in model_ids)
    set_health(provider_id, "ok")
    return {
        "ok": True,
        "kind": "health",
        "model_count": len(model_ids),
        "model_matched": matched,
        "note": "" if matched else "模型未出现在 /models 列表（部分图片网关如此），仅提示",
        "models": model_ids[:50],
    }


def default_provider() -> dict | None:
    """默认供应商：第一个启用的；没有则返回 None。"""
    with db.get_conn() as conn:
        row = conn.execute(
            f"SELECT {PROVIDER_COLUMNS} FROM image_providers WHERE enabled = 1"
            " ORDER BY id ASC LIMIT 1"
        ).fetchone()
    return _row_to_provider(row) if row is not None else None


__all__ = [
    "DEFAULT_TIMEOUT",
    "default_provider",
    "delete_provider",
    "get_provider",
    "health_check",
    "list_providers",
    "resolve",
    "set_enabled",
    "set_health",
    "upsert_provider",
]
