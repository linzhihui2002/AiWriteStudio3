"""ContextService：上下文包 8 级优先级组装 + Token 预算裁剪 + 降级事件记录。

优先级（1 最高，见 spec §8.3）
------------------------------
1. 用户显式材料（本次任务指定 / 对话输入）
2. 本章细纲与章节合同
3. **前章末 500 字**（必读）
4. Canon 正史（``设定/``，按需查，不整包注入）
5. **角色状态卡**（``状态/角色状态.md``，必读）
6. 伏笔台账（``设定/伏笔管理.md``）
7. 历史摘要（滚动摘要，``.meta/summaries.json``）
8. 检索召回（本地索引 / 向量）

裁剪规则：超预算时**自低向高**裁（8 → 1），**必读集**（第 3、5 级）保留；
每次裁剪都记录「降级事件」（哪个级别被裁、裁掉多少字），落
``.workbench/logs/context/`` 并随结果返回，供上下文预览与审计。
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from .. import config
from ..engine.runtime import estimate_tokens
from .errors import NodeNotFoundError
from .fs_utils import read_text, split_frontmatter
from .project_service import get_project_dir

DEFAULT_INPUT_BUDGET = 32000   # 默认输入预算（tokens）
PREV_TAIL_CHARS = 500          # 前章末截取字数
MANDATORY_LEVELS = (3, 5)      # 必读集：前章末 + 角色状态

LEVEL_TITLES = {
    1: "用户显式材料",
    2: "本章细纲与合同",
    3: "前章末 500 字",
    4: "Canon 正史（设定）",
    5: "角色状态卡",
    6: "伏笔台账",
    7: "历史摘要",
    8: "检索召回",
}


def _read(project_dir: Path, rel_path: str) -> str:
    path = Path(project_dir) / rel_path
    if not path.is_file():
        return ""
    try:
        return read_text(path)
    except (OSError, UnicodeDecodeError):
        return ""


def _body_only(project_dir: Path, rel_path: str) -> str:
    return split_frontmatter(_read(project_dir, rel_path))[1].strip()


def _block(level: int, title: str, source: str, text: str) -> dict:
    stripped = (text or "").strip()
    return {
        "level": level,
        "title": title or LEVEL_TITLES.get(level, f"级别{level}"),
        "source": source,
        "text": stripped,
        "chars": len(stripped),
        "tokens": estimate_tokens(stripped),
        "mandatory": level in MANDATORY_LEVELS,
    }


# ─────────────────────────── 各级材料收集 ───────────────────────────


def _level1(explicit: list[dict] | str | None) -> list[dict]:
    if not explicit:
        return []
    items = [{"title": "材料", "text": explicit}] if isinstance(explicit, str) else list(explicit)
    blocks: list[dict] = []
    for item in items:
        text = str(item.get("text") or "")
        if not text.strip():
            continue
        blocks.append(_block(1, str(item.get("title") or "用户材料"),
                             str(item.get("source") or "explicit"), text))
    return blocks


def _level2(project_dir: Path, chapter_rel: str | None) -> list[dict]:
    blocks: list[dict] = []
    if chapter_rel:
        contract = _read_contract(project_dir, chapter_rel)
        if contract:
            blocks.append(_block(2, "章节合同", f"{chapter_rel}#contract", contract))
        summary = _chapter_outline_entry(project_dir, chapter_rel)
        if summary:
            blocks.append(_block(2, "本章细纲", "大纲/章纲.md", summary))
    if not blocks:
        outline = _body_only(project_dir, "大纲/大纲.md")
        if outline:
            blocks.append(_block(2, "大纲", "大纲/大纲.md", outline[:4000]))
    return blocks


def _read_contract(project_dir: Path, chapter_rel: str) -> str:
    path = Path(project_dir) / ".meta" / "contracts.json"
    if not path.is_file():
        return ""
    try:
        data = json.loads(read_text(path))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return ""
    if not isinstance(data, dict):
        return ""
    entry = data.get(chapter_rel)
    if not isinstance(entry, dict):
        return ""
    lines = []
    if entry.get("plot_points"):
        lines.append("情节点：\n" + "\n".join(f"- {item}" for item in entry["plot_points"]))
    if entry.get("word_budget"):
        lines.append(f"字数预算：{entry['word_budget']}")
    if entry.get("hook_type"):
        lines.append(f"钩子类型：{entry['hook_type']}")
    if entry.get("entities"):
        lines.append("涉及实体：" + "、".join(str(item) for item in entry["entities"]))
    if entry.get("constraints"):
        lines.append("禁止事项：\n" + "\n".join(f"- {item}" for item in entry["constraints"]))
    return "\n".join(lines)


def _chapter_outline_entry(project_dir: Path, chapter_rel: str) -> str:
    """从 ``大纲/章纲.md`` 中提取本章条目（按章号匹配）。"""
    import re

    from .chapter_service import parse_chapter_number

    number = parse_chapter_number(Path(chapter_rel).name)
    if number is None:
        return ""
    text = _body_only(project_dir, "大纲/章纲.md")
    if not text:
        return ""
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if re.search(rf"第\s*0*{number}\s*章", line):
            return "\n".join(lines[index:index + 12]).strip()
    return ""


def _level3(project_dir: Path, chapter_rel: str | None) -> list[dict]:
    if not chapter_rel:
        return []

    from .chapter_service import is_chapter_filename, parse_chapter_number

    match = re.search(r"第(\d{4,})章", Path(chapter_rel).name)
    number = int(match.group(1)) if match else None

    chapters: list[tuple[int, str]] = []
    directory = Path(project_dir) / "章节"
    if directory.is_dir():
        for path in directory.iterdir():
            if path.is_file() and is_chapter_filename(path.name):
                chapters.append((parse_chapter_number(path.name) or 0, path.name))

    previous = [(num, name) for num, name in chapters if number is not None and num < number]
    if not previous:
        return []
    prev_number, prev_name = max(previous)
    text = _body_only(project_dir, f"章节/{prev_name}")
    if not text:
        return []
    tail = text[-PREV_TAIL_CHARS:]
    return [_block(3, f"前章（第{prev_number:04d}章）末 {PREV_TAIL_CHARS} 字",
                   f"章节/{prev_name}", tail)]


def _level4(project_dir: Path, related: list[str], query: str) -> list[dict]:
    """设定（按需查）：只取与本章相关的设定文件片段，避免整包注入。"""
    directory = Path(project_dir) / "设定"
    if not directory.is_dir():
        return []

    keywords = [item for item in (related or []) if item] + ([query] if query else [])
    blocks: list[dict] = []
    for path in sorted(directory.rglob("*.md")):
        text = split_frontmatter(_safe_read(path))[1].strip()
        if not text:
            continue
        rel = path.relative_to(project_dir).as_posix()
        picked = _pick_relevant(text, keywords)
        if picked:
            blocks.append(_block(4, f"设定：{path.stem}", rel, picked))
    return blocks


def _level4_teardown(project_id: int, project_dir: Path) -> list[dict]:
    """拆书对标（Task 44）：按**题材**召回同题材拆解事实卡，注入上下文第 4 级。

    只召回有章节依据的条目；依据缺失的条目仅在拆书页供人工查看，不进上下文。
    """
    try:
        from .project_service import read_project_document
        from .teardown_service import recall_block

        meta, _body = read_project_document(Path(project_dir))
        genre = str(meta.get("题材") or "")
        text = recall_block(project_id, genre=genre, limit=5)
    except Exception:  # noqa: BLE001 - 拆书资产缺失/损坏不影响主流程
        return []
    if not text.strip():
        return []
    return [_block(4, "拆书对标（同题材）", "拆书/事实卡.md", text)]


def _safe_read(path: Path) -> str:
    try:
        return read_text(path)
    except (OSError, UnicodeDecodeError):
        return ""


def _pick_relevant(text: str, keywords: list[str], *, max_chars: int = 2200) -> str:
    """从长文本中挑出与关键词相关的段落（无关键词时取前 N 字）。"""
    if not text:
        return ""
    if not keywords:
        return text[:max_chars]
    lines = text.splitlines()
    picked: list[str] = []
    for index, line in enumerate(lines):
        if any(keyword and keyword in line for keyword in keywords):
            start = max(0, index - 1)
            picked.extend(lines[start:index + 3])
    if not picked:
        return ""
    # 去重保序
    seen: set[str] = set()
    unique = [line for line in picked if not (line in seen or seen.add(line))]
    return "\n".join(unique)[:max_chars]


def _level5(project_dir: Path, related: list[str]) -> list[dict]:
    """角色状态卡（必读）：按本章涉及角色过滤，未指定则全量（受预算裁剪）。"""
    text = _body_only(project_dir, "状态/角色状态.md")
    if not text:
        return []
    if related:
        filtered = _pick_relevant(text, related, max_chars=3000)
        if filtered:
            return [_block(5, "角色状态卡（本章涉及角色）", "状态/角色状态.md", filtered)]
    return [_block(5, "角色状态卡", "状态/角色状态.md", text)]


def _level6(project_dir: Path) -> list[dict]:
    text = _body_only(project_dir, "设定/伏笔管理.md")
    if not text:
        return []
    return [_block(6, "伏笔台账", "设定/伏笔管理.md", text[:3000])]


def _level7(project_dir: Path, chapter_rel: str | None, limit: int = 6) -> list[dict]:
    """历史摘要（滚动摘要）：取本章之前的最近若干章摘要。"""
    path = Path(project_dir) / ".meta" / "summaries.json"
    if not path.is_file():
        return []
    try:
        data = json.loads(read_text(path))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return []
    if not isinstance(data, dict):
        return []

    entries = sorted(data.items())
    if chapter_rel:
        entries = [item for item in entries if item[0] < chapter_rel]
    entries = entries[-limit:]
    if not entries:
        return []
    lines = [f"{rel}：{str(value).strip()}" for rel, value in entries if str(value).strip()]
    return [_block(7, f"最近 {len(lines)} 章摘要", ".meta/summaries.json", "\n".join(lines))]


def _level8(project_id: int, query: str, limit: int = 5) -> list[dict]:
    """检索召回：优先向量检索（若建库），否则 FTS/LIKE（来源与相似度随结果返回）。"""
    if not query:
        return []
    hits: list[dict] = []
    try:
        from .vector_service import semantic_search, vector_ready

        if vector_ready(project_id):
            hits = semantic_search(project_id, query, limit=limit)
    except Exception:  # noqa: BLE001 - 向量不可用时降级
        hits = []

    if not hits:
        from .index_service import search

        hits = search(project_id, query, limit=limit)

    if not hits:
        return []
    lines = []
    for hit in hits:
        snippet = str(hit.get("snippet") or "").replace("\n", " ")[:160]
        score = hit.get("score")
        score_text = f"（相似度 {score:.2f}）" if isinstance(score, (int, float)) else ""
        lines.append(f"- [{hit.get('rel_path')}]{score_text} {snippet}")
    return [_block(8, "检索召回", "index/vector", "\n".join(lines))]


# ─────────────────────────── 组装与裁剪 ───────────────────────────


def assemble(
    project_id: int,
    *,
    chapter_rel: str | None = None,
    explicit: list[dict] | str | None = None,
    related_characters: list[str] | None = None,
    query: str = "",
    budget_tokens: int | None = None,
    exclude_levels: list[int] | None = None,
    use_retrieval: bool = True,
    record: bool = False,
) -> dict:
    """组装上下文包（含预算裁剪与降级事件）。

    :param exclude_levels: 用户一键排除的级别（上下文预览里的「排除某项」）
    :param record: 是否把本次组装落盘为审计记录
    """
    started = time.time()
    row, project_dir = get_project_dir(project_id)
    exclude = set(exclude_levels or [])
    budget = int(budget_tokens or DEFAULT_INPUT_BUDGET)
    related = list(related_characters or [])

    blocks: list[dict] = []
    for level, provider in (
        (1, lambda: _level1(explicit)),
        (2, lambda: _level2(project_dir, chapter_rel)),
        (3, lambda: _level3(project_dir, chapter_rel)),
        (4, lambda: _level4(project_dir, related, query)
                   + _level4_teardown(project_id, project_dir)),
        (5, lambda: _level5(project_dir, related)),
        (6, lambda: _level6(project_dir)),
        (7, lambda: _level7(project_dir, chapter_rel)),
        (8, lambda: _level8(project_id, query) if use_retrieval else []),
    ):
        if level in exclude:
            continue
        try:
            blocks.extend(provider())
        except Exception:  # noqa: BLE001 - 单级失败不影响整体组装
            continue

    degradation: list[dict] = []

    def total_tokens() -> int:
        return sum(int(block["tokens"]) for block in blocks)

    used = total_tokens()
    if used > budget:
        # 自低向高裁剪；必读集保留（但超预算极端情况下允许截断到预算的一半）
        for level in sorted({block["level"] for block in blocks}, reverse=True):
            if used <= budget:
                break
            if level in MANDATORY_LEVELS:
                continue
            for block in [b for b in blocks if b["level"] == level]:
                used -= int(block["tokens"])
                blocks.remove(block)
                degradation.append(
                    {
                        "level": level,
                        "title": block["title"],
                        "source": block["source"],
                        "reason": "超预算裁剪",
                        "dropped_tokens": int(block["tokens"]),
                        "dropped_chars": int(block["chars"]),
                    }
                )

    if used > budget:  # 必读集也超预算 → 截断（记录为强制降级）
        for block in [b for b in blocks if b["mandatory"]]:
            if used <= budget:
                break
            keep_chars = max(200, int(len(block["text"]) * budget / max(used, 1)))
            dropped = len(block["text"]) - keep_chars
            block["text"] = block["text"][-keep_chars:] if block["level"] == 3 \
                else block["text"][:keep_chars]
            block["chars"] = len(block["text"])
            new_tokens = estimate_tokens(block["text"])
            used -= int(block["tokens"]) - new_tokens
            block["tokens"] = new_tokens
            block["truncated"] = True
            degradation.append(
                {
                    "level": block["level"],
                    "title": block["title"],
                    "source": block["source"],
                    "reason": "必读集超预算，已截断保留",
                    "dropped_chars": dropped,
                }
            )

    effective_budget = budget - used
    result = {
        "project_id": project_id,
        "project_name": row["name"],
        "chapter_rel": chapter_rel,
        "budget_tokens": budget,
        "total_tokens": total_tokens(),
        "remaining_tokens": effective_budget,
        "blocks": blocks,
        "degradation": degradation,
        "excluded_levels": sorted(exclude),
        "mandatory_sources": [b["source"] for b in blocks if b["mandatory"]],
        "duration_ms": int((time.time() - started) * 1000),
    }
    result["preview"] = build_preview(result)

    if record:
        record_assembly(project_id, chapter_rel, result)
    return result


def build_preview(assembly: dict) -> dict:
    """上下文预览：构成、字数、Token、优先级、必读集、裁剪情况。"""
    return {
        "items": [
            {
                "level": block["level"],
                "title": block["title"],
                "source": block["source"],
                "chars": block["chars"],
                "tokens": block["tokens"],
                "mandatory": block["mandatory"],
                "truncated": bool(block.get("truncated")),
            }
            for block in assembly["blocks"]
        ],
        "total_tokens": assembly["total_tokens"],
        "budget_tokens": assembly["budget_tokens"],
        "usage_ratio": round(
            assembly["total_tokens"] / max(assembly["budget_tokens"], 1), 4
        ),
        "degradation": assembly["degradation"],
        "excluded_levels": assembly["excluded_levels"],
    }


def to_messages(assembly: dict, *, system: str = "") -> tuple[str, list[dict]]:
    """把上下文包转成引擎消息（系统提示 + 单条用户消息，含分级材料）。"""
    parts: list[str] = []
    for block in assembly["blocks"]:
        parts.append(f"【{block['title']}｜{block['source']}】\n{block['text']}")
    user_text = "\n\n".join(parts) if parts else "（无可用上下文）"
    return system, [{"role": "user", "content": user_text}]


def record_assembly(project_id: int, chapter_rel: str | None, assembly: dict) -> str:
    """把组装结果落盘为审计记录，返回文件路径。"""
    directory = config.logs_dir() / "context"
    directory.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    name = Path(chapter_rel).name.removesuffix(".md") if chapter_rel else "adhoc"
    path = directory / f"{stamp}-{project_id}-{name}.json"
    payload = {
        "project_id": project_id,
        "chapter_rel": chapter_rel,
        "total_tokens": assembly["total_tokens"],
        "budget_tokens": assembly["budget_tokens"],
        "degradation": assembly["degradation"],
        "items": build_preview(assembly)["items"],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)


def list_assemblies(limit: int = 30) -> list[dict]:
    """最近的上下文组装记录（审计）。"""
    directory = config.logs_dir() / "context"
    if not directory.is_dir():
        return []
    entries: list[dict] = []
    for path in sorted(directory.glob("*.json"), reverse=True)[:limit]:
        try:
            data = json.loads(read_text(path))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        entries.append({**data, "file": path.name})
    return entries


__all__ = [
    "DEFAULT_INPUT_BUDGET",
    "LEVEL_TITLES",
    "MANDATORY_LEVELS",
    "assemble",
    "build_preview",
    "list_assemblies",
    "record_assembly",
    "to_messages",
]