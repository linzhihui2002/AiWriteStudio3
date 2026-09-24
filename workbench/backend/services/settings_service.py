"""工作台偏好设置：`.workbench/settings.json`（可重建，非事实源）。

设置项集中在此，供前端「设置」页与后端各服务读取：
字数里程碑阈值、护眼/外观参数、引擎默认值、预算告警、编辑器行为等。
读取时与默认值深合并，写入时做浅层合并（只覆盖传入的键）。
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from .. import config
from .fs_utils import atomic_write_text, read_text

DEFAULT_SETTINGS: dict[str, Any] = {
    "chat": {"permission_mode": "auto", "discussion_only": False},
    "milestone": {
        "enabled": True,
        "step": config.MILESTONE_STEP_DEFAULT,
    },
    "appearance": {
        "theme": "light",          # light / dark / paper
        "followSystem": False,
        "reduceMotion": False,
        "fontSize": 17,            # px
        "lineHeight": 1.9,
        "letterSpacing": 0.0,      # px
        "pageWidth": 760,          # px
        "colorTemperature": 0,     # -50(冷) ~ +50(暖)
        "contrast": 1.0,           # 0.8 ~ 1.3
    },
    "engines": {
        "default": "dsh-headless",  # dsh-headless / direct-api
        "overrides": {},            # {"正文生成": "direct-api", ...}
        "dsh_timeout_seconds": config.DSH_TIMEOUT_SECONDS,
    },
    "budget": {
        "daily_token_limit": 0,     # 0 = 不限制
        "alert_ratio": 0.8,
        "currency_per_1k_tokens": 0.0,
    },
    "editor": {
        "autosave_seconds": 3,
        "ghost_text": True,
        "milestone_inline": True,
    },
}


def _merge(base: dict, patch: dict) -> dict:
    """深合并（patch 覆盖 base；dict 递归，其余类型直接替换）。"""
    result = deepcopy(base)
    for key, value in (patch or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value
    return result


def settings_file() -> Path:
    return config.settings_path()


def read_settings() -> dict:
    """读取设置（与默认值合并；文件缺失或损坏时返回默认值）。"""
    path = settings_file()
    if not path.is_file():
        return deepcopy(DEFAULT_SETTINGS)
    try:
        data = json.loads(read_text(path))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return deepcopy(DEFAULT_SETTINGS)
    if not isinstance(data, dict):
        return deepcopy(DEFAULT_SETTINGS)
    merged = _merge(DEFAULT_SETTINGS, data)
    chat = merged.get("chat")
    if (not isinstance(chat, dict) or not isinstance(chat.get("permission_mode"), str)
            or chat["permission_mode"] not in {"ask", "auto", "full"}
            or not isinstance(chat.get("discussion_only"), bool)):
        # A manually edited or outdated permission must not grant writing access.
        merged["chat"] = {"permission_mode": "ask", "discussion_only": True}
    return merged


def update_settings(patch: dict) -> dict:
    """局部更新设置并落盘，返回合并后的完整设置。"""
    merged = _merge(read_settings(), patch or {})
    from .errors import InvalidOperationError
    chat = merged.get("chat")
    if (not isinstance(chat, dict) or not isinstance(chat.get("permission_mode"), str)
            or chat["permission_mode"] not in {"ask", "auto", "full"}
            or not isinstance(chat.get("discussion_only"), bool)):
        raise InvalidOperationError("对话默认权限或只讨论设置无效")
    atomic_write_text(
        settings_file(), json.dumps(merged, ensure_ascii=False, indent=2) + "\n"
    )
    return merged


def reset_settings() -> dict:
    """恢复默认设置（删除文件，读取时自然回落默认值）。"""
    settings_file().unlink(missing_ok=True)
    return deepcopy(DEFAULT_SETTINGS)


__all__ = [
    "DEFAULT_SETTINGS",
    "read_settings",
    "reset_settings",
    "settings_file",
    "update_settings",
]
