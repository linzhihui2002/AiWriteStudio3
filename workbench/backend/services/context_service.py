"""ContextService：上下文包 8 级事实材料 + 独立写法参照 + Token 预算裁剪。

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
9. 作者认可写法样稿（仅写作请求启用，不作为 Canon 或剧情事实）

裁剪规则：超预算时**自低向高**裁（9 → 1），**必读集**（第 3、5 级）保留；
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
from . import knowledge_lifecycle

DEFAULT_INPUT_BUDGET = 32000   # 默认输入预算（tokens）
RETRIEVAL_TOKEN_BUDGET = 2400
RETRIEVAL_HIT_LIMIT = 6
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
    9: "作者认可写法参照（仅文风）",
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
        block = _block(1, str(item.get("title") or "用户材料"),
                       str(item.get("source") or "explicit"), text)
        block["protected"] = bool(item.get("protected", False))
        blocks.append(block)
    return blocks


def _level2(project_dir: Path, chapter_rel: str | None) -> list[dict]:
    blocks: list[dict] = []
    if chapter_rel:
        contract = _read_contract(project_dir, chapter_rel)
        if contract:
            blocks.append(_block(2, "章节合同", f"{chapter_rel}#contract", contract))
            blocks[-1]["protected"] = True
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
    if entry.get("must_connect"):
        lines.append("必须承上：\n" + "\n".join(f"- {item}" for item in entry["must_connect"]))
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


def _level3(project_id: int, project_dir: Path, chapter_rel: str | None) -> list[dict]:
    if not chapter_rel:
        return []

    from .chapter_service import is_chapter_filename, parse_chapter_number, fetch_chapter_meta

    match = re.search(r"第(\d{4,})章", Path(chapter_rel).name)
    number = int(match.group(1)) if match else None

    chapters: list[tuple[int, str]] = []
    directory = Path(project_dir) / "章节"
    if directory.is_dir():
        for path in directory.iterdir():
            if path.is_file() and is_chapter_filename(path.name):
                meta = fetch_chapter_meta(project_id, f"章节/{path.name}") or {}
                if meta.get("status") in {"完成", "发表", "completed", "published"}:
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


def _level4(project_dir: Path, related: list[str], query: str, *, profile: str = "history",
            chapter_before: int | None = None, project_id: int | None = None) -> list[dict]:
    """设定（按需查）：只取与本章相关的设定文件片段，避免整包注入。"""
    directory = Path(project_dir) / "设定"
    if not directory.is_dir():
        return []

    keywords = [item for item in (related or []) if item] + ([query] if query else [])
    blocks: list[dict] = []
    for path in sorted(directory.rglob("*.md")):
        text = _material_text(project_id, split_frontmatter(_safe_read(path))[1],
            profile=profile, chapter_before=chapter_before, preserve_offsets=False).strip()
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


def _level5(project_dir: Path, related: list[str], *, profile: str = "history",
            chapter_before: int | None = None, project_id: int | None = None) -> list[dict]:
    """角色状态卡（必读）：按本章涉及角色过滤，未指定则全量（受预算裁剪）。"""
    text = _material_text(project_id, _body_only(project_dir, "状态/角色状态.md"),
        profile=profile, chapter_before=chapter_before, preserve_offsets=False,
        at_chapter=(chapter_before - 1) if chapter_before and profile != "planning" else None).strip()
    if not text:
        return []
    if related:
        filtered = _pick_relevant(text, related, max_chars=3000)
        if filtered:
            return [_block(5, "角色状态卡（本章涉及角色）", "状态/角色状态.md", filtered)]
    return [_block(5, "角色状态卡", "状态/角色状态.md", text)]


def _level6(project_dir: Path, *, profile: str = "history",
            chapter_before: int | None = None, project_id: int | None = None) -> list[dict]:
    text = _material_text(project_id, _body_only(project_dir, "设定/伏笔管理.md"),
        profile=profile, chapter_before=chapter_before, preserve_offsets=False).strip()
    if not text:
        return []
    return [_block(6, "伏笔台账", "设定/伏笔管理.md", text[:3000])]


def _material_text(project_id: int | None, text: str, **kwargs) -> str:
    if project_id is not None:
        from . import ingestion_records
        text = ingestion_records.project_text(project_id, text, preserve_offsets=False,
                                             chapter_before=kwargs.get("chapter_before"))
    return knowledge_lifecycle.project_text(text, **kwargs)


def _level7(project_id: int, project_dir: Path, chapter_rel: str | None, limit: int = 6) -> list[dict]:
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

    from .chapter_service import parse_chapter_number, fetch_chapter_meta
    from .ingestion_commit import summary_is_current
    target_number = parse_chapter_number(Path(chapter_rel).name) if chapter_rel else None
    entries = []
    for rel, summary in data.items():
        number = parse_chapter_number(Path(rel).name)
        meta = fetch_chapter_meta(project_id, rel) or {}
        if (number is not None and meta.get("status") in {"完成", "发表", "completed", "published"}
                and (target_number is None or number < target_number)
                and summary_is_current(project_id, rel, summary)):
            entries.append((rel, summary))
    entries.sort(key=lambda item: parse_chapter_number(Path(item[0]).name) or 0)
    entries = entries[-limit:]
    if not entries:
        return []
    lines = [f"{rel}：{str(value).strip()}" for rel, value in entries if str(value).strip()]
    return [_block(7, f"最近 {len(lines)} 章摘要", ".meta/summaries.json", "\n".join(lines))]


def retrieval_request(project_dir: Path, chapter_rel: str | None, instruction: str,
                      related: list[str], document=None) -> tuple[list[str], list[str]]:
    """从作者要求、本章计划及已知实体构造最多三个查询，不额外调用模型。"""
    entities = list(dict.fromkeys(item for item in related if item))
    contract_text = _read(project_dir, ".meta/contracts.json")
    try:
        contract = json.loads(contract_text).get(chapter_rel, {}) if contract_text else {}
        entities.extend(str(item) for item in contract.get("entities", []) if item)
    except (ValueError, TypeError, AttributeError):
        contract = {}
    outline = _chapter_outline_entry(project_dir, chapter_rel) if chapter_rel else ""
    if chapter_rel and not outline:
        outline = _body_only(project_dir, "大纲/大纲.md")[:600]
    points = "；".join(str(item) for item in contract.get("plot_points", []) if item)
    document_text = str(document.get("text") or "") if isinstance(document, dict) else ""
    evidence = "\n".join((instruction, outline, points, document_text))
    setting_dir = project_dir / "设定"
    if setting_dir.is_dir():
        for path in sorted(setting_dir.rglob("*.md")):
            # 卡片标题/名字是确定性查询种子，不从正文猜测新的故事事实。
            text = _safe_read(path)
            names = [path.stem, *re.findall(r"(?m)^#{1,4}\s+([^\n#：:]{2,30})", text)]
            names += re.findall(r"(?m)^\s*(?:[-*]\s*)?(?:姓名|名称|角色名)[：:]\s*([^\n]{2,30})", text)
            for name in names:
                name = name.strip()
                if name and name in evidence:
                    entities.append(name)
    entities = list(dict.fromkeys(entities))[:12]
    generic = {"章节情节点", "章节情节点与状态", "本章情节点", "章节正文", "审稿", "续写"}
    seeds = [instruction.strip() if instruction.strip() not in generic else "",
             (points or outline).strip(), " ".join(entities)]
    if document_text and not any(seeds):
        seeds[0] = document_text[:600]
    queries = list(dict.fromkeys(seed[:600] for seed in seeds if seed.strip()))[:3]
    return queries, entities


def _level8(project_id: int, queries: list[str], *, profile: str,
            chapter_rel: str | None, document=None, direct_blocks=()) -> tuple[list[dict], dict]:
    """本书记忆：混合检索，保留逐条依据与降级信息，单独限制召回预算。"""
    audit = {"profile": profile, "queries": queries, "hits": [], "degradation": []}
    if not queries or profile == "off":
        return [], audit
    blocks, seen, per_document = [], set(), {}
    used = 0
    from . import knowledge_service
    for query in queries:
        try:
            result = knowledge_service.search(project_id, query, mode="hybrid", profile=profile,
                                              chapter_rel=chapter_rel, document=document,
                                              limit=RETRIEVAL_HIT_LIMIT)
        except Exception as exc:  # 单次召回失败不能阻断写作，但必须可见。
            audit["degradation"].append(str(exc))
            continue
        reason = result.get("degradation") or result.get("degraded_reason")
        if reason:
            audit["degradation"].extend(reason if isinstance(reason, list) else [reason])
        audit["knowledge_key"] = result.get("knowledge_key")
        for hit in result.get("hits", []):
            rel = str(hit.get("rel_path") or "")
            key = (rel, hit.get("line_start"), hit.get("line_end"), hit.get("hash"))
            if key in seen or per_document.get(rel, 0) >= 2:
                continue
            seen.add(key)
            text = str(hit.get("text") or hit.get("snippet") or "").strip()
            if not text:
                continue
            normalized = re.sub(r"\s+", " ", text)
            if any(normalized in re.sub(r"\s+", " ", b["text"])
                   for b in direct_blocks if str(b["source"]).split("#", 1)[0] == rel):
                continue
            source = f"{rel}:{hit.get('line_start', 1)}-{hit.get('line_end', 1)}"
            block = _block(8, "本书记忆依据", source, text)
            truncated = False
            if used + block["tokens"] > RETRIEVAL_TOKEN_BUDGET:
                audit["degradation"].append("召回依据超出 2400 token 预算，已逐条裁剪")
                remaining = RETRIEVAL_TOKEN_BUDGET - used
                marker = "…"
                if remaining <= estimate_tokens(marker):
                    continue
                # Keep the beginning of the *same cited source span*. Binary
                # search is needed because Chinese and non-Chinese characters
                # have different token estimates; simple character slicing
                # can still exceed the budget.
                low, high = 0, len(text)
                while low < high:
                    middle = (low + high + 1) // 2
                    excerpt = text[:middle].rstrip() + marker
                    if estimate_tokens(excerpt) <= remaining:
                        low = middle
                    else:
                        high = middle - 1
                if low <= 0:
                    continue
                block = _block(8, "本书记忆依据", source,
                               text[:low].rstrip() + marker)
                truncated = True
            block["retrieval"] = {k: hit.get(k) for k in (
                "rel_path", "line_start", "line_end", "hash", "chapter_no", "source",
                "kind", "node_ids", "score")}
            block["retrieval"]["query"] = query
            block["retrieval"]["excerpt_truncated"] = truncated
            used += block["tokens"]
            per_document[rel] = per_document.get(rel, 0) + 1
            blocks.append(block)
            audit["hits"].append(block["retrieval"])
            if len(blocks) >= RETRIEVAL_HIT_LIMIT:
                return blocks, audit
    audit["degradation"] = list(dict.fromkeys(str(item) for item in audit["degradation"]))
    return blocks, audit


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
    retrieval_profile: str = "history",
    include_style: bool = False,
    style_query: str | None = None,
    include_prose_history: bool = False,
    document=None,
    record: bool = False,
) -> dict:
    """组装上下文包（含预算裁剪与降级事件）。

    :param exclude_levels: 用户一键排除的级别（上下文预览里的「排除某项」）
    :param include_style: 注入独立作者认可样稿，仅用于写法参照，不作为事实材料
    :param include_prose_history: 注入近期已完成正文的复读参照，最低优先且不等于认可文风
    :param record: 是否把本次组装落盘为审计记录
    """
    started = time.time()
    row, project_dir = get_project_dir(project_id)
    exclude = set(exclude_levels or [])
    budget = int(budget_tokens or DEFAULT_INPUT_BUDGET)
    related = list(related_characters or [])
    if use_retrieval and retrieval_profile != "off":
        queries, related = retrieval_request(project_dir, chapter_rel, query, related, document)
    else:
        queries = []
    retrieval = {"profile": retrieval_profile, "queries": queries, "hits": [], "degradation": []}

    blocks: list[dict] = []
    chapter_match = re.search(r"第(\d+)章", chapter_rel or "")
    history_before = int(chapter_match[1]) if chapter_match and retrieval_profile != "planning" else None
    for level, provider in (
        (1, lambda: _level1(explicit)),
        (2, lambda: _level2(project_dir, chapter_rel)),
        (3, lambda: _level3(project_id, project_dir, chapter_rel)),
        (4, lambda: _level4(project_dir, related, "" if related or len(query) > 16 else query,
                           profile=retrieval_profile, chapter_before=history_before, project_id=project_id)
                   + (_level4_teardown(project_id, project_dir) if retrieval_profile == "teardown" else [])),
        (5, lambda: _level5(project_dir, related, profile=retrieval_profile, chapter_before=history_before, project_id=project_id)),
        (6, lambda: _level6(project_dir, profile=retrieval_profile, chapter_before=history_before, project_id=project_id)),
        (7, lambda: _level7(project_id, project_dir, chapter_rel)),
    ):
        if level in exclude:
            continue
        try:
            blocks.extend(provider())
        except Exception:  # noqa: BLE001 - 单级失败不影响整体组装
            continue

    if use_retrieval and 8 not in exclude and retrieval_profile != "off":
        try:
            recalled, retrieval = _level8(project_id, queries, profile=retrieval_profile,
                                         chapter_rel=chapter_rel, document=document, direct_blocks=blocks)
            blocks.extend(recalled)
        except Exception as exc:
            retrieval["degradation"].append(str(exc))

    style_reference = {"enabled": include_style, "samples": [], "excluded": [],
                       "reference_hash": "", "degradation": []}
    if include_style and 9 not in exclude:
        try:
            from . import style_service

            reference = style_service.generation_reference(
                project_id, chapter_rel=chapter_rel,
                query=query if style_query is None else style_query,
                related_characters=related, max_tokens=min(1600, max(0, budget)),
            )
            style_reference.update({key: value for key, value in reference.items()
                                    if key != "text"})
            if reference["text"]:
                block = _block(9, "作者认可写法参照（仅文风，不作正史）",
                               "style_fingerprints", reference["text"])
                block["type"] = "style"
                block["style_reference"] = {"reference_hash": reference["reference_hash"],
                                            "samples": reference["samples"]}
                blocks.append(block)
        except Exception as exc:  # 无认可样稿或读取降级不能阻断写作。
            style_reference["degradation"].append(str(exc))

    prose_history_reference = {"enabled": include_prose_history, "samples": [], "excluded": [],
                               "reference_hash": "", "tokens": 0, "truncated": False,
                               "status": "disabled", "degradation": []}
    if include_prose_history and 10 not in exclude:
        try:
            from . import prose_history_service

            reference = prose_history_service.generation_reference(
                project_id, chapter_rel, max_tokens=min(1000, max(0, budget)))
            prose_history_reference.update({key: value for key, value in reference.items() if key != "text"})
            if reference["text"]:
                block = _block(10, "近期已发生正文参照（检查重复交代）", "recent_completed_chapters", reference["text"])
                block["type"] = "prose_history"
                block["prose_history_reference"] = {"reference_hash": reference["reference_hash"],
                                                    "samples": reference["samples"]}
                blocks.append(block)
        except Exception as exc:
            prose_history_reference.update(status="not_checked")
            prose_history_reference["degradation"].append(f"近期正文参照不可用：{type(exc).__name__}")
    elif include_prose_history:
        prose_history_reference["status"] = "excluded"

    degradation: list[dict] = []

    def total_tokens() -> int:
        return sum(int(block["tokens"]) for block in blocks)

    used = total_tokens()
    if used > budget:
        # 自低向高裁剪；必读集保留（但超预算极端情况下允许截断到预算的一半）
        for level in sorted({block["level"] for block in blocks}, reverse=True):
            if used <= budget:
                break
            for block in [b for b in blocks if b["level"] == level and not b.get("protected") and not b["mandatory"]]:
                if used <= budget:
                    break
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

    if used > budget:  # 必读集也超预算：先压缩背景，最后才裁剪作者指定材料。
        for block in sorted(blocks, key=lambda b: bool(b.get("protected"))):
            if used <= budget:
                break
            original = block["text"]
            target_tokens = max(0, int(block["tokens"]) - (used - budget))
            low, high = 0, len(original)
            while low < high:
                middle = (low + high + 1) // 2
                excerpt = original[-middle:] if block["level"] == 3 else original[:middle]
                if estimate_tokens(excerpt) <= target_tokens:
                    low = middle
                else:
                    high = middle - 1
            block["text"] = (original[-low:] if block["level"] == 3 else original[:low]) if low else ""
            dropped = len(original) - low
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
    kept = {(b["retrieval"]["rel_path"], b["retrieval"].get("line_start"), b["retrieval"].get("hash"))
            for b in blocks if b.get("retrieval")}
    for hit in retrieval.get("hits", []):
        hit["injected"] = (hit["rel_path"], hit.get("line_start"), hit.get("hash")) in kept
    retained_style = next((block for block in blocks if block.get("type") == "style"), None)
    style_reference["selected_samples"] = style_reference.get("samples", [])
    style_reference["selected_reference_hash"] = style_reference.get("reference_hash", "")
    style_reference["selected_tokens"] = style_reference.get("tokens", 0)
    style_reference["injected"] = retained_style is not None
    style_reference["samples"] = (retained_style["style_reference"]["samples"]
                                  if retained_style else [])
    style_reference["reference_hash"] = (retained_style["style_reference"]["reference_hash"]
                                        if retained_style else "")
    style_reference["tokens"] = retained_style["tokens"] if retained_style else 0
    if style_reference["selected_samples"] and retained_style is None:
        style_reference["degradation"].append("本次上下文预算优先保留事实材料，写法样稿未注入")
    retained_history = next((block for block in blocks if block.get("type") == "prose_history"), None)
    prose_history_reference["selected_samples"] = prose_history_reference.get("samples", [])
    prose_history_reference["selected_reference_hash"] = prose_history_reference.get("reference_hash", "")
    prose_history_reference["selected_tokens"] = prose_history_reference.get("tokens", 0)
    prose_history_reference["injected"] = retained_history is not None
    prose_history_reference["samples"] = retained_history["prose_history_reference"]["samples"] if retained_history else []
    prose_history_reference["reference_hash"] = retained_history["prose_history_reference"]["reference_hash"] if retained_history else ""
    prose_history_reference["tokens"] = retained_history["tokens"] if retained_history else 0
    if prose_history_reference["selected_samples"] and retained_history is None:
        prose_history_reference["degradation"].append("本次上下文优先保留事实与认可写法，近期正文参照未注入")
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
        "retrieval": retrieval,
        "style_reference": style_reference,
        "prose_history_reference": prose_history_reference,
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
                **({"type": "style", "style_reference": block["style_reference"]}
                   if block.get("type") == "style" else {}),
                **({"type": "prose_history", "prose_history_reference": block["prose_history_reference"]}
                   if block.get("type") == "prose_history" else {}),
                **({"retrieval": block["retrieval"]} if block.get("retrieval") else {}),
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
        "retrieval": assembly.get("retrieval", {}),
        "style_reference": assembly.get("style_reference", {}),
        "prose_history_reference": assembly.get("prose_history_reference", {}),
    }


def to_messages(assembly: dict, *, system: str = "") -> tuple[str, list[dict]]:
    """把上下文包转成引擎消息（系统提示 + 单条用户消息，含分级材料）。"""
    parts: list[str] = []
    for block in assembly["blocks"]:
        note = ("（仅学习写法；示例剧情不是当前事实）" if block.get("type") == "style" else
                "（此前已经发生的正文；不把同一动作再演一遍，必要回声可保留；不代表作者认可文风）" if block.get("type") == "prose_history" else
                "（检索候选依据：回读原文核实；已确认设定与当前状态优先）" if block.get("retrieval") else "")
        parts.append(f"【{block['title']}｜{block['source']}】{note}\n{block['text']}")
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
        "retrieval": assembly.get("retrieval", {}),
        "style_reference": assembly.get("style_reference", {}),
        "prose_history_reference": assembly.get("prose_history_reference", {}),
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
