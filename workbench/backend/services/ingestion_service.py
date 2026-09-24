"""章节摄取服务（Task 30）：章节应用后 → 追踪四件套 + 滚动摘要 + 视角记忆。

四件套（时间线 / 角色状态 / 伏笔台账 / 资源账本）
- **时间线**、**资源账本**、**伏笔埋设**：确定性解析，直接托管写入（附「依据原文」）；
- **角色状态变更**：一律生成 **Proposal**（人工应用后才落盘，符合 spec）；
- **伏笔回收**：只把状态置为「疑似回收」，需人工在伏笔台账确认；
- **摘要**：写入 ``.meta/summaries.json``（滚动摘要，供上下文第 7 级）。

确定性核心路径零 LLM；AI 结构化抽取（direct-api）可选，失败自动降级。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from . import generation_service, prompt_registry_service, proposal_service
from .character_service import add_memory, apply_state_change, character_names
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
    r"(?:次日|翌日|第二天|三天后|三日后|数日后|半月后|一个月后|多年后|十年后|当夜|当晚|清晨|正午|黄昏|傍晚|深夜|午夜)",
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
            clues.append({"线索": match.group(0), "依据": snippet})
            if len(clues) >= 6:
                return clues
    return clues


def _context(text: str, start: int, end: int, width: int = 18) -> str:
    return text[max(0, start - width): min(len(text), end + width)].replace("\n", " ").strip()


def extract_resources(text: str) -> list[dict]:
    body = split_frontmatter(text)[1]
    entries: list[dict] = []
    for regex, sign in ((_GAIN_RE, 1), (_LOSS_RE, -1)):
        for match in regex.finditer(body):
            name = (match.group(1) or "").strip()
            amount = float(match.group(2))
            unit = (match.group(3) or "").strip()
            item = f"{name}{unit}" if name else unit
            entries.append(
                {
                    "物品": item,
                    "增减": sign * amount,
                    "依据": _context(body, match.start(), match.end()),
                }
            )
    return entries[:20]


def extract_foreshadow_marks(text: str) -> list[dict]:
    body = split_frontmatter(text)[1]
    return [
        {"内容": match.group(1).strip(), "依据": _context(body, match.start(), match.end())}
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
    """摄取一章 → 四件套 + 摘要 + 视角记忆。

    :param apply_state_directly: True 时角色状态直接写回（默认走 Proposal，需人工应用）
    """
    row, project_dir = get_project_dir(project_id)
    path = require_chapter_path(project_id, chapter_rel)
    text = read_text(path)
    body = split_frontmatter(text)[1]

    known = character_names(project_id)
    chapter_label = Path(chapter_rel).stem

    summary = summarize(text)
    clues = extract_time_clues(body)
    resources = extract_resources(body)
    marks = extract_foreshadow_marks(body)
    present = extract_characters_in_text(body, known)

    structured = extract_structured(project_id, text) if use_ai else None
    state_changes: list[dict] = []
    memory_entries: list[dict] = []
    if structured:
        summary = str(structured.get("摘要") or summary)
        for item in structured.get("状态变更") or []:
            if isinstance(item, dict) and item.get("角色") and item.get("字段"):
                state_changes.append(
                    {
                        "角色": str(item.get("角色")),
                        "字段": str(item.get("字段")),
                        "新值": str(item.get("新值") or ""),
                        "旧值": str(item.get("旧值") or ""),
                        "依据": str(item.get("依据") or ""),
                    }
                )
        for item in structured.get("伏笔") or []:
            if isinstance(item, dict) and item.get("内容"):
                marks.append({"内容": str(item["内容"]), "依据": str(item.get("依据") or "")})
        for item in structured.get("资源变更") or []:
            if isinstance(item, dict) and item.get("物品"):
                resources.append(
                    {
                        "物品": str(item["物品"]),
                        "增减": float(item.get("增减") or 0),
                        "依据": str(item.get("依据") or ""),
                    }
                )
        for item in structured.get("时间线索") or []:
            if isinstance(item, dict) and item.get("线索"):
                clues.append({"线索": str(item["线索"]), "依据": str(item.get("依据") or "")})
        for item in structured.get("出场角色") or []:
            name = str(item)
            if name and name not in present:
                present.append(name)

    # 1) 时间线（追加）
    timeline_rows = [
        f"- {chapter_label}｜{clue['线索']}｜{summary[:60]}｜依据：{clue['依据']}"
        for clue in clues
    ]
    if not timeline_rows and summary:
        timeline_rows = [f"- {chapter_label}｜（未标注时间）｜{summary[:60]}｜依据：章节摘要"]
    snapshot_file(project_id, project_dir, row["name"], TIMELINE_FILE, reason="ingest")
    _append_rows(project_dir, TIMELINE_FILE, "时间线", timeline_rows)

    # 2) 资源账本（追加，附算术自检提示）
    ledger_rows = [
        f"| {entry['物品']} | {chapter_label} | {entry['增减']:+g} | | |"
        for entry in resources
    ]
    if ledger_rows:
        snapshot_file(project_id, project_dir, row["name"], LEDGER_FILE, reason="ingest")
        _append_rows(project_dir, LEDGER_FILE, "资源账本", ledger_rows)

    # 3) 伏笔台账（埋设追加；回收只标疑似，需人工确认）
    foreshadow_rows = [
        f"- [待回收] {mark['内容']}｜埋设：{chapter_label}｜依据：{mark['依据']}"
        for mark in marks
        if mark.get("内容")
    ]
    if foreshadow_rows:
        snapshot_file(project_id, project_dir, row["name"], FORESHADOW_FILE, reason="ingest")
        _append_rows(project_dir, FORESHADOW_FILE, "伏笔管理", foreshadow_rows)

    # 4) 角色状态变更 → Proposal（人工应用）或直接写回
    proposals: list[dict] = []
    for change in state_changes:
        if apply_state_directly:
            apply_state_change(
                project_id,
                character=change["角色"],
                field=change["字段"],
                value=change["新值"],
                source="model",
                chapter=chapter_rel,
                evidence=change.get("依据", ""),
            )
            continue
        proposals.append(
            proposal_service.create_proposal(
                project_id=project_id,
                kind="state",
                title=f"{chapter_label} 状态变更：{change['角色']}.{change['字段']}",
                target_path=None or "状态/角色状态.md",
                content=f"- {change['字段']}：{change['新值']}",
                meta={
                    "character": change["角色"],
                    "field": change["字段"],
                    "old_value": change.get("旧值", ""),
                    "evidence": change.get("依据", ""),
                    "chapter": chapter_rel,
                    "ingest": True,
                },
            )
        )

    # 5) 视角记忆（谁知道什么）
    for name in present[:8]:
        if name in body:
            memory_entries.append(
                add_memory(
                    project_id,
                    character=name,
                    know_what=f"{chapter_label}：{summary[:60]}",
                    when_known=clues[0]["线索"] if clues else chapter_label,
                    source_event=chapter_rel,
                    source="model",
                )
            )

    # 6) 滚动摘要
    _write_summary(project_dir, chapter_rel, summary)

    result = {
        "project_id": project_id,
        "chapter_rel": chapter_rel,
        "summary": summary,
        "timeline_added": len(timeline_rows),
        "ledger_added": len(ledger_rows),
        "foreshadow_added": len(foreshadow_rows),
        "proposals_created": [item["id"] for item in proposals],
        "memory_added": len(memory_entries),
        "characters_present": present,
        "ai_used": structured is not None,
        "state_changes_detected": len(state_changes),
    }
    log_operation(project_id, "ingest", chapter_rel, result)
    return result


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
    """时间线条目（解析状态文件 → 结构化）。"""
    _row, project_dir = get_project_dir(project_id)
    text = split_frontmatter(_read(project_dir, TIMELINE_FILE))[1]
    entries: list[dict] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("-"):
            continue
        parts = [item.strip() for item in stripped.lstrip("- ").split("｜")]
        if len(parts) >= 2:
            entries.append(
                {
                    "chapter": parts[0],
                    "story_time": parts[1],
                    "event": parts[2] if len(parts) > 2 else "",
                    "evidence": parts[3].replace("依据：", "") if len(parts) > 3 else "",
                }
            )
    return entries


def list_foreshadows(project_id: int) -> list[dict]:
    """伏笔台账（解析 ``设定/伏笔管理.md``）。"""
    _row, project_dir = get_project_dir(project_id)
    text = split_frontmatter(_read(project_dir, FORESHADOW_FILE))[1]
    entries: list[dict] = []
    status_re = re.compile(r"\[(待回收|已回收|疑似回收|作废)\]")
    for index, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped.startswith("-"):
            continue
        match = status_re.search(stripped)
        status = match.group(1) if match else "待回收"
        parts = [item.strip() for item in stripped.lstrip("- ").split("｜")]
        content = status_re.sub("", parts[0]).strip()
        planted = ""
        evidence = ""
        for part in parts[1:]:
            if part.startswith("埋设："):
                planted = part.replace("埋设：", "").strip()
            elif part.startswith("依据："):
                evidence = part.replace("依据：", "").strip()
        entries.append(
            {
                "line": index,
                "content": content,
                "status": status,
                "planted_in": planted,
                "evidence": evidence,
            }
        )
    return entries


def confirm_payoff(project_id: int, line: int, *, planned_chapter: str = "") -> dict:
    """人工确认伏笔回收（spec：回收需人工确认）。"""
    row, project_dir = get_project_dir(project_id)
    path = Path(project_dir) / FORESHADOW_FILE
    if not path.is_file():
        raise NodeNotFoundError(f"伏笔文件不存在：{FORESHADOW_FILE}")

    text = read_text(path)
    meta, body = split_frontmatter(text)
    lines = body.splitlines()
    if line < 1 or line > len(lines):
        raise NodeNotFoundError(f"伏笔行号越界：{line}")

    target = lines[line - 1]
    if "[待回收]" in target:
        lines[line - 1] = target.replace("[待回收]", "[已回收]", 1)
    elif "[疑似回收]" in target:
        lines[line - 1] = target.replace("[疑似回收]", "[已回收]", 1)
    if planned_chapter:
        lines[line - 1] = f"{lines[line - 1]}｜计划回收：{planned_chapter}"

    snapshot_file(project_id, project_dir, row["name"], FORESHADOW_FILE, reason="payoff-confirm")
    atomic_write_text(path, compose_document(meta, "\n".join(lines) + "\n"))
    log_operation(project_id, "foreshadow-confirm", FORESHADOW_FILE, {"line": line})
    return {"line": line, "content": lines[line - 1]}


def list_ledger(project_id: int) -> list[dict]:
    """资源账本（解析表格行）。"""
    _row, project_dir = get_project_dir(project_id)
    text = split_frontmatter(_read(project_dir, LEDGER_FILE))[1]
    entries: list[dict] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if len(cells) < 3 or set(cells[0]) <= {"-", " "}:
            continue
        if cells[0] in ("物品", "资源", "名称"):
            continue
        entries.append(
            {
                "item": cells[0],
                "chapter": cells[1] if len(cells) > 1 else "",
                "delta": cells[2] if len(cells) > 2 else "",
                "balance": cells[3] if len(cells) > 3 else "",
                "unit": cells[4] if len(cells) > 4 else "",
            }
        )
    return entries


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
