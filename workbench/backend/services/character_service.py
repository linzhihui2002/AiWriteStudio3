"""角色服务（M7）：深度设定解析、状态机、视角记忆、成长弧线。

事实源仍是 Markdown：
- 深度设定（性格/价值观/说话风格/关系网络/秘密与认知边界）→ ``设定/人物设定.md``
- 状态机（位置/持有物/伤势/心理/关系/能力）→ ``状态/角色状态.md``
- 视角记忆（谁知道什么/何时知道/经何事件）→ ``状态/角色记忆.md``

SQLite 中的 ``characters`` / ``character_states`` / ``character_memory`` 只是索引，
可随时由上述文件重建。所有**写回**都经 Proposal 收件箱（人工应用）或带来源标记的解析同步。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .. import db
from .errors import InvalidOperationError, NodeNotFoundError
from .fs_utils import atomic_write_text, compose_document, read_text, split_frontmatter
from .operation_log import log as log_operation
from .project_service import get_project_dir

SETTING_FILE = "设定/人物设定.md"
STATE_FILE = "状态/角色状态.md"
MEMORY_FILE = "状态/角色记忆.md"

# 深度设定字段（人物卡结构；缺失即 unknown）
DEEP_FIELDS: tuple[str, ...] = (
    "定位", "性格", "行为倾向", "核心价值观", "说话风格", "口癖",
    "关系网络", "秘密", "认知边界", "外貌", "背景", "目标", "弱点",
)
# 状态机核心字段（必注入）与次要字段（按需注入）
CORE_STATE_FIELDS: tuple[str, ...] = ("位置", "伤势", "心理", "持有物")
SECONDARY_STATE_FIELDS: tuple[str, ...] = ("关系", "能力", "立场", "目标")

_HEADING_RE = re.compile(r"^(#{2,4})\s*([^\n#]{1,40})\s*$", re.MULTILINE)
_KV_RE = re.compile(r"^\s*[-*]?\s*(?:\*\*)?([^\s:：*]{1,16})(?:\*\*)?\s*[:：]\s*(.+?)\s*$")


# ─────────────────────────── 解析 ───────────────────────────


def _split_sections(text: str) -> list[tuple[str, str]]:
    """把 Markdown 按 ``##`` 级别标题切成 ``[(标题, 正文块)]``。"""
    body = split_frontmatter(text or "")[1]
    sections: list[tuple[str, list[str]]] = []
    current_title = ""
    current_lines: list[str] = []
    for line in body.splitlines():
        match = _HEADING_RE.match(line)
        if match and len(match.group(1)) <= 3:
            if current_title or any(item.strip() for item in current_lines):
                sections.append((current_title, current_lines))
            current_title = match.group(2).strip()
            current_lines = []
            continue
        current_lines.append(line)
    if current_title or any(item.strip() for item in current_lines):
        sections.append((current_title, current_lines))
    return [(title, "\n".join(lines).strip()) for title, lines in sections if title or lines]


def _parse_fields(block: str) -> dict[str, str]:
    """从块里解析 ``- 字段：值`` 形式的字段（也接受 ``**字段**：值``）。"""
    fields: dict[str, str] = {}
    for line in block.splitlines():
        match = _KV_RE.match(line)
        if not match:
            continue
        key = match.group(1).strip()
        value = match.group(2).strip()
        if not key or not value or key.startswith("#"):
            continue
        if key in fields:
            fields[key] = f"{fields[key]}；{value}"
        else:
            fields[key] = value
    return fields


def parse_characters(project_dir: Path) -> list[dict]:
    """解析 ``设定/人物设定.md`` → 角色卡列表（确定性骨架，零 LLM）。"""
    path = Path(project_dir) / SETTING_FILE
    if not path.is_file():
        return []
    text = read_text(path)

    characters: list[dict] = []
    for title, block in _split_sections(text):
        if not title:
            continue
        # 跳过明显的非角色节
        if title in ("人物设定", "角色", "主要人物", "说明", "总览"):
            continue
        fields = _parse_fields(block)
        if not fields and len(block) < 8:
            continue
        characters.append(
            {
                "name": title,
                "raw": block,
                "fields": fields,
                "missing": [field for field in DEEP_FIELDS if field not in fields],
                "source": "author",
            }
        )
    return characters


def list_characters(project_id: int) -> list[dict]:
    _row, project_dir = get_project_dir(project_id)
    characters = parse_characters(project_dir)
    if not characters:
        # 降级：读索引表（外部手工维护）
        with db.get_conn() as conn:
            rows = conn.execute(
                "SELECT name, card, source FROM characters WHERE project_id = ?",
                (project_id,),
            ).fetchall()
        return [
            {"name": row["name"], "fields": {}, "raw": row["card"] or "",
             "missing": list(DEEP_FIELDS), "source": row["source"]}
            for row in rows
        ]
    return characters


def get_character(project_id: int, name: str) -> dict:
    for character in list_characters(project_id):
        if character["name"] == name:
            return character
    raise NodeNotFoundError(f"角色不存在：{name}")


def character_names(project_id: int) -> list[str]:
    return [item["name"] for item in list_characters(project_id)]


# ─────────────────────────── 状态机 ───────────────────────────


def parse_states(project_dir: Path) -> dict[str, dict[str, str]]:
    """解析 ``状态/角色状态.md`` → ``{角色: {字段: 值}}``（确定性）。"""
    path = Path(project_dir) / STATE_FILE
    if not path.is_file():
        return {}
    text = read_text(path)

    states: dict[str, dict[str, str]] = {}
    for title, block in _split_sections(text):
        if not title:
            continue
        fields = _parse_fields(block)
        # 表格行：| 角色 | 字段 | 值 |
        for line in block.splitlines():
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if len(cells) >= 3 and cells[0] and cells[1] and set(cells[0]) - set("- "):
                fields.setdefault(cells[1], cells[2])
        if fields:
            states[title] = fields
    return states


def list_states(project_id: int) -> dict:
    _row, project_dir = get_project_dir(project_id)
    states = parse_states(project_dir)
    return states


def get_state_value(project_id: int, character: str, field: str) -> str | None:
    """取某角色某字段的当前值（供跨章连续性守卫校验旧值）。"""
    states = list_states(project_id)
    entry = states.get(character) or {}
    if field in entry:
        return entry[field]
    # 宽松匹配：字段名包含关系（如「心理」命中「心理状态」）
    for key, value in entry.items():
        if field in key or key in field:
            return value
    return None


def character_state_summary(project_id: int, names: list[str]) -> dict:
    """按需注入用的状态摘要（仅指定角色；核心字段优先）。"""
    states = list_states(project_id)
    summary: dict[str, dict] = {}
    for name in names:
        entry = states.get(name)
        if entry is None:
            continue
        summary[name] = {
            "核心": {key: entry[key] for key in CORE_STATE_FIELDS if key in entry},
            "次要": {key: entry[key] for key in SECONDARY_STATE_FIELDS if key in entry},
        }
    return summary


def sync_state_index(project_id: int) -> int:
    """把状态文件同步进 ``character_states`` 索引（版本递增保留来源）。"""
    _row, project_dir = get_project_dir(project_id)
    states = parse_states(project_dir)
    count = 0
    with db.get_conn() as conn:
        for character, fields in states.items():
            for field, value in fields.items():
                conn.execute(
                    "INSERT INTO character_states (project_id, character, field, value, source)"
                    " VALUES (?, ?, ?, ?, 'author')"
                    " ON CONFLICT(project_id, character, field) DO UPDATE SET"
                    " value = excluded.value,"
                    " version = character_states.version + 1,"
                    " updated_at = datetime('now')",
                    (project_id, character, field, value),
                )
                count += 1
    return count


def apply_state_change(
    project_id: int,
    *,
    character: str,
    field: str,
    value: str,
    source: str = "model",
    chapter: str = "",
    evidence: str = "",
) -> dict:
    """写回状态文件（经摄取的托管路径）：更新 ``状态/角色状态.md`` 对应行。

    角色节不存在时创建该节；字段不存在时追加。写回内容保持「自由书写」的其余部分不动。
    """
    row, project_dir = get_project_dir(project_id)
    path = Path(project_dir) / STATE_FILE
    text = read_text(path) if path.is_file() else compose_document({"标题": "角色状态"}, "")
    meta, body = split_frontmatter(text)

    lines = body.splitlines()
    section_start = None
    section_end = len(lines)
    for index, line in enumerate(lines):
        match = _HEADING_RE.match(line)
        if not match:
            continue
        title = match.group(2).strip()
        if section_start is None and title == character:
            section_start = index
            continue
        if section_start is not None:
            section_end = index
            break

    updated_line = f"- {field}：{value}"
    if section_start is None:
        block = [f"## {character}", updated_line, ""]
        lines = lines + ([""] if lines and lines[-1].strip() else []) + block
    else:
        replaced = False
        for index in range(section_start + 1, section_end):
            match = _KV_RE.match(lines[index])
            if match and match.group(1).strip() == field:
                lines[index] = updated_line
                replaced = True
                break
        if not replaced:
            insert_at = section_end if section_end <= len(lines) else len(lines)
            while insert_at > section_start + 1 and not lines[insert_at - 1].strip():
                insert_at -= 1
            lines.insert(insert_at, updated_line)

    atomic_write_text(path, compose_document(meta, "\n".join(lines).rstrip() + "\n"))

    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO character_states"
            " (project_id, character, field, value, source, chapter, evidence)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(project_id, character, field) DO UPDATE SET"
            " value = excluded.value, source = excluded.source,"
            " chapter = excluded.chapter, evidence = excluded.evidence,"
            " version = character_states.version + 1, updated_at = datetime('now')",
            (project_id, character, field, value, source, chapter, evidence),
        )
    log_operation(project_id, "character-state-change", STATE_FILE,
                  {"character": character, "field": field, "value": value,
                   "source": source, "chapter": chapter})
    return {
        "character": character,
        "field": field,
        "value": value,
        "source": source,
        "chapter": chapter,
        "evidence": evidence,
    }


def list_state_index(project_id: int, character: str | None = None) -> list[dict]:
    sql = ("SELECT character, field, value, source, version, chapter, evidence, updated_at"
           " FROM character_states WHERE project_id = ?")
    params: list = [project_id]
    if character:
        sql += " AND character = ?"
        params.append(character)
    sql += " ORDER BY character, field"
    with db.get_conn() as conn:
        rows = conn.execute(sql, tuple(params)).fetchall()
    return [dict(row) for row in rows]


# ─────────────────────────── 视角记忆 ───────────────────────────


def list_memory(project_id: int, character: str | None = None) -> list[dict]:
    sql = ("SELECT id, character, know_what, when_known, source_event, source, created_at"
           " FROM character_memory WHERE project_id = ?")
    params: list = [project_id]
    if character:
        sql += " AND character = ?"
        params.append(character)
    sql += " ORDER BY id DESC LIMIT 500"
    with db.get_conn() as conn:
        rows = conn.execute(sql, tuple(params)).fetchall()
    return [dict(row) for row in rows]


def add_memory(
    project_id: int,
    *,
    character: str,
    know_what: str,
    when_known: str = "",
    source_event: str = "",
    source: str = "model",
    write_file: bool = True,
) -> dict:
    """新增一条视角记忆（谁知道什么 / 何时知道 / 经何事件）。"""
    if not character or not know_what.strip():
        raise InvalidOperationError("视角记忆需要角色与内容")
    with db.get_conn() as conn:
        cursor = conn.execute(
            "INSERT INTO character_memory"
            " (project_id, character, know_what, when_known, source_event, source)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (project_id, character, know_what.strip(), when_known, source_event, source),
        )
        memory_id = int(cursor.lastrowid or 0)

    if write_file:
        _append_memory_line(project_id, character, know_what.strip(), when_known,
                            source_event, source)
    return {
        "id": memory_id,
        "character": character,
        "know_what": know_what.strip(),
        "when_known": when_known,
        "source_event": source_event,
        "source": source,
    }


def _append_memory_line(project_id: int, character: str, know_what: str,
                        when_known: str, source_event: str, source: str) -> None:
    _row, project_dir = get_project_dir(project_id)
    path = Path(project_dir) / MEMORY_FILE
    text = read_text(path) if path.is_file() else compose_document({"标题": "角色记忆"}, "")
    meta, body = split_frontmatter(text)

    detail = know_what
    if when_known:
        detail += f"（{when_known}）"
    if source_event:
        detail += f"｜来源事件：{source_event}"

    lines = body.rstrip().splitlines()
    section_index = None
    for index, line in enumerate(lines):
        match = _HEADING_RE.match(line)
        if match and match.group(2).strip() == character:
            section_index = index
            break

    entry = f"- [来源:{source}] {detail}"
    if section_index is None:
        lines = lines + ([""] if lines and lines[-1].strip() else []) + [f"## {character}", entry]
    else:
        insert_at = section_index + 1
        while insert_at < len(lines) and _KV_RE.match(lines[insert_at]):
            insert_at += 1
        lines.insert(insert_at, entry)

    atomic_write_text(path, compose_document(meta, "\n".join(lines).rstrip() + "\n"))


def memory_digest(project_id: int, names: list[str], limit: int = 6) -> dict[str, list[str]]:
    """按角色取近期记忆摘要（供上下文按需注入）。"""
    digest: dict[str, list[str]] = {}
    for name in names:
        entries = list_memory(project_id, name)[:limit]
        if entries:
            digest[name] = [
                f"{item['know_what']}"
                + (f"（{item['when_known']}）" if item.get("when_known") else "")
                for item in entries
            ]
    return digest


def update_character_field(
    project_id: int,
    *,
    character: str,
    field: str,
    value: str,
    source: str = "author",
) -> dict:
    """更新角色深度设定字段（写入 ``设定/人物设定.md`` 对应角色节）。"""
    row, project_dir = get_project_dir(project_id)
    path = Path(project_dir) / SETTING_FILE
    if not path.is_file():
        raise NodeNotFoundError(f"设定文件不存在：{SETTING_FILE}")

    text = read_text(path)
    meta, body = split_frontmatter(text)
    lines = body.splitlines()

    section_start = None
    section_end = len(lines)
    for index, line in enumerate(lines):
        match = _HEADING_RE.match(line)
        if not match:
            continue
        title = match.group(2).strip()
        if section_start is None and title == character:
            section_start = index
            continue
        if section_start is not None:
            section_end = index
            break

    entry_line = f"- **{field}**：{value}"
    if section_start is None:
        lines = lines + ([""] if lines and lines[-1].strip() else []) + [f"## {character}",
                                                                        entry_line]
    else:
        replaced = False
        for index in range(section_start + 1, section_end):
            match = _KV_RE.match(lines[index])
            if match and match.group(1).strip().strip("*") == field:
                lines[index] = entry_line
                replaced = True
                break
        if not replaced:
            insert_at = section_end
            while insert_at > section_start + 1 and not lines[insert_at - 1].strip():
                insert_at -= 1
            lines.insert(insert_at, entry_line)

    atomic_write_text(path, compose_document(meta, "\n".join(lines).rstrip() + "\n"))

    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO characters (project_id, name, card, source, updated_at)"
            " VALUES (?, ?, ?, ?, datetime('now'))"
            " ON CONFLICT(project_id, name) DO UPDATE SET"
            " source = excluded.source, updated_at = datetime('now')",
            (project_id, character, body[:2000], source),
        )
    log_operation(project_id, "character-field-update", SETTING_FILE,
                  {"character": character, "field": field, "source": source})
    return {"character": character, "field": field, "value": value, "source": source}


# ─────────────────────────── 成长弧线 ───────────────────────────


def growth_curve(project_id: int, character: str) -> dict:
    """成长弧线：状态字段随章节的变化点（可跳转触发章节）。"""
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT field, value, version, chapter, evidence, source, updated_at"
            " FROM character_states WHERE project_id = ? AND character = ?"
            " ORDER BY field, version",
            (project_id, character),
        ).fetchall()

    with db.get_conn() as conn:
        timeline = conn.execute(
            "SELECT chapter, story_time, event, evidence FROM timeline_events"
            " WHERE project_id = ? ORDER BY id ASC",
            (project_id,),
        ).fetchall()

    series: dict[str, list[dict]] = {}
    for row in rows:
        series.setdefault(row["field"], []).append(
            {
                "value": row["value"],
                "version": int(row["version"] or 1),
                "chapter": row["chapter"] or "",
                "evidence": row["evidence"] or "",
                "source": row["source"],
                "updated_at": row["updated_at"],
            }
        )

    events = [
        {"chapter": row["chapter"], "story_time": row["story_time"], "event": row["event"],
         "evidence": row["evidence"]}
        for row in timeline
        if row["event"] and (character in str(row["event"]) or character in str(row["evidence"] or ""))
    ]
    return {"character": character, "series": series, "events": events}


def purge(project_id: int) -> None:
    """清空某项目的角色索引（索引可重建）。"""
    with db.get_conn() as conn:
        conn.execute("DELETE FROM characters WHERE project_id = ?", (project_id,))
        conn.execute("DELETE FROM character_states WHERE project_id = ?", (project_id,))
        conn.execute("DELETE FROM character_memory WHERE project_id = ?", (project_id,))


__all__ = [
    "CORE_STATE_FIELDS",
    "DEEP_FIELDS",
    "MEMORY_FILE",
    "SECONDARY_STATE_FIELDS",
    "SETTING_FILE",
    "STATE_FILE",
    "add_memory",
    "apply_state_change",
    "character_names",
    "character_state_summary",
    "get_character",
    "get_state_value",
    "growth_curve",
    "list_characters",
    "list_memory",
    "list_state_index",
    "list_states",
    "memory_digest",
    "parse_characters",
    "parse_states",
    "sync_state_index",
    "update_character_field",
]