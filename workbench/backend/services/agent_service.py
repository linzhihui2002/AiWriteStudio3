"""Agent 定义与存储（Task 45 / 46 / 48）：声明式 YAML、内置保护、AI 辅助创建。

- 定义落盘 ``agents/<name>.yaml``（可版本化），字段：名称/描述/系统提示词/绑定技能/
  主责材料/能力声明/边界声明/模型/工具白名单/参数/``is_builtin``/版本；
- **内置 Agent（设定/规划/写作/审稿/连续性/拆书/创作助理/总编）标记 ``is_builtin``：UI 默认隐藏且不可删除**，
  可复制为自定义后再改；
- ``materials``/``capabilities``/``boundaries`` 为声明式能力注册表字段，由
  :func:`capability_index` 等模块级查询函数聚合，供意图路由与候选说明使用；
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
RETRIEVAL_PROFILES = ("history", "planning", "review", "setting", "continuity",
                      "foreshadow", "teardown", "off")

#: Agent 可声明的主责材料目录（空数组 = 不写书稿或未声明）
MATERIAL_DIRS = ("章节", "设定", "大纲", "状态", "备忘录")

#: 新会话默认 Agent，也是意图未命中时的兜底执行者
DEFAULT_AGENT = "writing-assistant"
READ_ONLY_AGENTS = frozenset({"reviewer", "teardown-analyst", "chief-editor", "writing-assistant"})

# ─────────────────────────── 内置 Agent（全部自写） ───────────────────────────

BUILTIN_AGENTS: dict[str, dict] = {
    "setting-keeper": {
        "name": "setting-keeper",
        "title": "设定（静态设定维护）",
        "description": "维护 设定/ 下的静态设定：世界观、势力、人物卡、物品与技能设定；只做设定。",
        "system_prompt": (
            "你是这部小说的设定维护者，只负责 设定/ 目录下的静态设定。\n"
            "做什么：\n"
            "1. 依据作者给的材料与已有 设定/ 文件，补全或修订世界观、势力、人物卡、物品、"
            "技能、场景等设定卡。\n"
            "2. 每条设定写清名称、归属、约束与已知边界；字段不全的设定卡先补齐字段再动内容。\n"
            "3. 改设定前先读同名文件，不覆盖作者已确认的设定。\n"
            "不做什么：\n"
            "- 不写大纲、章纲、分卷结构；\n"
            "- 不改 状态/ 下的角色状态、时间线与资源账本；\n"
            "- 不写章节正文；\n"
            "- 需要动上述材料时，只在回复里提示「建议另起一轮处理」，不擅自代做。\n"
            "输出契约：\n"
            "- 用写工具产出设定文件草稿（默认进收件箱待作者确认），不把整份设定贴在回复里；\n"
            "- 回复只写三件事：改了什么、依据是什么、下一步建议什么，简短直给。"
        ),
        "skills": ["novel-setting"],
        "tools": ["检索", "文件读写", "收件箱"],
        "materials": ["设定"],
        "retrieval_profile": "setting",
        "capabilities": [
            {
                "intent": "设定管理",
                "triggers": ["设定", "完善设定", "补设定", "世界设定", "人物设定", "世界观",
                             "势力", "物品", "技能设定", "场景", "设定卡"],
                "skill": "novel-setting",
                "task_type": "设定维护",
                "retrieval_profile": "setting",
            },
        ],
        "boundaries": "只维护 设定/ 下的静态设定；不写大纲/章纲、不改 状态/、不写章节正文。",
        "params": {"temperature": 0.4},
        "version": 4,
    },
    "planner": {
        "name": "planner",
        "title": "规划（大纲与章纲）",
        "description": "把灵感变成可执行的分卷结构与章纲：主线冲突、成长弧、卷末钩子。",
        "system_prompt": (
            "你是长篇小说的结构策划，只产出结构与章纲。\n"
            "做什么：\n"
            "1. 把作者给的灵感、已有设定与已写章节整理成可执行的分卷结构与章纲。\n"
            "2. 每个卷写明核心事件、主角目标与阻力、卷末钩子；每章一句话事件，"
            "章与章之间要有因果推进，不允许原地踏步。\n"
            "3. 卡文时先用追问把方向问清（最多 3 个问题），再给 2-3 条可选走向。\n"
            "不做什么：\n"
            "- 不写章节正文；\n"
            "- 不改 设定/ 下的设定卡；\n"
            "- 不改 状态/ 下的角色状态、时间线与资源账本；\n"
            "- 需要写正文或更新设定状态时，只在回复里提示建议另起一轮处理。\n"
            "输出契约：\n"
            "- 用写工具产出 大纲/ 下的结构与章纲草稿（默认进收件箱待作者确认）；\n"
            "- 回复只写：本次定了什么结构、依据哪些已有材料、下一步建议什么。"
        ),
        "skills": ["novel-planning"],
        "tools": ["检索", "文件读写", "收件箱"],
        "materials": ["大纲"],
        "retrieval_profile": "planning",
        "capabilities": [
            {
                "intent": "大纲规划",
                "triggers": ["大纲", "细纲", "卷纲", "章纲", "分卷", "剧情走向",
                             "接下来写什么", "章节规划"],
                "skill": "novel-planning",
                "task_type": "大纲生成",
                "retrieval_profile": "planning",
            },
            {
                "intent": "卡文追问",
                "triggers": ["卡文", "卡住", "写不下去", "没思路", "怎么接"],
                "skill": "novel-planning",
                "task_type": "灵感追问",
                "retrieval_profile": "planning",
            },
        ],
        "boundaries": "只产出结构与章纲；不写正文、不改设定与状态。",
        "params": {"temperature": 0.7},
        "version": 4,
    },
    "writer": {
        "name": "writer",
        "title": "写作（章节正文）",
        "description": "按章节合同写作正文，遵守去AI味硬约束，产物进收件箱不直接落盘。",
        "system_prompt": (
            "你是这部小说的写作者，只写 章节/ 下的正文。\n"
            "做什么：\n"
            "1. 按章节合同与章纲写作本章正文，只输出正文。\n"
            "2. 硬约束：不堆破折号；不写「不是…而是…」；不用「突然/忽然/仿佛/只见/缓缓」起句；"
            "对话要有信息差与个性；不用全知旁白替代动作细节；章末落在合同指定的钩子上。\n"
            "3. 正文为中文散文；写前先读本章合同与上一章结尾，保证衔接。\n"
            "不做什么：\n"
            "- 不改 设定/ 下的设定卡；\n"
            "- 不改 大纲/ 下的结构与章纲；\n"
            "- 不改 状态/ 下的角色状态与追踪台账；\n"
            "- 需要改上述材料时，只在回复里提示建议另起一轮处理。\n"
            "输出契约：\n"
            "- 用写工具把正文落到 章节/ 草稿（默认进收件箱待作者确认）；\n"
            "- 不把整篇正文贴在回复里；回复只写：写了哪一章、字数、下一步建议什么。"
        ),
        "skills": ["novel-writing", "human-linguistics"],
        "tools": ["检索", "文件读写", "收件箱"],
        "materials": ["章节"],
        "retrieval_profile": "history",
        "capabilities": [
            {
                "intent": "章节写作",
                "triggers": ["写这章", "写一章", "续写", "写正文", "接着写", "往下写",
                             "写下一章", "新建章节"],
                "skill": "novel-writing",
                "task_type": "章节正文",
                "retrieval_profile": "history",
            },
        ],
        "boundaries": "只写 章节/ 正文；不改设定、大纲与状态。",
        "params": {"temperature": 0.85},
        "version": 4,
    },
    "reviewer": {
        "name": "reviewer",
        "title": "审稿（逐项验收）",
        "description": "对照合同逐项验收，待核实不算通过；只报告不改稿。",
        "system_prompt": (
            "你是严格的中文小说审稿编辑，只出报告。\n"
            "做什么：\n"
            "1. 逐项对照章节合同给结论：已完成 / 未完成 / 待核实，并引用正文证据（≤40 字）。\n"
            "2. 找不到证据一律记「待核实」，不得凭感觉放行。\n"
            "3. 去 AI 味相关要求单独成节：列出 AI 腔、三连排比、推测词等具体位置与改法建议。\n"
            "不做什么：\n"
            "- 只报告不改稿，不写任何书稿文件；\n"
            "- 不改 章节/、设定/、大纲/、状态/ 下任何文件；\n"
            "- 不给没有证据支撑的「可以直接发」结论。\n"
            "输出契约：\n"
            "- 报告按「结论 → 证据 → 待核实 → 建议」分节给出；\n"
            "- 每项结论必须附正文证据，或明确标注无证据待核实。"
        ),
        "skills": ["novel-review", "human-linguistics"],
        "tools": ["检索", "审稿门禁"],
        "materials": [],
        "retrieval_profile": "review",
        "capabilities": [
            {
                "intent": "章节审稿",
                "triggers": ["审稿", "审这章", "审一下", "验收", "挑毛病", "这章行不行",
                             "能发吗", "能不能发", "看看这章", "检查这章", "有没有问题"],
                "skill": "novel-review",
                "task_type": "审稿",
                "retrieval_profile": "review",
            },
            {
                "intent": "去AI味",
                "triggers": ["去ai味", "去味", "太ai", "没人味", "像ai写的", "ai腔", "太工整"],
                "skill": "human-linguistics",
                "task_type": "去AI味软审",
                "retrieval_profile": "off",
            },
        ],
        "boundaries": "只报告不改稿；不写任何书稿文件。",
        "params": {"temperature": 0.1},
        "version": 4,
    },
    "context-keeper": {
        "name": "context-keeper",
        "title": "连续性（状态与追踪）",
        "description": "维护角色状态、时间线与追踪四件套，保证前后章一致。",
        "system_prompt": (
            "你是这部长篇的连续性维护者，负责 状态/ 与设定一致性；也可按作者明确要求记录备忘录。\n"
            "做什么：\n"
            "1. 从正文中有依据的信息里抽取角色状态、时间线、伏笔台账与资源账本，"
            "维护 状态/ 下的追踪文件。\n"
            "2. 只记录正文中有依据的信息，每条都附原文依据（章节号 + 短引文）。\n"
            "3. 冲突的地方显式指出而不是抹平；按既有基线与弧光核对 OOC 风险。\n"
            "不做什么：\n"
            "- 不改 章节/ 正文；\n"
            "- 不改 大纲/ 下的结构与章纲；\n"
            "- 不臆造正文里没有依据的状态变更。\n"
            "输出契约：\n"
            "- 用写工具更新 状态/ 下的追踪文件（默认进收件箱待作者确认）；\n"
            "- 回复只写：抽取了哪些条目、依据在哪、哪些冲突待作者裁定。"
        ),
        "skills": ["novel-setting", "character-arc"],
        "tools": ["检索", "文件读写", "收件箱"],
        "materials": ["状态", "设定"],
        "retrieval_profile": "continuity",
        "capabilities": [
            {
                "intent": "便签记录", "triggers": ["备忘录", "便签", "记下灵感"],
                "skill": "human-linguistics", "task_type": "便签记录", "retrieval_profile": "off",
            },
            {
                "intent": "状态维护",
                "triggers": ["角色状态", "时间线", "资源账本", "状态同步", "追踪四件套"],
                "skill": "novel-setting",
                "task_type": "结构化抽取",
                "retrieval_profile": "continuity",
            },
            {
                "intent": "伏笔检查",
                "triggers": ["伏笔", "埋的线", "回收", "坑没填", "坑忘了"],
                "skill": "foreshadow-check",
                "task_type": "伏笔检查",
                "retrieval_profile": "foreshadow",
            },
            {
                "intent": "角色成长检查",
                "triggers": ["角色成长", "人物弧光", "ooc", "人设崩", "性格转变", "关系变化"],
                "skill": "character-arc",
                "task_type": "一致性检查",
                "retrieval_profile": "continuity",
            },
        ],
        "boundaries": "状态只记录正文中有依据的信息并标注来源；作者明确要求时可记录备忘录；不改章节正文与大纲。",
        "params": {"temperature": 0.2},
        "version": 5,
    },
    "teardown-analyst": {
        "name": "teardown-analyst",
        "title": "拆书对标",
        "description": "按六维拆解参考作品并挂章节依据，产出可复用的对标资产与结论。",
        "system_prompt": (
            "你是拆书对标分析师，只产出对标结论与资产。\n"
            "做什么：\n"
            "1. 按六维（结构、节奏、人物、信息、情绪、爽点）拆解指定参考作品，"
            "每条结论必须挂章节依据（章号 + 事实卡）。\n"
            "2. 事实卡与结论分开列；找不到原文依据的结论标注「待核实」，不凭印象下判断。\n"
            "3. 输出可复用的对标资产，供本书写作时召回。\n"
            "不做什么：\n"
            "- 不写本书的书稿，章节正文、设定、大纲、状态一律不动；\n"
            "- 不改参考作品原文；\n"
            "- 不把参考作品内容原样复制进本书。\n"
            "输出契约：\n"
            "- 用写工具产出拆书资产与结论（默认进收件箱待作者确认）；\n"
            "- 每条结论附章节依据；回复只写：拆了什么、结论几条、下一步建议什么。"
        ),
        "skills": ["novel-teardown"],
        "tools": ["检索", "文件读写"],
        "materials": [],
        "retrieval_profile": "teardown",
        "capabilities": [
            {
                "intent": "拆书对标",
                "triggers": ["拆书", "拆解", "对标", "爆款分析", "这本书怎么写的"],
                "skill": "novel-teardown",
                "task_type": "拆书",
                "retrieval_profile": "teardown",
            },
        ],
        "boundaries": "只产出对标资产与结论；不写本书书稿。",
        "params": {"temperature": 0.5},
        "version": 5,
    },
    "writing-assistant": {
        "name": "writing-assistant",
        "title": "创作助理（只读咨询）",
        "description": "未确定任务时提供只读咨询与澄清；明确的创作任务交给具有对应材料写域的角色。",
        "system_prompt": (
            "你是这部小说的只读创作助理。读取相关材料，回答作者问题，提供具体建议。\n"
            "任务对象不明确时，用 ask_user_question 提出最少的必要问题。\n"
            "不能新建、修改、移动、删除或撤回任何文件，包括备忘录；不能用口头声明扩大权限。\n"
            "明确修改任务需由具有对应材料写域的执行角色处理。未保存的建议须说明尚未落盘。"
        ),
        "skills": ["human-linguistics"],
        "tools": ["检索"],
        "materials": [],
        "retrieval_profile": "history",
        "capabilities": [],
        "boundaries": (
            "仅只读咨询与澄清，不修改任何书稿或备忘录，不执行撤回。"
        ),
        "params": {"temperature": 0.6},
        "version": 5,
    },
    "chief-editor": {
        "name": "chief-editor",
        "title": "总编（路由咨询，不执行）",
        "description": "只做意图判断与路由咨询，给出建议处理者与依据；不执行、不改稿。",
        "system_prompt": (
            "你是这部小说的总编，只做意图判断与咨询。\n"
            "做什么：\n"
            "1. 判断作者这次的请求属于哪一类工作，给出建议的处理者与依据。\n"
            "2. 输出格式：第一行写「路由 → <建议处理者>（依据：<短句>）」，"
            "其后一句话说明建议怎么做。\n"
            "3. 信息不足时列出 2-3 个候选让作者选，不替作者做决定。\n"
            "不做什么：\n"
            "- 绝不改稿，不动 章节/、设定/、大纲/、状态/、备忘录/ 下任何文件；\n"
            "- 不代替执行者产出正文、设定或大纲；\n"
            "- 不使用任何写工具。\n"
            "输出契约：\n"
            "- 只输出「路由结论 + 依据 + 建议」，不产出任何稿件内容。"
        ),
        "skills": ["novel-review", "human-linguistics"],
        "tools": ["检索"],
        "materials": [],
        "retrieval_profile": "history",
        "capabilities": [],
        "boundaries": "只做意图判断与建议，绝不改稿。",
        "params": {"temperature": 0.2},
        "version": 4,
    },
}


def agents_root() -> Path:
    return Path(config.agents_dir())


def _builtin_upgraded(builtin: dict, on_disk: dict) -> dict:
    """把磁盘上的旧内置定义升级为新内置定义，保留作者改过的模型与温度。

    新内置定义整体覆盖 ``title/description/system_prompt/skills/tools/materials/
    capabilities/boundaries/version``；``provider_id/model_id/enabled/params`` 沿用磁盘值。
    """
    upgraded = {**builtin, "is_builtin": True}
    for field in ("provider_id", "model_id", "enabled"):
        if field in on_disk:
            upgraded[field] = on_disk[field]
    if isinstance(on_disk.get("params"), dict):
        upgraded["params"] = on_disk["params"]
    return upgraded


def ensure_builtin_agents() -> list[dict]:
    """确保内置 Agent 定义就绪（幂等）。

    磁盘不存在即写入内置定义；磁盘上仍是内置且版本低于内置版本时，
    只重写 ``title/description/system_prompt/skills/tools/materials/capabilities/
    boundaries/version``，``provider_id/model_id/enabled/params`` 沿用磁盘值
    （保留作者改过的模型与温度）；``is_builtin`` 为假的同名文件（作者自建）绝不覆盖。
    """
    root = agents_root()
    root.mkdir(parents=True, exist_ok=True)
    created: list[dict] = []
    for name, definition in BUILTIN_AGENTS.items():
        path = root / f"{name}.yaml"
        builtin = {**definition, "is_builtin": True}
        if not path.exists():
            atomic_write_text(path, _dump(builtin))
            _register(builtin)
        else:
            on_disk = _load(path)
            if (on_disk.get("is_builtin")
                    and int(on_disk.get("version") or 1) < int(builtin.get("version") or 1)):
                upgraded = _builtin_upgraded(builtin, on_disk)
                atomic_write_text(path, _dump(upgraded))
                _register(upgraded)
                created.append({"name": name, "path": str(path), "upgraded": True})
                continue
            _register(builtin)
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
        "materials": list(definition.get("materials") or []),
        "capabilities": list(definition.get("capabilities") or []),
        "retrieval_profile": definition.get("retrieval_profile", "history"),
        "boundaries": definition.get("boundaries", ""),
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


# ─────────────────────── 声明式能力注册表（路由唯一来源） ───────────────────────


def capability_index() -> list[dict]:
    """把全部 Agent 的 ``capabilities`` 摊平成关键词路由用的意图索引。

    返回 ``[{intent, agent, triggers, skill, task_type}]``，顺序稳定
    （按 Agent 名、再按声明顺序）；这是关键词路由的**唯一来源**，
    不得在别处硬编码意图表。
    """
    index: list[dict] = []
    for agent in sorted(list_all_agents(), key=lambda item: item["name"]):
        for capability in agent.get("capabilities") or []:
            index.append({
                "intent": capability.get("intent", ""),
                "agent": agent["name"],
                "triggers": list(capability.get("triggers") or []),
                "skill": capability.get("skill", ""),
                "task_type": capability.get("task_type", ""),
                "retrieval_profile": capability.get("retrieval_profile", agent.get("retrieval_profile", "history")),
            })
    return index


def agents_by_material(material: str) -> list[str]:
    """主责材料含该目录的 Agent 名（按名称排序）。"""
    return sorted(
        agent["name"] for agent in list_all_agents()
        if material in (agent.get("materials") or [])
    )


def retrieval_profile_of(agent: dict, *, intent: str = "") -> str:
    """检索场景只读声明；未命中/自定义 Agent 保守使用本书历史。"""
    for capability in agent.get("capabilities") or []:
        if capability.get("intent") == intent:
            profile = capability.get("retrieval_profile", agent.get("retrieval_profile", "history"))
            return profile if profile in RETRIEVAL_PROFILES else "history"
    profile = agent.get("retrieval_profile", "history")
    return profile if profile in RETRIEVAL_PROFILES else "history"


def retrieval_profile_for_task(task_type: str, *, skill: str = "") -> str:
    """直接生成入口按注册表任务声明选场景，不另建意图/触发词表。"""
    for capability in capability_index():
        if capability.get("task_type") == task_type or (skill and capability.get("skill") == skill):
            profile = capability.get("retrieval_profile", "history")
            return profile if profile in RETRIEVAL_PROFILES else "history"
    return "history"


def describe_candidates() -> list[dict]:
    """路由候选清单（供 LLM 意图判定作为候选说明）。"""
    return [
        {
            "name": agent["name"],
            "title": agent["title"],
            "description": agent["description"],
            "materials": list(agent.get("materials") or []),
            "capabilities": list(agent.get("capabilities") or []),
            "boundaries": agent.get("boundaries", ""),
        }
        for agent in sorted(list_all_agents(), key=lambda item: item["name"])
    ]


def compose_system_prompt(agent: dict) -> str:
    """把 ``system_prompt`` 与「不做什么」边界声明拼成最终提示词（对话编排调用）。"""
    base = (agent.get("system_prompt") or "").strip()
    boundaries = (agent.get("boundaries") or "").strip()
    if not boundaries:
        return base
    block = f"边界（不做什么）：{boundaries}"
    return f"{base}\n\n{block}" if base else block


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
    materials: list[str] | None = None,
    capabilities: list[dict] | None = None,
    boundaries: str = "",
    retrieval_profile: str = "history",
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
    _validate_materials(materials or [])
    _validate_capabilities(capabilities or [])
    if retrieval_profile not in RETRIEVAL_PROFILES:
        raise InvalidOperationError("retrieval_profile 无效")

    definition = {
        "name": name,
        "title": title or name,
        "description": description.strip(),
        "system_prompt": system_prompt.strip(),
        "skills": list(skills or []),
        "materials": list(materials or []),
        "capabilities": [dict(item) for item in (capabilities or [])],
        "retrieval_profile": retrieval_profile,
        "boundaries": (boundaries or "").strip(),
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
               "model_id", "tools", "params", "enabled",
               "materials", "capabilities", "boundaries", "retrieval_profile"}
    definition = {key: value for key, value in current.items()
                  if key not in ("path",)}
    for key, value in (patch or {}).items():
        if key in allowed:
            definition[key] = value
    if "skills" in patch:
        _validate_skills(definition["skills"])
    if "tools" in patch:
        _validate_tools(definition["tools"])
    if "materials" in patch:
        _validate_materials(definition["materials"] or [])
    if "capabilities" in patch:
        _validate_capabilities(definition["capabilities"] or [])
    if definition.get("retrieval_profile", "history") not in RETRIEVAL_PROFILES:
        raise InvalidOperationError("retrieval_profile 无效")

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
        materials=source["materials"],
        capabilities=source["capabilities"],
        boundaries=source["boundaries"],
        retrieval_profile=source.get("retrieval_profile", "history"),
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


def _validate_materials(materials: list[str]) -> None:
    unknown = [item for item in materials if item not in MATERIAL_DIRS]
    if unknown:
        raise InvalidOperationError(
            f"主责材料仅支持：{'、'.join(MATERIAL_DIRS)}；未知：{'、'.join(unknown)}"
        )


def _validate_capabilities(capabilities: list[dict]) -> None:
    """能力声明必须是 ``{intent, triggers, skill, task_type}``，intent 非空、triggers 非空。"""
    for index, capability in enumerate(capabilities, start=1):
        if not isinstance(capability, dict):
            raise InvalidOperationError(f"第 {index} 条能力声明必须是对象")
        intent = str(capability.get("intent") or "").strip()
        if not intent:
            raise InvalidOperationError(f"第 {index} 条能力声明缺少 intent")
        if capability.get("retrieval_profile", "history") not in RETRIEVAL_PROFILES:
            raise InvalidOperationError(f"第 {index} 条能力声明的 retrieval_profile 无效")
        triggers = capability.get("triggers")
        if not isinstance(triggers, list) or not triggers \
                or not all(isinstance(item, str) and item.strip() for item in triggers):
            raise InvalidOperationError(f"第 {index} 条能力声明的 triggers 必须是非空字符串列表")


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
        '  "materials": ["章节/设定/大纲/状态/备忘录 中该 Agent 主责的目录"],\n'
        '  "capabilities": [{"intent": "意图名", "triggers": ["触发词"],'
        ' "skill": "绑定技能或空串", "task_type": "任务类型"}],\n'
        '  "boundaries": "一行中文：这个 Agent 不做什么",\n'
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
        "materials": [str(item) for item in (parsed.get("materials") or [])],
        "capabilities": [item for item in (parsed.get("capabilities") or [])
                         if isinstance(item, dict)],
        "boundaries": str(parsed.get("boundaries") or ""),
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
    "MATERIAL_DIRS",
    "TOOL_WHITELIST",
    "agents_by_material",
    "agents_root",
    "capability_index",
    "compose_system_prompt",
    "create_agent",
    "delete_agent",
    "describe_candidates",
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
