"""Shared projection of reviewed knowledge records in author-owned Markdown.

The surrounding free Markdown remains compatible. Managed records carry their
review and temporal scope in the file, so rebuilding a derived database cannot
promote a draft or a plan into history. Projections preserve original offsets.
"""
from __future__ import annotations

import json
import re

from .errors import InvalidOperationError

_OPEN = re.compile(r"^[ \t]*<!--[ \t]*wb-knowledge(?=[ \t\r\n])([^\r\n]*)\r?$", re.MULTILINE)
_CLOSE = re.compile(r"^[ \t]*<!--[ \t]*/wb-knowledge[ \t]*-->[ \t]*\r?$", re.MULTILINE)
LIFECYCLES = {"draft", "planned", "setting_fact", "historical_event"}


def render_record(metadata: dict, body: str) -> str:
    """Serialize a readable block without permitting comment delimiter injection."""
    if not isinstance(metadata, dict) or not str(metadata.get("id") or "").strip():
        raise InvalidOperationError("知识记录必须有稳定标识")
    if "wb-knowledge" in body and "<!--" in body:
        raise InvalidOperationError("知识记录正文不能嵌套知识标记")
    encoded = json.dumps(metadata, ensure_ascii=False, separators=(",", ":"))
    encoded = encoded.replace("<", "\\u003c").replace(">", "\\u003e")
    return f"<!-- wb-knowledge {encoded} -->\n{body.strip()}\n<!-- /wb-knowledge -->\n"


def parse_records(text: str) -> list[dict]:
    """Return metadata and exact full-file/body spans; malformed blocks fail closed."""
    openings = list(_OPEN.finditer(text or ""))
    records = []
    for index, opening in enumerate(openings):
        next_start = openings[index + 1].start() if index + 1 < len(openings) else len(text)
        closing = _CLOSE.search(text, opening.end(), next_start)
        body_start = opening.end()
        if text[body_start:body_start + 2] == "\r\n":
            body_start += 2
        elif text[body_start:body_start + 1] == "\n":
            body_start += 1
        try:
            raw = opening.group(1).strip()
            terminated = raw.endswith("-->")
            metadata = json.loads(raw[:-3].strip() if terminated else raw)
            valid = terminated and isinstance(metadata, dict) and bool(metadata.get("id")) and closing is not None
        except (ValueError, TypeError):
            metadata, valid = {}, False
        records.append({"metadata": metadata if isinstance(metadata, dict) else {},
                        "valid": valid, "start": opening.start(),
                        "end": closing.end() if closing else next_start,
                        "body_start": body_start, "body_end": closing.start() if closing else next_start})
    return records


def lifecycle(metadata: dict) -> str:
    value = str(metadata.get("lifecycle") or "")
    return {"plan": "planned", "canon": "setting_fact"}.get(value, value)


def approved(metadata: dict) -> bool:
    reviewer = metadata.get("reviewer") or {}
    if "review_status" in metadata:
        return metadata["review_status"] == "approved"
    return isinstance(reviewer, dict) and reviewer.get("decision") == "approved"


def _number(value):
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        found = re.search(r"第\s*(\d+)\s*章", str(value))
        return int(found[1]) if found else None


def visible(metadata: dict, profile: str = "history", chapter_before: int | None = None,
            at_chapter: int | None = None) -> bool:
    state = lifecycle(metadata)
    if not approved(metadata) or state not in LIFECYCLES:
        return False
    if profile not in {"planning", "graph", "all"} and state in {"draft", "planned"}:
        return False
    start, end = _number(metadata.get("chapter_start")), _number(metadata.get("chapter_end"))
    if start is not None and end is not None and end < start:
        return False
    if chapter_before is not None and start is not None and start >= int(chapter_before):
        return False
    if chapter_before is not None and state == "setting_fact" and end is not None and end < int(chapter_before) - 1:
        return False
    if at_chapter is not None and ((start is not None and start > at_chapter)
                                   or (end is not None and end < at_chapter)):
        return False
    return True


def _blank(value: str) -> str:
    return "".join(char if char in "\r\n" else " " for char in value)


def project_text(text: str, profile: str = "history", chapter_before: int | None = None,
                 preserve_offsets: bool = True, at_chapter: int | None = None) -> str:
    """Hide metadata and ineligible bodies while preserving free legacy content."""
    result, cursor = [], 0
    for record in parse_records(text):
        result.append(text[cursor:record["start"]])
        allowed = record["valid"] and visible(record["metadata"], profile, chapter_before, at_chapter)
        opener = text[record["start"]:record["body_start"]]
        body = text[record["body_start"]:record["body_end"]]
        closer = text[record["body_end"]:record["end"]]
        if preserve_offsets:
            result.extend((_blank(opener), body if allowed else _blank(body), _blank(closer)))
        elif allowed:
            result.append(body)
        cursor = record["end"]
    result.append(text[cursor:])
    return "".join(result)


def record_at_span(text: str, start: int, end: int) -> dict | None:
    """Locate the managed record owning a content span (metadata never qualifies)."""
    return next((record for record in parse_records(text)
                 if record["body_start"] <= start < record["body_end"] and end <= record["body_end"]), None)


def content_ranges(text: str) -> list[tuple[int, int]]:
    """Separate record bodies from legacy prose; no chunk may mix lifecycle scopes."""
    ranges, cursor = [], 0
    for record in parse_records(text):
        if cursor < record["start"]:
            ranges.append((cursor, record["start"]))
        if record["valid"] and visible(record["metadata"], "all"):
            ranges.append((record["body_start"], record["body_end"]))
        cursor = record["end"]
    if cursor < len(text):
        ranges.append((cursor, len(text)))
    return [(start, end) for start, end in ranges if text[start:end].strip()]


def public_metadata(metadata: dict) -> dict:
    """Provenance and author review stay separate in graph and retrieval payloads."""
    return {"knowledge_id": metadata.get("id"), "candidate_id": metadata.get("candidate_id"),
            "provenance": metadata.get("provenance", "model"),
            "review_status": "approved" if approved(metadata) else "pending",
            "lifecycle": lifecycle(metadata), "chapter_start": _number(metadata.get("chapter_start")),
            "chapter_end": _number(metadata.get("chapter_end")),
            "reviewer": metadata.get("reviewer", {}),
            "sources": metadata.get("sources") or ([metadata["evidence"]] if metadata.get("evidence") else [])}
