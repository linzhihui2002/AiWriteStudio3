"""凭据安全存储：``.workbench/secrets.json``（Windows 走 DPAPI 加密）。

设计约束（spec：Key 泄漏防护）
------------------------------
- 明文 Key **只**存在加密后的 ``secrets.json`` 中；不入 Git、不入导出包、不入日志；
- Windows 上用 DPAPI（``CryptProtectData``，当前用户作用域）加密；
  非 Windows 或 DPAPI 不可用时降级为「明文 + 显式警告」（仍隔离在本机运行时目录）；
- 对外只暴露 :func:`get_secret` 给引擎调用，UI 一律走 :func:`mask` 掩码；
- 日志脱敏统一走 :func:`redact`。
"""

from __future__ import annotations

import base64
import ctypes
import json
import sys
from ctypes import wintypes
from pathlib import Path

from .. import config
from .fs_utils import atomic_write_text, read_text

ENTRIES_KEY = "entries"
MASK = "••••••••"


# ─────────────────────────── DPAPI ───────────────────────────


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob_to_bytes(blob: _DataBlob) -> bytes:
    return ctypes.string_at(blob.pbData, blob.cbData)


def _dpapi_available() -> bool:
    return sys.platform == "win32"


def _dpapi_protect(data: bytes) -> bytes:
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    blob_in = _DataBlob(len(data), ctypes.cast(ctypes.create_string_buffer(data),
                                               ctypes.POINTER(ctypes.c_char)))
    blob_out = _DataBlob()
    if not crypt32.CryptProtectData(ctypes.byref(blob_in), None, None, None, None, 0,
                                    ctypes.byref(blob_out)):
        raise OSError("DPAPI CryptProtectData 失败")
    try:
        return _blob_to_bytes(blob_out)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def _dpapi_unprotect(data: bytes) -> bytes:
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    blob_in = _DataBlob(len(data), ctypes.cast(ctypes.create_string_buffer(data),
                                               ctypes.POINTER(ctypes.c_char)))
    blob_out = _DataBlob()
    if not crypt32.CryptUnprotectData(ctypes.byref(blob_in), None, None, None, None, 0,
                                      ctypes.byref(blob_out)):
        raise OSError("DPAPI CryptUnprotectData 失败（可能跨用户或跨机器）")
    try:
        return _blob_to_bytes(blob_out)
    finally:
        kernel32.LocalFree(blob_out.pbData)


# ─────────────────────────── 存储 ───────────────────────────


def secrets_file() -> Path:
    return config.secrets_path()


def _read_store() -> dict:
    path = secrets_file()
    if not path.is_file():
        return {"version": 1, "cipher": "dpapi" if _dpapi_available() else "plain",
                ENTRIES_KEY: {}}
    try:
        data = json.loads(read_text(path))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return {"version": 1, "cipher": "plain", ENTRIES_KEY: {}}
    if not isinstance(data, dict):
        return {"version": 1, "cipher": "plain", ENTRIES_KEY: {}}
    data.setdefault(ENTRIES_KEY, {})
    return data


def _write_store(store: dict) -> None:
    atomic_write_text(
        secrets_file(), json.dumps(store, ensure_ascii=False, indent=2) + "\n"
    )


def _encode(value: str, cipher: str) -> str:
    raw = value.encode("utf-8")
    if cipher == "dpapi":
        return "dpapi:" + base64.b64encode(_dpapi_protect(raw)).decode("ascii")
    return "plain:" + base64.b64encode(raw).decode("ascii")


def _decode(value: str) -> str:
    if not value:
        return ""
    if value.startswith("dpapi:"):
        try:
            return _dpapi_unprotect(base64.b64decode(value[6:])).decode("utf-8")
        except (OSError, ValueError):
            return ""
    if value.startswith("plain:"):
        try:
            return base64.b64decode(value[6:]).decode("utf-8")
        except ValueError:
            return ""
    return ""


def set_secret(secret_id: str, value: str) -> dict:
    """写入（或覆盖）一条凭据，返回状态摘要（**不含明文**）。"""
    store = _read_store()
    cipher = "dpapi" if _dpapi_available() else "plain"
    store["cipher"] = cipher
    store[ENTRIES_KEY][secret_id] = _encode(value or "", cipher)
    _write_store(store)
    return {
        "secret_ref": secret_id,
        "cipher": cipher,
        "masked": mask(value),
        "encrypted": cipher == "dpapi",
    }


def get_secret(secret_id: str) -> str:
    """读取明文凭据（仅引擎调用；UI/日志禁止使用）。"""
    store = _read_store()
    entry = store.get(ENTRIES_KEY, {}).get(secret_id)
    return _decode(entry) if isinstance(entry, str) else ""


def has_secret(secret_id: str) -> bool:
    return bool(get_secret(secret_id))


def delete_secret(secret_id: str) -> bool:
    store = _read_store()
    removed = store.get(ENTRIES_KEY, {}).pop(secret_id, None) is not None
    if removed:
        _write_store(store)
    return removed


def list_secret_ids() -> list[str]:
    return sorted(_read_store().get(ENTRIES_KEY, {}).keys())


def cipher_mode() -> str:
    return _read_store().get("cipher", "plain")


# ─────────────────────────── 掩码与脱敏 ───────────────────────────


def mask(value: str | None) -> str:
    """把 Key 掩码成 ``sk-ab…1234`` 形式（UI 展示用）。"""
    text = str(value or "")
    if not text:
        return ""
    if len(text) <= 8:
        return MASK
    return f"{text[:5]}…{text[-4:]}"


def redact(text: str, secrets: list[str] | None = None) -> str:
    """日志脱敏：抹掉 Authorization/apiKey 值与本机已知凭据。"""
    import re

    result = str(text or "")
    for secret in secrets or []:
        if secret and len(secret) >= 8:
            result = result.replace(secret, "***REDACTED***")
    for secret_id in list_secret_ids():
        value = get_secret(secret_id)
        if value and len(value) >= 8:
            result = result.replace(value, "***REDACTED***")

    result = re.sub(
        r"(?i)(authorization\s*[:=]\s*(?:bearer\s+)?)([A-Za-z0-9_\-\.]{12,})",
        r"\1***REDACTED***",
        result,
    )
    result = re.sub(
        r'(?i)("?(?:api[_-]?key|apikey|token)"?\s*[:=]\s*"?)([A-Za-z0-9_\-.]{12,})',
        r"\1***REDACTED***",
        result,
    )
    return result


def import_keys_from_models_md(path: Path | None = None) -> dict:
    """把 ``模型API.md`` 里的明文 Key 迁入安全存储（原文件不动）。

    约定格式（宽松解析）：形如 ``名称: sk-xxx`` 或 ``| 名称 | sk-xxx |`` 的行。
    """
    import re

    source = Path(path or (config.PROJECT_ROOT / "模型API.md"))
    if not source.is_file():
        return {"migrated": [], "found": 0, "note": f"未找到 {source.name}"}

    text = read_text(source)
    pattern = re.compile(r"([\w\u4e00-\u9fff\- ]{2,32})\s*[:：|]\s*(sk-[A-Za-z0-9_\-]{8,})")
    migrated: list[dict] = []
    for name, key in pattern.findall(text):
        secret_id = f"imported:{name.strip()}"
        set_secret(secret_id, key)
        migrated.append({"secret_ref": secret_id, "masked": mask(key)})
    return {
        "migrated": migrated,
        "found": len(migrated),
        "note": "原文件未做任何修改；请自行决定是否删除其中的明文 Key。",
    }


__all__ = [
    "cipher_mode",
    "delete_secret",
    "get_secret",
    "has_secret",
    "import_keys_from_models_md",
    "list_secret_ids",
    "mask",
    "redact",
    "secrets_file",
    "set_secret",
]