"""Per-book chapter word range (每章字数区间).

Book-level overrides live in the registered book's ``.meta/writing-prefs.json``;
the global defaults live in ``settings.writing``. The range drives the generated
target, the word-count gate and the editor display.
"""

from __future__ import annotations

import json
from pathlib import Path

from .errors import InvalidOperationError
from .fs_utils import atomic_write_text
from .project_service import get_project_dir
from .settings_service import read_settings

PREFERENCE_FILE = ".meta/writing-prefs.json"
# A present but malformed book file falls back to this book-level safe range
# instead of inheriting a possibly looser global value (mirrors chat preferences).
SAFE_DEFAULT = {"chapter_min_words": 2000, "chapter_max_words": 4000}
MAX_CHAPTER_WORDS = 50000


def _path(project_id: int) -> Path:
    _, book_dir = get_project_dir(project_id)
    path = book_dir / PREFERENCE_FILE
    for node in (book_dir, path.parent, path):
        if node.is_symlink() or (hasattr(node, "is_junction") and node.is_junction()):
            raise InvalidOperationError("本书字数区间不能通过链接读取或写入")
    return path


def _valid_pair(minimum: object, maximum: object) -> bool:
    return (isinstance(minimum, int) and not isinstance(minimum, bool)
            and isinstance(maximum, int) and not isinstance(maximum, bool)
            and 0 < minimum < maximum <= MAX_CHAPTER_WORDS)


def _valid(value: object) -> bool:
    return (isinstance(value, dict)
            and _valid_pair(value.get("chapter_min_words"), value.get("chapter_max_words")))


def get_writing_prefs(project_id: int) -> dict:
    """Read this book's range, or the validated global defaults.

    A present but malformed book file falls back to the book-level safe range
    instead of inheriting the (possibly looser) global value. It remains on disk
    for inspection and can be repaired by PUT.
    """
    path = _path(project_id)
    if not path.exists():
        writing = read_settings().get("writing")
        if not isinstance(writing, dict) or not _valid_pair(
                writing.get("chapter_min_words"), writing.get("chapter_max_words")):
            return {**SAFE_DEFAULT, "source": "global"}
        return {"chapter_min_words": int(writing["chapter_min_words"]),
                "chapter_max_words": int(writing["chapter_max_words"]), "source": "global"}
    try:
        if not path.is_file() or path.stat().st_size > 64 * 1024:
            raise ValueError("invalid preference file")
        value = json.loads(path.read_text(encoding="utf-8"))
        if not _valid(value):
            raise ValueError("invalid preference value")
    except (OSError, UnicodeError, ValueError, TypeError):
        return {**SAFE_DEFAULT, "source": "book"}
    return {"chapter_min_words": int(value["chapter_min_words"]),
            "chapter_max_words": int(value["chapter_max_words"]), "source": "book"}


def chapter_word_range(project_id: int) -> tuple[int, int]:
    """生效区间 ``(下限, 上限)``，供字数门、合同预算与导出对齐使用。"""
    prefs = get_writing_prefs(project_id)
    return int(prefs["chapter_min_words"]), int(prefs["chapter_max_words"])


def update_writing_prefs(project_id: int, *, chapter_min_words: int,
                         chapter_max_words: int) -> dict:
    if not _valid_pair(chapter_min_words, chapter_max_words):
        raise InvalidOperationError(
            f"每章字数区间无效：下限与上限须为正整数且下限小于上限，上限不超过 {MAX_CHAPTER_WORDS}")
    path = _path(project_id)
    value = {"chapter_min_words": int(chapter_min_words),
             "chapter_max_words": int(chapter_max_words)}
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    return {**value, "source": "book"}


__all__ = [
    "MAX_CHAPTER_WORDS",
    "PREFERENCE_FILE",
    "SAFE_DEFAULT",
    "chapter_word_range",
    "get_writing_prefs",
    "update_writing_prefs",
]