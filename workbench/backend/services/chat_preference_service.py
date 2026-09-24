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
SAFE_DEFAULT = {"permission_mode": "ask", "discussion_only": True}
PERMISSION_MODES = frozenset({"ask", "auto", "full"})


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

    A present but malformed book file never falls back to a more permissive
    global value. It remains on disk for inspection and can be repaired by PUT.
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
