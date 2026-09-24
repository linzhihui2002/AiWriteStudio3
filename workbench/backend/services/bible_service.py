"""Story Bible（Task 27）：角色卡 / 世界观实体 / 时间线 / 伏笔台账 + 来源分级。

- 全部数据来自项目 Markdown（``设定/`` ``状态/``），本模块只做**解析与来源标注**；
- 来源分级：``author``（作者写入）/ ``model``（模型提炼）/ ``unknown``（无法判定）；
  标注存 ``.meta/bible_sources.json``（可重建，非事实源）；
- 审稿时 ``unknown`` 字段视为**待核实**（不算通过），见 :func:`unknown_fields`。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .character_service import list_characters
from .errors import InvalidOperationError
from .fs_utils import atomic_write_text, read_text, split_frontmatter
from .ingestion_service import list_foreshadows, list_timeline
from .project_service import get_project_dir

SETTING_FILES = {
    "world": "设定/世界设定.md",
    "faction": "设定/势力设定.md",
    "character": "设定/人物设定.md",
    "item": "设定/物品设定.md",
    "skill": "设定/技能设定.md",
    "scene": "设定/场景设定.md",
    "foreshadow": "设定/伏笔管理.md",
}

KIND_LABELS = {
    "world": "世界",
    "faction": "势力",
    "character": "人物",
    "item": "物品",
    "skill": "技能",
    "scene": "场景",
    "foreshadow": "伏笔",
}

SOURCE_LEVELS = ("author", "model", "unknown")
META_FILE = ".meta/bible_sources.json"

_HEADING_RE = re.compile(r"^(#{2,4})\s*([^\n#]{1,40})\s*$", re.MULTILINE)
_TABLE_ROW_RE = re.compile(r"^\s*\|(.+)\|\s*$")


def _meta_path(project_dir: Path) -> Path:
    return Path(project_dir) / META_FILE


def read_sources(project_dir: Path) -> dict:
    path = _meta_path(project_dir)
    if not path.is_file():
        return {}
    try:
        data = json.loads(read_text(path))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def write_source(project_id: int, ref: str, source: str, *, note: str = "") -> dict:
    """标注某条目来源（``kind:name`` 形式的 ref）。"""
    if source not in SOURCE_LEVELS:
        raise InvalidOperationError(f"来源必须是 {'/'.join(SOURCE_LEVELS)}：{source}")
    _row, project_dir = get_project_dir(project_id)
    data = read_sources(project_dir)
    data[ref] = {"source": source, "note": note}
    path = _meta_path(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    return {"ref": ref, "source": source, "note": note}


# ─────────────────────────── 解析 ───────────────────────────


def _parse_entities(project_dir: Path, kind: str) -> list[dict]:
    """解析设定文件里的实体条目：优先 ``## 名称`` 段落，退化用列表行。"""
    rel = SETTING_FILES.get(kind)
    if not rel:
        return []
    path = Path(project_dir) / rel
    if not path.is_file():
        return []
    body = split_frontmatter(read_text(path))[1]

    entries: list[dict] = []
    current: str | None = None
    buffer: list[str] = []
    for line in body.splitlines():
        match = _HEADING_RE.match(line)
        if match:
            if current:
                entries.append({"name": current, "content": "\n".join(buffer).strip()})
            current = match.group(2).strip()
            buffer = []
            continue
        if current is not None:
            buffer.append(line)
    if current:
        entries.append({"name": current, "content": "\n".join(buffer).strip()})

    if not entries:
        for line in body.splitlines():
            stripped = line.strip()
            if stripped.startswith(("- ", "* ")) and len(stripped) > 4:
                text = stripped[2:].strip()
                name = _TABLE_ROW_RE.sub("", text)[:20].strip() or text[:20]
                entries.append({"name": name, "content": text})

    # 表格行也算实体（| 名称 | 说明 |）
    for line in body.splitlines():
        match = _TABLE_ROW_RE.match(line)
        if not match:
            continue
        cells = [cell.strip() for cell in match.group(1).split("|")]
        if len(cells) < 2 or set(cells[0]) <= {"-", " "} or not cells[0]:
            continue
        if cells[0] in ("名称", "势力", "物品", "技能", "场景", "世界", "条目"):
            continue
        if any(entry["name"] == cells[0] for entry in entries):
            continue
        entries.append({"name": cells[0], "content": " ｜ ".join(cells[1:])})

    return [entry for entry in entries if entry["name"]]


def list_entities(project_id: int, kind: str) -> list[dict]:
    """列出某类实体（含来源标注与摘要）。"""
    if kind not in SETTING_FILES:
        raise InvalidOperationError(
            f"未知类别：{kind}（可选：{'/'.join(SETTING_FILES)}）"
        )
    row, project_dir = get_project_dir(project_id)
    sources = read_sources(project_dir)

    if kind == "character":
        entities = [
            {
                "name": item["name"],
                "content": item.get("raw", ""),
                "fields": item.get("fields", {}),
                "missing": item.get("missing", []),
            }
            for item in list_characters(project_id)
        ]
    elif kind == "foreshadow":
        entities = [
            {
                "name": item["content"][:24] or f"伏笔{item['line']}",
                "content": item["content"],
                "status": item["status"],
                "planted_in": item["planted_in"],
                "evidence": item["evidence"],
                "line": item["line"],
            }
            for item in list_foreshadows(project_id)
        ]
    else:
        entities = _parse_entities(project_dir, kind)

    for entity in entities:
        ref = f"{kind}:{entity['name']}"
        meta = sources.get(ref) or {}
        entity["ref"] = ref
        # 未显式标注的一律 unknown（来源不可考 → 审稿按待核实处理）
        entity["source"] = meta.get("source") or "unknown"
        entity["summary"] = re.sub(r"\s+", " ", str(entity.get("content") or ""))[:120]

    return entities


def overview(project_id: int) -> dict:
    """Story Bible 总览：各类条数 + 时间线 + 未标注来源统计。"""
    row, project_dir = get_project_dir(project_id)
    counts: dict[str, int] = {}
    unknown: list[str] = []
    for kind in ("character", "world", "faction", "item", "skill", "scene", "foreshadow"):
        entities = list_entities(project_id, kind)
        counts[kind] = len(entities)
        unknown.extend(entity["ref"] for entity in entities if entity["source"] == "unknown")

    timeline = list_timeline(project_id)
    foreshadows = list_foreshadows(project_id)
    return {
        "project_id": project_id,
        "project_name": row["name"],
        "labels": KIND_LABELS,
        "counts": counts,
        "total": sum(counts.values()),
        "timeline_count": len(timeline),
        "foreshadow_open": sum(1 for item in foreshadows if item["status"] == "待回收"),
        "foreshadow_total": len(foreshadows),
        "unknown_refs": unknown[:50],
        "unknown_count": len(unknown),
    }


def unknown_fields(project_id: int) -> list[dict]:
    """审稿用：来源为 ``unknown`` 的条目（视为待核实，不算通过）。"""
    result: list[dict] = []
    for kind in ("character", "world", "faction", "item", "skill", "scene"):
        for entity in list_entities(project_id, kind):
            if entity["source"] == "unknown":
                result.append(
                    {"kind": kind, "name": entity["name"], "ref": entity["ref"],
                     "说明": "来源未标注，审稿按待核实处理"}
                )
            if kind == "character":
                for field in entity.get("missing") or []:
                    result.append(
                        {
                            "kind": kind,
                            "name": entity["name"],
                            "ref": f"{entity['ref']}#{field}",
                            "说明": f"深度设定字段「{field}」缺失，审稿按待核实处理",
                        }
                    )
    return result


def mark_model_extracted(project_id: int, refs: list[str]) -> dict:
    """批量把某批条目标为 ``model`` 来源（AI 提炼产物）。"""
    _row, project_dir = get_project_dir(project_id)
    data = read_sources(project_dir)
    for ref in refs:
        data[ref] = {"source": "model", "note": "AI 提炼"}
    path = _meta_path(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    return {"marked": len(refs)}


__all__ = [
    "KIND_LABELS",
    "SETTING_FILES",
    "SOURCE_LEVELS",
    "list_entities",
    "mark_model_extracted",
    "overview",
    "read_sources",
    "unknown_fields",
    "write_source",
]