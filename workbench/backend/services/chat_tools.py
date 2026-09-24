"""对话工具表（chat tools）：Agent 可在对话中主动调用的工作台能力。

红线（与 AGENTS.md 一致）
- **读工具无副作用**；**写工具只产 Proposal**（应用前强制 diff），
  仅当会话开启 ``auto_apply``（允许直接落盘）时由本模块按同一路径立即应用，
  而 ``proposal_service.apply_proposal`` 内部**先存快照**，可回滚。
- 所有路径参数一律经 ``fs_utils.resolve_within`` 校验；
  写工具另有目录白名单（``章节/``、``设定/``、``大纲/``、``状态/``），越界直接拒绝。
- 工具输出统一 ``{"ok", "summary", "text", "proposal_ids", "applied"}``，
  ``text`` 超长会被截断并标注，避免把上下文撑爆。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import chapter_service, operation_log, proposal_service, review_service
from .asset_card_service import BASE_CATEGORIES
from .errors import InvalidOperationError, NodeNotFoundError
from .fs_utils import read_text, resolve_within
from .project_service import get_project_dir

#: 单个工具回灌给模型的观察文本上限（字符）
MAX_OBSERVATION_CHARS = 4000
#: 写工具允许的单次正文长度上限
MAX_WRITE_CHARS = 20000

#: 写工具允许落盘的顶层目录（相对项目根）
WRITE_DIRS: tuple[str, ...] = (chapter_service.CHAPTER_DIR, "设定", "大纲", "状态")

STATE_FILE = "状态/角色状态.md"
FORESHADOW_FILE = "设定/伏笔管理.md"
OUTLINE_FILES: dict[str, str] = {"大纲": "大纲/大纲.md", "章纲": "大纲/章纲.md"}


@dataclass
class ToolContext:
    """一次工具调用的上下文（由对话服务构造）。"""

    project_id: int | None = None
    chapter_rel: str | None = None
    session_id: int = 0
    auto_apply: bool = False
    task_id: int | None = None
    proposals: list[int] = field(default_factory=list)


# ─────────────────────────── 结果与校验 ───────────────────────────


def _ok(summary: str, text: str = "", **extra) -> dict:
    return {"ok": True, "summary": summary, "text": str(text or ""), **extra}


def _fail(message: str) -> dict:
    return {"ok": False, "summary": f"失败：{message}", "text": "", "error": message}


def _clip(text: str, limit: int = MAX_OBSERVATION_CHARS) -> str:
    body = str(text or "")
    if len(body) <= limit:
        return body
    return f"{body[:limit]}\n…（已截断，原文共 {len(body)} 字）"


def _require_project(ctx: ToolContext) -> tuple[dict, Path]:
    if ctx.project_id is None:
        raise InvalidOperationError("当前会话未绑定项目，工具不可用")
    return get_project_dir(ctx.project_id)


def _project_dir(ctx: ToolContext) -> Path:
    return _require_project(ctx)[1]


def _read_rel(ctx: ToolContext, rel: str) -> str:
    path = resolve_within(_project_dir(ctx), rel)
    if not path.is_file():
        raise NodeNotFoundError(f"文件不存在：{rel}")
    return read_text(path)


def _validate_content(content: object, *, limit: int = MAX_WRITE_CHARS) -> str:
    text = str(content or "")
    if not text.strip():
        raise InvalidOperationError("content 不能为空")
    if len(text) > limit:
        raise InvalidOperationError(f"content 过长（>{limit} 字），请分段提交")
    return text


def _validate_write_rel(ctx: ToolContext, rel: str) -> str:
    project_dir = _project_dir(ctx)
    target = resolve_within(project_dir, rel)
    rel_posix = target.relative_to(project_dir.resolve()).as_posix()
    top = rel_posix.split("/", 1)[0]
    if top not in WRITE_DIRS:
        raise InvalidOperationError(
            f"不允许写入 {rel_posix}（只允许：{'/'.join(WRITE_DIRS)}）"
        )
    return rel_posix


# ─────────────────────────── 读工具 ───────────────────────────


def tool_list_chapters(args: dict, ctx: ToolContext) -> dict:
    items = chapter_service.list_chapters(ctx.project_id)  # type: ignore[arg-type]
    if not items:
        return _ok("项目暂无章节", "（没有章节文件）")
    lines = [
        f"- {item['rel_path']}｜{item['title'] or '(无标题)'}｜{item['word_count']} 字｜{item['status']}"
        for item in items
    ]
    return _ok(f"共 {len(items)} 章", "\n".join(lines))


def tool_read_chapter(args: dict, ctx: ToolContext) -> dict:
    rel = str(args.get("rel_path") or ctx.chapter_rel or "").strip()
    if not rel:
        raise InvalidOperationError("rel_path 不能为空（可用 list_chapters 取路径）")
    detail = chapter_service.read_chapter(ctx.project_id, rel)  # type: ignore[arg-type]
    limit = int(args.get("max_chars") or 6000)
    body = detail["body"]
    note = ""
    if len(body) > limit:
        note = f"\n…（正文共 {len(body)} 字，已截取前 {limit} 字）"
        body = body[:limit]
    header = (
        f"路径：{detail['rel_path']}\n标题：{detail['title'] or '(无标题)'}\n"
        f"状态：{detail['status']}\n字数：{detail['word_count']}\n"
        f"合同ID：{detail['contract_id'] or '(无)'}"
    )
    return _ok(f"已读取 {detail['rel_path']}（{detail['word_count']} 字）",
               f"{header}\n\n{body}{note}")


def tool_read_outline(args: dict, ctx: ToolContext) -> dict:
    which = str(args.get("which") or "大纲").strip()
    rel = OUTLINE_FILES.get(which)
    if rel is None:
        raise InvalidOperationError(f"which 只支持：{'/'.join(OUTLINE_FILES)}")
    content = _read_rel(ctx, rel).strip()
    if not content:
        return _ok(f"{rel} 为空", "（文件为空）")
    return _ok(f"已读取 {rel}", _clip(content))


def tool_read_setting(args: dict, ctx: ToolContext) -> dict:
    category = str(args.get("category") or "").strip()
    if not category:
        listing = "、".join(BASE_CATEGORIES.keys())
        return _ok("可用设定分类", f"分类：{listing}；调用时传 category=<分类名>")
    rel = BASE_CATEGORIES.get(category)
    if rel is None:
        raise InvalidOperationError(
            f"未知设定分类：{category}（可用：{'、'.join(BASE_CATEGORIES)}）"
        )
    content = _read_rel(ctx, rel).strip()
    return _ok(f"已读取 {rel}", _clip(content) or "（文件为空）")


def tool_read_character_state(args: dict, ctx: ToolContext) -> dict:
    content = _read_rel(ctx, STATE_FILE).strip()
    names = [str(name).strip() for name in (args.get("names") or []) if str(name).strip()]
    if names and content:
        blocks = _split_sections(content)
        hit = [block for block in blocks if any(name in block for name in names)]
        if hit:
            return _ok(f"角色状态（{'、'.join(names)}）", _clip("\n\n".join(hit)))
        return _ok(f"未匹配到 {'、'.join(names)}，返回全量", _clip(content))
    return _ok("角色状态卡", _clip(content) or "（文件为空）")


def _split_sections(text: str) -> list[str]:
    blocks: list[str] = []
    current: list[str] = []
    for line in text.splitlines():
        if line.startswith("#") and current:
            blocks.append("\n".join(current).strip())
            current = [line]
        else:
            current.append(line)
    if current:
        blocks.append("\n".join(current).strip())
    return [block for block in blocks if block]


def tool_list_foreshadows(args: dict, ctx: ToolContext) -> dict:
    content = _read_rel(ctx, FORESHADOW_FILE).strip()
    if not content:
        return _ok("伏笔台账为空", "（文件为空）")
    status = str(args.get("status") or "").strip()
    if status:
        hit = [line for line in content.splitlines() if status in line]
        if hit:
            return _ok(f"伏笔台账（含「{status}」）", _clip("\n".join(hit)))
        return _ok(f"台账中没有含「{status}」的条目，返回全量", _clip(content))
    return _ok("伏笔台账", _clip(content))


def tool_search_project(args: dict, ctx: ToolContext) -> dict:
    from . import index_service

    query = str(args.get("query") or "").strip()
    if not query:
        raise InvalidOperationError("query 不能为空")
    limit = max(1, min(int(args.get("limit") or 6), 20))
    hits = index_service.search(ctx.project_id, query, limit=limit)  # type: ignore[arg-type]
    if not hits:
        return _ok(f"「{query}」无命中", "（没有检索到相关材料）")
    lines = [
        f"- {item['rel_path']}（{item.get('kind', '')}）：{str(item.get('snippet') or '')[:200]}"
        for item in hits
    ]
    return _ok(f"「{query}」命中 {len(hits)} 处", "\n".join(lines))


def tool_run_gates(args: dict, ctx: ToolContext) -> dict:
    text = str(args.get("text") or "")
    label = "传入文本"
    if not text.strip():
        rel = str(args.get("rel_path") or ctx.chapter_rel or "").strip()
        if not rel:
            raise InvalidOperationError("需要 rel_path 或 text 之一")
        text = _read_rel(ctx, rel)
        label = rel
    result = review_service.run_hard_gates(text)
    blocked = "、".join(result["blocking_gates"]) or "无"
    payload = {
        "passed": result["passed"],
        "words": result["words"],
        "blocking_gates": result["blocking_gates"],
        "locations": result["locations"][:10],
        "gates": [
            {"key": gate["key"], "passed": gate["passed"], "detail": gate["detail"]}
            for gate in result["gates"]
        ],
    }
    return _ok(
        f"{label} 门禁{'通过' if result['passed'] else '未通过'}（阻断项：{blocked}）",
        _clip(json.dumps(payload, ensure_ascii=False, indent=2)),
    )


def tool_list_teardown(args: dict, ctx: ToolContext) -> dict:
    from . import teardown_service

    limit = max(1, min(int(args.get("limit") or 3), 10))
    target = str(args.get("target") or "").strip()
    try:
        if target:
            facts = teardown_service.list_facts(ctx.project_id, target)[: limit * 4]
            if not facts:
                return _ok(f"{target} 暂无事实卡", "（无资产）")
            lines = [
                f"- [{item.get('dimension', '')}] {item.get('claim', '')}"
                f"（依据：{item.get('chapter_ref', '')}）"
                for item in facts
            ]
            return _ok(f"{target} 事实卡 {len(lines)} 条", "\n".join(lines))
        block = teardown_service.recall_block(
            ctx.project_id, genre=str(args.get("genre") or ""), limit=limit  # type: ignore[arg-type]
        )
    except Exception as exc:  # noqa: BLE001 - 无资产/索引缺失不阻断对话
        return _ok("拆书资产暂不可用", f"（{exc}）")
    if not str(block or "").strip():
        return _ok("暂无拆书资产", "（没有可召回的对标材料）")
    return _ok("拆书资产召回", _clip(str(block)))


# ─────────────────────────── 写工具 ───────────────────────────


def _create_write_proposal(
    ctx: ToolContext,
    *,
    kind: str,
    title: str,
    target_path: str,
    content: str,
    meta: dict | None = None,
) -> dict:
    proposal = proposal_service.create_proposal(
        project_id=ctx.project_id,
        kind=kind,
        title=title,
        target_path=target_path,
        content=content,
        task_id=ctx.task_id,
        meta={"from": "chat-tool", "session_id": ctx.session_id, **(meta or {})},
    )
    added = sum(1 for item in proposal.get("diff", []) if item["type"] == "add")
    removed = sum(1 for item in proposal.get("diff", []) if item["type"] == "remove")
    ctx.proposals.append(int(proposal["id"]))
    if ctx.auto_apply:
        applied = proposal_service.apply_proposal(int(proposal["id"]))
        detail = applied.get("meta") or {}
        extra = f"，{detail['word_count']} 字" if detail.get("word_count") else ""
        operation_log.log(ctx.project_id, "chat-tool-apply", target_path,
                          {"proposal_id": proposal["id"], "session_id": ctx.session_id})
        return _ok(
            f"已直接落盘 {target_path}（+{added}/-{removed} 行{extra}，写前已存快照）",
            f"提案 #{proposal['id']} 已应用；可在快照中回滚。",
            proposal_ids=[int(proposal["id"])],
            applied=True,
        )
    return _ok(
        f"已生成待确认草稿 → {target_path}（+{added}/-{removed} 行），请在收件箱确认",
        f"提案 #{proposal['id']} 待处理：应用前会展示差异，未落盘。",
        proposal_ids=[int(proposal["id"])],
        applied=False,
    )


def tool_propose_chapter_edit(args: dict, ctx: ToolContext) -> dict:
    rel = _validate_write_rel(ctx, str(args.get("rel_path") or ctx.chapter_rel or ""))
    content = _validate_content(args.get("content"))
    detail = chapter_service.read_chapter(ctx.project_id, rel)  # type: ignore[arg-type]
    note = str(args.get("note") or "").strip()
    title = f"对话改写：{detail['title'] or rel}"[:40]
    return _create_write_proposal(
        ctx, kind="chapter_draft", title=title, target_path=rel, content=content,
        meta={"chapter": rel, "note": note},
    )


def tool_propose_new_chapter(args: dict, ctx: ToolContext) -> dict:
    content = _validate_content(args.get("content"))
    _row, project_dir = _require_project(ctx)
    number = chapter_service.next_chapter_number(project_dir)
    filename = chapter_service.chapter_filename(number)
    rel = _validate_write_rel(ctx, f"{chapter_service.CHAPTER_DIR}/{filename}")
    chapter_title = str(args.get("title") or f"第{number}章").strip()
    return _create_write_proposal(
        ctx, kind="chapter_draft", title=f"新建章节：{chapter_title}"[:40],
        target_path=rel, content=content, meta={"chapter": rel, "new": True, "title": chapter_title},
    )


def tool_propose_setting_edit(args: dict, ctx: ToolContext) -> dict:
    category = str(args.get("category") or "").strip()
    rel = BASE_CATEGORIES.get(category)
    if rel is None:
        raise InvalidOperationError(
            f"未知设定分类：{category}（可用：{'、'.join(BASE_CATEGORIES)}）"
        )
    content = _validate_content(args.get("content"))
    rel = _validate_write_rel(ctx, rel)
    append = bool(args.get("append"))
    return _create_write_proposal(
        ctx, kind="setting", title=f"设定更新：{category}"[:40], target_path=rel,
        content=content, meta={"category": category, "append": append},
    )


def tool_propose_outline_edit(args: dict, ctx: ToolContext) -> dict:
    which = str(args.get("which") or "大纲").strip()
    rel = OUTLINE_FILES.get(which)
    if rel is None:
        raise InvalidOperationError(f"which 只支持：{'/'.join(OUTLINE_FILES)}")
    content = _validate_content(args.get("content"))
    rel = _validate_write_rel(ctx, rel)
    return _create_write_proposal(
        ctx, kind="outline", title=f"大纲更新：{which}"[:40], target_path=rel,
        content=content, meta={"which": which},
    )


def tool_propose_foreshadow_edit(args: dict, ctx: ToolContext) -> dict:
    content = _validate_content(args.get("content"))
    rel = _validate_write_rel(ctx, FORESHADOW_FILE)
    return _create_write_proposal(
        ctx, kind="foreshadow", title="伏笔台账更新"[:40], target_path=rel,
        content=content, meta={},
    )


def tool_propose_state_edit(args: dict, ctx: ToolContext) -> dict:
    content = _validate_content(args.get("content"))
    rel = _validate_write_rel(ctx, STATE_FILE)
    return _create_write_proposal(
        ctx, kind="state", title="角色状态更新"[:40], target_path=rel, content=content, meta={},
    )


# ─────────────────────────── 注册表 ───────────────────────────

TOOLS: tuple[dict, ...] = (
    {
        "name": "list_chapters",
        "label": "列出章节",
        "write": False,
        "args": {},
        "run": tool_list_chapters,
    },
    {
        "name": "read_chapter",
        "label": "读取章节",
        "write": False,
        "args": {"rel_path": "章节相对路径", "max_chars": "截取字数（默认 6000）"},
        "run": tool_read_chapter,
    },
    {
        "name": "read_outline",
        "label": "读大纲",
        "write": False,
        "args": {"which": "大纲 | 章纲"},
        "run": tool_read_outline,
    },
    {
        "name": "read_setting",
        "label": "读设定",
        "write": False,
        "args": {"category": "人物 | 世界 | 势力 | 物品 | 技能 | 场景 | 伏笔"},
        "run": tool_read_setting,
    },
    {
        "name": "read_character_state",
        "label": "读角色状态",
        "write": False,
        "args": {"names": "角色名数组（可选）"},
        "run": tool_read_character_state,
    },
    {
        "name": "list_foreshadows",
        "label": "查伏笔台账",
        "write": False,
        "args": {"status": "状态关键词（可选，如 未回收）"},
        "run": tool_list_foreshadows,
    },
    {
        "name": "search_project",
        "label": "项目内检索",
        "write": False,
        "args": {"query": "检索词", "limit": "条数（默认 6）"},
        "run": tool_search_project,
    },
    {
        "name": "run_gates",
        "label": "跑写作门禁",
        "write": False,
        "args": {"rel_path": "章节相对路径", "text": "直接送检的文本（与 rel_path 二选一）"},
        "run": tool_run_gates,
    },
    {
        "name": "list_teardown",
        "label": "召回拆书资产",
        "write": False,
        "args": {"target": "对标书名（可选）", "genre": "题材（可选）", "limit": "条数（默认 3）"},
        "run": tool_list_teardown,
    },
    {
        "name": "propose_chapter_edit",
        "label": "改写章节草稿",
        "write": True,
        "args": {"rel_path": "章节相对路径", "content": "改写后的完整正文", "note": "改动说明（可选）"},
        "run": tool_propose_chapter_edit,
    },
    {
        "name": "propose_new_chapter",
        "label": "新建章节草稿",
        "write": True,
        "args": {"title": "章节标题（可选）", "content": "新章正文"},
        "run": tool_propose_new_chapter,
    },
    {
        "name": "propose_setting_edit",
        "label": "改设定卡",
        "write": True,
        "args": {"category": "设定分类", "content": "新的设定内容", "append": "true=追加到文件末尾"},
        "run": tool_propose_setting_edit,
    },
    {
        "name": "propose_outline_edit",
        "label": "改大纲",
        "write": True,
        "args": {"which": "大纲 | 章纲", "content": "新的大纲内容"},
        "run": tool_propose_outline_edit,
    },
    {
        "name": "propose_foreshadow_edit",
        "label": "改伏笔台账",
        "write": True,
        "args": {"content": "新的台账内容"},
        "run": tool_propose_foreshadow_edit,
    },
    {
        "name": "propose_state_edit",
        "label": "改角色状态",
        "write": True,
        "args": {"content": "新的角色状态内容"},
        "run": tool_propose_state_edit,
    },
)

_BY_NAME: dict[str, dict] = {item["name"]: item for item in TOOLS}


def list_specs() -> list[dict]:
    """工具清单（供 API/前端展示与测试）。"""
    return [
        {"name": item["name"], "label": item["label"], "write": item["write"],
         "args": dict(item["args"])}
        for item in TOOLS
    ]


def describe_for_prompt() -> str:
    """生成给模型看的工具说明（拼进 working system）。"""
    lines: list[str] = []
    for item in TOOLS:
        args = ", ".join(f"{key}: {desc}" for key, desc in item["args"].items())
        tag = "写" if item["write"] else "读"
        lines.append(f"- {item['name']}({args})　[{tag}] {item['label']}")
    return "\n".join(lines)


def execute(name: str, args: dict, ctx: ToolContext) -> dict:
    """执行工具：未知工具/参数非法/执行异常一律返回 ``ok=False``（不抛到上层）。"""
    spec = _BY_NAME.get(str(name or "").strip())
    if spec is None:
        return _fail(f"未知工具 {name}（可用：{'、'.join(_BY_NAME)}）")
    payload = args if isinstance(args, dict) else {}
    func: Callable[[dict, ToolContext], dict] = spec["run"]
    try:
        result = func(payload, ctx)
    except Exception as exc:  # noqa: BLE001 - 工具失败回灌给模型，不中断整轮
        return _fail(str(exc))
    result.setdefault("tool", spec["name"])
    result.setdefault("label", spec["label"])
    result.setdefault("write", spec["write"])
    return result


__all__ = [
    "BASE_CATEGORIES",
    "FORESHADOW_FILE",
    "MAX_OBSERVATION_CHARS",
    "MAX_WRITE_CHARS",
    "OUTLINE_FILES",
    "STATE_FILE",
    "TOOLS",
    "ToolContext",
    "WRITE_DIRS",
    "describe_for_prompt",
    "execute",
    "list_specs",
]
