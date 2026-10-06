"""对话工具智能体循环：模型请求工具 → 工作台执行 → 结果回灌 → 继续推理。

设计约束
--------
- **引擎无关**：每一步都是一次 ``generation_service.run_task``，dsh headless
  与 direct-api 都适用；不改 dsh 调用方式（仍经 ``dsh_paths`` + vendor）。
- **协议是文本标签**：dsh headless 无 JSON 输出模式（见 docs/compat-matrix.md B3），
  因此用 ``<tool_call>{"tool": ..., "args": {...}}</tool_call>`` 约束 + 工作台解析。
- **上下文包只注入第 1 步**：后续步骤的材料已经在工具观察里，重复注入会让
  token 成本随步数线性翻倍。
- **步数有上限**：``config.CHAT_MAX_TOOL_STEPS``；用尽后强制收尾，绝不无限循环。
- 工具失败/未知工具**不中断整轮**：把失败原因回灌，让模型自行纠正。
"""

from __future__ import annotations

import json
import re
import time
from typing import Iterator

from .. import config
from . import chat_tools, generation_service
from .chat_tools import ToolContext

#: 单轮对话允许的工具调用步数
MAX_TOOL_STEPS = int(getattr(config, "CHAT_MAX_TOOL_STEPS", 6))

#: 工具调用标签（无 JSON 模式 → 标签内裁 JSON）
TOOL_CALL_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.S)

INVALID_TOOL_NAME = "__invalid_json__"

FORCE_FINAL_HINT = "已达工具调用上限，请基于已获得的材料直接给出最终答复，不要再调用工具。"

TOOL_PROTOCOL = """【可用工具】
{tools}

【调用方式】需要工具时，只输出下面这一行（不要加任何其他内容）：
<tool_call>{{"tool": "read_chapter", "args": {{"rel_path": "章节/第0012章.txt"}}}}</tool_call>
一次只调用一个工具；看到【工具结果】后再决定下一步。

【结束方式】信息足够时不要再输出 <tool_call>，直接给出最终答复（中文，不要复述工具结果）。
能直接办的事就直接办；不要只宣布计划、只列候选或只描述你的职责。

【写作要求】正文改写/续写/新建交给写工具产出草稿，不要把整章正文贴在回复里；
写工具默认只生成待确认草稿（除非会话已开启「允许直接落盘」）。"""


def tool_protocol_text() -> str:
    """拼装给模型看的工具协议说明（放在 Agent 人格 + 规则摘要之后）。"""
    return TOOL_PROTOCOL.format(tools=chat_tools.describe_for_prompt())


def parse_tool_call(text: str) -> tuple[str, dict] | None:
    """解析模型回复中的工具调用；无标签返回 None，标签内 JSON 坏掉返回占位名字。"""
    match = TOOL_CALL_RE.search(str(text or ""))
    if match is None:
        return None
    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError:
        return INVALID_TOOL_NAME, {}
    if not isinstance(payload, dict):
        return INVALID_TOOL_NAME, {}
    name = str(payload.get("tool") or "").strip()
    args = payload.get("args")
    return name or INVALID_TOOL_NAME, args if isinstance(args, dict) else {}


def iter_round(
    *,
    project_id: int | None,
    task_type: str,
    system: str,
    context_messages: list[dict],
    history: list[dict],
    user_text: str,
    ctx: ToolContext,
    provider: str = "",
    model: str = "",
    agent: str = "",
    should_cancel=None,
) -> Iterator[dict]:
    """跑一轮对话（含最多 ``MAX_TOOL_STEPS`` 次工具调用）。

    产出事件：
    - ``{"event": "step", ...}``：工具调用开始/结束（前端渲染步骤时间线）；
    - ``{"event": "final", ...}``：本轮结束（最终答复、步骤记录、task_ids、错误）。
    """
    messages: list[dict] = [*context_messages, *history, {"role": "user", "content": user_text}]
    steps: list[dict] = []
    task_ids: list[int] = []
    step_index = 0
    forced = False
    final_text = ""
    error_code: str | None = None
    error_message: str | None = None
    cancelled = False

    while True:
        if should_cancel is not None and should_cancel():
            cancelled = True
            break

        result = generation_service.run_task(
            project_id=project_id,
            task_type=task_type,
            system=system,
            messages=messages,
            engine="",
            provider=provider,
            model=model,
            agent=agent,
            context_snapshot={
                "chat_session": ctx.session_id,
                "round_step": step_index + 1,
                "tools": [item["tool"] for item in steps],
            },
            should_cancel=should_cancel,
        )
        task_ids.append(int(result["task_id"]))
        ctx.task_id = int(result["task_id"])

        if not result["ok"]:
            error_code = result.get("error_code")
            error_message = result.get("error_message")
            cancelled = bool(result.get("cancelled"))
            break

        reply = str(result.get("text") or "")
        call = parse_tool_call(reply)

        if call is not None and step_index < MAX_TOOL_STEPS:
            step_index += 1
            name, args = call
            label = _tool_label(name)
            yield {"event": "step", "index": step_index, "tool": name, "label": label,
                   "args": args, "status": "running"}
            started = time.time()
            outcome = chat_tools.execute(name, args, ctx)
            record = {
                "index": step_index,
                "tool": name,
                "label": str(outcome.get("label") or label),
                "args": args,
                "status": "done" if outcome.get("ok") else "error",
                "summary": str(outcome.get("summary") or ""),
                "elapsed_ms": int((time.time() - started) * 1000),
            }
            steps.append(record)
            yield {"event": "step", **record}
            observation = str(outcome.get("text") or "") or record["summary"]
            messages = [
                *messages,
                {"role": "assistant", "content": reply},
                {"role": "user", "content": f"【工具结果 {name}】\n{observation}"},
            ]
            continue

        if call is not None:
            if forced:
                # 模型始终只回工具调用：把这段回复当答复收尾，不再执行工具
                final_text = reply.strip()
                break
            forced = True
            messages = [
                *messages,
                {"role": "assistant", "content": reply},
                {"role": "user", "content": FORCE_FINAL_HINT},
            ]
            continue

        final_text = reply.strip()
        break

    yield {
        "event": "final",
        "text": final_text,
        "steps": steps,
        "proposal_ids": list(ctx.proposals),
        "task_ids": task_ids,
        "task_id": task_ids[-1] if task_ids else None,
        "ok": error_code is None and not cancelled,
        "error_code": error_code,
        "error_message": error_message,
        "cancelled": cancelled,
    }


def _tool_label(name: str) -> str:
    for spec in chat_tools.TOOLS:
        if spec["name"] == name:
            return str(spec["label"])
    return "工具"


__all__ = [
    "FORCE_FINAL_HINT",
    "INVALID_TOOL_NAME",
    "MAX_TOOL_STEPS",
    "TOOL_CALL_RE",
    "TOOL_PROTOCOL",
    "iter_round",
    "parse_tool_call",
    "tool_protocol_text",
]