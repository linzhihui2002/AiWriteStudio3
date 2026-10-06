"""Read explicit Markdown material without guessing facts or rewriting its format.

Offsets always address the original document. Frontmatter, examples and managed
comment markers are excluded; property tables belong to their named object.
"""
from __future__ import annotations

import hashlib
import re

_FRONT = re.compile(r"^\ufeff?---[ \t]*\r?\n.*?\r?\n---[ \t]*(?:\r?\n|$)", re.S)
_HEADING = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
_FIELD = re.compile(r"^\s*(?:[-*+]\s+)?(?:\*\*)?([^:：*|]{1,80}?)(?:\*\*)?\s*[:：]\s*(.+?)\s*$")
_STATES = {"待回收", "疑似回收", "已回收", "作废"}
_PROPERTY = {"项目", "字段", "字段名", "属性", "属性名"}
_NAMES = {"名称", "姓名", "人物", "角色", "条目", "势力", "物品", "技能", "场景", "世界", "伏笔", "伏笔内容"}
_GENERIC = {"人物", "角色", "主要人物", "主角", "配角", "其他角色", "待建人物", "人物设定", "人物卡", "总览", "双界总览", "说明", "世界设定", "势力设定", "物品设定", "技能设定", "场景设定", "伏笔管理", "回收记录", "角色状态", "时间线", "待补充", "待完善", "意向登记"}
_SUBFIELDS = {"基本信息", "基础信息", "外貌", "性格", "人物关系", "关系", "经历", "背景", "目标", "能力", "弱点", "来源", "备注", "状态", "详细设定", "简介", "状态机", "动态状态", "时间流速"}


def clean(value: str) -> str:
    return re.sub(r"[*`]+", "", str(value)).strip()


def source_value(value: str) -> str:
    """Mixed or merely mentioned authorship is unknown, never implicitly author."""
    value = clean(value).lower()
    if value.startswith(("author", "model", "unknown")):
        found = re.match(r"^(author|model|unknown)\b", value)
        if found and not re.search(r"(?:/|、|\+|与|及)\s*(?:author|model|unknown)\b", value):
            return found[1]
    found = set(re.findall(r"\b(?:author|model|unknown)\b", value))
    return next(iter(found)) if len(found) == 1 else "unknown"


def visible_lines(text: str) -> list[dict]:
    front = _FRONT.match(text)
    body_start = front.end() if front else 0
    body_line = text[:body_start].count("\n")
    result, offset, fence, comment = [], 0, None, False
    for number, raw in enumerate(text.splitlines(keepends=True), 1):
        value = raw.rstrip("\r\n")
        stripped = value.strip()
        visible = offset >= body_start
        marker = re.match(r"^\s*(`{3,}|~{3,})(.*)$", value)
        if visible:
            if comment or "<!--" in value:
                visible = False
                comment = "-->" not in value[value.find("<!--") + 4:] if not comment else "-->" not in value
            elif fence:
                visible = False
                if marker and marker[1][0] == fence[0] and len(marker[1]) >= len(fence) and not marker[2].strip():
                    fence = None
            elif marker:
                fence, visible = marker[1], False
        result.append({"raw": raw, "text": value, "start": offset, "end": offset + len(value),
                       "line_start": number, "line_end": number, "line": number - body_line,
                       "visible": visible, "heading": _HEADING.match(value) if visible else None})
        offset += len(raw)
    return result


def table_cells(line: dict) -> list[dict]:
    value = line["text"]
    separators = [i for i, char in enumerate(value) if char == "|" and (i == 0 or value[i - 1] != "\\")]
    if len(separators) < 2 or value[:separators[0]].strip():
        return []
    bounds = list(zip(separators, separators[1:] + ([len(value)] if value[separators[-1] + 1:].strip() else [])))
    cells = []
    for left, right in bounds:
        raw = value[left + 1:right]
        start = left + 1 + len(raw) - len(raw.lstrip())
        end = right - (len(raw) - len(raw.rstrip()))
        cells.append({"value": clean(raw), "raw": raw, "span": [line["start"] + start, line["start"] + max(start, end)]})
    return cells


def table_rows(text: str) -> list[dict]:
    lines, result, headers = visible_lines(text), [], None
    for index, line in enumerate(lines):
        cells = table_cells(line) if line["visible"] else []
        if not cells:
            headers = None
            continue
        if index + 1 < len(lines):
            following = table_cells(lines[index + 1]) if lines[index + 1]["visible"] else []
            if following and len(following) == len(cells) and all(re.fullmatch(r":?-{2,}:?", c["value"]) for c in following):
                headers = [cell["value"] for cell in cells]
                continue
        if all(re.fullmatch(r":?-{2,}:?", c["value"]) for c in cells):
            continue
        if headers and len(cells) == len(headers):
            result.append({**line, "headers": list(headers), "cells": cells,
                           "values": dict(zip(headers, (c["value"] for c in cells))),
                           "spans": dict(zip(headers, (c["span"] for c in cells)))})
    return result


def _location(record: dict) -> dict:
    return {key: record[key] for key in ("start", "end", "line_start", "line_end")}


def _record(text: str, row: dict, format: str) -> dict:
    return {"line": row["line"], "document_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "raw": row["text"], "format": format, "source_location": _location(row)}


def _column(row: dict, aliases: tuple[str, ...]) -> tuple[str, list[int] | None]:
    key = next((name for name in aliases if name in row["values"]), None)
    return (row["values"][key], row["spans"][key]) if key else ("", None)


def _parts(line: dict) -> list[dict]:
    """Fullwidth separators are managed fields; preserve exact scalar offsets."""
    value = line["text"]
    prefix = re.match(r"^\s*(?:[-*+]\s+|\d+[.、)]\s+)", value)
    if not prefix:
        return []
    result, start = [], prefix.end()
    for end in [m.start() for m in re.finditer("｜", value) if m.start() >= start] + [len(value)]:
        raw = value[start:end]
        left = start + len(raw) - len(raw.lstrip())
        right = end - (len(raw) - len(raw.rstrip()))
        result.append({"value": raw.strip(), "span": [line["start"] + left, line["start"] + max(left, right)]})
        start = end + 1
    return result


def parse_timeline(text: str) -> list[dict]:
    result = []
    aliases = {"chapter": ("章节", "章号", "章", "节点"), "story_time": ("故事内时间", "故事时间", "时间"),
               "location": ("地点", "位置"), "event": ("事件", "内容"), "characters": ("涉及角色", "角色", "人物"),
               "evidence": ("依据原文", "依据", "证据")}
    for row in table_rows(text):
        if not any(name in row["values"] for name in aliases["event"]):
            continue
        values, spans = {}, {}
        for field, names in aliases.items():
            values[field], spans[field] = _column(row, names)
        if not values["event"]:
            continue
        if not values["story_time"]:
            values["story_time"] = "；".join(f"{key}：{row['values'][key]}" for key in row["headers"] if key.endswith("侧") and row["values"][key])
        result.append({**_record(text, row, "table"), **values, "spans": spans})
    for row in visible_lines(text):
        parts = _parts(row) if row["visible"] else []
        if len(parts) not in {4, 6}:
            continue
        keys = ["chapter", "story_time", "event", "evidence"] if len(parts) == 4 else ["chapter", "story_time", "location", "event", "characters", "evidence"]
        if not re.search(r"第\s*\d+\s*章|^章节[:：]", parts[0]["value"]):
            continue
        values = {key: re.sub(r"^(?:依据原文|依据|证据)[:：]\s*", "", part["value"]) for key, part in zip(keys, parts)}
        result.append({**_record(text, row, "list"), "location": "", "characters": "", **values,
                       "spans": dict(zip(keys, (part["span"] for part in parts)))})
    return sorted(result, key=lambda record: record["source_location"]["start"])


def parse_foreshadows(text: str) -> list[dict]:
    result = []
    aliases = {"content": ("内容", "伏笔内容", "伏笔", "名称"), "status": ("回收状态", "状态"),
               "planted_in": ("埋设章", "埋设位置", "埋设于", "埋设"), "evidence": ("依据原文", "依据", "证据"),
               "planned_chapter": ("计划回收章", "计划回收", "计划回收位置"), "record_id": ("伏笔ID", "编号", "ID"), "source": ("来源",)}
    for row in table_rows(text):
        if not any(key in row["values"] for key in aliases["content"]) or not any(key in row["values"] for key in aliases["status"]):
            continue
        values, spans = {}, {}
        for field, names in aliases.items():
            values[field], spans[field] = _column(row, names)
        if not values["content"]:
            continue
        values["source"] = source_value(values["source"])
        result.append({**_record(text, row, "table"), **values, "is_planned": values["status"] not in _STATES, "spans": spans})
    for row in visible_lines(text):
        parts = _parts(row) if row["visible"] else []
        if not parts:
            continue
        marker = re.search(r"\[(待回收|已回收|疑似回收|作废)\]", parts[0]["value"])
        structured = len(parts) >= 2 and any(re.match(r"(?:埋设|埋设章|依据)[:：]", p["value"]) for p in parts[1:])
        planned_title = re.match(r"\*\*([^*]+)\*\*[:：]", parts[0]["value"])
        if not marker and not structured and not planned_title:
            continue
        values = {"content": re.sub(r"\[(?:待回收|已回收|疑似回收|作废)\]\s*", "", parts[0]["value"]),
                  "status": marker[1] if marker else "候选", "planted_in": "", "evidence": "", "planned_chapter": "", "record_id": "", "source": "unknown"}
        spans = {"content": parts[0]["span"], "status": None, "planned_chapter": None}
        if marker:
            at = text.find(marker[0], *parts[0]["span"])
            spans["status"] = [at + 1, at + len(marker[0]) - 1]
        for part in parts[1:]:
            field = re.match(r"([^:：]+)[:：]\s*(.*)", part["value"])
            if not field:
                continue
            key = next((key for key, names in aliases.items() if field[1] in names), None)
            if key:
                values[key] = field[2]
                spans[key] = [part["span"][0] + field.start(2), part["span"][1]]
        values["source"] = source_value(values["source"])
        result.append({**_record(text, row, "list"), **values, "is_planned": values["status"] not in _STATES, "spans": spans})
    return sorted(result, key=lambda record: record["source_location"]["start"])


def parse_ledger(text: str) -> list[dict]:
    aliases = {"item": ("项目", "物品", "资源", "名称"), "chapter": ("章节", "章号", "章"),
               "change": ("变动", "增减", "变化"), "before": ("前值", "原值"), "after": ("结余", "余额", "结余值"), "evidence": ("依据原文", "依据", "证据")}
    result = []
    for row in table_rows(text):
        if not any(key in row["values"] for key in aliases["change"]):
            continue
        values, spans = {}, {}
        for field, names in aliases.items():
            values[field], spans[field] = _column(row, names)
        if values["item"]:
            result.append({**_record(text, row, "table"), **values, "spans": spans})
    seen = {record["source_location"]["start"] for record in result}
    for row in visible_lines(text):
        cells = table_cells(row) if row["visible"] else []
        if row["start"] in seen or len(cells) != 6 or not re.search(r"第\s*\d+\s*章", cells[1]["value"]) or not re.fullmatch(r"[+-]?\d+(?:\.\d+)?", cells[2]["value"]):
            continue
        keys = ["item", "chapter", "change", "before", "after", "evidence"]
        result.append({**_record(text, row, "table"), **dict(zip(keys, (cell["value"] for cell in cells))),
                       "spans": dict(zip(keys, (cell["span"] for cell in cells)))})
    return result


def _entity_name(title: str, kind: str) -> str:
    title = clean(title)
    title = re.sub(r"^(?:\d+[.、)]|[一二三四五六七八九十]+[、.])\s*", "", title)
    base = re.split(r"[（(]", title, maxsplit=1)[0].strip()
    if base in _GENERIC or base in _SUBFIELDS or re.search(r"状态机|待补充|待完善|待建", base):
        return ""
    if kind == "character":
        title = re.sub(r"^(?:主角|角色|人物|配角)[:：]\s*", "", title)
        title = re.split(r"[（(]", title, maxsplit=1)[0].strip()
    return title


def _fields(text: str, start: int, end: int) -> tuple[dict, dict, dict, dict]:
    fields, spans, rows, sources = {}, {}, {}, {}
    for row in table_rows(text):
        if not (start <= row["start"] and row["end"] <= end) or not row["headers"] or row["headers"][0] not in _PROPERTY:
            continue
        if len(row["cells"]) < 2:
            continue
        name, value = row["cells"][0]["value"], row["cells"][1]["value"]
        if name and value and name not in fields:
            fields[name], spans[name], rows[name] = value, row["cells"][1]["span"], [row["start"], row["start"] + len(row["raw"])]
            sources[name] = source_value(row["values"].get("来源", ""))
    for row in visible_lines(text):
        if not row["visible"] or not (start <= row["start"] and row["end"] <= end) or row["heading"]:
            continue
        match = _FIELD.match(row["text"])
        if match:
            name, value = clean(match[1]), match[2]
            if name and name not in fields:
                fields[name], spans[name], rows[name] = value, [row["start"] + match.start(2), row["start"] + match.end(2)], [row["start"], row["start"] + len(row["raw"])]
                declared = re.search(r"来源[:：]\s*(.*)", value)
                sources[name] = source_value(declared[1]) if declared else "unknown"
    return fields, spans, rows, sources


def parse_entities(text: str, kind: str) -> list[dict]:
    headings = [line for line in visible_lines(text) if line["heading"]]
    result = []
    for index, row in enumerate(headings):
        heading = row["heading"]
        name = _entity_name(heading[2], kind)
        if not name:
            continue
        end = next((other["start"] for other in headings[index + 1:] if len(other["heading"][1]) <= len(heading[1])), len(text))
        # A grouping parent with named child definitions has no own material.
        first_child = headings[index + 1] if index + 1 < len(headings) else None
        if first_child and first_child["start"] < end and _entity_name(first_child["heading"][2], kind) and not text[row["end"]:first_child["start"]].strip():
            continue
        fields, spans, rows, sources = _fields(text, row["end"], end)
        body = text[row["end"]:end].strip()
        if not body or re.fullmatch(r"(?:待补充|待完善|待定)[。.]?", body):
            continue
        declared = source_value(fields.get("来源", ""))
        if declared == "unknown":
            header_source = re.search(r"[（(]\s*(author|model|unknown)\b", heading[2])
            declared = header_source[1] if header_source else "unknown"
        result.append({"name": name, "content": body, "fields": fields, "field_sources": sources,
                       "field_spans": spans, "row_spans": rows, "source": declared,
                       "source_location": {"start": row["start"], "end": end, "line_start": row["line_start"], "line_end": text.count("\n", 0, end) + 1},
                       "format": "heading", "title": heading[2]})
    for row in table_rows(text):
        if row["headers"][0] in _PROPERTY:
            continue
        name_key = next((key for key in ("名称", "姓名", "人物", "角色", "条目", "势力", "物品", "技能", "场景", "世界") if key in row["values"]), None)
        if not name_key or not row["values"][name_key]:
            continue
        name = row["values"][name_key]
        if name in _GENERIC:
            continue
        fields = {key: value for key, value in row["values"].items() if key != name_key and key != "来源" and value}
        result.append({"name": name, "content": row["text"], "fields": fields,
                       "field_spans": {key: row["spans"][key] for key in fields}, "row_spans": {},
                       "field_sources": {key: source_value(row["values"].get("来源", "")) for key in fields},
                       "source": source_value(row["values"].get("来源", "")), "source_location": _location(row), "format": "table", "title": name})
    return sorted(result, key=lambda entity: entity["source_location"]["start"])


def parse_character_states(text: str) -> list[dict]:
    return parse_entities(text, "character")
