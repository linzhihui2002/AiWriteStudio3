"""Agent 定义与存储（Task 45 / 46 / 48）：声明式 YAML、内置保护、AI 辅助创建。

- 定义落盘 ``agents/<name>.yaml``（可版本化），字段：名称/描述/系统提示词/绑定技能/
  模型/工具白名单/参数/``is_builtin``/版本；
- **内置 Agent（创作助理/总编路由/规划/写作/审稿/上下文）标记 ``is_builtin``：UI 默认隐藏且不可删除**，
  可复制为自定义后再改；
- 每个 Agent 可独立配模型（provider + model）；未配置时按 任务级 → 项目级 → 全局 回退
  （回退逻辑在 :mod:`engine.router`）。提示词**全部自写**（合规要求，不移植外部 Agent 文本）。
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from .. import config, db
from . import generation_service, skill_service
from .errors import InvalidNameError, InvalidOperationError, NodeNotFoundError
from .fs_utils import atomic_write_text, read_text

NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
TOOL_WHITELIST = ("检索", "文件读写", "收件箱", "审稿门禁", "向量检索", "无")

#: 新会话默认 Agent，也是意图未命中时的兜底执行者
DEFAULT_AGENT = "writing-assistant"

# ─────────────────────────── 内置 Agent（全部自写） ───────────────────────────

BUILTIN_AGENTS: dict[str, dict] = {
    "writing-assistant": {
        "name": "writing-assistant",
        "title": "创作助理（默认执行）",
        "description": "默认执行者：直接办作者交代的事并产出可落盘草稿；缺关键信息时先问最少的问题。",
        "system_prompt": (
            "你是这部小说的创作助理。作者交代什么，你就直接办；"
            "不要只做分类、只列候选或只描述职责。\n"
            "工作方式：\n"
            "1. 先判断这件事动的是哪份材料（大纲/章纲、设定、角色状态、伏笔台账、章节正文），"
            "需要时先读再改。\n"
            "2. 材料不足时先问不超过 3 个必须回答的问题；其余按最合理假设继续做，"
            "并在草稿里显式标出你的假设。\n"
            "3. 要落盘就调用写工具产出草稿（默认进收件箱待作者确认），"
            "不要把整篇正文贴在回复里。\n"
            "4. 回复只写三件事：你做了什么、你假设了什么、下一步建议什么，简短直给。"
        ),
        "skills": ["novel-planning", "novel-setting"],
        "tools": ["检索", "文件读写", "收件箱"],
        "params": {"temperature": 0.6},
    },
    "chief-editor": {
        "name": "chief-editor",
        "title": "总编（意图路由）",
        "description": "识别作者意图，路由到合适的技能或 Agent，并展示路由依据；不确定时给候选。",
        "system_prompt": (
            "你是这部小说的总编。你的唯一职责是判断作者这次的请求属于哪一类工作，"
            "并说明判断依据。可选工作：大纲规划、章节写作、审稿、去AI味、设定管理、"
            "拆书对标、伏笔检查、角色成长。\n"
            "输出格式：第一行写「路由 → <工作名>（依据：<短句>）」，其后一句话说明你将怎么做。\n"
            "若信息不足，列出 2-3 个候选让作者选，绝不替作者做决定，也绝不直接改稿。"
        ),
        "skills": ["novel-review", "human-linguistics"],
        "tools": ["检索", "收件箱"],
        "params": {"temperature": 0.2},
    },
    "planner": {
        "name": "planner",
        "title": "规划（大纲与章纲）",
        "description": "把灵感变成可执行的分卷结构与章纲：主线冲突、成长弧、卷末钩子。",
        "system_prompt": (
            "你是长篇小说的结构策划。产出结构，不写正文。\n"
            "要求：每个卷写明核心事件、主角目标与阻力、卷末钩子；每章一句话事件，"
            "章与章之间要有因果推进，不允许原地踏步。\n"
            "信息不足时先提问；不臆造作者没给的设定。"
        ),
        "skills": [],
        "tools": ["检索", "文件读写"],
        "params": {"temperature": 0.7},
    },
    "writer": {
        "name": "writer",
        "title": "写作（章节正文）",
        "description": "按章节合同写作正文，遵守去AI味硬约束，产物进收件箱不直接落盘。",
        "system_prompt": (
            "你是这部小说的写作者。按合同写正文，只输出正文。\n"
            "硬约束：不堆破折号；不写「不是…而是…」；不用「突然/忽然/仿佛/只见/缓缓」起句；"
            "对话要有信息差与个性；不用全知旁白替代动作细节；章末落在合同指定的钩子上。"
        ),
        "skills": ["human-linguistics"],
        "tools": ["检索", "文件读写"],
        "params": {"temperature": 0.85},
    },
    "reviewer": {
        "name": "reviewer",
        "title": "审稿（逐项验收）",
        "description": "对照合同逐项验收，待核实不算通过；只报告不改稿。",
        "system_prompt": (
            "你是严格的中文小说审稿编辑。逐项对照合同给结论：已完成 / 未完成 / 待核实，"
            "并引用正文证据（≤40 字）。找不到证据一律「待核实」，不得凭感觉放行。\n"
            "你只报告，不改稿。"
        ),
        "skills": ["novel-review"],
        "tools": ["检索", "审稿门禁"],
        "params": {"temperature": 0.1},
    },
    "context-keeper": {
        "name": "context-keeper",
        "title": "上下文（设定与状态）",
        "description": "维护 Story Bible、角色状态与追踪四件套，保证前后章一致。",
        "system_prompt": (
            "你负责这部长篇的设定一致性：整理角色状态、时间线、伏笔与资源账本。\n"
            "规则：只记录正文中有依据的信息，每条都附原文依据；冲突的地方显式指出而不是抹平；"
            "你不直接改正文。"
        ),
        "skills": [],
        "tools": ["检索", "文件读写", "收件箱"],
        "params": {"temperature": 0.2},
    },
}


def agents_root() -> Path:
    return Path(config.agents_dir())


def ensure_builtin_agents() -> list[dict]:
    """确保内置 Agent 定义就绪（幂等，不覆盖用户已有定义）。"""
    root = agents_root()
    root.mkdir(parents=True, exist_ok=True)
    created: list[dict] = []
    for name, definition in BUILTIN_AGENTS.items():
        path = root / f"{name}.yaml"
        if not path.exists():
            atomic_write_text(path, _dump({**definition, "is_builtin": True, "version": 1}))
        _register({**definition, "is_builtin": True})
        created.append({"name": name, "path": str(path)})
    return created


def _dump(definition: dict) -> str:
    return yaml.safe_dump(definition, allow_unicode=True, sort_keys=False)


def _load(path: Path) -> dict:
    try:
        data = yaml.safe_load(read_text(path))
    except (yaml.YAMLError, OSError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _register(definition: dict) -> None:
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO agents (name, description, system_prompt, skills, provider_id,"
            " model_id, tools, params, is_builtin, enabled, version)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(name) DO UPDATE SET"
            " description = excluded.description, system_prompt = excluded.system_prompt,"
            " skills = excluded.skills, provider_id = excluded.provider_id,"
            " model_id = excluded.model_id, tools = excluded.tools, params = excluded.params,"
            " is_builtin = excluded.is_builtin, version = excluded.version",
            (
                definition["name"],
                definition.get("description", ""),
                definition.get("system_prompt", ""),
                yaml.safe_dump(definition.get("skills") or [], allow_unicode=True),
                definition.get("provider_id", ""),
                definition.get("model_id", ""),
                yaml.safe_dump(definition.get("tools") or [], allow_unicode=True),
                yaml.safe_dump(definition.get("params") or {}, allow_unicode=True),
                1 if definition.get("is_builtin") else 0,
                1 if definition.get("enabled", True) else 0,
                int(definition.get("version") or 1),
            ),
        )


def _to_public(definition: dict) -> dict:
    return {
        "name": definition.get("name", ""),
        "title": definition.get("title", definition.get("name", "")),
        "description": definition.get("description", ""),
        "system_prompt": definition.get("system_prompt", ""),
        "skills": definition.get("skills") or [],
        "provider_id": definition.get("provider_id", ""),
        "model_id": definition.get("model_id", ""),
        "tools": definition.get("tools") or [],
        "params": definition.get("params") or {},
        "is_builtin": bool(definition.get("is_builtin")),
        "enabled": bool(definition.get("enabled", True)),
        "version": int(definition.get("version") or 1),
    }


# ─────────────────────────── 查询 ───────────────────────────


def list_agents(*, include_builtin: bool = False) -> list[dict]:
    """Agent 列表（默认**隐藏内置**，符合 UI 约定）。"""
    ensure_builtin_agents()
    items: list[dict] = []
    for path in sorted(agents_root().glob("*.yaml")):
        definition = _load(path)
        if not definition.get("name"):
            continue
        item = _to_public(definition)
        item["path"] = str(path)
        items.append(item)
    if not include_builtin:
        items = [item for item in items if not item["is_builtin"]]
    return items


def list_all_agents() -> list[dict]:
    return list_agents(include_builtin=True)


def get_agent(name: str) -> dict:
    path = agents_root() / f"{name}.yaml"
    if not path.is_file():
        raise NodeNotFoundError(f"Agent 不存在：{name}")
    definition = _load(path)
    return {**_to_public(definition), "path": str(path)}


def skills_of(name: str) -> list[str]:
    return list(get_agent(name)["skills"])


# ─────────────────────────── 增删改 ───────────────────────────


def create_agent(
    name: str,
    *,
    description: str = "",
    system_prompt: str = "",
    skills: list[str] | None = None,
    provider_id: str = "",
    model_id: str = "",
    tools: list[str] | None = None,
    params: dict | None = None,
    title: str = "",
) -> dict:
    """新建自定义 Agent。"""
    name = (name or "").strip()
    if not NAME_RE.match(name):
        raise InvalidNameError(f"Agent 名必须是 kebab-case（如 foreshadow-checker）：{name}")
    path = agents_root() / f"{name}.yaml"
    if path.exists():
        raise InvalidOperationError(f"同名 Agent 已存在：{name}")
    if not system_prompt.strip():
        raise InvalidOperationError("Agent 必须提供系统提示词")
    _validate_skills(skills or [])
    _validate_tools(tools or [])

    definition = {
        "name": name,
        "title": title or name,
        "description": description.strip(),
        "system_prompt": system_prompt.strip(),
        "skills": list(skills or []),
        "provider_id": provider_id,
        "model_id": model_id,
        "tools": list(tools or []),
        "params": dict(params or {"temperature": 0.7}),
        "is_builtin": False,
        "enabled": True,
        "version": 1,
    }
    agents_root().mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, _dump(definition))
    _register(definition)
    return get_agent(name)


def update_agent(name: str, patch: dict) -> dict:
    """更新 Agent 定义（内置 Agent 允许改参数，但不可改 ``is_builtin``）。"""
    current = get_agent(name)
    if current["is_builtin"] and patch.get("is_builtin") is False:
        raise InvalidOperationError("内置 Agent 的 is_builtin 标记不可修改")

    allowed = {"title", "description", "system_prompt", "skills", "provider_id",
               "model_id", "tools", "params", "enabled"}
    definition = {key: value for key, value in current.items()
                  if key not in ("path",)}
    for key, value in (patch or {}).items():
        if key in allowed:
            definition[key] = value
    if "skills" in patch:
        _validate_skills(definition["skills"])
    if "tools" in patch:
        _validate_tools(definition["tools"])

    definition["version"] = int(current["version"]) + 1
    atomic_write_text(agents_root() / f"{name}.yaml", _dump(definition))
    _register(definition)
    return get_agent(name)


def duplicate_agent(name: str, new_name: str) -> dict:
    """复制 Agent（内置 Agent 的推荐改法）。

    复制时过滤掉当前不存在的绑定技能（内置定义可能引用仓库中尚未创建的技能）。
    """
    source = get_agent(name)
    available = {item["name"] for item in skill_service.list_skills()}
    return create_agent(
        new_name,
        title=f"{source['title']}（副本）",
        description=source["description"],
        system_prompt=source["system_prompt"],
        skills=[item for item in source["skills"] if item in available],
        provider_id=source["provider_id"],
        model_id=source["model_id"],
        tools=source["tools"],
        params=source["params"],
    )


def delete_agent(name: str) -> dict:
    agent = get_agent(name)
    if agent["is_builtin"]:
        raise InvalidOperationError(
            f"「{name}」是内置 Agent，不可删除；可复制后修改（复制为自定义 Agent）。"
        )
    Path(agent["path"]).unlink(missing_ok=True)
    with db.get_conn() as conn:
        conn.execute("DELETE FROM agents WHERE name = ?", (name,))
    return {"name": name, "deleted": True}


def set_agent_model(name: str, provider_id: str, model_id: str) -> dict:
    """给 Agent 配模型（对话内切换也走这里留痕）。"""
    return update_agent(name, {"provider_id": provider_id, "model_id": model_id})


def _validate_skills(skills: list[str]) -> None:
    available = {item["name"] for item in skill_service.list_skills()}
    unknown = [item for item in skills if item not in available]
    if unknown:
        raise InvalidOperationError(
            f"绑定技能不存在：{'、'.join(unknown)}（可用：{'、'.join(sorted(available))}）"
        )


def _validate_tools(tools: list[str]) -> None:
    unknown = [item for item in tools if item not in TOOL_WHITELIST]
    if unknown:
        raise InvalidOperationError(
            f"工具白名单仅支持：{'、'.join(TOOL_WHITELIST)}；未知：{'、'.join(unknown)}"
        )


# ─────────────────────────── AI 辅助创建（Task 46） ───────────────────────────


def draft_agent(description: str, *, name: str = "") -> dict:
    """AI 辅助创建：按需求生成 Agent 定义草案（不落盘，确认后再保存）。"""
    prompt = (
        "你是工作台 Agent 定义助手。按用户需求产出一份 Agent 定义草案，只输出 JSON：\n"
        "{\n"
        '  "name": "kebab-case 英文名",\n'
        '  "title": "中文短名",\n'
        '  "description": "一句话职责",\n'
        '  "system_prompt": "系统提示词（写清做什么/不做什么/输出格式）",\n'
        '  "skills": ["可选绑定技能名"],\n'
        '  "tools": ["检索/文件读写/收件箱/审稿门禁/向量检索"],\n'
        '  "params": {"temperature": 0.5}\n'
        "}\n"
        "提示词要具体、可执行，不写空话；不确定的字段给保守默认值。"
    )
    result = generation_service.run_task(
        project_id=None,
        task_type="Agent 生成",
        system=prompt,
        messages=[{"role": "user", "content": f"需求：{description}\n建议名称：{name or '（自拟）'}"}],
        temperature=0.5,
    )
    if not result["ok"]:
        return {"ok": False, "error": result.get("error_message"), "draft": None}

    from .review_service import _parse_json

    parsed = _parse_json(result["text"]) or {}
    draft = {
        "name": str(parsed.get("name") or name or "custom-agent"),
        "title": str(parsed.get("title") or parsed.get("name") or "自定义 Agent"),
        "description": str(parsed.get("description") or description)[:200],
        "system_prompt": str(parsed.get("system_prompt") or ""),
        "skills": [str(item) for item in (parsed.get("skills") or [])],
        "tools": [str(item) for item in (parsed.get("tools") or [])],
        "params": parsed.get("params") if isinstance(parsed.get("params"), dict)
        else {"temperature": 0.5},
        "is_builtin": False,
    }
    return {"ok": True, "draft": draft, "task_id": result.get("task_id"),
            "note": "草案需作者确认后保存（POST /api/agents）"}


__all__ = [
    "BUILTIN_AGENTS",
    "DEFAULT_AGENT",
    "TOOL_WHITELIST",
    "agents_root",
    "create_agent",
    "delete_agent",
    "draft_agent",
    "duplicate_agent",
    "ensure_builtin_agents",
    "get_agent",
    "list_agents",
    "list_all_agents",
    "set_agent_model",
    "skills_of",
    "update_agent",
]