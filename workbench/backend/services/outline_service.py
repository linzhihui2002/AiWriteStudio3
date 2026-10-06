"""大纲规划（Task 28）：灵感扩展定向追问、候选生成与锁定、冻结与差异确认、滚动规划。

事实源：``大纲/大纲.md``（正式大纲）与 ``.meta/outline.json``（候选 / 锁定 / 历史）。

纪律
- 候选 2-3 个，作者**确认锁定**后才写入 ``大纲/大纲.md``；
- 冻结后修改必须走差异确认（记录 diff）；
- 滚动规划：只细化最近 2 卷，远景只保留卷级目标（控成本）。
"""

from __future__ import annotations

import json
import re
import time
import unicodedata
import uuid
from difflib import SequenceMatcher
from pathlib import Path

from . import generation_service, operation_log, prompt_registry_service
from .conflict_service import line_diff
from .contract_service import freeze_outline, read_gates, unfreeze_outline
from .errors import InvalidOperationError, NodeNotFoundError
from .fs_utils import atomic_write_text, compose_document, read_text, split_frontmatter
from .project_service import get_project_dir
from .snapshot_service import snapshot_file

OUTLINE_FILE = "大纲/大纲.md"
CHAPTER_OUTLINE_FILE = "大纲/章纲.md"
META_FILE = ".meta/outline.json"
ROLLING_WINDOW = 2  # 只细化最近 2 卷


def _meta_path(project_dir: Path) -> Path:
    return Path(project_dir) / META_FILE


def _read_meta(project_dir: Path) -> dict:
    path = _meta_path(project_dir)
    if not path.is_file():
        return {"candidates": [], "locked": None, "history": [], "questions": []}
    try:
        data = json.loads(read_text(path))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {"candidates": [], "locked": None, "history": [], "questions": []}
    if not isinstance(data, dict):
        return {"candidates": [], "locked": None, "history": [], "questions": []}
    # Legacy candidates have no identity. Derive one without writing during reads,
    # then persist it on the next metadata change. Positions preserve duplicate
    # legacy titles and keep their existing display order intact.
    candidates = data.get("candidates")
    normalized = []
    seen_ids: set[str] = set()
    for index, original in enumerate(candidates if isinstance(candidates, list) else []):
        if not isinstance(original, dict):
            continue
        candidate = dict(original)
        candidate["title"] = str(candidate.get("title") or f"候选{index + 1}")
        candidate["content"] = str(candidate.get("content") or "")
        candidate["source"] = str(candidate.get("source") or "model")
        candidate_id = str(candidate.get("id") or "")
        if not candidate_id or candidate_id in seen_ids:
            identity = f"outline-candidate:{index}:{candidate['title']}:{candidate['content']}"
            candidate_id = "candidate-" + uuid.uuid5(uuid.NAMESPACE_URL, identity).hex
        candidate["id"] = candidate_id
        seen_ids.add(candidate_id)
        normalized.append(candidate)
    data["candidates"] = normalized
    return data


def _write_meta(project_dir: Path, data: dict) -> None:
    atomic_write_text(
        _meta_path(project_dir), json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    )


def read_outline(project_id: int) -> dict:
    row, project_dir = get_project_dir(project_id)
    text = ""
    path = Path(project_dir) / OUTLINE_FILE
    if path.is_file():
        text = split_frontmatter(read_text(path))[1].strip()
    meta = _read_meta(project_dir)
    return {
        "project_id": project_id,
        "project_name": row["name"],
        "content": text,
        "candidates": meta.get("candidates") or [],
        "locked": meta.get("locked"),
        "questions": meta.get("questions") or [],
        "frozen": bool(read_gates(project_dir).get("outline_frozen")),
        "history_count": len(meta.get("history") or []),
        "rolling_window": ROLLING_WINDOW,
    }


def save_outline(project_id: int, content: str, *, confirm: bool = False) -> dict:
    """保存大纲正文；已冻结时必须 ``confirm=True``（差异确认）。"""
    from .file_change_service import project_lock
    with project_lock(project_id):
        return _save_outline_locked(project_id, content, confirm=confirm)


def _save_outline_locked(project_id: int, content: str, *, confirm: bool = False) -> dict:
    row, project_dir = get_project_dir(project_id)
    gates = read_gates(project_dir)
    path = Path(project_dir) / OUTLINE_FILE
    previous = read_text(path) if path.is_file() else ""

    if gates.get("outline_frozen") and not confirm:
        raise InvalidOperationError(
            "大纲已冻结，修改需差异确认（confirm=true）；确认后将记录本次差异并重新冻结。"
        )

    meta, _body = split_frontmatter(previous)
    meta = meta or {"标题": "大纲"}
    snapshot_file(project_id, project_dir, row["name"], OUTLINE_FILE, reason="outline-save")
    atomic_write_text(path, compose_document(meta, content))
    from .index_service import invalidate_knowledge
    invalidate_knowledge(project_id, OUTLINE_FILE)

    data = _read_meta(project_dir)
    data.setdefault("history", []).append(
        {
            "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "confirmed": bool(confirm),
            "diff": line_diff(split_frontmatter(previous)[1], content)[:200],
        }
    )
    _write_meta(project_dir, data)
    operation_log.log(project_id, "outline-save", OUTLINE_FILE, {"confirm": confirm})
    return {"content": content, "frozen": bool(gates.get("outline_frozen"))}


def expand_ideas(project_id: int, *, idea: str = "", use_ai: bool = True) -> dict:
    """灵感扩展：定向追问（列出需作者拍板的关键问题）。"""
    prompt = prompt_registry_service.get_prompt("novel.outline.generate")
    questions = [
        "这本书的核心冲突是什么（主角想要什么、谁在阻止）？",
        "主角的成长弧对应的起点与终点分别是什么？",
        "世界规则里最容易让读者出戏的一条是什么，打算怎么处理？",
        "计划写多少卷、每卷结尾留什么钩子？",
        "读者爽点从哪里来（打脸/升级/复仇/解密），节奏怎么排？",
    ]
    if use_ai and idea.strip():
        result = generation_service.run_task(
            project_id=project_id,
            task_type="灵感追问",
            system=prompt["body"],
            messages=[
                {"role": "user", "content": f"我的灵感是：{idea}\n请只列出我需要先拍板的关键问题（不超过 5 条），不要写大纲。"}
            ],
            prompt_id=prompt["prompt_id"],
            prompt_version=prompt["version"],
            temperature=0.6,
        )
        if result["ok"]:
            text = result["text"]
            parsed = [line.strip("- 　\t") for line in text.splitlines()
                      if line.strip() and not line.strip().startswith("#")]
            if parsed:
                questions = parsed[:8]

    from .file_change_service import project_lock
    with project_lock(project_id):
        _row, project_dir = get_project_dir(project_id)
        data = _read_meta(project_dir)
        data["questions"] = questions
        data["idea"] = idea
        _write_meta(project_dir, data)
    return {"questions": questions, "idea": idea}


def generate_candidates(
    project_id: int,
    *,
    idea: str = "",
    count: int = 3,
    use_ai: bool = True,
) -> dict:
    """追加 2-3 个不同的候选；模型运行期间不持有项目写锁。"""
    from .file_change_service import project_lock

    count = max(2, min(3, int(count)))
    prompt = prompt_registry_service.get_prompt("novel.outline.generate")
    with project_lock(project_id):
        existing = read_outline(project_id)

    candidates: list[dict] = []
    if use_ai:
        previous = "\n\n".join(
            f"{item['title']}：\n{item['content']}" for item in existing["candidates"]
        )
        result = generation_service.run_task(
            project_id=project_id,
            task_type="大纲生成",
            system=prompt["body"],
            messages=[
                {
                    "role": "user",
                    "content": (
                        (f"灵感：{idea}\n" if idea else "")
                        + (f"现有大纲：\n{existing['content'][:3000]}\n" if existing["content"] else "")
                        + (f"此前已生成的候选（全部保留）：\n{previous}\n\n" if previous else "")
                        + f"请给出 {count} 个差异化的大纲候选，用「候选一/候选二/候选三」分段。"
                        + "各候选的主线冲突、成长路径与卷级事件必须有实质区别；"
                        "不得重复此前候选，也不得仅换标题、措辞、标点或人物名字。"
                    ),
                }
            ],
            prompt_id=prompt["prompt_id"],
            prompt_version=prompt["version"],
            temperature=0.9,
        )
        if not result.get("ok"):
            raise InvalidOperationError(
                "候选生成失败，已有候选已保留。" + str(result.get("error_message") or "请稍后重试。")
            )
        candidates = _split_candidates(str(result.get("text") or ""), count)
        if not candidates:
            raise InvalidOperationError("模型未返回有效候选，已有候选已保留，请重试。")
    elif not existing["candidates"]:
        # Keep the explicit offline/manual workflow supported by existing callers.
        # AI errors never create these placeholders, and repeat calls never append them.
        candidates = [
            {
                "title": f"候选{index + 1}",
                "content": "（AI 不可用：请手工填写本候选的卷级结构与主线冲突）",
                "source": "placeholder",
            }
            for index in range(count)
        ]

    with project_lock(project_id):
        _row, project_dir = get_project_dir(project_id)
        data = _read_meta(project_dir)
        all_candidates = data["candidates"]
        added_count = 0
        duplicate_count = 0
        for candidate in candidates:
            # Recheck the latest disk contents after model completion so two
            # simultaneous requests cannot overwrite or duplicate one another.
            if ((not use_ai and all_candidates and added_count == 0)
                    or (use_ai and _is_duplicate(candidate["content"], all_candidates))):
                duplicate_count += 1
                continue
            candidate["id"] = "candidate-" + uuid.uuid4().hex
            candidate["title"] = _next_candidate_title(all_candidates)
            all_candidates.append(candidate)
            added_count += 1
        if added_count:
            data["idea"] = idea or data.get("idea", "")
            _write_meta(project_dir, data)
            operation_log.log(project_id, "outline-candidates", OUTLINE_FILE,
                              {"count": len(all_candidates), "added_count": added_count,
                               "duplicate_count": duplicate_count, "ai": bool(use_ai)})
        response = {"candidates": all_candidates, "count": len(all_candidates),
                    "added_count": added_count, "duplicate_count": duplicate_count}
        if not added_count:
            response["message"] = ("本次候选与已有候选重复，未新增候选；已有候选已保留。"
                                   if use_ai else "AI 未启用，已有候选已保留，未重复添加占位候选。")
        elif duplicate_count:
            response["message"] = f"新增 {added_count} 个候选，已过滤 {duplicate_count} 个重复候选。"
        return response


def _next_candidate_title(candidates: list[dict]) -> str:
    number = len(candidates) + 1
    titles = {item["title"] for item in candidates}
    while f"候选{number}" in titles:
        number += 1
    return f"候选{number}"


def _candidate_fingerprint(content: str) -> str:
    text = unicodedata.normalize("NFKC", content).casefold().strip()
    # Strip only numbered candidate wrappers. A substantive heading such as
    # "# 主线冲突：主角反叛城主" remains part of the plot and may be refined.
    text = re.sub(r"\A[ \t]*(?:#{1,6}[ \t]*)?(?:候选|方案)[一二三四五六七八九十\d]+[^\n]*(?:\n|$)", "", text)
    return "".join(char for char in text if not char.isspace()
                   and unicodedata.category(char)[0] not in {"P", "S", "C"})


def _is_duplicate(content: str, candidates: list[dict], *, similar: bool = True) -> bool:
    fingerprint = _candidate_fingerprint(content)
    if not fingerprint:
        return True
    for candidate in candidates:
        previous = _candidate_fingerprint(candidate["content"])
        if fingerprint == previous:
            return True
        if similar and previous:
            # Length check avoids expensive comparisons for clearly different plans.
            length_ratio = 2 * min(len(fingerprint), len(previous)) / (len(fingerprint) + len(previous))
            if length_ratio >= 0.92 and SequenceMatcher(
                None, fingerprint, previous, autojunk=False
            ).ratio() >= 0.92:
                return True
    return False


def _split_candidates(text: str, count: int) -> list[dict]:
    pattern = re.compile(r"(?:^|\n)[ \t]*(?:#{1,6}[ \t]*)?(候选[一二三四五六七八九十\d]+|方案[一二三四五六七八九十\d]+)[ \t]*[:：]?[ \t]*(.*)")
    matches = list(pattern.finditer(text or ""))
    if not matches:
        if not (text or "").strip():
            return []
        body = text.strip()
        return [{"title": "候选1", "content": body, "source": "model"}] \
            if _candidate_fingerprint(body) else []
    candidates: list[dict] = []
    for index, match in enumerate(matches[:count]):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = "\n".join(part for part in (match.group(2).strip(), text[start:end].strip()) if part)
        if _candidate_fingerprint(body):
            candidates.append({"title": match.group(1), "content": body, "source": "model"})
    return candidates


def refine_candidate(project_id: int, candidate_id: str, *, message: str) -> dict:
    """对选定候选开展连续对话，保留原版本并追加优化后的完整候选。"""
    from .file_change_service import FileConflictError, project_lock

    message = message.strip()
    if not message:
        raise InvalidOperationError("请填写对当前候选的优化要求。")
    with project_lock(project_id):
        _row, project_dir = get_project_dir(project_id)
        data = _read_meta(project_dir)
        target = next((item for item in data["candidates"] if item["id"] == candidate_id), None)
        if target is None:
            raise NodeNotFoundError("找不到当前候选，请刷新候选列表后重试。")
        expected_version = json.dumps(target, ensure_ascii=False, sort_keys=True)
        history = [dict(item) for item in (target.get("conversation") or [])
                   if isinstance(item, dict) and item.get("role") in {"user", "assistant"}
                   and isinstance(item.get("content"), str)]

    prompt = prompt_registry_service.get_prompt("novel.outline.refine")
    result = generation_service.run_task(
        project_id=project_id,
        task_type="大纲完善",
        system=prompt["body"] + "\n你正在优化未锁定的大纲候选。根据作者要求输出完整的优化后候选，"
                               "保留未要求改动的内容，不要只给修改建议、解释或多个备选；不要写入正式大纲。",
        messages=[{"role": "user", "content": f"当前选择：{target['title']}\n候选全文：\n{target['content']}"}]
                 + history + [{"role": "user", "content": message}],
        prompt_id=prompt["prompt_id"], prompt_version=prompt["version"], temperature=0.7,
    )
    if not result.get("ok"):
        raise InvalidOperationError("候选优化失败，原候选已保留。"
                                    + str(result.get("error_message") or "请稍后重试。"))
    content = str(result.get("text") or "").strip()
    if not _candidate_fingerprint(content):
        raise InvalidOperationError("模型未返回有效优化内容，原候选已保留，请重试。")

    with project_lock(project_id):
        _row, project_dir = get_project_dir(project_id)
        data = _read_meta(project_dir)
        current = next((item for item in data["candidates"] if item["id"] == candidate_id), None)
        if current is None or json.dumps(current, ensure_ascii=False, sort_keys=True) != expected_version:
            raise FileConflictError("当前候选已发生变化，本次优化未写入，请刷新后重试。", path=META_FILE)
        if _is_duplicate(content, data["candidates"], similar=False):
            raise InvalidOperationError("优化结果与已有候选没有实质差异，原候选已保留，请调整优化要求。")
        candidate = {"id": "candidate-" + uuid.uuid4().hex,
                     "title": _next_candidate_title(data["candidates"]), "content": content,
                     "source": "model", "parent_id": candidate_id,
                     "conversation": history + [{"role": "user", "content": message},
                                                 {"role": "assistant", "content": content}]}
        data["candidates"].append(candidate)
        _write_meta(project_dir, data)
        operation_log.log(project_id, "outline-candidate-refine", META_FILE,
                          {"candidate_id": candidate["id"], "parent_id": candidate_id})
        return {"candidate": candidate, "candidates": data["candidates"],
                "count": len(data["candidates"]), "message": "已保存优化后的新候选，原候选已保留。"}


def lock_candidate(project_id: int, index_or_title: int | str, *, confirm: bool = True) -> dict:
    """锁定某个候选为正式大纲（写入 ``大纲/大纲.md``）。"""
    from .file_change_service import project_lock
    with project_lock(project_id):
        return _lock_candidate_locked(project_id, index_or_title, confirm=confirm)


def _lock_candidate_locked(project_id: int, index_or_title: int | str, *, confirm: bool = True) -> dict:
    if not confirm:
        raise InvalidOperationError("锁定候选需要作者确认（confirm=true）")
    row, project_dir = get_project_dir(project_id)
    data = _read_meta(project_dir)
    candidates = data.get("candidates") or []
    if not candidates:
        raise NodeNotFoundError("尚无大纲候选，请先生成候选")

    target = None
    if isinstance(index_or_title, int):
        if 0 <= index_or_title < len(candidates):
            target = candidates[index_or_title]
    else:
        for candidate in candidates:
            if candidate.get("id") == index_or_title or candidate.get("title") == index_or_title:
                target = candidate
                break
    if target is None:
        raise NodeNotFoundError(f"找不到候选：{index_or_title}")

    gates = read_gates(project_dir)
    content = str(target.get("content") or "").strip()
    if gates.get("outline_frozen"):
        content = f"# 大纲（重新锁定于 {time.strftime('%Y-%m-%d %H:%M')}）\n\n{content}"
    save_outline(project_id, content, confirm=bool(gates.get("outline_frozen")))

    data = _read_meta(project_dir)
    data["locked"] = {"id": target["id"], "title": target.get("title"),
                      "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    _write_meta(project_dir, data)
    operation_log.log(project_id, "outline-lock", OUTLINE_FILE, {"title": target.get("title")})
    return {"locked": data["locked"], "content": content}


def freeze(project_id: int, *, confirm: bool = True) -> dict:
    """冻结大纲（CHECK 门控项）。"""
    return freeze_outline(project_id, confirm=confirm)


def unfreeze(project_id: int) -> dict:
    """取消冻结，允许作者继续调整正式大纲。"""
    return unfreeze_outline(project_id)


def rolling_plan(project_id: int, *, use_ai: bool = True) -> dict:
    """滚动规划：只细化最近 2 卷（远景仅保留卷级目标）。"""
    prompt = prompt_registry_service.get_prompt("novel.outline.refine")
    outline = read_outline(project_id)
    if not outline["content"]:
        raise InvalidOperationError("大纲为空，无法滚动规划（请先锁定大纲）")

    volumes = _split_volumes(outline["content"])
    recent = volumes[-ROLLING_WINDOW:]
    result_text = ""
    if use_ai and recent:
        payload = "\n\n".join(item["content"] for item in recent)
        result = generation_service.run_task(
            project_id=project_id,
            task_type="大纲完善",
            system=prompt["body"],
            messages=[
                {"role": "user", "content": f"只细化以下 {len(recent)} 卷，产出每卷的分章规划"
                                            f"（章号 + 一句话事件 + 卷末钩子）：\n\n{payload}"}
            ],
            prompt_id=prompt["prompt_id"],
            prompt_version=prompt["version"],
            temperature=0.7,
        )
        result_text = result["text"] if result["ok"] else ""

    suggestion = result_text.strip() or "（AI 不可用：请手工细化最近两卷的分章规划）"
    _row, project_dir = get_project_dir(project_id)
    chapter_outline_path = Path(project_dir) / CHAPTER_OUTLINE_FILE
    existing = split_frontmatter(read_text(chapter_outline_path))[1] if \
        chapter_outline_path.is_file() else ""
    snapshot_file(project_id, project_dir, _row["name"], CHAPTER_OUTLINE_FILE,
                  reason="rolling-plan")
    atomic_write_text(
        chapter_outline_path,
        compose_document({"标题": "章纲"}, f"{existing.rstrip()}\n\n{suggestion}"),
    )
    operation_log.log(project_id, "outline-rolling", CHAPTER_OUTLINE_FILE,
                      {"volumes": len(recent)})
    from .index_service import invalidate_knowledge
    invalidate_knowledge(project_id, CHAPTER_OUTLINE_FILE)
    return {
        "volumes_total": len(volumes),
        "refined": [item["title"] for item in recent],
        "suggestion": suggestion,
        "note": f"远景 {max(0, len(volumes) - ROLLING_WINDOW)} 卷维持卷级目标，暂不细化",
    }


def _split_volumes(text: str) -> list[dict]:
    pattern = re.compile(r"(?:^|\n)\s*(?:#{1,4}\s*)?(第[一二三四五六七八九十\d]+卷[^\n]*)")
    matches = list(pattern.finditer(text or ""))
    if not matches:
        return [{"title": "全书", "content": (text or "").strip()}] if text.strip() else []
    volumes: list[dict] = []
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        volumes.append({"title": match.group(1).strip(), "content": text[start:end].strip()})
    return volumes


def list_history(project_id: int) -> list[dict]:
    _row, project_dir = get_project_dir(project_id)
    return _read_meta(project_dir).get("history") or []


__all__ = [
    "CHAPTER_OUTLINE_FILE",
    "OUTLINE_FILE",
    "ROLLING_WINDOW",
    "expand_ideas",
    "freeze",
    "generate_candidates",
    "list_history",
    "lock_candidate",
    "read_outline",
    "refine_candidate",
    "rolling_plan",
    "save_outline",
    "unfreeze",
]
