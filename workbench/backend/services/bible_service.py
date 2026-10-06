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

from .character_service import DEEP_FIELDS
from .errors import InvalidOperationError
from .fs_utils import atomic_write_text, read_text, resolve_within
from .ingestion_service import list_foreshadows, list_timeline
from .material_parser import parse_entities
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

def _meta_path(project_dir: Path) -> Path:
    return _safe_path(project_dir, META_FILE)


def _safe_path(project_dir: Path, rel: str) -> Path:
    candidate = Path(project_dir) / rel
    for part in (candidate, *candidate.parents):
        if part == Path(project_dir):
            break
        if part.is_symlink() or (hasattr(part, "is_junction") and part.is_junction()):
            raise InvalidOperationError("设定资料拒绝链接文件或目录")
    return resolve_within(project_dir, rel)


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
    from .file_change_service import project_lock
    with project_lock(project_id):
        if not any(entity["ref"] == ref for kind in SETTING_FILES for entity in list_entities(project_id, kind)):
            raise InvalidOperationError("条目不存在或字段尚未补齐，请打开原材料核对")
        data = read_sources(project_dir)
        data[ref] = {"source": source, "note": note}
        path = _meta_path(project_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    from .index_service import invalidate_knowledge
    invalidate_knowledge(project_id)
    return {"ref": ref, "source": source, "note": note}


# ─────────────────────────── 解析 ───────────────────────────


def _parse_entities(project_dir: Path, kind: str) -> list[dict]:
    """读取明确对象定义；属性表属于对象，不计为额外实体。"""
    rel = SETTING_FILES.get(kind)
    if not rel:
        return []
    path = _safe_path(project_dir, rel)
    if not path.is_file():
        return []
    return parse_entities(read_text(path), kind)


def list_entities(project_id: int, kind: str) -> list[dict]:
    """列出某类实体（含来源标注与摘要）。"""
    if kind not in SETTING_FILES:
        raise InvalidOperationError(
            f"未知类别：{kind}（可选：{'/'.join(SETTING_FILES)}）"
        )
    row, project_dir = get_project_dir(project_id)
    sources = read_sources(project_dir)

    if kind == "foreshadow":
        entities = [
            {
                "name": item["content"][:24] or f"伏笔{item['line']}",
                "content": item["content"],
                "status": item["status"],
                "planted_in": item["planted_in"],
                "evidence": item["evidence"],
                "line": item["line"],
                "source": item.get("source", "unknown"),
                "is_planned": item.get("is_planned", False),
            }
            for item in list_foreshadows(project_id)
        ]
    else:
        entities = _parse_entities(project_dir, kind)
        if kind == "character":
            for entity in entities:
                entity["missing"] = [field for field in DEEP_FIELDS if not entity.get("fields", {}).get(field)]

    for entity in entities:
        ref = f"{kind}:{entity['name']}"
        meta = sources.get(ref) if isinstance(sources.get(ref), dict) else {}
        entity["ref"] = ref
        # 未显式标注的一律 unknown（来源不可考 → 审稿按待核实处理）
        entity["source"] = meta.get("source") if meta.get("source") in SOURCE_LEVELS else entity.get("source") or "unknown"
        entity["rel_path"] = SETTING_FILES[kind]
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
        "foreshadow_open": sum(1 for item in foreshadows if item["status"] in {"待回收", "疑似回收"} and not item.get("is_planned")),
        "foreshadow_total": sum(1 for item in foreshadows if not item.get("is_planned")),
        "foreshadow_planned": sum(1 for item in foreshadows if item.get("is_planned")),
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
                    {"kind": kind, "name": entity["name"], "ref": entity["ref"], "reason": "source_unknown", "rel_path": entity["rel_path"],
                     "说明": "来源未标注，审稿按待核实处理"}
                )
            if kind == "character":
                for field in entity.get("missing") or []:
                    result.append(
                        {
                            "kind": kind,
                            "name": entity["name"],
                            "ref": f"{entity['ref']}#{field}",
                            "reason": "missing_field", "field": field, "rel_path": entity["rel_path"],
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
    from .index_service import invalidate_knowledge
    invalidate_knowledge(project_id)
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
