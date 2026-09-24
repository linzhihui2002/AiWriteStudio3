"""模型供应商（ModelProviderProfile）：CRUD / 模型拉取 / 健康检查 / dsh 投影。

关键约束
--------
1. **凭据不入库**：``providers`` 表只存 ``secret_ref``，明文 Key 在
   ``.workbench/secrets.json``（见 :mod:`secret_store`）；
2. **投影单向**：工作台 → ``.workbench/dsh-home/settings.yaml``；
   **绝不**写用户 ``~/.dsh``，且**绝不**在 settings.yaml 写 ``agent-default-model``
   （见 docs/compat-matrix.md §3.4：settings 用户层会静默压过 ``--patch``）；
3. 健康检查通过才允许 ``enabled=1``。
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import httpx
import yaml

from .. import db
from ..engine.dsh_paths import get_dsh_home
from .errors import InvalidOperationError, NodeNotFoundError
from .fs_utils import atomic_write_text, read_text
from .secret_store import get_secret, mask, redact, set_secret
from .settings_service import read_settings, update_settings

DEFAULT_TIMEOUT = 60
DEFAULT_BASE_URL = "https://api.openai.com/v1"

# 模型参数的兜底默认（前端留空即用这两项）
DEFAULT_CONTEXT_WINDOW = 131072
DEFAULT_MAX_TOKENS = 8192

# 能力标签（供「按擅长场景推荐模型」使用）
CAPABILITY_TAGS = ("长文本", "结构化", "创意", "低成本", "嵌入")

PROVIDER_COLUMNS = (
    "id, provider_id, display_name, source, base_url, models, model_id, capabilities,"
    " secret_ref, timeout_seconds, retry_policy, enabled, health, created_at"
)
_PROJECTION_LOCK = threading.Lock()


def _normalize_model(item: dict | str) -> dict | None:
    """单个模型条目 → 规整 dict；``id`` 为空则丢弃。"""
    if isinstance(item, str):
        item = {"id": item}
    if not isinstance(item, dict):
        return None
    model_id = str(item.get("id") or "").strip()
    if not model_id:
        return None
    return {
        "id": model_id,
        "name": str(item.get("name") or "").strip() or model_id,
        "context_window": int(item.get("context_window") or 0) or DEFAULT_CONTEXT_WINDOW,
        "max_tokens": int(item.get("max_tokens") or 0) or DEFAULT_MAX_TOKENS,
    }


def normalize_models(items) -> list[dict]:
    """规整模型列表：去空、按 id 去重保序、补齐默认参数。"""
    models: list[dict] = []
    seen: set[str] = set()
    for item in items or []:
        model = _normalize_model(item)
        if model is None or model["id"] in seen:
            continue
        seen.add(model["id"])
        models.append(model)
    return models


def _parse_models(raw, legacy_model_id: str = "") -> list[dict]:
    """解析库内 ``models`` JSON 列；旧库无该列时用历史 ``model_id`` 合成单模型。"""
    parsed: list[dict] = []
    if raw:
        try:
            loaded = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            loaded = []
        if isinstance(loaded, list):
            parsed = normalize_models(loaded)
    if not parsed and legacy_model_id:
        parsed = normalize_models([{"id": legacy_model_id}])
    return parsed


def primary_model_id(provider: dict) -> str:
    """供应商的首个模型 id（未配置模型时返回空串）。"""
    models = provider.get("models") or []
    return models[0]["id"] if models else ""


def _row_to_provider(row, *, legacy_model_id: str = "") -> dict:
    capabilities = row["capabilities"]
    try:
        parsed_caps = json.loads(capabilities) if capabilities else []
    except json.JSONDecodeError:
        parsed_caps = []
    retry_policy = row["retry_policy"]
    try:
        parsed_retry = json.loads(retry_policy) if retry_policy else {"max_retries": 2}
    except json.JSONDecodeError:
        parsed_retry = {"max_retries": 2}
    models = _parse_models(row["models"], legacy_model_id)
    return {
        "id": int(row["id"]),
        "provider_id": row["provider_id"],
        "display_name": row["display_name"] or row["provider_id"],
        "source": row["source"],
        "base_url": row["base_url"] or "",
        "models": models,
        "model_count": len(models),
        "capabilities": parsed_caps,
        "secret_ref": row["secret_ref"] or "",
        "timeout_seconds": int(row["timeout_seconds"] or DEFAULT_TIMEOUT),
        "retry_policy": parsed_retry,
        "enabled": bool(row["enabled"]),
        "health": row["health"] or "",
        "has_secret": bool(row["secret_ref"]) and bool(get_secret(row["secret_ref"])),
        "masked_secret": mask(get_secret(row["secret_ref"])) if row["secret_ref"] else "",
        "created_at": row["created_at"],
    }


# ─────────────────────────── CRUD ───────────────────────────


def list_providers() -> list[dict]:
    with db.get_conn() as conn:
        rows = conn.execute(
            f"SELECT {PROVIDER_COLUMNS} FROM providers ORDER BY enabled DESC, id ASC"
        ).fetchall()
    return [_row_to_provider(row, legacy_model_id=row["model_id"] or "") for row in rows]


def get_provider(provider_id: str) -> dict:
    with db.get_conn() as conn:
        row = conn.execute(
            f"SELECT {PROVIDER_COLUMNS} FROM providers WHERE provider_id = ?",
            (provider_id,),
        ).fetchone()
    if row is None:
        raise NodeNotFoundError(f"供应商不存在：{provider_id}")
    return _row_to_provider(row, legacy_model_id=row["model_id"] or "")


def upsert_provider(
    provider_id: str,
    *,
    display_name: str = "",
    base_url: str = "",
    models: list[dict] | None = None,
    capabilities: list[str] | None = None,
    api_key: str | None = None,
    timeout_seconds: int = DEFAULT_TIMEOUT,
    retry_policy: dict | None = None,
    source: str = "workbench_custom",
    enabled: bool | None = None,
) -> dict:
    """新建/更新供应商；传入 ``api_key`` 时同步写入安全存储（不落库）。"""
    if not provider_id.strip():
        raise InvalidOperationError("provider_id 不能为空")
    secret_ref = f"provider:{provider_id}"
    if api_key:
        set_secret(secret_ref, api_key)

    models_json = json.dumps(normalize_models(models), ensure_ascii=False)
    caps = json.dumps(list(capabilities or []), ensure_ascii=False)
    retry = json.dumps(retry_policy or {"max_retries": 2, "backoff_seconds": 2.0},
                       ensure_ascii=False)
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO providers (provider_id, display_name, source, base_url, models,"
            " capabilities, secret_ref, timeout_seconds, retry_policy, enabled)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(provider_id) DO UPDATE SET"
            " display_name = excluded.display_name, base_url = excluded.base_url,"
            " models = excluded.models, capabilities = excluded.capabilities,"
            " secret_ref = excluded.secret_ref, timeout_seconds = excluded.timeout_seconds,"
            " retry_policy = excluded.retry_policy,"
            " enabled = CASE WHEN ? IS NULL THEN providers.enabled ELSE ? END",
            (
                provider_id,
                display_name or provider_id,
                source,
                base_url,
                models_json,
                caps,
                secret_ref,
                int(timeout_seconds),
                retry,
                1 if enabled else 0,
                None if enabled is None else int(bool(enabled)),
                None if enabled is None else int(bool(enabled)),
            ),
        )
    provider = get_provider(provider_id)
    # The editor can save an already-enabled provider without toggling it. Native
    # chat reads the isolated projection, so saving must update that projection.
    project_to_dsh_home()
    return provider


def _clear_default_if(provider_id: str) -> None:
    """若该供应商正是全局默认模型指向的供应商，则清空默认设置。"""
    if get_default_target()["provider_id"] == provider_id:
        set_default_target("", "")


def set_enabled(provider_id: str, enabled: bool) -> dict:
    get_provider(provider_id)
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE providers SET enabled = ? WHERE provider_id = ?",
            (1 if enabled else 0, provider_id),
        )
    if not enabled:
        _clear_default_if(provider_id)
    provider = get_provider(provider_id)
    # Disabling must remove the route and its credential as well.
    project_to_dsh_home()
    return provider


def delete_provider(provider_id: str) -> dict:
    get_provider(provider_id)
    _clear_default_if(provider_id)
    with db.get_conn() as conn:
        conn.execute("DELETE FROM providers WHERE provider_id = ?", (provider_id,))
    from .secret_store import delete_secret

    delete_secret(f"provider:{provider_id}")
    project_to_dsh_home()
    return {"provider_id": provider_id, "deleted": True}


def set_health(provider_id: str, health: str) -> None:
    with db.get_conn() as conn:
        conn.execute("UPDATE providers SET health = ? WHERE provider_id = ?",
                     (health, provider_id))


# ─────────────────────────── 解析与探测 ───────────────────────────


def resolve(provider_id: str, model_id: str = "") -> dict:
    """解析供应商为引擎可用的连接信息（含明文 Key，仅引擎内部使用）。

    ``model_id`` 留空时取该供应商的首个模型（可能为空串，表示未指定）。
    """
    provider = get_provider(provider_id)
    secret_ref = provider["secret_ref"] or f"provider:{provider_id}"
    api_key = get_secret(secret_ref)
    if not api_key:
        raise InvalidOperationError(
            f"供应商「{provider['display_name']}」尚未配置 API Key（请在模型配置页填写）"
        )
    return {
        "provider_id": provider_id,
        "display_name": provider["display_name"],
        "base_url": (provider["base_url"] or DEFAULT_BASE_URL).rstrip("/"),
        "api_key": api_key,
        "model_id": model_id.strip() or primary_model_id(provider),
        "models": provider["models"],
        "timeout_seconds": provider["timeout_seconds"],
        "retry_policy": provider["retry_policy"],
        "capabilities": provider["capabilities"],
    }


# ─────────────────────────── 全局默认模型 ───────────────────────────


def get_default_target() -> dict:
    """读取全局默认模型的原始存储值（不做校验）。"""
    engines = read_settings().get("engines") or {}
    return {
        "provider_id": str(engines.get("default_provider") or ""),
        "model_id": str(engines.get("default_model") or ""),
    }


def set_default_target(provider_id: str = "", model_id: str = "") -> dict:
    """设置/清除全局默认模型（供应商 + 模型）。

    ``provider_id`` 为空表示清除；``model_id`` 为空时取该供应商的首个模型。
    """
    provider_id = (provider_id or "").strip()
    model_id = (model_id or "").strip()
    if not provider_id:
        update_settings({"engines": {"default_provider": "", "default_model": ""}})
        return {"provider_id": "", "model_id": ""}

    provider = get_provider(provider_id)
    model_ids = [model["id"] for model in provider["models"]]
    if model_id and model_id not in model_ids:
        raise InvalidOperationError(
            f"模型 {model_id} 不在供应商「{provider_id}」的模型列表中"
        )
    if not model_id:
        model_id = model_ids[0] if model_ids else ""
    update_settings({"engines": {"default_provider": provider_id, "default_model": model_id}})
    return {"provider_id": provider_id, "model_id": model_id}


def default_target() -> dict:
    """运行时解析默认模型：存储值（校验后） → 第一个启用的供应商 → 空。"""
    stored = get_default_target()
    if stored["provider_id"]:
        try:
            provider = get_provider(stored["provider_id"])
        except NodeNotFoundError:
            provider = None
        if provider is not None and provider["enabled"] and provider["models"]:
            model_ids = [model["id"] for model in provider["models"]]
            return {
                "provider_id": provider["provider_id"],
                "model_id": stored["model_id"] if stored["model_id"] in model_ids else model_ids[0],
            }

    fallback = _first_enabled_provider()
    if fallback is None:
        return {"provider_id": "", "model_id": ""}
    return {"provider_id": fallback["provider_id"], "model_id": primary_model_id(fallback)}


def _first_enabled_provider() -> dict | None:
    with db.get_conn() as conn:
        row = conn.execute(
            f"SELECT {PROVIDER_COLUMNS} FROM providers WHERE enabled = 1"
            " ORDER BY id ASC LIMIT 1"
        ).fetchone()
    if row is None:
        return None
    return _row_to_provider(row, legacy_model_id=row["model_id"] or "")


def default_provider() -> dict | None:
    """默认供应商：优先取全局默认指向的（且启用中），否则第一个启用的。"""
    stored = get_default_target()["provider_id"]
    if stored:
        with db.get_conn() as conn:
            row = conn.execute(
                f"SELECT {PROVIDER_COLUMNS} FROM providers"
                " WHERE provider_id = ? AND enabled = 1",
                (stored,),
            ).fetchone()
        if row is not None:
            return _row_to_provider(row, legacy_model_id=row["model_id"] or "")
    return _first_enabled_provider()


def list_models(base_url: str, api_key: str, timeout: int = 30) -> list[dict]:
    """从 baseURL 拉取模型列表（OpenAI 兼容 ``GET /models``）。"""
    url = f"{base_url.rstrip('/')}/models"
    try:
        response = httpx.get(
            url, headers={"Authorization": f"Bearer {api_key}"}, timeout=timeout
        )
    except httpx.HTTPError as exc:
        raise InvalidOperationError(f"无法连接 {url}：{exc}") from exc

    if response.status_code != 200:
        raise InvalidOperationError(
            f"拉取模型列表失败（HTTP {response.status_code}）：{response.text[:200]}"
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise InvalidOperationError("模型列表返回的不是 JSON") from exc

    items = payload.get("data") if isinstance(payload, dict) else payload
    models: list[dict] = []
    for item in items or []:
        if isinstance(item, dict):
            models.append({"id": str(item.get("id") or ""), "owned_by": item.get("owned_by", "")})
        elif isinstance(item, str):
            models.append({"id": item, "owned_by": ""})
    return [model for model in models if model["id"]]


def explain_auth_denial(provider_id: str, status: int = 403) -> str:
    """Recover a useful provider reason when the native stream reports no body.

    Some compatible streaming clients discard an HTTP error body. A bounded
    metadata request to the same configured provider can reveal a provider
    account error without replaying the author's prompt or modifying files.
    """
    fallback = f"模型服务拒绝访问（HTTP {status}）。请检查供应商「{provider_id}」的 API Key 和模型权限。"
    try:
        target = resolve(provider_id)
        response = httpx.get(
            f"{target['base_url']}/models",
            headers={"Authorization": f"Bearer {target['api_key']}"},
            timeout=5,
        )
        if response.status_code != status:
            return fallback
        body = response.json()
        if not isinstance(body, dict):
            return fallback
        error = body.get("error")
        detail = body.get("message") or (error.get("message") if isinstance(error, dict) else "")
        if not isinstance(detail, str) or not detail.strip():
            return fallback
        code = body.get("code") or (error.get("code") if isinstance(error, dict) else "")
        suffix = f"（{redact(str(code))[:80]}）" if code else ""
        return f"模型服务拒绝访问（HTTP {status}）：{redact(detail.strip())[:300]}{suffix}"
    except (InvalidOperationError, NodeNotFoundError, httpx.HTTPError, ValueError, OSError):
        return fallback


def health_check(provider_id: str, *, model_id: str | None = None) -> dict:
    """最小请求健康检查：``GET /models`` 通过即视为连通（不消耗生成额度）。"""
    provider = get_provider(provider_id)
    try:
        resolved = resolve(provider_id)
    except InvalidOperationError as exc:
        result = {"ok": False, "reason": exc.message, "kind": "credentials"}
        set_health(provider_id, "credentials-missing")
        return result

    try:
        models = list_models(resolved["base_url"], resolved["api_key"],
                             timeout=min(30, resolved["timeout_seconds"]))
    except InvalidOperationError as exc:
        set_health(provider_id, "unreachable")
        return {"ok": False, "reason": exc.message, "kind": "network"}

    target = model_id or primary_model_id(provider)
    matched = not target or any(model["id"] == target for model in models)
    health = "ok" if matched else "model-missing"
    set_health(provider_id, health)
    return {
        "ok": matched,
        "kind": "health",
        "model_count": len(models),
        "model_matched": matched,
        "reason": "" if matched else f"baseURL 上未找到模型 {target}",
        "models": [model["id"] for model in models[:50]],
    }


# ─────────────────────────── dsh 投影（单向） ───────────────────────────


def _env_name(provider_id: str) -> str:
    safe = "".join(ch if ch.isalnum() else "_" for ch in provider_id).upper()
    return f"WB_{safe}_API_KEY"


def dsh_home_paths() -> tuple[Path, Path]:
    home = get_dsh_home()
    return home / "settings.yaml", home / ".credentials.yaml"


def project_to_dsh_home() -> dict:
    """把启用中的工作台供应商单向投影到独立 dsh-home。

    - ``settings.yaml``：只写 ``llm-pi-ai.providers``，
      **绝不**写 ``agent-default-model``（否则会静默压过 ``--patch``）；
    - ``.credentials.yaml``：写入本项目 dsh-home 内的凭据（隔离环境，非用户目录）。
    """
    with _PROJECTION_LOCK:
        return _project_to_dsh_home_locked()


def _project_to_dsh_home_locked() -> dict:
    settings_path, credentials_path = dsh_home_paths()
    settings_path.parent.mkdir(parents=True, exist_ok=True)

    existing: dict = {}
    if settings_path.is_file():
        try:
            loaded = yaml.safe_load(read_text(settings_path))
            if isinstance(loaded, dict):
                existing = loaded
        except (yaml.YAMLError, OSError, UnicodeDecodeError):
            existing = {}

    # 强制遵守约束：删除可能存在的 agent-default-model
    existing.pop("agent-default-model", None)

    providers_block: list[dict] = []
    credentials: dict[str, str] = {}
    if credentials_path.is_file():
        try:
            loaded_credentials = yaml.safe_load(read_text(credentials_path))
            if isinstance(loaded_credentials, dict):
                credentials.update({key: value for key, value in loaded_credentials.items()
                                    if isinstance(key, str) and isinstance(value, str)
                                    and not key.startswith("WB_")})
        except (yaml.YAMLError, OSError, UnicodeDecodeError):
            pass
    for provider in list_providers():
        if not provider["enabled"]:
            continue
        api_key = get_secret(provider["secret_ref"] or f"provider:{provider['provider_id']}")
        env_name = _env_name(provider["provider_id"])
        if api_key:
            credentials[env_name] = api_key
        models = [
            {
                "id": model["id"],
                "name": model["name"],
                "contextWindow": int(model["context_window"]),
                "maxTokens": int(model["max_tokens"]),
            }
            for model in provider["models"]
        ]
        if not models:
            continue
        providers_block.append(
            {
                "provider_key": provider["provider_id"],
                "display_name": provider["display_name"],
                "base_url": provider["base_url"].rstrip("/"),
                "api_key_env": env_name,
                "models": models,
            }
        )

    llm_section = existing.get("llm-pi-ai")
    llm_section = dict(llm_section) if isinstance(llm_section, dict) else {}
    llm_section["providers"] = {
        item["provider_key"]: {
            "displayName": item["display_name"],
            "api": "openai-completions",
            "baseURL": item["base_url"],
            "apiKeyEnv": item["api_key_env"],
            "compat": {
                "supportsDeveloperRole": False,
                "maxTokensField": "max_tokens",
            },
            "models": item["models"],
        }
        for item in providers_block
    }
    # This projection owns only provider routes; preserve other isolated-home
    # settings instead of erasing them on every save and application startup.
    settings = {**existing, "llm-pi-ai": llm_section}

    header = (
        "# ============================================================\n"
        "# 工作台独立 DSH_HOME 配置文件（由工作台单向投影，请勿手工编辑）\n"
        "# 路径：<项目根>/.workbench/dsh-home/settings.yaml\n"
        "# 约束：此处**不写** agent-default-model（模型一律经 --patch 按次注入）。\n"
        "# ============================================================\n\n"
    )
    atomic_write_text(
        settings_path, header + yaml.safe_dump(settings, allow_unicode=True, sort_keys=False)
    )

    cred_header = (
        "# 工作台隔离环境的 dsh 凭据（仅本项目 .workbench/dsh-home，非用户 ~/.dsh）\n"
    )
    atomic_write_text(
        credentials_path,
        cred_header + yaml.safe_dump(credentials, allow_unicode=True, sort_keys=False),
    )

    return {
        "settings_path": str(settings_path),
        "credentials_path": str(credentials_path),
        "projected": len(providers_block),
        "agent_default_model_written": False,
    }


def import_from_yaml_text(text: str) -> dict:
    """从用户**主动粘贴**的 dsh settings.yaml 文本导入非敏感 provider 信息。

    安全约束：Key **不自动复制**（必须重录入）；只导入 baseURL / 模型列表 / 模型参数。
    工作台不会主动读取用户全局 ``~/.dsh`` 目录（隔离红线）。
    """
    try:
        data = yaml.safe_load(text or "")
    except yaml.YAMLError as exc:
        raise InvalidOperationError(f"settings.yaml 解析失败：{exc}") from exc
    if not isinstance(data, dict):
        raise InvalidOperationError("settings.yaml 内容不是 YAML 映射")

    block = data.get("llm-pi-ai", {}).get("providers", {})
    if not isinstance(block, dict) or not block:
        raise InvalidOperationError("未找到 llm-pi-ai.providers 配置节")

    imported: list[dict] = []
    for provider_id, config in block.items():
        if not isinstance(config, dict):
            continue
        raw_models = config.get("models") or []
        imported_models = [
            {
                "id": str(item.get("id") or ""),
                "name": str(item.get("name") or ""),
                "context_window": item.get("contextWindow") or 0,
                "max_tokens": item.get("maxTokens") or 0,
            }
            for item in raw_models
            if isinstance(item, dict)
        ]
        imported.append(
            upsert_provider(
                str(provider_id),
                display_name=str(config.get("displayName") or provider_id),
                base_url=str(config.get("baseURL") or ""),
                models=imported_models,
                capabilities=[],
                api_key=None,  # Key 必须重新录入
                source="imported",
                enabled=False,
            )
        )
    return {
        "imported": imported,
        "note": "Key 未导入（安全约束）；请在模型配置页为每个供应商重新填写 Key。",
    }


__all__ = [
    "CAPABILITY_TAGS",
    "DEFAULT_CONTEXT_WINDOW",
    "DEFAULT_MAX_TOKENS",
    "default_provider",
    "default_target",
    "delete_provider",
    "get_default_target",
    "get_provider",
    "health_check",
    "import_from_yaml_text",
    "list_models",
    "list_providers",
    "normalize_models",
    "primary_model_id",
    "project_to_dsh_home",
    "resolve",
    "set_default_target",
    "set_enabled",
    "set_health",
    "upsert_provider",
]
