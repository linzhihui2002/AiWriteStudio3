"""两段式意图路由：关键词快路径 + LLM 语义判定 + 确定性回退。

流程
----
1. **关键词快路径（永远先跑，零模型调用）**：触发词与意图名**只从 Agent 能力注册表**
   （:func:`agent_service.capability_index`）聚合，本模块不硬编码任何意图表；
   最高分 ≥1 且唯一最高时直接产出结果（``source="keyword"``），不打模型。
2. **LLM 语义判定（仅在满足条件时）**：仅当有 ``project_id``、设置 ``chat.intent_llm``
   未显式关闭、且判定引擎（``direct-api``）可用时调用；候选清单由注册表动态生成，
   要求结构化 JSON 输出。任何异常 / 非法 JSON / 未知角色名都吞掉并**确定性回退**。
3. **结果校验与归一（两条路径都要过）**：Agent 名必须在注册表内；材料只保留
   ``MATERIAL_DIRS`` 目录名或本书内确实存在的相对路径；未知计划步整步丢弃。

核心语义：**只读依据（``read_only_refs``）与产出材料（``write_targets``）严格分离**。
作者说「根据设定写大纲」时，设定是只读依据、大纲才是产出，不得顺手改设定。
"""

from __future__ import annotations

import re

from . import (
    agent_service,
    generation_service,
    project_service,
    settings_service,
    skill_service,
)
from .fs_utils import resolve_within

#: 意图判定的任务类型（引擎决策表未命中时按全局默认引擎回退）
INTENT_TASK_TYPE = "意图判定"
#: 判定引擎：廉价、结构化输出（dsh 语义任务不合适）
INTENT_ENGINE = "direct-api"
#: 判定超时（秒）
INTENT_TIMEOUT_SECONDS = 20
#: 多步计划最多步数
MAX_PLAN_STEPS = 3
#: 候选回传最多条数
CANDIDATE_LIMIT = 3

#: 采纳 LLM 判定的最低置信阈值。
#: 依据：与 :func:`_confidence_from_llm` 的「中置信」下界对齐（0.7 / 0.4 两档），
#: 低于 0.4 的判定按既有语义本就落 ``low`` 档；模型自己都给了低分时，
#: 丢弃判定、回退确定性关键词结果（关键词也未命中则交默认执行者）比采纳更安全。
LLM_MIN_CONFIDENCE = 0.4

#: 作者口语 → 材料目录名：关键词快路径的「显式材料扫描」词典。
#: 作者在消息里点名了哪类材料，本轮就应当把它算进写域（助理类只读角色除外）。
MATERIAL_HINTS: dict[str, tuple[str, ...]] = {
    "设定": ("设定", "世界设定", "人物设定", "世界观", "势力", "物品", "技能设定",
             "场景", "设定卡"),
    "大纲": ("大纲", "细纲", "卷纲", "章纲", "分卷", "剧情走向"),
    "状态": ("角色状态", "状态", "时间线", "资源账本", "追踪"),
    "章节": ("章节", "正文", "这一章", "这节", "下一章", "续写"),
    "备忘录": ("备忘录", "便签", "临时想法", "灵感"),
}

#: 每类材料的首选执行 Agent（须确实主责该材料，否则退回该材料的首个主责 Agent）
_MATERIAL_AGENT = {"章节": "writer", "设定": "setting-keeper",
                   "大纲": "planner", "状态": "context-keeper"}

#: 依据介词：材料词紧跟在它们之后（允许中间隔 0–2 个非材料字符）即为**只读依据**，不计产出。
_BASIS_PREPOSITIONS = ("根据", "按", "按照", "依据", "依照", "基于", "参考", "参照",
                       "照着", "结合", "沿用")
#: 位置限定后缀：材料词后紧跟它们且其后 12 个字符内出现**另一类材料词**时，前者只作查找位置。
_LOCATION_MARKERS = ("里", "中", "内", "下", "内页")
#: 位置限定的前视窗口（字符数）
_LOCATION_LOOKAHEAD = 12
#: 自由便签区：永不进入 ``write_targets``（写它永不判越界）
_FREE_NOTE_MATERIAL = "备忘录"
_WRITE_ACTION = re.compile(r"完善|补全|补充|补|修改|改写|修正|润色|调整|新建|创建|更新|续写|写|列|删除|移动|撤回|撤销")
_NEGATE = re.compile(r"(?:不要|不许|不得|不能|禁止|先别|别|不)(?:再|直接|顺手|要|误|擅自|额外|顺便|继续|随意){0,2}(?:改|写|删|动|更新|修改)|(?:保持|保留).{0,80}(?:不变|原样)")


def later_write_stage(text: str) -> bool:
    """An explicit later write stage can follow a report-only first stage."""
    if re.search(r"(?:全程|整个任务|本轮|全部|始终|一直).{0,8}(?:只|仅)(?:报告|讨论|分析|审稿)", text):
        return False
    exclusive = re.search(r"(?:只|仅)(?:报告|讨论|分析|审稿|提(?:修改)?建议)", text)
    if exclusive and exclusive.start() < text.find("先"):
        return False  # A leading whole-task constraint is not a first-step label.
    if re.search(r"(?:不要|禁止|不能|不得|不许)(?:直接)?(?:修改|新建|写入|改|写|删|动)(?:任何|所有)(?:文件|材料|文档)", text):
        return False
    positive = _NEGATE.sub("", text)
    return bool(re.search(r"先[\s\S]+(?:再|然后|接着)(?:由[^，,。；;]{0,24}|(?:请|根据|依据)[^，,。；;]{0,24})?"
        r"(?:实际|直接)?(?:修改|改写|修正|润色|完善|更新|创建|新建|续写|写)", positive))


def report_only(text: str) -> bool:
    """An exclusive report/discussion instruction cannot grant write authority."""
    if re.search(r"(?:不要|禁止|不能|不得|不许)(?:直接)?(?:修改|新建|写入|改|写|删|动)(?:任何|所有)(?:文件|材料|文档)", text):
        return True
    if re.search(r"(?:只|仅)(?:报告|讨论|分析|审稿|提(?:修改)?建议)", text) and not later_write_stage(text):
        return True
    prohibition = re.search(r"(?:不要|不能|不许|不得|禁止)(?:直接)?(?:改稿|改正文|修改正文|修改任何文件|落盘)", text)
    return bool(prohibition and not _WRITE_ACTION.search(_NEGATE.sub("", text)))


def forbidden_materials(text: str) -> list[str]:
    """Explicit author prohibitions survive both keyword and model routing."""
    forbidden = []
    clauses = re.split(r"[，,。；;！!\n\r]|(?:但|只|仅)(?=(?:完善|补|修改|改写|改|修正|润色|调整|新建|创建|更新|续写|写|列|删除|移动))", str(text or ""))
    for raw_clause in clauses:
        clause = _normalize(raw_clause)
        if not _NEGATE.search(clause):
            continue
        for material, hints in MATERIAL_HINTS.items():
            if not any(_normalize(hint) in clause for hint in hints):
                continue
            paths = re.findall(re.escape(material) + r"[/\\][^，,。；;！!\s]+?\.(?:md|txt)", raw_clause, re.I)
            for target in paths or [material]:
                target = target.replace("\\", "/")
                if target not in forbidden:
                    forbidden.append(target)
    return forbidden
#: 全角 → 半角的码点偏移（``０`` 0xFF10 → ``0`` 0x30）
_FULLWIDTH_OFFSET = 0xFEE0


def _normalize(text: str) -> str:
    """触发词与材料词匹配共用的归一化：去空白/制表/换行、全角转半角、英文小写。

    展示文本（``basis`` 等）仍用作者原文片段，只有匹配基于归一化结果。
    中文不受影响，因此「按」「根据」等中文介词照常参与判定。
    """
    out: list[str] = []
    for char in str(text or ""):
        code = ord(char)
        if char.isspace() or code == 0x3000:
            continue
        if 0xFF01 <= code <= 0xFF5E:
            char = chr(code - _FULLWIDTH_OFFSET)
        out.append(char.lower())
    return "".join(out)


# ─────────────────────────── 关键词快路径 ───────────────────────────


def intent_index() -> list[dict]:
    """把能力注册表摊平成「一意图一行」的关键词索引（顺序稳定）。

    同一意图名由多个 Agent 声明时合并触发词，Agent / 技能取注册表顺序里的第一个。
    """
    grouped: dict[str, dict] = {}
    for item in agent_service.capability_index():
        intent = str(item.get("intent") or "").strip()
        if not intent:
            continue
        entry = grouped.get(intent)
        if entry is None:
            entry = {
                "intent": intent,
                "agent": str(item.get("agent") or ""),
                "skill": str(item.get("skill") or ""),
                "task_type": str(item.get("task_type") or ""),
                "retrieval_profile": str(item.get("retrieval_profile") or "history"),
                "triggers": [],
            }
            grouped[intent] = entry
        for trigger in item.get("triggers") or []:
            text = str(trigger or "").strip()
            if text and text not in entry["triggers"]:
                entry["triggers"].append(text)
    return list(grouped.values())


def _score_keywords(text: str) -> list[tuple[int, dict, list[str]]]:
    basis = _normalize(text)
    scored: list[tuple[int, dict, list[str]]] = []
    for entry in intent_index():
        hits = [trigger for trigger in entry["triggers"] if _normalize(trigger) in basis]
        if hits:
            scored.append((len(hits), entry, hits))
    scored.sort(key=lambda item: item[0], reverse=True)
    return scored


def _candidates(rows: list[tuple[int, dict, list[str]]]) -> list[dict]:
    return [
        {"intent": entry["intent"], "agent": entry["agent"],
         "trigger": hits[0] if hits else (entry["triggers"][0] if entry["triggers"] else "")}
        for _score, entry, hits in rows
    ]


def _first_candidates(limit: int = CANDIDATE_LIMIT) -> list[dict]:
    return [
        {"intent": entry["intent"], "agent": entry["agent"],
         "trigger": entry["triggers"][0] if entry["triggers"] else ""}
        for entry in intent_index()[:limit]
    ]


def _keyword_decision(text: str) -> dict:
    """确定性关键词判定：``decisive`` 为真时无需再调 LLM。"""
    scored = _score_keywords(text)
    if report_only(text):
        report_candidates = [row for row in scored if row[1]["agent"] in agent_service.READ_ONLY_AGENTS]
        if report_candidates:
            # Registry-defined review triggers beat names of the reference
            # materials when the author explicitly requests a report only.
            scored = report_candidates
    if not scored:
        return {
            "matched": False,
            "intent": "",
            "agent": "",
            "skill": "",
            "task_type": "",
            "retrieval_profile": "history",
            "confidence": "low",
            "hits": [],
            "candidates": _first_candidates(),
            "basis": "未命中任何意图触发词，已交创作助理直接处理；也可手动指定 Agent",
            "decisive": False,
            "ambiguous": False,
        }
    score, entry, hits = scored[0]
    tied = [row for row in scored if row[0] == score]
    return {
        "matched": True,
        "intent": entry["intent"],
        "agent": entry["agent"],
        "skill": entry["skill"],
        "task_type": entry["task_type"],
        "retrieval_profile": entry["retrieval_profile"],
        "confidence": "high" if score >= 2 else "medium",
        "hits": hits,
        "candidates": _candidates(scored[1:CANDIDATE_LIMIT]),
        "basis": f"命中触发词「{'、'.join(hits)}」→ {entry['intent']}",
        "decisive": len(tied) == 1 and not forbidden_materials(text)
            and not any(prep in _normalize(text) for prep in _BASIS_PREPOSITIONS)
            and len(_named_materials(text)["explicit"]) < 2,
        "ambiguous": len(tied) > 1,
    }


def _is_basis_position(norm: str, position: int) -> bool:
    """材料词是否**紧跟在依据介词之后**（中间允许 0–2 个非材料字符）。"""
    for prep in _BASIS_PREPOSITIONS:
        for gap in range(0, 3):
            start = position - gap - len(prep)
            if start >= 0 and norm[start:start + len(prep)] == prep:
                return True
    return False


def _is_location_position(norm: str, span: dict, spans: list[dict]) -> bool:
    """材料词是否**位置限定**：后紧跟「里/中/内/下…」且其后 12 字内出现另一类材料词。"""
    end = span["position"] + span["length"]
    marker_len = 0
    for marker in _LOCATION_MARKERS:
        if norm.startswith(marker, end):
            marker_len = len(marker)
            break
    if not marker_len:
        return False
    window_start = end + marker_len
    window_end = window_start + _LOCATION_LOOKAHEAD
    for other in spans:
        if other["material"] == span["material"]:
            continue
        if window_start <= other["position"] <= window_end:
            return True
    return False


def _scan_material_mentions(text: str) -> list[dict]:
    """在归一化文本里扫描作者点名的材料，返回**每次出现**（含依据 / 位置限定标注）。"""
    norm = _normalize(text)
    spans: list[dict] = []
    for material, hints in MATERIAL_HINTS.items():
        for hint in hints:
            key = _normalize(hint)
            if not key:
                continue
            start = 0
            while True:
                position = norm.find(key, start)
                if position < 0:
                    break
                spans.append({"material": material, "position": position, "length": len(key)})
                start = position + 1
    spans.sort(key=lambda item: (item["position"], -item["length"]))
    for span in spans:
        span["basis"] = _is_basis_position(norm, span["position"])
        span["locating"] = _is_location_position(norm, span, spans)
        # A coordinated reference list stays a reference until a new action.
        prefix = re.split(r"[，,。；;！!]", norm[:span["position"]])[-1]
        prep = max((prefix.rfind(p) for p in _BASIS_PREPOSITIONS), default=-1)
        if prep >= 0 and not re.search(r"把|将|然后|再|来", prefix[prep + 2:]) and not _WRITE_ACTION.search(prefix[prep + 2:]):
            span["basis"] = True
        if span["material"] == "章节" and _WRITE_ACTION.search(prefix):
            span["locating"] = False
        # 场景/人物 mentioned inside prose are prose subjects, not setting edits.
        if span["material"] == "设定" and re.search(r"(?:正文|章节|这一章).{0,8}(?:中|里|内)", prefix):
            span["basis"] = True
    return spans


def _named_materials(text: str) -> dict[str, list[str]]:
    """把点名的材料按语义归类，列表均按**首次出现先后**排序。

    - ``explicit``：产出材料（既非依据、也非位置限定）；
    - ``basis`` / ``locating``：只读依据（依据介词之后 / 位置限定）；
    - ``readonly``：``basis`` ∪ ``locating``，按出现先后去重。
    """
    spans = _scan_material_mentions(text)
    first: dict[str, int] = {}
    kinds: dict[str, set[str]] = {}
    for span in spans:
        material = span["material"]
        first.setdefault(material, span["position"])
        if span["basis"]:
            kinds.setdefault(material, set()).add("basis")
        elif span["locating"]:
            kinds.setdefault(material, set()).add("locating")
        else:
            kinds.setdefault(material, set()).add("explicit")
    order = sorted(first, key=lambda material: first[material])
    explicit, basis, locating = [], [], []
    for material in order:
        if "explicit" in kinds[material]:
            explicit.append(material)
        elif "basis" in kinds[material]:
            basis.append(material)
        else:
            locating.append(material)
    prohibited = set(forbidden_materials(text))
    explicit = [material for material in explicit if material not in prohibited]
    readonly = [material for material in order if material not in explicit]
    return {"explicit": explicit, "basis": basis, "locating": locating,
            "readonly": readonly}


def _preferred_agent(material: str) -> str:
    """该材料的首选执行 Agent；首选不在注册表主责名单里时退回首个主责 Agent。"""
    candidates = agent_service.agents_by_material(material)
    preferred = _MATERIAL_AGENT.get(material, "")
    if preferred and preferred in candidates:
        return preferred
    return candidates[0] if candidates else ""


def _material_plan(write_targets: list[str], order: list[str]) -> list[dict]:
    """按作者消息中点名的先后，为每类待写材料各排一步（最多 :data:`MAX_PLAN_STEPS` 步）。

    每步只写自己那类材料，``read_only_refs`` 填**前序步骤产出的材料**（后续步骤据其接续）。
    """
    ranked = [material for material in order if material in write_targets]
    ranked += [material for material in write_targets if material not in ranked]
    steps: list[dict] = []
    produced: list[str] = []
    for material in ranked:
        agent = _preferred_agent(material)
        if not agent:
            continue
        steps.append({"step": len(steps) + 1, "agent": agent, "write_targets": [material],
                      "read_only_refs": list(produced), "note": f"完成 {material}/ 这一步"})
        produced.append(material)
        if len(steps) >= MAX_PLAN_STEPS:
            break
    return steps


# ─────────────────────────── LLM 语义判定 ───────────────────────────


def _intent_llm_enabled() -> bool:
    """设置开关：``chat.intent_llm`` 缺省视为开启，显式 ``False`` 才跳过。"""
    try:
        chat = settings_service.read_settings().get("chat")
    except Exception:  # noqa: BLE001 - 设置读取失败按开启处理
        return True
    return not (isinstance(chat, dict) and chat.get("intent_llm") is False)


def _llm_engine_available() -> bool:
    """廉价可用性检查：引擎不可用就直接跳过判定，不做任何调用。"""
    try:
        from ..engine import router as engine_router

        return bool(engine_router.get_engine(INTENT_ENGINE).available())
    except Exception:  # noqa: BLE001 - 探测失败按不可用处理
        return False


def _llm_system_prompt() -> str:
    lines = [
        "你是这部小说的意图路由判定器。只输出 JSON，不要解释、不要 Markdown 代码围栏。",
        "",
        "可选清单（只能从这份清单里选 agent 与技能；不得发明新的 agent 名或技能名）：",
    ]
    for item in agent_service.describe_candidates():
        lines.append(f"- agent: {item['name']}（{item['title']}）：{item['description']}")
        lines.append(f"  主责材料：{'、'.join(item['materials']) or '（未声明）'}")
        if item.get("boundaries"):
            lines.append(f"  边界：{item['boundaries']}")
        for capability in item.get("capabilities") or []:
            triggers = "、".join(capability.get("triggers") or []) or "（无）"
            skill = capability.get("skill") or "（无）"
            lines.append(
                f"  能力：{capability.get('intent')}｜触发词：{triggers}｜技能：{skill}"
            )
    lines += [
        "",
        "判定规则：",
        "1. 依据材料（read_only_refs）与产出材料（write_targets）必须分开："
        "作者说「根据设定写大纲」时，设定是只读依据，大纲才是产出。",
        "2. 作者只要求动一类材料时，write_targets 只能含那一类，不得顺手加上其他材料；"
        "确实需要跨材料时用 plan 显式分步。",
        "3. write_targets / read_only_refs 的元素用材料顶层目录名"
        "（章节/设定/大纲/状态/备忘录）或具体本书相对路径（如 设定/世界设定.md）。",
        "4. 作者在消息里显式点名的文件或章节要计入产出或依据；若本轮上下文给了"
        "「当前打开的文件」或「正文目标章节」，作者要求改的就是它时必须计入 write_targets。",
        "5. 无法判断产出材料时给空数组并把 confidence 压低，不要猜。",
        "",
        "输出 JSON（字段名与类型必须一致）：",
        "{",
        '  "intent": "设定管理",',
        '  "primary_agent": "setting-keeper",',
        '  "support_agents": [],',
        '  "write_targets": ["设定"],',
        '  "read_only_refs": [],',
        '  "skills": ["novel-setting"],',
        '  "plan": [],',
        '  "confidence": 0.86,',
        '  "reason": "作者要求完善设定"',
        "}",
        "plan 每步："
        '{"step": 1, "agent": "...", "write_targets": [...], "read_only_refs": [...], '
        '"note": "..."}，最多 3 步；单材料任务必须给空数组 []。',
        "只输出这个 JSON 对象本身。",
    ]
    return "\n".join(lines)


def _llm_user_content(text: str, *, active_file: str, target_chapter: str,
                      context_hint: str) -> str:
    lines = ["作者消息：", (text or "").strip() or "（空）"]
    context: list[str] = []
    if str(active_file or "").strip():
        context.append(f"- 当前打开的文件：{active_file}")
    if str(target_chapter or "").strip():
        context.append(f"- 正文目标章节：{target_chapter}")
    if str(context_hint or "").strip():
        context.append(f"- 补充上下文：{context_hint}")
    if context:
        lines += ["", "本轮上下文：", *context]
    return "\n".join(lines)


def _parse_json(text: str) -> dict | None:
    from .review_service import _parse_json as parse

    return parse(text)


def _llm_decision(
    text: str,
    *,
    project_id: int | None,
    active_file: str = "",
    target_chapter: str = "",
    context_hint: str = "",
) -> tuple[dict | None, str]:
    """调用 LLM 语义判定；返回 ``(payload, 失败原因)``，任何失败都回退（payload=None）。"""
    if project_id is None:
        return None, ""
    if not _intent_llm_enabled():
        return None, "LLM 意图判定已在设置中关闭"
    if not _llm_engine_available():
        return None, "判定引擎不可用"
    try:
        result = generation_service.run_task(
            project_id=project_id,
            task_type=INTENT_TASK_TYPE,
            engine=INTENT_ENGINE,
            system=_llm_system_prompt(),
            messages=[{
                "role": "user",
                "content": _llm_user_content(
                    text, active_file=active_file, target_chapter=target_chapter,
                    context_hint=context_hint,
                ),
            }],
            temperature=0,
            max_tokens=800,
            timeout_seconds=INTENT_TIMEOUT_SECONDS,
        )
    except Exception:  # noqa: BLE001 - 判定失败绝不阻断整轮对话
        return None, "判定调用异常"
    if not isinstance(result, dict) or not result.get("ok"):
        return None, "判定调用失败"
    payload = _parse_json(result.get("text") or "")
    if payload is None:
        return None, "判定返回非法 JSON"
    return payload, ""


# ─────────────────────────── 校验与归一 ───────────────────────────


def _agent_catalog() -> tuple[list[str], dict[str, dict]]:
    try:
        agents = agent_service.list_all_agents()
    except Exception:  # noqa: BLE001 - 注册表不可读时退化为空清单
        agents = []
    return [item["name"] for item in agents], {item["name"]: item for item in agents}


def _resolve_agent(requested: str, names: list[str],
                   catalog: dict[str, dict]) -> tuple[str, dict | None]:
    """把 Agent 名收敛到注册表内；未知时按 默认执行者 → 总编 → 首个 回退。"""
    if requested and requested in catalog:
        return requested, catalog[requested]
    if not names:
        return requested, None
    fallback = (
        agent_service.DEFAULT_AGENT if agent_service.DEFAULT_AGENT in catalog
        else ("chief-editor" if "chief-editor" in catalog else names[0])
    )
    return fallback, catalog.get(fallback)


def _project_dir(project_id: int | None):
    if project_id is None:
        return None
    try:
        return project_service.get_project_dir(int(project_id))[1]
    except Exception:  # noqa: BLE001 - 项目目录缺失时只做目录名白名单校验
        return None


def _normalize_materials(values, project_dir) -> list[str]:
    """只保留 ``MATERIAL_DIRS`` 目录名或本书内确实存在的相对路径。"""
    out: list[str] = []
    for raw in values or []:
        item = str(raw or "").strip().replace("\\", "/").strip("/")
        if not item or item in out:
            continue
        if item not in agent_service.MATERIAL_DIRS:
            top = item.split("/", 1)[0]
            if top not in agent_service.MATERIAL_DIRS:
                continue
            if project_dir is not None:
                try:
                    resolved = resolve_within(project_dir, item)
                except Exception:  # noqa: BLE001 - 路径越界等一律丢弃
                    continue
                if not (resolved.is_file() or resolved.is_dir()):
                    continue
        out.append(item)
    return out


def _normalize_plan(raw_plan, project_dir, names: list[str]) -> list[dict]:
    """计划步：Agent 必须存在、材料必须合法；只剩 0-1 步则置空数组。"""
    steps: list[dict] = []
    for raw in raw_plan or []:
        if not isinstance(raw, dict):
            continue
        agent = str(raw.get("agent") or "").strip()
        if agent not in names:
            continue
        steps.append({
            "step": len(steps) + 1,
            "agent": agent,
            "write_targets": _normalize_materials(raw.get("write_targets"), project_dir),
            "read_only_refs": _normalize_materials(raw.get("read_only_refs"), project_dir),
            "note": str(raw.get("note") or ""),
            "retrieval_profile": agent_service.retrieval_profile_of(agent_service.get_agent(agent)),
        })
        if len(steps) >= MAX_PLAN_STEPS:
            break
    return steps if len(steps) >= 2 else []


def _known_skills(catalog: dict[str, dict]) -> set[str]:
    """可用技能名 = 已启用技能 ∪ 注册表声明过的技能（作者自建技能也参与）。"""
    known: set[str] = set()
    try:
        known.update(
            item["name"] for item in skill_service.list_skills() if item.get("enabled", True)
        )
    except Exception:  # noqa: BLE001 - 技能目录不可读时只看注册表声明
        pass
    for agent in catalog.values():
        for name in agent.get("skills") or []:
            text = str(name or "").strip()
            if text:
                known.add(text)
        for capability in agent.get("capabilities") or []:
            if not isinstance(capability, dict):
                continue
            text = str(capability.get("skill") or "").strip()
            if text:
                known.add(text)
    return known


def _capability(spec_intent: str, agent: str, catalog: dict[str, dict]) -> dict:
    """该 Agent 上与意图匹配的能力声明（优先 intent 相同，其次首个非空技能）。"""
    capabilities = [
        item for item in (catalog.get(agent) or {}).get("capabilities") or []
        if isinstance(item, dict)
    ]
    for item in capabilities:
        if (str(item.get("intent") or "") == spec_intent
                and str(item.get("skill") or "").strip()):
            return item
    return {}


def _collect_skills(*, primary_skill: str, declared, plan: list[dict], intent: str,
                    catalog: dict[str, dict], known: set[str]) -> list[str]:
    """技能列表：主能力技能 → 判定声明技能 → 各计划步 Agent 的绑定技能，去重并过滤。"""
    names: list[str] = []

    def _add(name: str) -> None:
        key = str(name or "").strip()
        if key and key not in names:
            names.append(key)

    _add(primary_skill)
    for name in declared or []:
        _add(name)
    for step in plan:
        agent = catalog.get(step["agent"]) or {}
        capabilities = [
            item for item in agent.get("capabilities") or [] if isinstance(item, dict)
        ]
        skill = next(
            (str(item.get("skill") or "").strip() for item in capabilities
             if str(item.get("skill") or "").strip()
             and str(item.get("intent") or "") == intent), "",
        )
        if not skill:
            skill = next(
                (str(item.get("skill") or "").strip() for item in capabilities
                 if str(item.get("skill") or "").strip()), "",
            )
        if not skill:
            bound = [str(name).strip() for name in agent.get("skills") or [] if str(name).strip()]
            skill = bound[0] if bound else ""
        _add(skill)
    return [name for name in names if name in known]


def _confidence_from_llm(value) -> str:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return "medium"
    if score >= 0.7:
        return "high"
    if score >= 0.4:
        return "medium"
    return "low"


def _low_confidence(payload: dict) -> float | None:
    """判定置信是否低于采纳阈值：低于阈值返回分值，达标返回 ``None``。

    保守口径：``confidence`` 缺失 / 非数值 / 无法转 ``float`` 一律**按 0 处理**
    （即低于阈值，直接回退关键词结果），不把「没给分」当成可信判定。
    """
    try:
        score = float(payload.get("confidence") or 0)
    except (TypeError, ValueError):
        score = 0.0
    return score if score < LLM_MIN_CONFIDENCE else None


# ─────────────────────────── 结果组装 ───────────────────────────


def _keyword_result(keyword: dict, note: str, names: list[str], catalog: dict[str, dict],
                    known_skills: set[str], project_dir, text: str = "") -> dict:
    """关键词路径结果（含并列取首个、显式材料扫描、多步计划与回退说明）。

    显式材料扫描**只在命中的主责 Agent 有主责材料**（即已识别为写作/维护类意图）时启用；
    审稿 / 去AI味 / 拆书 / 兜底助理这类 ``materials == []`` 的只读角色，即便消息里出现
    「这章 / 正文」等词也**不获得写域**（审稿请求保持 ``write_targets == []``）。
    ``备忘录`` 是自由便签区，不进 ``write_targets``。

    **依据与产出分离**：跟依据介词（根据 / 按 / 照着 …）或位置限定（「设定里的…」）点名的
    材料只进 ``read_only_refs``；``write_targets`` 由显式产出材料 ∪ 主责 Agent 的自带材料
    组成（剔除只读依据），且**多步计划只在显式产出材料 ≥ 2 类时生成**。
    """
    matched = bool(keyword["matched"])
    requested = keyword["agent"] if matched else agent_service.DEFAULT_AGENT
    stage = re.search(r"先(?P<report>[\s\S]+?)(?:再|然后|接着)(?P<write>[\s\S]+)", text)
    report_stage = None
    if stage and later_write_stage(text) and not report_only(text):
        report_rows = [row for row in _score_keywords(stage["report"])
                       if row[1]["agent"] in agent_service.READ_ONLY_AGENTS]
        if report_rows:
            report_stage = report_rows[0][1]
    named = _named_materials(stage["write"] if report_stage else text)
    # Action objects beat object words occurring in references or prohibitions.
    if len(named["explicit"]) == 1 and _WRITE_ACTION.search(text) and not report_only(text):
        requested = _preferred_agent(named["explicit"][0]) or requested
    agent_name, definition = _resolve_agent(requested, names, catalog)
    base_materials = _normalize_materials((definition or {}).get("materials") or [], project_dir)

    explicit: list[str] = []
    basis_refs: list[str] = []
    read_only_refs: list[str] = []
    if base_materials:
        explicit = list(named["explicit"])
        basis_refs = list(named["basis"])
        readonly = set(named["readonly"])
        merged = set(explicit) if explicit else (set(base_materials) - readonly)
        write_targets = [name for name in agent_service.MATERIAL_DIRS if name in merged]
        read_only_refs = [item for item in named["readonly"] if item not in merged]
    else:
        write_targets = list(base_materials)

    if report_stage and write_targets:
        first_materials = _named_materials(stage["report"])
        plan = _normalize_plan([{"agent": report_stage["agent"], "write_targets": [],
            "read_only_refs": list(dict.fromkeys([*first_materials["explicit"], *first_materials["readonly"]])),
            "note": "先报告问题与依据"},
            *_material_plan(write_targets, explicit)], project_dir, names)
    else:
        plan = ([] if len(explicit) < 2 else _normalize_plan(
            _material_plan(write_targets, explicit), project_dir, names))
    skills = _collect_skills(
        primary_skill=keyword["skill"], declared=[], plan=plan, intent=keyword["intent"],
        catalog=catalog, known=known_skills,
    )

    basis = keyword["basis"]
    if keyword["ambiguous"]:
        basis = f"{basis}（触发词并列，按注册表顺序取首个）"
    if matched and base_materials:
        segments: list[str] = []
        if basis_refs:
            segments.append(f"依据材料：{'、'.join(basis_refs)}")
        if write_targets:
            segments.append(f"产出材料：{'、'.join(write_targets)}")
        if plan:
            tail = f" → 生成 {len(plan)} 步协作计划"
        elif explicit:
            tail = "；未产生多步计划"
        else:
            tail = ""
        if segments:
            basis = f"{basis}；{'；'.join(segments)}{tail}"
    if note:
        basis = f"{basis}；{note}，已回退关键词结果"

    return {
        "matched": matched,
        "intent": keyword["intent"],
        "agent": agent_name,
        "agent_title": (definition or {}).get("title", ""),
        "agent_skills": list((definition or {}).get("skills") or []),
        "skill": skills[0] if skills else "",
        "task_type": keyword["task_type"],
        "retrieval_profile": agent_service.retrieval_profile_of(definition or {}, intent=keyword["intent"]),
        "confidence": keyword["confidence"],
        "hits": keyword["hits"],
        "candidates": keyword["candidates"],
        "basis": basis,
        "override": False,
        "available_agents": names,
        "silent_write": False,
        "skills": skills,
        "write_targets": write_targets,
        "read_only_refs": read_only_refs,
        "plan": plan,
        "scope_unresolved": not write_targets,
        "source": "keyword" if matched else "fallback",
        "llm_used": False,
        "reason": "",
    }


def _llm_result(payload: dict, keyword: dict, names: list[str], catalog: dict[str, dict],
                known_skills: set[str], project_dir, text: str = "") -> dict:
    """采纳 LLM 判定；未知角色名确定性回退关键词结果。"""
    primary = str(payload.get("primary_agent") or "").strip()
    if primary not in catalog:
        result = _keyword_result(keyword, "", names, catalog, known_skills, project_dir, text)
        note = f"判定返回了未知角色「{primary or '（空）'}」，已回退关键词"
        result["basis"] = f"{result['basis']}；{note}"
        return result

    intent = str(payload.get("intent") or "").strip() or keyword["intent"]
    plan = _normalize_plan(payload.get("plan"), project_dir, names)
    write_targets = _normalize_materials(payload.get("write_targets"), project_dir)
    named = _named_materials(text)
    if (not plan and len(named["explicit"]) >= 2 and not report_only(text)
            and re.search(r"先[\s\S]+(?:再|然后|接着)[\s\S]+", text)):
        # A semantic answer may name only its first executor, even while its
        # explanation acknowledges both ordered outputs. Preserve the actual
        # author request instead of silently dropping the remaining material.
        write_targets = _normalize_materials(named["explicit"], project_dir)
        plan = _normalize_plan(_material_plan(write_targets, named["explicit"]), project_dir, names)
    read_only_refs = _normalize_materials(payload.get("read_only_refs"), project_dir)
    definition = catalog[primary]
    if not write_targets:  # 兜底：主责 Agent 的主责材料
        write_targets = _normalize_materials(definition.get("materials") or [], project_dir)
    capability = _capability(intent, primary, catalog)
    task_type = str(capability.get("task_type") or "") or (
        keyword["task_type"] if intent == keyword["intent"] else ""
    )
    skills = _collect_skills(
        primary_skill=str(capability.get("skill") or ""), declared=payload.get("skills"),
        plan=plan, intent=intent, catalog=catalog, known=known_skills,
    )

    reason = str(payload.get("reason") or "")
    basis = f"意图判定（LLM）：{reason}" if reason else "意图判定（LLM）：已采纳语义判定结果"
    if keyword["matched"]:
        basis = f"{basis}（关键词命中「{'、'.join(keyword['hits'])}」，以语义判定为准）"

    return {
        "matched": True,
        "intent": intent,
        "agent": primary,
        "agent_title": definition.get("title", ""),
        "agent_skills": list(definition.get("skills") or []),
        "skill": skills[0] if skills else "",
        "task_type": task_type,
        "retrieval_profile": agent_service.retrieval_profile_of(definition, intent=intent),
        "confidence": _confidence_from_llm(payload.get("confidence")),
        "hits": keyword["hits"],
        "candidates": keyword["candidates"] or _first_candidates(),
        "basis": basis,
        "override": False,
        "available_agents": names,
        "silent_write": False,
        "skills": skills,
        "write_targets": write_targets,
        "read_only_refs": read_only_refs,
        "plan": plan,
        "scope_unresolved": not write_targets,
        "source": "llm",
        "llm_used": True,
        "reason": reason,
    }


def _override_result(override_agent: str, keyword: dict, names: list[str],
                     catalog: dict[str, dict], known_skills: set[str], project_dir) -> dict:
    """作者/调用方显式指定 Agent：不判定，材料取该 Agent 主责材料。"""
    requested = str(override_agent).strip()
    agent_name, definition = _resolve_agent(requested, names, catalog)
    intent = keyword["intent"]
    capability = _capability(intent, agent_name, catalog) if intent else {}
    write_targets = _normalize_materials((definition or {}).get("materials") or [], project_dir)
    skills = _collect_skills(
        primary_skill=str(capability.get("skill") or ""),
        declared=[keyword["skill"]], plan=[], intent=intent,
        catalog=catalog, known=known_skills,
    )
    basis = f"作者指定 Agent「{requested}」，跳过自动路由"
    if requested and requested != agent_name:
        basis = f"{basis}（该 Agent 不存在，已交「{agent_name}」处理）"

    return {
        "matched": bool(keyword["matched"]),
        "intent": intent,
        "agent": agent_name,
        "agent_title": (definition or {}).get("title", ""),
        "agent_skills": list((definition or {}).get("skills") or []),
        "skill": skills[0] if skills else "",
        "task_type": str(capability.get("task_type") or "") or keyword["task_type"],
        "retrieval_profile": agent_service.retrieval_profile_of(definition or {}, intent=intent),
        "confidence": keyword["confidence"],
        "hits": keyword["hits"],
        "candidates": keyword["candidates"],
        "basis": basis,
        "override": True,
        "available_agents": names,
        "silent_write": False,
        "skills": skills,
        "write_targets": write_targets,
        "read_only_refs": [],
        "plan": [],
        "scope_unresolved": not write_targets,
        "source": "override",
        "llm_used": False,
        "reason": "",
    }


# ─────────────────────────── 对外入口 ───────────────────────────


def classify(
    project_id: int | None,
    text: str,
    *,
    override_agent: str = "",
    active_file: str = "",
    target_chapter: str = "",
    context_hint: str = "",
) -> dict:
    """两段式意图路由主入口：关键词快路径 → LLM 判定 → 校验归一。

    无 ``project_id`` 或关键词已唯一命中时**绝不调用模型**。
    """
    names, catalog = _agent_catalog()
    known_skills = _known_skills(catalog)
    project_dir = _project_dir(project_id)
    keyword = _keyword_decision(text)

    if str(override_agent or "").strip():
        return _guard_result(_override_result(override_agent, keyword, names, catalog, known_skills, project_dir), text)

    payload: dict | None = None
    failure = ""
    if not keyword["decisive"]:
        payload, failure = _llm_decision(
            text, project_id=project_id, active_file=active_file,
            target_chapter=target_chapter, context_hint=context_hint,
        )
    if payload is None:
        return _guard_result(_keyword_result(keyword, failure, names, catalog, known_skills, project_dir, text), text)
    low_confidence = _low_confidence(payload)
    if low_confidence is not None:
        # 判定置信不足：丢弃判定、确定性回退关键词结果（关键词也未命中则交默认执行者）。
        # 结果实际来自关键词，``llm_used`` 与「未知角色名」回退口径一致取 False。
        note = f"判定置信 {low_confidence:g} 低于阈值 {LLM_MIN_CONFIDENCE:g}"
        return _guard_result(_keyword_result(keyword, note, names, catalog, known_skills, project_dir, text), text)
    return _guard_result(_llm_result(payload, keyword, names, catalog, known_skills, project_dir, text), text)


def _guard_result(result: dict, text: str) -> dict:
    denied = forbidden_materials(text)
    named = _named_materials(text)
    basis = set(named["readonly"]) - set(named["explicit"])
    def permitted(target):
        target = str(target).replace("\\", "/")
        return target not in set(denied) | basis and target.split("/", 1)[0] not in set(denied) | basis
    result["forbidden_targets"] = denied
    result["read_only_refs"] = list(dict.fromkeys([*result.get("read_only_refs", []), *named["readonly"]]))
    plan = []
    for step in result.get("plan", []):
        step = dict(step)
        step["write_targets"] = [] if step.get("agent") in agent_service.READ_ONLY_AGENTS else [t for t in step.get("write_targets", []) if permitted(t)]
        if step["write_targets"] or step.get("agent") in agent_service.READ_ONLY_AGENTS:
            step["step"] = len(plan) + 1
            plan.append(step)
    # A reviewer can lead a serial report → revision plan without granting
    # that reviewer any write tools. An exclusive author report instruction
    # still denies the whole round, irrespective of later proposed roles.
    writable_plan = len(plan) >= 2 and any(step["write_targets"] for step in plan)
    readonly = report_only(text) or (result.get("agent") in agent_service.READ_ONLY_AGENTS and not writable_plan)
    result["read_only"] = readonly
    result["write_targets"] = [] if readonly else list(dict.fromkeys([
        *[t for t in result.get("write_targets", []) if permitted(t)],
        *[t for step in plan for t in step["write_targets"]],
    ]))
    result["plan"] = [] if readonly else plan
    result["scope_unresolved"] = not result["write_targets"]
    return result


__all__ = [
    "CANDIDATE_LIMIT",
    "INTENT_ENGINE",
    "INTENT_TASK_TYPE",
    "LLM_MIN_CONFIDENCE",
    "MATERIAL_HINTS",
    "MAX_PLAN_STEPS",
    "classify",
    "intent_index",
]
