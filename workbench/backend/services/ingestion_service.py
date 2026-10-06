"""章节摄取：有原文依据的四件套 + 滚动摘要，按正文版本幂等提交。

四件套（时间线 / 角色状态 / 伏笔台账 / 资源账本）
- **时间线**、**资源账本**、**伏笔埋设**：确定性解析，直接托管写入（附「依据原文」）；
- **角色状态变更**：一律生成 **Proposal**（人工应用后才落盘，符合 spec）；
- **伏笔回收**：需人工确认，重复提取保留既有确认结果；
- **摘要**：写入 ``.meta/summaries.json``（滚动摘要，供上下文第 7 级）。

确定性核心路径零 LLM；AI 结构化抽取可选，失败明确降级。人物出场不推断其知情记忆。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from . import generation_service, prompt_registry_service, proposal_service
from .character_service import character_names
from .chapter_service import require_chapter_path
from .errors import NodeNotFoundError
from .fs_utils import atomic_write_text, compose_document, read_text, split_frontmatter
from .operation_log import log as log_operation
from .project_service import get_project_dir
from .snapshot_service import snapshot_file

TIMELINE_FILE = "状态/时间线.md"
LEDGER_FILE = "状态/资源账本.md"
FORESHADOW_FILE = "设定/伏笔管理.md"
SUMMARIES_FILE = ".meta/summaries.json"

# 时间线索（确定性）：章内出现的时间表述
_TIME_PATTERNS: tuple[str, ...] = (
    r"\d{1,4}\s*年(?:\s*\d{1,2}\s*月)?",
    r"\d{1,2}\s*月\s*\d{1,2}\s*日",
    r"(?:次日|翌日|第二天|三天后|三日后|数日后|半月后|一个月后|多年后|十年后|当夜|当晚|今夜|今晚|今晨|今早|昨夜|昨日|今日|清晨|正午|黄昏|傍晚|深夜|午夜)",
)

# 资源/数值（确定性）：如「获得 300 灵石」「消耗 2 枚丹药」
_GAIN_RE = re.compile(
    r"(?:获得|得到|拿到|入账|奖励|捡到|收取)\s*([\u4e00-\u9fffA-Za-z]{1,8})?\s*([0-9]+)\s*([\u4e00-\u9fff]{1,4})"
)
_LOSS_RE = re.compile(
    r"(?:消耗|失去|花掉|用掉|支出|扣除|耗费)\s*([\u4e00-\u9fffA-Za-z]{1,8})?\s*([0-9]+)\s*([\u4e00-\u9fff]{1,4})"
)

_FORESHADOW_MARK_RE = re.compile(r"(?:伏笔|埋线|埋下)[：:]\s*([^\n。；;]{2,60})")


def _read(project_dir: Path, rel: str) -> str:
    path = Path(project_dir) / rel
    if not path.is_file():
        return ""
    try:
        return read_text(path)
    except (OSError, UnicodeDecodeError):
        return ""


def _append_rows(project_dir: Path, rel: str, title: str, rows: list[str]) -> None:
    """向托管文件追加行（保持既有自由书写内容不变）。"""
    path = Path(project_dir) / rel
    text = read_text(path) if path.is_file() else compose_document({"标题": title}, "")
    meta, body = split_frontmatter(text)
    body = body.rstrip()
    if rows:
        body = f"{body}\n" + "\n".join(rows) + "\n"
    atomic_write_text(path, compose_document(meta, body))


# ─────────────────────────── 确定性抽取 ───────────────────────────


def summarize(text: str, limit: int = 150) -> str:
    """确定性摘要：取首尾关键句（零 LLM，保证可用）。"""
    body = split_frontmatter(text)[1]
    sentences = [item.strip() for item in re.split(r"(?<=[。！？…])", body) if item.strip()]
    if not sentences:
        return ""
    if len(sentences) <= 3:
        return "".join(sentences)[:limit]
    head = "".join(sentences[:2])
    tail = sentences[-1]
    summary = f"{head}……{tail}"
    return summary[:limit]


def extract_time_clues(text: str) -> list[dict]:
    body = split_frontmatter(text)[1]
    clues: list[dict] = []
    for pattern in _TIME_PATTERNS:
        for match in re.finditer(pattern, body):
            snippet = _context(body, match.start(), match.end())
            clues.append({"线索": match.group(0), "依据": snippet,
                          "_start": body.rfind("\n", 0, match.start()) + 1,
                          "_event_start": match.start(), "_event_end": match.end()})
            if len(clues) >= 6:
                return clues
    return clues


def _context(text: str, start: int, end: int, width: int = 18) -> str:
    # Whole-line excerpts stay literal and independently locatable. The exact
    # event span disambiguates repeated events within the same line.
    left = text.rfind("\n", 0, start) + 1
    right = text.find("\n", end)
    return text[left:right if right >= 0 else len(text)].strip()[:3000]


def extract_resources(text: str) -> list[dict]:
    body = split_frontmatter(text)[1]
    entries: list[dict] = []
    for regex, sign in ((_GAIN_RE, 1), (_LOSS_RE, -1)):
        for match in regex.finditer(body):
            name = (match.group(1) or "").strip()
            amount = float(match.group(2))
            unit = (match.group(3) or "").strip()
            measure = unit[:1] if len(unit) > 1 and unit[:1] in "枚袋颗块瓶把本两斤个件粒份张条支箱盏桶封" else ""
            item = f"{name}{unit[1:]}" if measure else f"{name}{unit}"
            entries.append(
                {
                    "物品": item,
                    "增减": sign * amount,
                    "依据": _context(body, match.start(), match.end()),
                    "单位": measure,
                    "_start": body.rfind("\n", 0, match.start()) + 1,
                    "_event_start": match.start(), "_event_end": match.end(),
                }
            )
    return entries[:20]


def extract_foreshadow_marks(text: str) -> list[dict]:
    body = split_frontmatter(text)[1]
    return [
        {"内容": match.group(1).strip(), "依据": _context(body, match.start(), match.end()),
         "_start": body.rfind("\n", 0, match.start()) + 1,
         "_event_start": match.start(), "_event_end": match.end()}
        for match in _FORESHADOW_MARK_RE.finditer(body)
    ][:10]


def extract_characters_in_text(text: str, known: list[str]) -> list[str]:
    body = split_frontmatter(text)[1]
    return [name for name in known if body.count(name) >= 2]


# ─────────────────────────── AI 结构化抽取（可选） ───────────────────────────


def extract_structured(project_id: int, text: str) -> dict | None:
    prompt = prompt_registry_service.get_prompt("novel.structure.extract")
    result = generation_service.run_task(
        project_id=project_id,
        task_type="结构化抽取",
        system=prompt["body"],
        messages=[{"role": "user", "content": split_frontmatter(text)[1][:20000]}],
        prompt_id=prompt["prompt_id"],
        prompt_version=prompt["version"],
        temperature=0.0,
    )
    if not result["ok"]:
        return None
    from .review_service import _parse_json  # 复用 JSON 抠取

    return _parse_json(result["text"])


# ─────────────────────────── 摄取主流程 ───────────────────────────


def ingest_chapter(
    project_id: int,
    chapter_rel: str,
    *,
    use_ai: bool = True,
    apply_state_directly: bool = False,
) -> dict:
    """Grounded finalized-chapter trackers; role fields enter one inbox proposal."""
    from . import ingestion_commit as commit, ingestion_records as records
    from .file_change_service import file_state, project_lock
    from .character_service import compose_state_document
    from .errors import InvalidOperationError

    # Windows accepts either separator; replay identity must have one spelling.
    chapter_rel = str(chapter_rel).replace("\\", "/")

    # The prompt revision participates in replay identity: a new extraction
    # policy must not be mistaken for a previously completed review.
    prompt_version = prompt_registry_service.get_prompt("novel.structure.extract")["version"] if use_ai else "offline"
    revision = commit.REVISION + ":" + str(prompt_version)
    run, replay, text = commit.claim(project_id, chapter_rel, use_ai, revision)
    if replay is not None:
        return replay
    assert run is not None
    try:
        body = split_frontmatter(text)[1]
        warnings = []
        structured = None
        if use_ai:
            try:
                structured = extract_structured(project_id, text)
                if (not isinstance(structured, dict) or not any(key in structured for key in
                        ("时间线索", "资源变更", "伏笔", "状态变更", "出场角色"))):
                    structured = None
            except Exception as exc:
                warnings.append(f"AI 提取异常（{type(exc).__name__}），已使用确定性提取。")
            if structured is None and not warnings:
                warnings.append("AI 提取未完成，已使用确定性提取；角色状态尚未获得新建议。")
        if apply_state_directly:
            warnings.append("角色状态变更需要作者核对，本次已按收件箱提案处理。")
        normalized, rejected = records.normalize(body, run["book_key"], chapter_rel, {
            "timeline": extract_time_clues(body),
            "ledger": extract_resources(body),
            "foreshadow": extract_foreshadow_marks(body),
        }, structured)
        summary = summarize(text)
        known = character_names(project_id)
        present = extract_characters_in_text(body, known)
        # Presence is descriptive only. It does not imply that a character knows
        # the chapter summary or events seen by somebody else.
        previous = commit.latest(project_id, chapter_rel)
        previous_plan = json.loads(previous["plan_json"]) if previous else {}
        source_key = previous_plan.get("source_key") or records._id(run["book_key"], chapter_rel)
        files, projections, counts = [], {}, {}
        targets = {"timeline": (TIMELINE_FILE, "时间线"), "ledger": (LEDGER_FILE, "资源账本"),
                   "foreshadow": (FORESHADOW_FILE, "伏笔管理")}
        with project_lock(project_id):
            for track, (rel, title) in targets.items():
                current = file_state(project_id, rel)
                old_projection = previous_plan.get("projections", {}).get(track)
                projection_text = current["content"]
                if not old_projection:
                    projection_text, old_projection, adopted, possible = records.adopt_legacy(
                        projection_text, track, source_key, normalized[track], Path(chapter_rel).stem, body)
                    if adopted:
                        warnings.append(f"{title}已接管 {adopted} 条可唯一核验的历史系统条目，未重复追加。")
                    if possible:
                        warnings.append(f"{title}发现 {possible} 条可能重复的历史记录，归属或依据不唯一，已保留；请核对原材料。")
                rendered, projection = records.projection(
                    projection_text, track, source_key, normalized[track], old_projection,
                    Path(chapter_rel).stem, summary)
                if rendered is not None and current["content"] is None:
                    rendered = compose_document({"标题": title}, rendered.lstrip("\n"))
                projections[track] = projection
                warnings.extend(projection.get("warnings", []))
                if rendered != current["content"]:
                    files.append({"path": rel, "before": current["content"], "after": rendered})
            for track, entries in normalized.items():
                prior = {item["key"]: item for item in previous_plan.get("records", {}).get(track, [])}
                counts[track] = {
                    "added": sum(item["key"] not in prior for item in entries),
                    "updated": sum(item["key"] in prior and item != prior[item["key"]] for item in entries),
                    "skipped": sum(item["key"] in prior and item == prior[item["key"]] for item in entries),
                    "rejected": sum(item["kind"] == track for item in rejected),
                }
            state_proposal = None
            if normalized["state"]:
                state = file_state(project_id, "状态/角色状态.md")
                changes = [{"角色": item["subject"], "字段": item["field"], "新值": item["value"],
                            "依据": item["evidence"]} for item in normalized["state"]]
                candidate = compose_state_document(state["content"] or "", changes)
                if candidate != (state["content"] or ""):
                    state_proposal = {
                        "title": f"{Path(chapter_rel).stem} 角色状态变更（{len(changes)} 项）",
                        "base": state["content"] or "", "base_exists": state["exists"], "content": candidate,
                        "meta": {"ingest": True, "state_document_version": 1, "state_changes": changes,
                                 "chapter": chapter_rel, "ingestion": {
                                     "run_id": run["run_id"], "book_key": run["book_key"],
                                     "chapter_rel": chapter_rel, "source_hash": run["source_hash"],
                                     "target_hash": state["hash"],
                                 }},
                    }
                else:
                    counts["state"]["skipped"] = len(changes)
                    counts["state"]["added"] = counts["state"]["updated"] = 0
            if normalized["ledger"]:
                warnings.append("资源变化已登记；没有明确前值的结余标为待核实。")
            result = {
                "project_id": project_id, "chapter_rel": chapter_rel, "summary": summary,
                "run_id": run["run_id"], "source_hash": run["source_hash"],
                "source_changed": False, "replayed": False,
                "timeline_added": counts["timeline"]["added"],
                "ledger_added": counts["ledger"]["added"],
                "foreshadow_added": counts["foreshadow"]["added"],
                "memory_added": 0, "characters_present": present,
                "ai_used": structured is not None, "state_changes_detected": len(normalized["state"]),
                "counts": counts, "warnings": warnings, "rejected": rejected,
            }
            plan = {"source_key": source_key, "records": normalized, "projections": projections,
                    "files": files, "summary": commit.summary_plan(project_id, chapter_rel, summary),
                    "state_proposal": state_proposal, "result": result}
            result = commit.commit_ingestion(run, plan)
        from .index_service import invalidate_knowledge
        invalidate_knowledge(project_id)
        log_operation(project_id, "ingest", chapter_rel, result)
        return result
    except Exception:
        commit.abandon(run)
        raise
    finally:
        commit.release(run)


def _write_summary(project_dir: Path, chapter_rel: str, summary: str) -> None:
    if not summary:
        return
    path = Path(project_dir) / SUMMARIES_FILE
    data: dict = {}
    if path.is_file():
        try:
            loaded = json.loads(read_text(path))
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            data = {}
    data[chapter_rel] = summary
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def list_timeline(project_id: int) -> list[dict]:
    from .material_parser import parse_timeline
    from .file_change_service import file_state
    text = file_state(project_id, TIMELINE_FILE)["content"] or ""
    return [{**record, "rel_path": TIMELINE_FILE} for record in parse_timeline(text)]


def list_foreshadows(project_id: int) -> list[dict]:
    from .material_parser import parse_foreshadows
    from .file_change_service import file_state
    text = file_state(project_id, FORESHADOW_FILE)["content"] or ""
    return [{**record, "rel_path": FORESHADOW_FILE} for record in parse_foreshadows(text)]


def confirm_payoff(project_id: int, line: int, *, planned_chapter: str = "",
                   expected_hash: str | None = None) -> dict:
    """Change only the selected real record; stale UI versions never shift rows."""
    import uuid
    from . import file_change_service as changes
    from .material_parser import parse_foreshadows
    from .errors import InvalidOperationError
    if not isinstance(line, int) or isinstance(line, bool) or line < 1:
        raise InvalidOperationError("伏笔行号无效")
    if any(char in planned_chapter for char in "\r\n|｜"):
        raise InvalidOperationError("计划回收章必须为单行")
    with changes.project_lock(project_id):
        state = changes.file_state(project_id, FORESHADOW_FILE)
        if not state["exists"]:
            raise NodeNotFoundError(f"伏笔文件不存在：{FORESHADOW_FILE}")
        if expected_hash is not None:
            changes.assert_version(FORESHADOW_FILE, state["content"], expected_hash)
        records = [item for item in parse_foreshadows(state["content"]) if item["line"] == line]
        if len(records) != 1:
            raise InvalidOperationError("选中行不是可唯一定位的伏笔记录，请刷新")
        record = records[0]
        if record["is_planned"] or record["status"] == "作废":
            raise InvalidOperationError("候选或作废记录不能确认回收")
        status_span = record["spans"].get("status")
        if not status_span:
            raise InvalidOperationError("伏笔回收状态无法定位，请整理原材料")
        edits = [(status_span[0], status_span[1], "已回收")]
        if planned_chapter:
            span = record["spans"].get("planned_chapter")
            if span:
                edits.append((span[0], span[1], planned_chapter))
            elif record["format"] == "list":
                at = record["source_location"]["end"]
                edits.append((at, at, "｜计划回收：" + planned_chapter))
            else:
                raise InvalidOperationError("伏笔表格没有计划回收列，请先补列或留空")
        text = state["content"]
        for start, end, value in sorted(edits, reverse=True):
            text = text[:start] + value + text[end:]
        if text == state["content"]:
            return {"line": line, "content": record["raw"], "document_hash": state["hash"], "idempotent": True}
        result = changes.apply_change(project_id, run_id="payoff:" + uuid.uuid4().hex, session_id=None,
                                      tool_call_id="confirm", operation="rewrite", rel_path=FORESHADOW_FILE,
                                      expected_hash=state["hash"], content=text)
        updated = next(item for item in parse_foreshadows(text) if item["line"] == line)
        log_operation(project_id, "foreshadow-confirm", FORESHADOW_FILE, {"line": line, "change_id": result["id"]})
        return {"line": line, "content": updated["raw"], "document_hash": updated["document_hash"],
                "change_id": result["id"], "idempotent": False}


def list_ledger(project_id: int) -> list[dict]:
    from .material_parser import parse_ledger
    from .file_change_service import file_state
    text = file_state(project_id, LEDGER_FILE)["content"] or ""
    return [{**record, "rel_path": LEDGER_FILE, "delta": record["change"],
             "balance": record["after"], "unit": record.get("unit", "")}
            for record in parse_ledger(text)]


__all__ = [
    "LEDGER_FILE",
    "SUMMARIES_FILE",
    "TIMELINE_FILE",
    "confirm_payoff",
    "extract_characters_in_text",
    "extract_foreshadow_marks",
    "extract_resources",
    "extract_structured",
    "extract_time_clues",
    "ingest_chapter",
    "list_foreshadows",
    "list_ledger",
    "list_timeline",
    "summarize",
]
