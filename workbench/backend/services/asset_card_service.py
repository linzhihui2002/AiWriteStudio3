"""Author-triggered AI cards: evidence-bound views and persistent object colours.

Reading a card cache never invokes a model. Parsing never modifies source text.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from . import asset_card_colors as colors
from . import generation_service, prompt_registry_service, proposal_service
from .errors import InvalidOperationError, NodeNotFoundError
from .fs_utils import atomic_write_text, read_text, resolve_within, split_frontmatter
from .operation_log import log as log_operation
from .project_service import get_project_dir
from .material_parser import clean, table_rows, visible_lines

META_FILE = ".meta/asset_cards.json"
CACHE_VERSION = 2
PARSER_VERSION = "grounded-cards-v2"
BASE_CATEGORIES = {"人物": "设定/人物设定.md", "世界": "设定/世界设定.md", "势力": "设定/势力设定.md",
                   "物品": "设定/物品设定.md", "技能": "设定/技能设定.md", "场景": "设定/场景设定.md",
                   "伏笔": "设定/伏笔管理.md"}
HIGHLIGHT_PALETTE = tuple(colors.LEGACY)
_GENERIC_NAMES = {"项目", "姓名", "名称", "身份", "年龄", "来源", "性格", "目标", "配角",
                  "年龄/职业/家境", "性格与说话风格", "现实侧起点与债务", "金手指",
                  *BASE_CATEGORIES.keys(), "人物设定", "世界设定", "势力设定", "技能设定", "物品设定",
                  "场景设定", "unknown", "author", "model", "待定", "待确认", "未定"}
_executor: ThreadPoolExecutor | None = None
_guard = threading.RLock()
_active: dict[tuple[str, int], str] = {}
_cancelled: set[str] = set()


def _now():
    return datetime.now(timezone.utc).isoformat()


def _hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _key(project_id):
    from .. import config
    return str(Path(config.DB_PATH).resolve()), int(project_id)


def _lock(project_id):
    from .file_change_service import project_lock
    return project_lock(project_id)


def _safe(root, rel, *, metadata=False):
    raw = str(rel).replace("\\", "/")
    parts = PurePosixPath(raw).parts
    if (not parts or raw.startswith("/") or ":" in raw or any(p in {".", ".."} for p in parts)
            or (not metadata and (parts[0] != "设定" or Path(raw).suffix.lower() != ".md"))):
        raise InvalidOperationError("卡片仅访问当前小说的设定 Markdown")
    candidate = Path(root) / raw
    for part in (candidate, *candidate.parents):
        if part == Path(root):
            break
        if part.is_symlink() or (hasattr(part, "is_junction") and part.is_junction()):
            raise InvalidOperationError("卡片拒绝链接文件或目录")
    return resolve_within(root, raw)


def _read_meta(root):
    path = _safe(root, META_FILE, metadata=True)
    if not path.is_file():
        return {"version": CACHE_VERSION, "files": {}, "entities": {}, "jobs": {}, "highlights": {}}
    try:
        data = json.loads(read_text(path))
    except (OSError, ValueError) as exc:
        raise InvalidOperationError("设定卡片缓存无法读取，原文件已保留") from exc
    if not isinstance(data, dict):
        raise InvalidOperationError("设定卡片缓存格式无效，原文件已保留")
    if data.get("version") != CACHE_VERSION:
        # Discard old format-derived '姓名/项目/身份' cards, retain manual colours.
        data = {"version": CACHE_VERSION, "files": {}, "entities": {}, "jobs": {},
                "highlights": data.get("highlights") or {}}
    for key in ("files", "entities", "jobs", "highlights"):
        data.setdefault(key, {})
    return data


def _write_meta(root, meta):
    meta["version"] = CACHE_VERSION
    atomic_write_text(_safe(root, META_FILE, metadata=True), json.dumps(meta, ensure_ascii=False, indent=2) + "\n")


def categories(project_id):
    _, root = get_project_dir(project_id)
    items = [{"key": key, "label": key, "path": rel, "builtin": True} for key, rel in BASE_CATEGORIES.items()]
    known = set(BASE_CATEGORIES.values())
    folder = _safe(root, "设定/placeholder.md").parent
    if folder.is_dir():
        for path in sorted(folder.rglob("*.md")):
            rel = path.relative_to(root).as_posix()
            _safe(root, rel)
            if rel not in known:
                label = path.stem.removesuffix("设定") or path.stem
                items.append({"key": label, "label": label, "path": rel, "builtin": False})
    return items


def _targets(project_id, category=None):
    _, root = get_project_dir(project_id)
    items = categories(project_id)
    if category and category not in {item["key"] for item in items}:
        raise InvalidOperationError(f"未知设定分类：{category}")
    return [(item["key"], item["path"], read_text(_safe(root, item["path"]))) for item in items
            if (not category or item["key"] == category) and _safe(root, item["path"]).is_file()]


def _category_path(root, category):
    if category in BASE_CATEGORIES:
        return BASE_CATEGORIES[category]
    from .fs_utils import validate_node_name
    category = validate_node_name(category)
    rel = f"设定/{category}设定.md"
    if _safe(root, rel).is_file():
        return rel
    raise InvalidOperationError(f"未知设定分类：{category}")


def _proof(text, raw, rel, source_hash):
    if not isinstance(raw, dict) or not isinstance(raw.get("quote"), str) or not raw["quote"].strip():
        return None
    quote, start = raw["quote"], raw.get("start")
    if isinstance(start, int) and not isinstance(start, bool) and start >= 0 and text[start:start + len(quote)] == quote:
        pass
    elif text.count(quote) == 1:
        start = text.index(quote)
    else:
        return None
    end = start + len(quote)
    _, body = split_frontmatter(text)
    prefix = len(text) - len(body)
    if start < prefix:
        return None
    fence_start, fence, offset = None, None, prefix
    for line in body.splitlines(keepends=True):
        marker = re.match(r"^\s*(`{3,}|~{3,})(.*)$", line.rstrip("\r\n"))
        if fence is None and marker:
            fence_start, fence = offset, marker[1]
        elif fence is not None and marker and marker[1][0] == fence[0] and len(marker[1]) >= len(fence) and not marker[2].strip():
            if start < offset + len(line) and fence_start < end:
                return None
            fence_start, fence = None, None
        offset += len(line)
    if fence is not None and end > fence_start:
        return None
    return {"quote": quote, "start": start, "end": end, "rel_path": rel,
            "line_start": text.count("\n", 0, start) + 1, "line_end": text.count("\n", 0, end) + 1,
            "document_hash": source_hash}


def _source(declared, proof):
    if declared not in {"author", "model", "unknown"} or not proof:
        return "unknown"
    quote = proof["quote"].lower()
    markers = re.findall(r"(?:来源|source)\s*[:：=]?\s*`?(author|model|unknown)\b|[（(]\s*`?(author|model|unknown)\b|(?:^|\|)\s*`?(author|model|unknown)`?\s*(?:\||$)", quote)
    found = {value for group in markers for value in group if value}
    return declared if found == {declared} else "unknown"


def _definition_bounds(text, card):
    """Verify an independent evidence region; this never discovers new cards.

    An AI-provided extent cannot grant ownership of somebody else's section.
    Heading definitions and explicit name-field tables have verifiable bounds;
    prose and object-row tables conservatively use the name's physical paragraph.
    """
    evidence, name = card["evidence"], card["name"]
    headings = list(re.finditer(r"^ {0,3}(#{1,6})[ \t]+([^\r\n]+)", text, re.M))
    matches = [h for h in headings if name in h[2] and h.start() < evidence["end"] and h.end() > evidence["start"]]
    if len(matches) == 1:
        heading = matches[0]
        end = next((h.start() for h in headings if h.start() > heading.start() and len(h[1]) <= len(heading[1])), len(text))
        return {"start": heading.start(), "end": end}
    name_at = text.find(name, evidence["start"], evidence["end"])
    start = text.rfind("\n", 0, name_at) + 1
    end = text.find("\n", name_at)
    end = len(text) if end < 0 else end
    line = text[start:end]
    cells = [cell.strip().strip("`*").strip() for cell in line.strip().strip("|").split("|")]
    identifiers = {"姓名", "名称", "正式名", "角色", "人物"}
    if line.lstrip().startswith("|") and len(cells) >= 2 and cells[0] in identifiers and cells[1] == name:
        # Field tables use one named object; a second name row ends this scope.
        cursor = start
        while cursor > 0:
            prior = text.rfind("\n", 0, cursor - 1) + 1
            if not text[prior:cursor].lstrip().startswith("|"):
                break
            prior_cells = [c.strip().strip("`*").strip() for c in text[prior:cursor].strip().strip("|").split("|")]
            if prior_cells and prior_cells[0] in identifiers:
                break
            start, cursor = prior, prior
        cursor = end + 1
        while cursor < len(text):
            next_end = text.find("\n", cursor)
            next_end = len(text) if next_end < 0 else next_end
            next_line = text[cursor:next_end]
            if not next_line.lstrip().startswith("|"):
                break
            next_cells = [c.strip().strip("`*").strip() for c in next_line.strip().strip("|").split("|")]
            if next_cells and next_cells[0] in identifiers and len(next_cells) > 1 and next_cells[1] != name:
                break
            end, cursor = next_end, next_end + 1
    return {"start": start, "end": end}


def _belongs(proof, card, cards, anchor=None):
    """A quote in the same file is insufficient: bind it to this object's scope."""
    if not proof:
        return False
    bounds = card.get("ownership_bounds")
    if not bounds or not (bounds["start"] <= proof["start"] and proof["end"] <= bounds["end"]):
        return False
    overlap = lambda left, right: left["start"] < right["end"] and right["start"] < left["end"]
    if any(other is not card and overlap(proof, other["evidence"]) for other in cards):
        return False
    return True


def _validate_objects(raw, text, rel, category):
    if not isinstance(raw, dict) or not isinstance(raw.get("objects"), list):
        raise InvalidOperationError("AI 解析结果必须包含 objects 数组")
    digest, cards = _hash(text), []
    for item in raw["objects"]:
        if not isinstance(item, dict):
            raise InvalidOperationError("AI 解析返回了无效对象")
        name = str(item.get("name") or "").strip()
        proof = _proof(text, item.get("evidence"), rel, digest)
        if not name or len(name) > 80 or name in _GENERIC_NAMES or not proof or name not in proof["quote"]:
            raise InvalidOperationError(f"对象缺少可验证的名称依据：{name or '未命名'}")
        typed = str(item.get("category") or category).strip()
        if not typed or len(typed) > 40 or not isinstance(item.get("fields", []), list):
            raise InvalidOperationError("对象分类或字段数组格式无效")
        fields, field_meta = {}, {}
        for field in item.get("fields", []):
            if not isinstance(field, dict):
                raise InvalidOperationError("AI 返回的字段无效")
            key, value = str(field.get("name") or "").strip(), str(field.get("value") or "").strip()
            evidence = _proof(text, field.get("evidence"), rel, digest)
            if not key or len(key) > 80 or not value or not evidence or value not in evidence["quote"]:
                raise InvalidOperationError(f"字段「{key}」不在原文依据中")
            if key in fields and fields[key] != value:
                raise InvalidOperationError(f"字段「{key}」内容冲突")
            provenance = _proof(text, field.get("source_evidence"), rel, digest)
            fields[key] = value
            field_meta[key] = {"evidence": evidence, "source": _source(field.get("source"), provenance or evidence),
                               "source_evidence": provenance}
        aliases, alias_meta = [], []
        for alias in item.get("aliases", []):
            if not isinstance(alias, dict):
                raise InvalidOperationError("别名必须附原文依据")
            label = str(alias.get("name") or "").strip()
            evidence = _proof(text, alias.get("evidence"), rel, digest)
            if (not label or label in _GENERIC_NAMES or not evidence or label not in evidence["quote"]
                    or not re.search(r"别名|别称|化名|又名|称号|外号|绰号|也叫|又称", evidence["quote"])):
                raise InvalidOperationError(f"别名「{label}」缺少明确原文声明")
            if label != name and label not in aliases:
                aliases.append(label)
                alias_meta.append({"name": label, "evidence": evidence})
        provenance = _proof(text, item.get("source_evidence"), rel, digest)
        extent = _proof(text, item.get("extent"), rel, digest)
        proofs = [proof, *(m["evidence"] for m in field_meta.values())]
        if extent and any(not (extent["start"] <= p["start"] and p["end"] <= extent["end"]) for p in proofs):
            extent = None
        first = next(iter(fields), None)
        summary = f"{first}：{fields[first]}" if first else proof["quote"].strip()
        cards.append({"name": name, "category": typed, "fields": fields, "field_meta": field_meta,
                      "aliases": aliases, "alias_meta": alias_meta, "source": _source(item.get("source"), provenance or proof),
                      "source_evidence": provenance, "evidence": proof, "extent": extent,
                      "summary": re.sub(r"\s+", " ", summary)[:160], "field_list": list(fields), "file": rel, "source_hash": digest})
    for card in cards:
        card["ownership_bounds"] = _definition_bounds(text, card)
    return _verify_ownership(cards)


def _verify_ownership(cards):
    for card in cards:
        if not _belongs(card.get("source_evidence") or card["evidence"], card, cards):
            card["source"] = "unknown"
        for meta in card["field_meta"].values():
            if not _belongs(meta["evidence"], card, cards):
                raise InvalidOperationError(f"字段依据不属于对象「{card['name']}」")
            if not _belongs(meta.get("source_evidence") or meta["evidence"], card, cards, meta["evidence"]):
                meta["source"] = "unknown"
        for alias in card["alias_meta"]:
            if not _belongs(alias["evidence"], card, cards):
                raise InvalidOperationError(f"别名「{alias['name']}」的依据不属于对象「{card['name']}」")
    return cards


def _identity_fingerprint(card):
    fields = {k: v for k, v in card["fields"].items() if k not in {"姓名", "名称", "正式名", "别名", "别称", "化名", "称号"}}
    return _hash(json.dumps({"category": card["category"], "fields": fields}, ensure_ascii=False, sort_keys=True)) if fields else ""


def _bind_cards(meta, rel, cards):
    registry, assigned = meta["entities"], set()
    old = [entity for entity in registry.values() if entity.get("file") == rel]
    for entity in old:
        entity["active"] = False
    for card in sorted(cards, key=lambda c: (c["category"], c["name"], c["evidence"]["start"])):
        fingerprint = _identity_fingerprint(card)
        available = [entity for entity in old if entity["entity_id"] not in assigned]
        matches = [e for e in available if e.get("category") == card["category"] and card["name"] in {e.get("name"), e.get("pending_name")}]
        # A coincidentally equal field value is never proof of a renamed object.
        # Only disambiguate already name-matched definitions, never cross names.
        if len(matches) > 1:
            matches = [e for e in matches if fingerprint and e.get("identity_fingerprint") == fingerprint]
        if len(matches) == 1:
            entity = matches[0]
        else:
            ident = uuid.uuid4().hex
            entity = {"entity_id": ident, "refs": [], "auto_colors": colors.allocate(meta)}
            registry[ident] = entity
            colors.apply_mode(entity, "auto")
        ident = entity["entity_id"]
        assigned.add(ident)
        ref = f"{rel}#{card['name']}@{ident}"
        entity.update(active=True, file=rel, name=card["name"], category=card["category"], aliases=card["aliases"],
                      identity_fingerprint=fingerprint, ref=ref)
        entity.pop("pending_name", None)
        if ref not in entity["refs"]:
            entity["refs"].append(ref)
        legacy = meta.get("highlights", {}).pop(f"{rel}#{card['name']}", None)
        if legacy:
            color = legacy.get("color", "") if isinstance(legacy, dict) else legacy
            try:
                colors.apply_mode(entity, "manual", color)
                if isinstance(legacy, dict) and legacy.get("note"):
                    entity["note"] = legacy["note"]
            except ValueError:
                pass
        card.update(id=ident, entity_id=ident, ref=ref)
    return cards


def _public(card, meta, stale=False, text=None):
    entity = meta["entities"].get(card["entity_id"], {})
    fields = {key: {**value, "capabilities": _field_capabilities(text, card, key) if text is not None and not stale else {"rename": False, "delete": False, "reason": "原材料已变化，请重新解析"}}
              for key, value in card.get("field_meta", {}).items()}
    return {**card, "field_meta": fields, "stale": stale, "highlight": entity.get("highlight", ""),
            "highlight_mode": entity.get("highlight_mode", "auto"),
            "highlight_colors": entity.get("highlight_colors", dict(colors.EMPTY_COLORS))}


def list_cards(project_id, *, category=None, query="", highlight_only=False, use_ai=False):
    row, root = get_project_dir(project_id)
    meta = _read_meta(root)
    texts = {rel: text for _, rel, text in _targets(project_id)}
    targets = {rel: _hash(text) for rel, text in texts.items()}
    stale = sorted(rel for rel, digest in targets.items() if meta["files"].get(rel, {}).get("hash") != digest)
    cards, counts = [], {}
    for rel, entry in meta["files"].items():
        if rel not in targets:
            continue
        for card in entry.get("cards", []):
            item = _public(card, meta, rel in stale, texts.get(rel))
            counts[item["category"]] = counts.get(item["category"], 0) + 1
            if (category and item["category"] != category) or (query and query not in json.dumps(item, ensure_ascii=False)):
                continue
            if not highlight_only or item["highlight"]:
                cards.append(item)
    task = meta["jobs"].get(meta.get("latest_job"))
    running = task and task["status"] in {"queued", "running"}
    status = "running" if running else "pending" if stale else "completed" if meta["files"] else "not_parsed"
    if task and task["status"] == "failed" and not running:
        status = "failed"
    cats = categories(project_id)
    for key in counts:
        if key not in {item["key"] for item in cats}:
            cats.append({"key": key, "label": key, "path": "", "builtin": False})
    return {"project_id": project_id, "project_name": row["name"], "cards": cards, "count": len(cards),
            "counts_by_category": counts, "palette": colors.palette(), "categories": cats,
            "parse_status": status, "task_id": task["id"] if task else None, "task": task, "stale_files": stale}


def get_card(project_id, ref):
    matches = [c for c in list_cards(project_id)["cards"] if c["ref"] == ref or c["entity_id"] == ref or f"{c['file']}#{c['name']}" == ref]
    if not matches:
        _, root = get_project_dir(project_id)
        ids = {e["entity_id"] for e in _read_meta(root)["entities"].values() if ref in e.get("refs", [])}
        matches = [c for c in list_cards(project_id)["cards"] if c["entity_id"] in ids]
    if len(matches) != 1:
        raise NodeNotFoundError("卡片不存在或名称重复，请重新选择具体卡片")
    return matches[0]


def _save_job(project_id, task):
    with _lock(project_id):
        _, root = get_project_dir(project_id)
        meta = _read_meta(root)
        if meta["jobs"].get(task["id"], {}).get("status") == "cancelled":
            task.update(status="cancelled", phase="cancelled")
        meta["jobs"][task["id"]] = {**task, "updated_at": _now()}
        _write_meta(root, meta)


def get_parse_task(project_id, task_id):
    _, root = get_project_dir(project_id)
    task = _read_meta(root)["jobs"].get(task_id)
    if not task:
        raise NodeNotFoundError("AI 卡片解析任务不存在或不属于本书")
    return task


class _Cancelled(Exception):
    pass


def _check_task(project_id, task_id):
    get_project_dir(project_id)
    with _guard:
        if task_id in _cancelled or _active.get(_key(project_id)) != task_id:
            raise _Cancelled()


def _extract(project_id, text, rel, category, model, prompt, task_id):
    from .asset_card_extraction import extract_file
    cards = extract_file(project_id, text, rel, category, model, prompt, task_id,
                         lambda: _check_task(project_id, task_id), _validate_objects)
    return _verify_ownership(cards)


def _perform(project_id, task_id, targets, model, prompt, force):
    task = get_parse_task(project_id, task_id)
    try:
        task.update(status="running", phase="parsing")
        _save_job(project_id, task)
        for category, rel, text in targets:
            _check_task(project_id, task_id)
            task["current_file"] = rel
            _save_job(project_id, task)
            fingerprint = _hash(json.dumps({"hash": _hash(text), "model": model, "prompt": prompt["version"],
                                           "prompt_body": _hash(prompt["body"]), "parser": PARSER_VERSION}, sort_keys=True))
            _, root = get_project_dir(project_id)
            entry = _read_meta(root)["files"].get(rel, {})
            if not force and entry.get("fingerprint") == fingerprint:
                task["cached_files"] += 1
            else:
                try:
                    cards = _extract(project_id, text, rel, category, model, prompt, task_id)
                    _check_task(project_id, task_id)
                    with _lock(project_id):
                        _, root = get_project_dir(project_id)
                        path = _safe(root, rel)
                        if not path.is_file() or _hash(read_text(path)) != _hash(text):
                            raise InvalidOperationError("解析期间原文已变化，结果未应用，请再次解析")
                        meta = _read_meta(root)
                        with _guard:
                            _check_task(project_id, task_id)
                            meta["files"][rel] = {"hash": _hash(text), "fingerprint": fingerprint, "parsed_at": _now(),
                                                  "category": category, "cards": _bind_cards(meta, rel, cards)}
                            _write_meta(root, meta)
                    task["parsed_files"] += 1
                except _Cancelled:
                    raise
                except Exception as exc:
                    task["failures"].append({"file": rel, "error": str(exc)})
            task["files_completed"] += 1
            _save_job(project_id, task)
        task.update(status="failed" if task["failures"] else "completed", phase="done", current_file=None)
    except _Cancelled:
        task.update(status="cancelled", phase="cancelled")
    except Exception as exc:
        task.update(status="failed", phase="failed", error=str(exc))
    finally:
        with _guard:
            if _active.get(_key(project_id)) == task_id:
                _active.pop(_key(project_id), None)
        try:
            _save_job(project_id, task)
        except NodeNotFoundError:
            pass


def refresh(project_id, *, category=None, force=False, use_ai=True, background=True):
    global _executor
    targets = _targets(project_id, category)
    _, root = get_project_dir(project_id)
    from ..engine import router
    model = router.resolve_model(project_dir=root)
    # Public endpoint/model configuration fingerprint, never API keys.
    if model.get("provider"):
        from . import provider_service
        try:
            provider = provider_service.get_provider(model["provider"])
            model["config_hash"] = _hash(json.dumps({key: provider.get(key) for key in ("base_url", "models", "enabled")}, sort_keys=True))
        except NodeNotFoundError:
            pass
    prompt = prompt_registry_service.get_prompt("novel.card.extract")
    with _guard:
        previous = _active.get(_key(project_id))
        if previous:
            return get_parse_task(project_id, previous)
        task_id = uuid.uuid4().hex
        _active[_key(project_id)] = task_id
    task = {"id": task_id, "project_id": project_id, "status": "queued", "phase": "queued", "files_total": len(targets),
            "files_completed": 0, "parsed_files": 0, "cached_files": 0, "failures": [], "current_file": None, "error": "", "created_at": _now()}
    try:
        with _lock(project_id):
            meta = _read_meta(root)
            meta["jobs"][task_id] = task
            meta["latest_job"] = task_id
            _write_meta(root, meta)
    except Exception:
        with _guard:
            if _active.get(_key(project_id)) == task_id:
                _active.pop(_key(project_id), None)
        raise
    if background:
        with _guard:
            if _executor is None:
                _executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="card-parser")
            _executor.submit(_perform, project_id, task_id, targets, model, prompt, force)
        return task
    _perform(project_id, task_id, targets, model, prompt, force)
    return get_parse_task(project_id, task_id)


def cancel_parse_task(project_id, task_id):
    task = get_parse_task(project_id, task_id)
    if task["status"] in {"queued", "running"}:
        with _guard:
            _cancelled.add(task_id)
        task.update(status="cancelled", phase="cancelled")
        _save_job(project_id, task)
    return task


def rebind_file(project_id, source, destination):
    """Preserve identities only for an explicit, committed workbench file move."""
    with _lock(project_id):
        _, root = get_project_dir(project_id)
        meta = _read_meta(root)
        changed = False
        for old in list(meta["files"]):
            if old != source and not old.startswith(source.rstrip("/") + "/"):
                continue
            new = destination.rstrip("/") + old[len(source):]
            _safe(root, new)
            entry = meta["files"].pop(old)
            meta["files"][new] = entry
            for card in entry["cards"]:
                entity = meta["entities"][card["entity_id"]]
                card["file"] = entity["file"] = new
                ref = f"{new}#{card['name']}@{card['entity_id']}"
                card["ref"] = entity["ref"] = ref
                entity["refs"].append(ref)
                for proof in [card["evidence"], card.get("extent"), card.get("source_evidence"),
                              *(m["evidence"] for m in card["field_meta"].values()),
                              *(m.get("source_evidence") for m in card["field_meta"].values()),
                              *(m["evidence"] for m in card["alias_meta"])]:
                    if proof:
                        proof["rel_path"] = new
            changed = True
        if changed:
            _write_meta(root, meta)


def set_highlight(project_id, ref, color, *, note="", mode=None):
    card = get_card(project_id, ref)
    mode = mode or ("manual" if color else "off")
    if mode not in {"auto", "manual", "off"}:
        raise InvalidOperationError("未知高亮模式")
    with _lock(project_id):
        _, root = get_project_dir(project_id)
        meta = _read_meta(root)
        entity = meta["entities"][card["entity_id"]]
        try:
            if mode == "manual":
                selected = colors.colors_for(color)
                if any(e["entity_id"] != entity["entity_id"] and any(value == (e.get(key) or {}).get(theme)
                       for key in ("highlight_colors", "auto_colors") for theme, value in selected.items()) for e in meta["entities"].values()):
                    raise InvalidOperationError("该颜色已被本书其他对象使用，请选择其他颜色")
            elif mode == "auto" and any(e["entity_id"] != entity["entity_id"] and
                    any(value == (e.get("highlight_colors") or {}).get(theme) for theme, value in entity["auto_colors"].items())
                    for e in meta["entities"].values()):
                entity["auto_colors"] = colors.allocate(meta)
            colors.apply_mode(entity, mode, color)
        except ValueError as exc:
            raise InvalidOperationError(str(exc)) from exc
        if note:
            entity["note"] = note
        _write_meta(root, meta)
    log_operation(project_id, "card-highlight", card["ref"], {"color": color, "mode": mode})
    return {"ref": card["ref"], "entity_id": card["entity_id"], "highlight": entity["highlight"],
            "highlight_mode": mode, "highlight_colors": entity["highlight_colors"]}


def clear_highlight(project_id, ref):
    return set_highlight(project_id, ref, "")


def list_highlights(project_id):
    return [{key: c[key] for key in ("entity_id", "ref", "name", "aliases", "highlight", "highlight_mode", "highlight_colors")}
            for c in list_cards(project_id)["cards"] if not c["stale"]]


def _checked_card(project_id, ref):
    card = get_card(project_id, ref)
    _, root = get_project_dir(project_id)
    text = read_text(_safe(root, card["file"]))
    if card["source_hash"] != _hash(text):
        raise InvalidOperationError("卡片依据已变化，请先点击 AI 解析更新卡片")
    for proof in [card["evidence"], *(m["evidence"] for m in card["field_meta"].values())]:
        if text[proof["start"]:proof["end"]] != proof["quote"]:
            raise InvalidOperationError("原文依据不再匹配，修改未执行")
    return card, root, text


def _heading_extent(card, text):
    extent = card.get("extent")
    if not extent:
        return None
    section = text[extent["start"]:extent["end"]]
    match = re.match(r"^(#{1,6})\s+([^\n]+)\n", section)
    if not match or card["name"] not in match[2]:
        return None
    if any(len(m[1]) <= len(match[1]) for m in re.finditer(r"^(#{1,6})\s+", section[match.end():], re.MULTILINE)):
        return None
    return extent


def _field_target(text, card, key):
    """Return structural edits only inside this object's grounded field evidence."""
    if key not in card.get("fields", {}) or key in {"姓名", "名称", "正式名"}:
        return None
    proof = card["field_meta"][key]["evidence"]
    bounds = card.get("ownership_bounds") or card.get("extent") or proof
    value = card["fields"][key]
    candidates = []
    for row in table_rows(text):
        if not (bounds["start"] <= row["start"] and row["end"] <= bounds["end"]):
            continue
        if row["headers"][0] in {"项目", "字段", "字段名", "属性", "属性名"}:
            if len(row["cells"]) < 2 or row["cells"][0]["value"] != key:
                continue
            label, scalar = row["cells"][0]["span"], row["cells"][1]["span"]
            kind, deletion = "property_table", [row["start"], row["start"] + len(row["raw"])]
        elif key in row["values"]:
            label, scalar = None, row["spans"][key]
            kind, deletion = "object_table", list(scalar)
        else:
            continue
        raw = text[scalar[0]:scalar[1]]
        if raw.count(value) != 1:
            continue
        start = scalar[0] + raw.index(value)
        value_span = [start, start + len(value)]
        if not (proof["start"] <= start and value_span[1] <= proof["end"]):
            continue
        candidates.append({"kind": kind, "label": label, "value": value_span, "delete": deletion})
    for row in visible_lines(text):
        if not row["visible"] or not (bounds["start"] <= row["start"] and row["end"] <= bounds["end"]):
            continue
        match = re.match(r"^\s*(?:[-*+]\s+)?(?:\*\*)?([^:：*|]+?)(?:\*\*)?\s*[:：]\s*(.+?)\s*$", row["text"])
        if not match or clean(match[1]) != key or match[2].count(value) != 1:
            continue
        start = row["start"] + match.start(2) + match[2].index(value)
        if not (proof["start"] <= start and start + len(value) <= proof["end"]):
            continue
        candidates.append({"kind": "list", "label": [row["start"] + match.start(1), row["start"] + match.end(1)],
                           "value": [start, start + len(value)], "delete": [row["start"], row["start"] + len(row["raw"])]})
    return candidates[0] if len(candidates) == 1 else None


def _field_capabilities(text, card, key):
    target = _field_target(text, card, key)
    if not target:
        return {"rename": False, "delete": False, "reason": "该字段没有独立结构，请打开原材料改名或删除"}
    return {"rename": target["label"] is not None, "delete": True,
            "reason": "该列名由多个对象共用，改名请打开原材料" if target["label"] is None else ""}


def _field_text(value, *, label=False):
    value = str(value)
    if any(char in value for char in "\r\n\t|") or (label and (not value.strip() or any(char in value for char in ":：*`"))):
        raise InvalidOperationError("字段名称或值不能包含换行、表格分隔符或结构标记")
    return value.strip() if label else value


def update_card(project_id, ref, fields, *, name=None, field_edits=None, expected_hash=None):
    from . import file_change_service as changes
    with _lock(project_id):
        card, root, text = _checked_card(project_id, ref)
        if expected_hash is not None:
            changes.assert_version(card["file"], text, expected_hash)
        edits, extra = [], []
        explicit, final_names = set(), set(card["fields"])
        for operation in field_edits or []:
            if not isinstance(operation, dict) or operation.get("original_name") not in card["fields"]:
                raise InvalidOperationError("编辑字段不存在，请重新解析卡片")
            key = operation["original_name"]
            if key in explicit:
                raise InvalidOperationError("同一字段不能提交多次编辑")
            explicit.add(key)
            target = _field_target(text, card, key)
            if operation.get("delete"):
                if not target:
                    raise InvalidOperationError(f"字段「{key}」不能独立删除，请打开原材料")
                start, end = target["delete"]
                edits.append((start, end, ""))
                final_names.discard(key)
                continue
            new_name = _field_text(operation.get("new_name", key), label=True)
            if new_name != key:
                if not target or not target["label"]:
                    raise InvalidOperationError(f"字段「{key}」不能独立改名，请打开原材料")
                if new_name in final_names or new_name in fields:
                    raise InvalidOperationError(f"字段名称重复：{new_name}")
                final_names.discard(key)
                final_names.add(new_name)
                edits.append((*target["label"], new_name))
            value = _field_text(operation.get("value", card["fields"][key]))
            if value != card["fields"][key]:
                if target:
                    edits.append((*target["value"], value))
                else:
                    proof = card["field_meta"][key]["evidence"]
                    old = card["fields"][key]
                    if proof["quote"].count(old) != 1:
                        raise InvalidOperationError(f"字段「{key}」无法唯一定位，请打开原材料")
                    start = proof["start"] + proof["quote"].index(old)
                    edits.append((start, start + len(old), value))
        for key, value in fields.items():
            key, value = _field_text(key, label=True), _field_text(value)
            if key in explicit:
                raise InvalidOperationError("字段不能同时采用两种编辑格式")
            if key not in card["fields"]:
                if key in final_names:
                    raise InvalidOperationError(f"字段名称重复：{key}")
                final_names.add(key)
                extra.append(f"- {key}：{value}")
                continue
            old = card["fields"][key]
            if value == old:
                continue
            proof = card["field_meta"][key]["evidence"]
            if proof["quote"].count(old) != 1:
                raise InvalidOperationError(f"字段「{key}」无法唯一定位，请打开原材料编辑")
            start = proof["start"] + proof["quote"].index(old)
            edits.append((start, start + len(old), value))
        if name and name != card["name"]:
            name = _field_text(name, label=True)
            proof = card["evidence"]
            if proof["quote"].count(card["name"]) != 1:
                raise InvalidOperationError("名称无法唯一定位，请打开原材料编辑")
            start = proof["start"] + proof["quote"].index(card["name"])
            edits.append((start, start + len(card["name"]), name.strip()))
        if extra:
            extent = _heading_extent(card, text)
            if not extent:
                raise InvalidOperationError("自由段落或表格新增字段请到原材料编辑，已有字段可安全修改")
            edits.append((extent["end"], extent["end"], "\n" + "\n".join(extra) + "\n"))
        edits.sort()
        others = [other for other in list_cards(project_id)["cards"] if other["file"] == card["file"] and other["entity_id"] != card["entity_id"]]
        if any(start < other["evidence"]["end"] and other["evidence"]["start"] < end for start, end, _ in edits for other in others):
            raise InvalidOperationError("编辑范围包含其他对象，修改未执行")
        if any(left[1] > right[0] for left, right in zip(edits, edits[1:])):
            raise InvalidOperationError("字段依据相互重叠，请逐项修改")
        output = text
        for start, end, value in reversed(edits):
            output = output[:start] + value + output[end:]
        if output != text:
            changes.apply_change(project_id, run_id=f"card-edit:{uuid.uuid4().hex}", session_id=None, tool_call_id="edit", operation="rewrite",
                                 rel_path=card["file"], expected_hash=changes.content_hash(text), content=output)
        if name:
            meta = _read_meta(root)
            meta["entities"][card["entity_id"]]["pending_name"] = name.strip()
            _write_meta(root, meta)
    return {"ref": ref, "updated": fields, "file": card["file"], "parse_status": "pending"}


def add_card(project_id, category, name, fields=None, *, via_proposal=True):
    _, root = get_project_dir(project_id)
    rel = _category_path(root, category)
    if not name.strip() or "\n" in name:
        raise InvalidOperationError("请填写有效卡片名称")
    path = _safe(root, rel)
    text = read_text(path) if path.is_file() else ""
    output = text.rstrip() + f"\n\n## {name.strip()}\n" + "\n".join(f"- {key}：{value}" for key, value in (fields or {}).items()) + "\n"
    if via_proposal:
        proposal = proposal_service.create_proposal(project_id=project_id, kind="card", title=f"新增卡片：{name}（{category}）",
                                                     target_path=rel, content=output, meta={"category": category, "card_name": name})
        return {"proposal_id": proposal["id"], "via": "proposal"}
    from . import file_change_service as changes
    changes.apply_change(project_id, run_id=f"card-add:{uuid.uuid4().hex}", session_id=None, tool_call_id="add", operation="rewrite" if path.is_file() else "create",
                         rel_path=rel, expected_hash=changes.content_hash(text) if path.is_file() else None, content=output)
    return {"written": rel, "card": name, "via": "direct", "parse_status": "pending"}


def delete_card(project_id, ref, *, via_proposal=True):
    from . import file_change_service as changes
    with _lock(project_id):
        card, root, text = _checked_card(project_id, ref)
        extent = card.get("extent")
        if not extent:
            raise InvalidOperationError("对象没有独立完整的删除依据，请打开原材料编辑")
        others = [other for other in list_cards(project_id)["cards"] if other["file"] == card["file"] and other["entity_id"] != card["entity_id"]]
        if any(extent["start"] < other["evidence"]["end"] and other["evidence"]["start"] < extent["end"] for other in others):
            raise InvalidOperationError("删除范围包含其他对象，修改未执行，请打开原材料编辑")
        if not via_proposal and not _heading_extent(card, text):
            raise InvalidOperationError("自由段落删除必须先查看提案 diff")
        output = text[:extent["start"]] + text[extent["end"]:]
        if via_proposal:
            proposal = proposal_service.create_proposal(project_id=project_id, kind="card", title=f"删除卡片：{card['name']}",
                                                         target_path=card["file"], content=output,
                                                         meta={"entity_id": card["entity_id"], "delete_card": card["name"]})
            return {"proposal_id": proposal["id"], "via": "proposal"}
        changes.apply_change(project_id, run_id=f"card-delete:{uuid.uuid4().hex}", session_id=None, tool_call_id="delete", operation="rewrite",
                             rel_path=card["file"], expected_hash=changes.content_hash(text), content=output)
    return {"written": card["file"], "deleted": card["name"], "via": "direct", "parse_status": "pending"}


def generate_card(project_id, *, category, name, hint=""):
    prompt = prompt_registry_service.get_prompt("novel.card.complete")
    result = generation_service.run_task(project_id=project_id, task_type="卡片补全", system=prompt["body"],
                                        messages=[{"role": "user", "content": f"分类：{category}\n名称：{name}\n线索：{hint}"}],
                                        prompt_id=prompt["prompt_id"], prompt_version=prompt["version"], temperature=.6)
    if not result.get("ok"):
        raise InvalidOperationError(f"智能生成失败：{result.get('error_message') or result.get('error_code')}")
    from .review_service import _parse_json
    parsed = _parse_json(result["text"]) or {}
    fields = parsed.get("字段") if isinstance(parsed.get("字段"), dict) else {}
    _, root = get_project_dir(project_id)
    rel = _category_path(root, category)
    path = _safe(root, rel)
    existing = read_text(path) if path.is_file() else ""
    output = existing.rstrip() + f"\n\n## {name}\n- 来源：model\n" + "\n".join(f"- {key}：{value}" for key, value in fields.items()) + "\n"
    proposal = proposal_service.create_proposal(project_id=project_id, kind="card", title=f"智能生成卡片：{name}（{category}）",
                                                 target_path=rel, content=output, task_id=result.get("task_id"),
                                                 meta={"category": category, "card_name": name, "source": "model"})
    return {"proposal_id": proposal["id"], "card": name, "fields": fields}


def chapter_line(project_id, ref, limit=20):
    card = get_card(project_id, ref)
    _, root = get_project_dir(project_id)
    from .chapter_service import is_chapter_filename, parse_chapter_number
    from .file_change_service import managed_path
    occurrences = []
    folder = root / "章节"
    if folder.is_dir():
        for path in sorted(folder.iterdir()):
            if not path.is_file() or not is_chapter_filename(path.name):
                continue
            rel, safe = managed_path(root, f"章节/{path.name}")
            count = split_frontmatter(read_text(safe))[1].count(card["name"])
            if count:
                occurrences.append({"rel_path": rel, "number": parse_chapter_number(path.name), "count": count})
    return {"ref": ref, "name": card["name"], "occurrences": occurrences[:limit], "total": len(occurrences)}


def export_cards(project_id, *, category=None, fmt="md"):
    cards = list_cards(project_id, category=category)["cards"]
    content = json.dumps({"cards": cards, "count": len(cards)}, ensure_ascii=False, indent=2) if fmt == "json" else "\n\n".join(
        f"## {card['name']}\n" + "\n".join(f"- {key}：{value}" for key, value in card["fields"].items()) for card in cards)
    return {"count": len(cards), "format": fmt, "content": content}


def recover():
    from .. import db
    with db.get_conn() as conn:
        ids = [row[0] for row in conn.execute("SELECT id FROM projects WHERE deleted_at IS NULL")]
    for project_id in ids:
        try:
            with _lock(project_id):
                _, root = get_project_dir(project_id)
                meta = _read_meta(root)
                changed = False
                for task in meta["jobs"].values():
                    if task["status"] in {"queued", "running"}:
                        task.update(status="cancelled", phase="interrupted", error="应用关闭中断了解析，请重新点击 AI 解析")
                        changed = True
                if changed:
                    _write_meta(root, meta)
        except (OSError, InvalidOperationError, NodeNotFoundError):
            continue


def shutdown():
    global _executor
    with _guard:
        _cancelled.update(_active.values())
        executor, _executor = _executor, None
    if executor:
        executor.shutdown(wait=False, cancel_futures=True)
