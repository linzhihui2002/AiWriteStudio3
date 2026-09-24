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
from pathlib import Path

from . import generation_service, operation_log, prompt_registry_service
from .conflict_service import line_diff
from .contract_service import freeze_outline, read_gates
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
    return data if isinstance(data, dict) else {"candidates": [], "locked": None,
                                                "history": [], "questions": []}


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
    """生成 2-3 个大纲候选（不写正式大纲，等作者锁定）。"""
    count = max(2, min(3, int(count)))
    prompt = prompt_registry_service.get_prompt("novel.outline.generate")
    _row, project_dir = get_project_dir(project_id)
    existing = read_outline(project_id)

    candidates: list[dict] = []
    if use_ai:
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
                        + f"请给出 {count} 个差异化的大纲候选，用「候选一/候选二/候选三」分段。"
                    ),
                }
            ],
            prompt_id=prompt["prompt_id"],
            prompt_version=prompt["version"],
            temperature=0.9,
        )
        if result["ok"]:
            candidates = _split_candidates(result["text"], count)

    if not candidates:
        candidates = [
            {
                "title": f"候选{index + 1}",
                "content": "（AI 不可用：请手工填写本候选的卷级结构与主线冲突）",
                "source": "placeholder",
            }
            for index in range(count)
        ]

    data = _read_meta(project_dir)
    data["candidates"] = candidates
    data["idea"] = idea or data.get("idea", "")
    _write_meta(project_dir, data)
    operation_log.log(project_id, "outline-candidates", OUTLINE_FILE,
                      {"count": len(candidates), "ai": bool(use_ai)})
    return {"candidates": candidates, "count": len(candidates)}


def _split_candidates(text: str, count: int) -> list[dict]:
    pattern = re.compile(r"(?:^|\n)\s*(?:#{1,3}\s*)?(候选[一二三123]|方案[一二三123])\s*[:：]?\s*(.*)")
    matches = list(pattern.finditer(text or ""))
    if not matches:
        if not (text or "").strip():
            return []
        return [{"title": f"候选{index + 1}", "content": text.strip(), "source": "model"}
                for index in range(1)]
    candidates: list[dict] = []
    for index, match in enumerate(matches[:count]):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        candidates.append({"title": match.group(1), "content": body, "source": "model"})
    return candidates


def lock_candidate(project_id: int, index_or_title: int | str, *, confirm: bool = True) -> dict:
    """锁定某个候选为正式大纲（写入 ``大纲/大纲.md``）。"""
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
            if candidate.get("title") == index_or_title:
                target = candidate
                break
    if target is None:
        raise NodeNotFoundError(f"找不到候选：{index_or_title}")

    gates = read_gates(project_dir)
    content = str(target.get("content") or "").strip()
    if gates.get("outline_frozen"):
        content = f"# 大纲（重新锁定于 {time.strftime('%Y-%m-%d %H:%M')}）\n\n{content}"
    save_outline(project_id, content, confirm=bool(gates.get("outline_frozen")))

    data["locked"] = {"title": target.get("title"), "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    _write_meta(project_dir, data)
    operation_log.log(project_id, "outline-lock", OUTLINE_FILE, {"title": target.get("title")})
    return {"locked": data["locked"], "content": content}


def freeze(project_id: int, *, confirm: bool = True) -> dict:
    """冻结大纲（CHECK 门控项）。"""
    return freeze_outline(project_id, confirm=confirm)


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
    "rolling_plan",
    "save_outline",
]