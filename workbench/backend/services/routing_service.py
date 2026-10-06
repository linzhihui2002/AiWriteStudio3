"""意图路由门面（Task 41 / FR-AWS-01）：识别作者意图 → 选 Agent 与技能 → 展示路由依据。

规则
- **两段式**：关键词快路径（触发词聚合自 Agent 能力注册表，零模型调用）→ 未命中或
  触发词并列时调 LLM 语义判定 → 判定失败/非法 JSON/未知角色名时**确定性回退**关键词结果；
- **意图表来源为 Agent 注册表**：:func:`describe` 与关键词打分都来自
  :func:`agent_service.capability_index`，本模块不再硬编码意图/触发词常量；
- **未命中时交默认执行者**（``agent_service.DEFAULT_AGENT``，即创作助理），同时给候选供作者改选；
- **绝不静默代写**：路由只决定「谁来处理」与「本轮可写哪些材料」，是否落盘仍走 Proposal 收件箱；
- 路由结果与依据回传前端展示，作者可一键改选（改选结果记入任务记录/操作日志）。

实现细节在 :mod:`services.intent_service`；本模块只保留 ``classify()``/``route()``/
``describe()`` 三个对外入口，保持既有调用方（``chat_run_service``/``chat_service``）不变即可工作。
"""

from __future__ import annotations

from . import agent_service, intent_service, operation_log


def classify(text: str) -> dict:
    """确定性意图识别（关键词快路径，**零模型调用**）；无命中时返回候选而非乱猜。"""
    return intent_service.classify(None, text)


def route(project_id: int | None, text: str, *, override_agent: str = "",
          record: bool = True, active_file: str = "", target_chapter: str = "",
          context_hint: str = "") -> dict:
    """路由决策：返回 Agent / 技能 / 任务类型 / 依据 / 候选 / 写域 / 计划（可回退）。"""
    routing = intent_service.classify(project_id, text, override_agent=override_agent,
        active_file=active_file, target_chapter=target_chapter, context_hint=context_hint)
    if record:
        operation_log.log(project_id, "route", None,
                          {"intent": routing.get("intent"), "agent": routing.get("agent"),
                           "basis": routing.get("basis"), "override": bool(override_agent),
                           "source": routing.get("source"),
                           "write_targets": routing.get("write_targets")})
    return routing


def describe() -> dict:
    """意图表（设置页/帮助展示），来源为 Agent 能力注册表。"""
    return {
        "intents": [
            {"intent": item["intent"], "agent": item["agent"], "skill": item["skill"],
             "task_type": item["task_type"], "triggers": list(item["triggers"]),
             "retrieval_profile": item.get("retrieval_profile", "history")}
            for item in agent_service.capability_index()
        ]
    }


__all__ = ["classify", "describe", "route"]
