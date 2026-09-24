"""设定卡片（Task 27a）：卡片视图 / 高亮色 / 三层解析 / 双向同步 / 智能生成。

三层解析策略（SubTask 27a.1）
1. **确定性骨架**：零 LLM，机械提取标题层级与表格行 → 保证条目数与名称正确；
2. **AI 字段补全**：缺失字段走 direct-api 结构化解析，字段带来源标记；
3. **缓存指纹**：以文件内容 hash 为键，内容未变复用缓存。

事实源仍是 Markdown：卡片是视图层；编辑/新增/删除一律以 **diff 写回** Markdown
（或经 Proposal 收件箱），不破坏作者的自由书写内容。

高亮色（SubTask 27a.4）：存 ``.meta/asset_cards.json`` 的 ``highlights`` 段，
**不写入** ``设定/*.md``；色板走 token 名（前端按主题映射实际色值）。
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path

from . import generation_service, prompt_registry_service, proposal_service
from .errors import InvalidOperationError, NodeNotFoundError
from .fs_utils import atomic_write_text, read_text, split_frontmatter
from .operation_log import log as log_operation
from .project_service import get_project_dir
from .snapshot_service import snapshot_file

META_FILE = ".meta/asset_cards.json"
CACHE_VERSION = 1

# 分类 → 设定文件（可扩展自定义类型：文件放 ``设定/{名称}设定.md``）
BASE_CATEGORIES: dict[str, str] = {
    "人物": "设定/人物设定.md",
    "世界": "设定/世界设定.md",
    "势力": "设定/势力设定.md",
    "物品": "设定/物品设定.md",
    "技能": "设定/技能设定.md",
    "场景": "设定/场景设定.md",
    "伏笔": "设定/伏笔管理.md",
}

# 高亮色板（token 名；前端按主题映射为可辨识色值）
HIGHLIGHT_PALETTE: tuple[str, ...] = (
    "highlight-1", "highlight-2", "highlight-3", "highlight-4", "highlight-5",
)

_HEADING_RE = re.compile(r"^(#{2,4})\s*([^\n#]{1,40})\s*$", re.MULTILINE)
_FIELD_RE = re.compile(r"^\s*[-*]?\s*(?:\*\*)?([^\s:：*]{1,16})(?:\*\*)?\s*[:：]\s*(.+?)\s*$")


def _meta_path(project_dir: Path) -> Path:
    return Path(project_dir) / META_FILE


def _read_meta(project_dir: Path) -> dict:
    path = _meta_path(project_dir)
    if not path.is_file():
        return {"version": CACHE_VERSION, "files": {}, "highlights": {}}
    try:
        data = json.loads(read_text(path))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {"version": CACHE_VERSION, "files": {}, "highlights": {}}
    if not isinstance(data, dict):
        return {"version": CACHE_VERSION, "files": {}, "highlights": {}}
    data.setdefault("files", {})
    data.setdefault("highlights", {})
    return data


def _write_meta(project_dir: Path, data: dict) -> None:
    path = _meta_path(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    data["version"] = CACHE_VERSION
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


# ─────────────────────────── 分类与文件 ───────────────────────────


def categories(project_id: int) -> list[dict]:
    """分类 Tab：内置七类 + 用户自定义类型（``设定/X设定.md``）。"""
    _row, project_dir = get_project_dir(project_id)
    items = [
        {"key": key, "label": key, "path": path, "builtin": True}
        for key, path in BASE_CATEGORIES.items()
    ]
    setting_dir = Path(project_dir) / "设定"
    if setting_dir.is_dir():
        known_paths = set(BASE_CATEGORIES.values())
        for path in sorted(setting_dir.glob("*设定.md")):
            rel = path.relative_to(project_dir).as_posix()
            if rel in known_paths:
                continue
            label = path.stem.replace("设定", "") or path.stem
            items.append({"key": label, "label": label, "path": rel, "builtin": False})
    return items


def _category_path(project_dir: Path, category: str) -> str:
    if category in BASE_CATEGORIES:
        return BASE_CATEGORIES[category]
    candidate = f"设定/{category}设定.md"
    if (Path(project_dir) / candidate).is_file():
        return candidate
    raise InvalidOperationError(f"未知设定分类：{category}")


# ─────────────────────────── 解析（确定性骨架） ───────────────────────────


def parse_skeleton(text: str) -> list[dict]:
    """确定性骨架：提取 ``## 名称`` 段落与表格行（零 LLM，保证条目数正确）。"""
    body = split_frontmatter(text or "")[1]
    cards: list[dict] = []
    current: dict | None = None

    for line in body.splitlines():
        match = _HEADING_RE.match(line)
        if match:
            if current and current["name"]:
                cards.append(current)
            current = {"name": match.group(2).strip(), "fields": {}, "lines": []}
            continue
        if current is None:
            continue
        current["lines"].append(line)
        field_match = _FIELD_RE.match(line)
        if field_match:
            key = field_match.group(1).strip()
            value = field_match.group(2).strip()
            if key and value:
                current["fields"][key] = value

    if current and current["name"]:
        cards.append(current)

    # 表格行作为卡片（无标题节时的主要形态）
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if len(cells) < 2 or set(cells[0]) <= {"-", " "} or not cells[0]:
            continue
        if cells[0] in ("名称", "条目", "分类"):
            continue
        if any(card["name"] == cells[0] for card in cards):
            continue
        cards.append(
            {
                "name": cells[0],
                "fields": {"说明": " ｜ ".join(cells[1:])},
                "lines": [stripped],
            }
        )

    for index, card in enumerate(cards):
        card["id"] = f"{index}:{card['name']}"
        card["summary"] = _summarize(card)
        card["source"] = "author" if card["fields"] else "unknown"
        card["field_list"] = list(card["fields"].keys())
        card.pop("lines", None)
    return cards


def _summarize(card: dict) -> str:
    if card.get("fields"):
        first_key = next(iter(card["fields"]))
        text = f"{first_key}：{card['fields'][first_key]}"
    else:
        text = card.get("name", "")
    return re.sub(r"\s+", " ", text)[:120]


# ─────────────────────────── 缓存刷新（三层策略） ───────────────────────────


def refresh(
    project_id: int,
    *,
    category: str | None = None,
    force: bool = False,
    use_ai: bool = False,
) -> dict:
    """刷新卡片缓存：内容未变则复用（缓存指纹），变了才重解析（必要时 AI 补全）。"""
    row, project_dir = get_project_dir(project_id)
    meta = _read_meta(project_dir)
    files_cache: dict = meta["files"]

    targets: list[tuple[str, str]] = []
    if category:
        targets.append((category, _category_path(project_dir, category)))
    else:
        for key in BASE_CATEGORIES:
            targets.append((key, BASE_CATEGORIES[key]))

    parsed_files = 0
    cached_files = 0
    ai_used = False

    for key, rel in targets:
        path = Path(project_dir) / rel
        if not path.is_file():
            files_cache.pop(rel, None)
            continue
        text = read_text(path)
        digest = _hash(text)
        entry = files_cache.get(rel)
        if entry and entry.get("hash") == digest and not force:
            cached_files += 1
            continue

        cards = parse_skeleton(text)
        for card in cards:
            card["category"] = key
            card["file"] = rel

        if use_ai and cards:
            completed = _ai_complete(project_id, key, cards)
            if completed:
                ai_used = True

        files_cache[rel] = {
            "hash": digest,
            "parsed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "category": key,
            "cards": cards,
        }
        parsed_files += 1

    _write_meta(project_dir, meta)
    return {
        "project_id": project_id,
        "parsed_files": parsed_files,
        "cached_files": cached_files,
        "ai_used": ai_used,
        "cards": sum(len(entry.get("cards") or []) for entry in files_cache.values()),
        "project_name": row["name"],
    }


def _ai_complete(project_id: int, category: str, cards: list[dict]) -> bool:
    """AI 字段补全（结构化）：仅补缺失字段，失败静默降级。"""
    prompt = prompt_registry_service.get_prompt("novel.card.complete")
    targets = [card for card in cards if len(card["fields"]) < 3][:5]
    if not targets:
        return False

    payload = "\n\n".join(
        f"名称：{card['name']}\n现有内容：{json.dumps(card['fields'], ensure_ascii=False)}"
        for card in targets
    )
    result = generation_service.run_task(
        project_id=project_id,
        task_type="卡片补全",
        system=prompt["body"],
        messages=[{"role": "user", "content": f"分类：{category}\n{payload}"}],
        prompt_id=prompt["prompt_id"],
        prompt_version=prompt["version"],
        temperature=0.2,
    )
    if not result["ok"]:
        return False

    from .review_service import _parse_json

    parsed = _parse_json(result["text"])
    if not parsed:
        return False

    fields = parsed.get("字段") if isinstance(parsed.get("字段"), dict) else {}
    name = str(parsed.get("名称") or "")
    for card in targets:
        if name and card["name"] != name:
            continue
        for field_key, field_value in fields.items():
            if field_key not in card["fields"] and str(field_value).strip():
                card["fields"][field_key] = str(field_value)
        card["source"] = "model"
        card["summary"] = _summarize(card)
        card["field_list"] = list(card["fields"].keys())
    return True


# ─────────────────────────── 查询 ───────────────────────────


def list_cards(
    project_id: int,
    *,
    category: str | None = None,
    query: str = "",
    highlight_only: bool = False,
    use_ai: bool = False,
) -> dict:
    """卡片列表（含高亮色、来源、条数统计）。"""
    row, project_dir = get_project_dir(project_id)
    refresh(project_id, category=category, use_ai=use_ai)
    meta = _read_meta(project_dir)
    highlights: dict = meta.get("highlights") or {}

    cards: list[dict] = []
    counts: dict[str, int] = {}
    for rel, entry in meta["files"].items():
        entry_category = entry.get("category") or "其他"
        if category and entry_category != category:
            continue
        for card in entry.get("cards") or []:
            ref = f"{rel}#{card['name']}"
            item = {
                **card,
                "ref": ref,
                "category": entry_category,
                "file": rel,
                "highlight": highlights.get(ref, ""),
            }
            counts[entry_category] = counts.get(entry_category, 0) + 1
            cards.append(item)

    if query:
        needle = query.strip()
        cards = [
            card for card in cards
            if needle in card["name"]
            or needle in card.get("summary", "")
            or needle in json.dumps(card.get("fields", {}), ensure_ascii=False)
            or needle in (card.get("field_list") or [])
        ]
    if highlight_only:
        cards = [card for card in cards if card.get("highlight")]

    return {
        "project_id": project_id,
        "project_name": row["name"],
        "cards": cards,
        "count": len(cards),
        "counts_by_category": counts,
        "palette": list(HIGHLIGHT_PALETTE),
        "categories": categories(project_id),
    }


def get_card(project_id: int, ref: str) -> dict:
    file_rel, _, name = ref.partition("#")
    project = list_cards(project_id)
    for card in project["cards"]:
        if card["file"] == file_rel and card["name"] == name:
            return card
    raise NodeNotFoundError(f"卡片不存在：{ref}")


# ─────────────────────────── 高亮 ───────────────────────────


def set_highlight(project_id: int, ref: str, color: str, *, note: str = "") -> dict:
    """指派/更改高亮色（token 名；持久化在 ``.meta/asset_cards.json``，不写 md）。"""
    if color and color not in HIGHLIGHT_PALETTE:
        raise InvalidOperationError(f"未知高亮色：{color}（可选：{'/'.join(HIGHLIGHT_PALETTE)}）")
    _row, project_dir = get_project_dir(project_id)
    meta = _read_meta(project_dir)
    highlights = meta.setdefault("highlights", {})
    if color:
        highlights[ref] = color if not note else {"color": color, "note": note}
    else:
        highlights.pop(ref, None)
    _write_meta(project_dir, meta)
    log_operation(project_id, "card-highlight", ref, {"color": color})
    return {"ref": ref, "highlight": color}


def clear_highlight(project_id: int, ref: str) -> dict:
    return set_highlight(project_id, ref, "")


def list_highlights(project_id: int) -> list[dict]:
    _row, project_dir = get_project_dir(project_id)
    highlights = _read_meta(project_dir).get("highlights") or {}
    return [
        {"ref": ref, "highlight": value if isinstance(value, str) else value.get("color"),
         "note": value.get("note", "") if isinstance(value, dict) else ""}
        for ref, value in highlights.items()
    ]


# ─────────────────────────── 写回 Markdown（双向同步） ───────────────────────────


def update_card(project_id: int, ref: str, fields: dict, *, name: str | None = None) -> dict:
    """卡片编辑 → 以 diff 写回 Markdown 对应段落（其余自由书写内容保持不动）。"""
    file_rel, _, card_name = ref.partition("#")
    row, project_dir = get_project_dir(project_id)
    path = Path(project_dir) / file_rel
    if not path.is_file():
        raise NodeNotFoundError(f"设定文件不存在：{file_rel}")

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
        if section_start is None and title == card_name:
            section_start = index
            continue
        if section_start is not None:
            section_end = index
            break

    if section_start is None:
        raise NodeNotFoundError(f"Markdown 中未找到卡片「{card_name}」（{file_rel}）")

    resolved: dict = {}
    for key, value in (fields or {}).items():
        entry_line = f"- {key}：{value}"
        replaced = False
        for index in range(section_start + 1, section_end):
            match = _FIELD_RE.match(lines[index])
            if match and match.group(1).strip().strip("*") == key:
                lines[index] = entry_line
                replaced = True
                break
        if not replaced:
            insert_at = section_end
            while insert_at > section_start + 1 and not lines[insert_at - 1].strip():
                insert_at -= 1
            lines.insert(insert_at, entry_line)
            section_end += 1
        resolved[key] = value

    new_body = "\n".join(lines).rstrip() + "\n"
    snapshot_file(project_id, project_dir, row["name"], file_rel, reason="card-update")
    atomic_write_text(path, f"{_frontmatter_block(meta)}{new_body}" if meta else new_body)
    refresh(project_id, force=True)
    log_operation(project_id, "card-update", file_rel, {"card": card_name,
                                                        "fields": list(resolved)})
    return {"ref": ref, "updated": resolved, "file": file_rel}


def _frontmatter_block(meta: dict) -> str:
    from .fs_utils import compose_document

    return compose_document(meta, "")


def add_card(project_id: int, category: str, name: str, fields: dict | None = None,
             *, via_proposal: bool = True) -> dict:
    """新增卡片：默认经收件箱（展示 diff 后应用），可选直接写入。"""
    _row, project_dir = get_project_dir(project_id)
    rel = _category_path(project_dir, category)
    if not name.strip():
        raise InvalidOperationError("卡片名称不能为空")

    block_lines = [f"## {name.strip()}"]
    for key, value in (fields or {}).items():
        block_lines.append(f"- {key}：{value}")
    block = "\n".join(block_lines) + "\n"

    if via_proposal:
        path = Path(project_dir) / rel
        existing = read_text(path) if path.is_file() else ""
        proposal = proposal_service.create_proposal(
            project_id=project_id,
            kind="card",
            title=f"新增卡片：{name}（{category}）",
            target_path=rel,
            content=(split_frontmatter(existing)[1].rstrip() + "\n\n" + block).strip() + "\n",
            meta={"frontmatter": split_frontmatter(existing)[0] or {"标题": Path(rel).stem},
                  "category": category, "card_name": name},
        )
        return {"proposal_id": proposal["id"], "via": "proposal"}

    path = Path(project_dir) / rel
    text = read_text(path) if path.is_file() else ""
    meta_fm, body = split_frontmatter(text)
    new_text = (body.rstrip() + "\n\n" + block).strip() + "\n"
    atomic_write_text(
        path, f"{_frontmatter_block(meta_fm)}{new_text}" if meta_fm else new_text
    )
    refresh(project_id, force=True)
    return {"written": rel, "card": name, "via": "direct"}


def delete_card(project_id: int, ref: str, *, via_proposal: bool = True) -> dict:
    """删除卡片：默认经收件箱确认（避免误删自由书写内容）。"""
    file_rel, _, card_name = ref.partition("#")
    row, project_dir = get_project_dir(project_id)
    path = Path(project_dir) / file_rel
    if not path.is_file():
        raise NodeNotFoundError(f"设定文件不存在：{file_rel}")

    meta_fm, body = split_frontmatter(read_text(path))
    lines = body.splitlines()
    start = None
    end = len(lines)
    for index, line in enumerate(lines):
        match = _HEADING_RE.match(line)
        if not match:
            continue
        if start is None and match.group(2).strip() == card_name:
            start = index
            continue
        if start is not None:
            end = index
            break
    if start is None:
        raise NodeNotFoundError(f"Markdown 中未找到卡片「{card_name}」")

    remaining = lines[:start] + lines[end:]
    new_body = "\n".join(remaining).rstrip() + "\n"

    if via_proposal:
        proposal = proposal_service.create_proposal(
            project_id=project_id,
            kind="card",
            title=f"删除卡片：{card_name}（{file_rel}）",
            target_path=file_rel,
            content=new_body,
            meta={"frontmatter": meta_fm or {"标题": Path(file_rel).stem},
                  "delete_card": card_name},
        )
        return {"proposal_id": proposal["id"], "via": "proposal"}

    snapshot_file(project_id, project_dir, row["name"], file_rel, reason="card-delete")
    atomic_write_text(
        path, f"{_frontmatter_block(meta_fm)}{new_body}" if meta_fm else new_body
    )
    refresh(project_id, force=True)
    return {"written": file_rel, "deleted": card_name, "via": "direct"}


# ─────────────────────────── 智能生成 ───────────────────────────


def generate_card(
    project_id: int,
    *,
    category: str,
    name: str,
    hint: str = "",
) -> dict:
    """AI 智能生成卡片内容 → 进 Proposal 收件箱（应用前展示 diff）。"""
    prompt = prompt_registry_service.get_prompt("novel.card.complete")
    result = generation_service.run_task(
        project_id=project_id,
        task_type="卡片补全",
        system=prompt["body"],
        messages=[
            {"role": "user", "content": f"分类：{category}\n名称：{name}\n补充线索：{hint}"}
        ],
        prompt_id=prompt["prompt_id"],
        prompt_version=prompt["version"],
        temperature=0.6,
    )
    if not result["ok"]:
        raise InvalidOperationError(
            f"智能生成失败：{result.get('error_message') or result.get('error_code')}"
        )

    from .review_service import _parse_json

    parsed = _parse_json(result["text"]) or {}
    fields = parsed.get("字段") if isinstance(parsed.get("字段"), dict) else {}
    content_fields = {"摘要": parsed.get("摘要", "")} if parsed.get("摘要") else {}
    content_fields.update(fields or {})

    proposal = proposal_service.create_proposal(
        project_id=project_id,
        kind="card",
        title=f"智能生成卡片：{name}（{category}）",
        target_path=_category_path(get_project_dir(project_id)[1], category),
        content=(f"## {name}\n"
                 + "\n".join(f"- {key}：{value}" for key, value in content_fields.items())
                 + "\n"),
        task_id=result.get("task_id"),
        meta={"append": True, "category": category, "card_name": name,
              "source": "model"},
    )
    return {"proposal_id": proposal["id"], "card": name, "fields": content_fields}


# ─────────────────────────── 定位章节线 / 导出 ───────────────────────────


def chapter_line(project_id: int, ref: str, limit: int = 20) -> dict:
    """定位章节线：该卡片（名称）在各章正文中的出现位置与次数。"""
    file_rel, _, name = ref.partition("#")
    if not name:
        raise InvalidOperationError("卡片引用格式应为「文件#名称」")
    _row, project_dir = get_project_dir(project_id)
    chapter_dir = Path(project_dir) / "章节"
    occurrences: list[dict] = []
    if chapter_dir.is_dir():
        from .chapter_service import is_chapter_filename, parse_chapter_number

        for path in sorted(chapter_dir.iterdir()):
            if not path.is_file() or not is_chapter_filename(path.name):
                continue
            body = split_frontmatter(read_text(path))[1]
            count = body.count(name)
            if count:
                occurrences.append(
                    {
                        "rel_path": f"章节/{path.name}",
                        "number": parse_chapter_number(path.name),
                        "count": count,
                    }
                )
    occurrences.sort(key=lambda item: item["number"] or 0)
    return {"ref": ref, "name": name, "occurrences": occurrences[:limit],
            "total": len(occurrences)}


def export_cards(project_id: int, *, category: str | None = None,
                 fmt: str = "md") -> dict:
    """导出卡片（md / json）。"""
    data = list_cards(project_id, category=category)
    cards = data["cards"]
    if fmt == "json":
        content = json.dumps({"cards": cards, "count": len(cards)},
                             ensure_ascii=False, indent=2)
    else:
        lines: list[str] = []
        grouped: dict[str, list[dict]] = {}
        for card in cards:
            grouped.setdefault(card["category"], []).append(card)
        for group, items in grouped.items():
            lines.append(f"# {group}（{len(items)} 条）\n")
            for card in items:
                flag = " ★" if card.get("highlight") else ""
                lines.append(f"## {card['name']}{flag}")
                for key, value in (card.get("fields") or {}).items():
                    lines.append(f"- {key}：{value}")
                lines.append("")
        content = "\n".join(lines).strip() + "\n"
    return {"count": len(cards), "format": fmt, "content": content}


__all__ = [
    "BASE_CATEGORIES",
    "HIGHLIGHT_PALETTE",
    "add_card",
    "categories",
    "chapter_line",
    "clear_highlight",
    "delete_card",
    "export_cards",
    "generate_card",
    "get_card",
    "list_cards",
    "list_highlights",
    "parse_skeleton",
    "refresh",
    "set_highlight",
    "update_card",
]