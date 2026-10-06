"""Full-source semantic extraction, using the existing metered generation service."""
from __future__ import annotations

import json
import re

from . import generation_service
from .errors import InvalidOperationError


def _windows(text, size=6000, overlap=1000):
    """Cover every character; boundaries do not decide which objects exist."""
    start = 0
    while start < len(text):
        end = min(len(text), start + size)
        if end < len(text):
            boundary = text.rfind("\n", start + size // 2, end)
            if boundary > start:
                end = boundary + 1
        yield start, text[start:end]
        if end == len(text):
            return
        start = max(start + 1, end - overlap)


def extract_file(project_id, text, rel, category, model, prompt, job_id, checkpoint, validate):
    merged = {}
    for offset, window in _windows(text):
        checkpoint()
        # Context only helps attach continuation fields. It does not add facts.
        known = [{"name": card["name"], "category": card["category"], "evidence": card["evidence"]}
                 for card in merged.values()]
        result = generation_service.run_task(
            project_id=project_id, task_type="结构化抽取", engine="direct-api",
            provider=model["provider"], model=model["model"], prompt_id=prompt["prompt_id"], prompt_version=prompt["version"],
            system=prompt["body"], messages=[{"role": "user", "content": json.dumps(
                {"document": rel, "category_hint": category, "offset": offset, "text": window,
                 "previous_objects": known}, ensure_ascii=False)}], temperature=0, max_tokens=12000,
            context_snapshot={"document": rel, "window_start": offset, "job_id": job_id},
            should_cancel=lambda: _is_cancelled(checkpoint), timeout_seconds=120)
        checkpoint()
        if not result.get("ok"):
            raise InvalidOperationError(result.get("error_message") or "AI 卡片解析失败")
        response = re.sub(r"^```(?:json)?\s*|\s*```$", "", str(result.get("text") or "").strip())
        try:
            raw = json.loads(response)
        except (TypeError, ValueError) as exc:
            raise InvalidOperationError("模型返回的 JSON 不完整或无效；旧解析结果已保留") from exc
        cards = validate(raw, text, rel, category)
        for card in cards:
            key = card["category"], card["name"], card["evidence"]["start"]
            if key not in merged:
                merged[key] = card
                continue
            prior = merged[key]
            for field, value in card["fields"].items():
                if field in prior["fields"] and prior["fields"][field] != value:
                    raise InvalidOperationError(f"同一对象的字段「{field}」存在冲突，文件结果未应用")
                prior["fields"][field] = value
                prior["field_meta"][field] = card["field_meta"][field]
            prior["aliases"] = list(dict.fromkeys([*prior["aliases"], *card["aliases"]]))
            prior["alias_meta"].extend(a for a in card["alias_meta"] if a["name"] not in {e["name"] for e in prior["alias_meta"]})
            prior["field_list"] = list(prior["fields"])
            extent = prior.get("extent")
            if extent and any(not (extent["start"] <= f["evidence"]["start"] and f["evidence"]["end"] <= extent["end"])
                              for f in prior["field_meta"].values()):
                prior["extent"] = None
    return list(merged.values())


def _is_cancelled(checkpoint):
    try:
        checkpoint()
        return False
    except Exception:
        return True
