"""意图路由（Task 41 / FR-AWS-01）：识别作者意图 → 选 Agent 与技能 → 展示路由依据。

规则
- **未命中时交默认执行者**（``agent_service.DEFAULT_AGENT``，即创作助理），同时给候选供作者改选；
- **绝不静默代写**：路由只决定「谁来处理」，是否落盘仍走 Proposal 收件箱；
- 路由结果与依据回传前端展示，作者可一键改选（改选结果记入任务记录/操作日志）。
"""

from __future__ import annotations

from . import agent_service, operation_log

# 意图表（触发词 → Agent / 技能 / 任务类型）
INTENTS: tuple[dict, ...] = (
    {
        "intent": "章节审稿",
        "triggers": ("审稿", "审这章", "审一下", "验收", "挑毛病", "这章行不行", "能发吗",
                     "能不能发", "看看这章", "检查这章", "有没有问题"),
        "agent": "reviewer",
        "skill": "novel-review",
        "task_type": "审稿",
    },
    {
        "intent": "去AI味",
        "triggers": ("去ai味", "去味", "太ai", "没人味", "像ai写的", "ai腔", "太工整"),
        "agent": "reviewer",
        "skill": "human-linguistics",
        "task_type": "去AI味软审",
    },
    {
        "intent": "章节写作",
        "triggers": ("写这章", "写一章", "续写", "写正文", "接着写", "往下写"),
        "agent": "writer",
        "skill": "novel-writing",
        "task_type": "章节正文",
    },
    {
        "intent": "大纲规划",
        "triggers": ("大纲", "细纲", "卷纲", "章纲", "接下来写什么", "剧情走向", "分卷"),
        "agent": "planner",
        "skill": "novel-planning",
        "task_type": "大纲生成",
    },
    {
        "intent": "设定管理",
        "triggers": ("设定", "人物卡", "世界观", "角色状态", "时间线", "资源账本", "伏笔管理"),
        "agent": "context-keeper",
        "skill": "novel-setting",
        "task_type": "结构化抽取",
    },
    {
        "intent": "伏笔检查",
        "triggers": ("伏笔", "埋的线", "回收", "坑没填", "坑忘了"),
        "agent": "context-keeper",
        "skill": "foreshadow-check",
        "task_type": "伏笔检查",
    },
    {
        "intent": "角色成长检查",
        "triggers": ("角色成长", "人物弧光", "ooc", "人设崩", "性格转变", "关系变化"),
        "agent": "context-keeper",
        "skill": "character-arc",
        "task_type": "一致性检查",
    },
    {
        "intent": "拆书对标",
        "triggers": ("拆书", "拆解", "对标", "爆款分析", "这本书怎么写的"),
        "agent": "planner",
        "skill": "",
        "task_type": "拆书",
    },
    {
        "intent": "卡文追问",
        "triggers": ("卡文", "卡住", "写不下去", "没思路", "怎么接"),
        "agent": "planner",
        "skill": "novel-planning",
        "task_type": "灵感追问",
    },
)


def classify(text: str) -> dict:
    """确定性意图识别（关键词打分）；无命中时返回候选而非乱猜。"""
    lowered = (text or "").lower()
    scored: list[tuple[int, dict, list[str]]] = []
    for intent in INTENTS:
        hits = [trigger for trigger in intent["triggers"] if trigger in lowered]
        if hits:
            scored.append((len(hits), intent, hits))

    if not scored:
        return {
            "matched": False,
            "intent": "",
            "candidates": [
                {"intent": intent["intent"], "agent": intent["agent"],
                 "trigger": intent["triggers"][0]}
                for intent in INTENTS[:3]
            ],
            "basis": "未命中任何意图触发词，已交创作助理直接处理；也可手动指定 Agent",
        }

    scored.sort(key=lambda item: item[0], reverse=True)
    best_score, best, hits = scored[0]
    candidates = [
        {"intent": intent["intent"], "agent": intent["agent"], "trigger": hit_list[0]}
        for _score, intent, hit_list in scored[1:3]
    ]
    confidence = "high" if best_score >= 2 else "medium"
    return {
        "matched": True,
        "intent": best["intent"],
        "agent": best["agent"],
        "skill": best["skill"],
        "task_type": best["task_type"],
        "confidence": confidence,
        "hits": hits,
        "candidates": candidates,
        "basis": f"命中触发词「{'、'.join(hits)}」→ {best['intent']}",
    }


def route(project_id: int | None, text: str, *, override_agent: str = "",
          record: bool = True) -> dict:
    """路由决策：返回 Agent / 技能 / 任务类型 / 依据 / 候选（可回退）。"""
    result = classify(text)
    agent_name = override_agent or result.get("agent", "")

    # 先确保 Agent 定义就绪（内置 Agent 首次访问时落盘）
    try:
        available = [item["name"] for item in agent_service.list_all_agents()]
    except Exception:  # noqa: BLE001
        available = []

    agent_definition: dict | None = None
    if agent_name:
        try:
            agent_definition = agent_service.get_agent(agent_name)
        except Exception:  # noqa: BLE001 - Agent 缺失时降级到内置
            agent_definition = None

    if agent_definition is None and available:
        agent_name = (
            agent_service.DEFAULT_AGENT if agent_service.DEFAULT_AGENT in available
            else ("chief-editor" if "chief-editor" in available else available[0])
        )
        agent_definition = agent_service.get_agent(agent_name)

    routing = {
        **result,
        "agent": agent_name,
        "agent_title": (agent_definition or {}).get("title", ""),
        "agent_skills": (agent_definition or {}).get("skills", []),
        "override": bool(override_agent),
        "available_agents": available,
        "silent_write": False,  # 明确：路由不等于落盘
    }
    if record:
        operation_log.log(project_id, "route", None,
                          {"intent": routing.get("intent"), "agent": agent_name,
                           "basis": routing.get("basis"), "override": bool(override_agent)})
    return routing


def describe() -> dict:
    """意图表（设置页/帮助展示）。"""
    return {
        "intents": [
            {"intent": item["intent"], "agent": item["agent"], "skill": item["skill"],
             "task_type": item["task_type"], "triggers": list(item["triggers"])}
            for item in INTENTS
        ]
    }


__all__ = ["INTENTS", "classify", "describe", "route"]