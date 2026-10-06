"""Per-book defaults for newly created conversations.

These preferences belong to the registered book. Existing sessions keep the
permission values captured when they were created or explicitly updated.
"""

from __future__ import annotations

import json
from pathlib import Path

from .errors import InvalidOperationError
from .fs_utils import atomic_write_text
from .project_service import get_project_dir
from .settings_service import read_settings

PREFERENCE_FILE = ".meta/chat-preferences.json"
SAFE_DEFAULT = {"permission_mode": "auto", "discussion_only": False}
PERMISSION_MODES = frozenset({"ask", "auto", "full"})
#: 写域严格度：ask=范围外需批准（默认）/ reject=范围外一律拒绝
SCOPE_STRICTNESS_VALUES = frozenset({"ask", "reject"})
SCOPE_DEFAULT = {"scope_strictness": "ask", "scope_limit_full": False}


def get_scope_prefs() -> dict:
    """全局「写域」对话偏好：严格度与「完全访问是否也受写域限制」。

    缺失或手工改坏时回落到默认值（范围外需批准 / 完全访问不受写域限制），绝不抛错。
    """
    chat = read_settings().get("chat")
    chat = chat if isinstance(chat, dict) else {}
    strictness = chat.get("scope_strictness")
    if strictness not in SCOPE_STRICTNESS_VALUES:
        strictness = SCOPE_DEFAULT["scope_strictness"]
    limit_full = chat.get("scope_limit_full")
    if not isinstance(limit_full, bool):
        limit_full = SCOPE_DEFAULT["scope_limit_full"]
    return {"scope_strictness": strictness, "scope_limit_full": limit_full}


def _path(project_id: int) -> Path:
    _, book_dir = get_project_dir(project_id)
    path = book_dir / PREFERENCE_FILE
    for node in (book_dir, path.parent, path):
        if node.is_symlink() or (hasattr(node, "is_junction") and node.is_junction()):
            raise InvalidOperationError("本书对话偏好不能通过链接读取或写入")
    return path


def _valid(value: object) -> bool:
    return (isinstance(value, dict)
            and value.get("permission_mode") in PERMISSION_MODES
            and isinstance(value.get("discussion_only"), bool))


def get_chat_defaults(project_id: int) -> dict:
    """Read this book's defaults, or the validated global defaults.

    A present but malformed book file falls back to the book-level safe default
    instead of inheriting the global value. It remains on disk for inspection
    and can be repaired by PUT.
    """
    path = _path(project_id)
    if not path.exists():
        global_chat = read_settings()["chat"]
        return {"permission_mode": global_chat["permission_mode"],
                "discussion_only": global_chat["discussion_only"], "source": "global"}
    try:
        if not path.is_file() or path.stat().st_size > 64 * 1024:
            raise ValueError("invalid preference file")
        value = json.loads(path.read_text(encoding="utf-8"))
        if not _valid(value):
            raise ValueError("invalid preference value")
    except (OSError, UnicodeError, ValueError, TypeError):
        return {**SAFE_DEFAULT, "source": "book"}
    return {"permission_mode": value["permission_mode"],
            "discussion_only": value["discussion_only"], "source": "book"}


def update_chat_defaults(project_id: int, *, permission_mode: str,
                         discussion_only: bool) -> dict:
    if (not isinstance(permission_mode, str) or permission_mode not in PERMISSION_MODES
            or not isinstance(discussion_only, bool)):
        raise InvalidOperationError("对话默认权限或只讨论设置无效")
    path = _path(project_id)
    value = {"permission_mode": permission_mode, "discussion_only": discussion_only}
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    return {**value, "source": "book"}
