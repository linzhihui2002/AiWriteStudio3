"""PromptRegistry：提示词注册制管理（id / version / taskType / contextPolicy）。

- 所有提示词**自写**（合规要求：不移植任何第三方提示词文本）；
- 注册表落 SQLite ``prompt_registry``（索引层），代码内 :data:`PROMPTS` 是事实源，
  启动时幂等同步；任务记录（``tasks`` 表）保留 ``prompt_id`` + ``prompt_version``
  供追溯；
- ``context_policy`` 描述该任务需要的上下文级别（见 ContextService）。
"""

from __future__ import annotations

from .. import db
from .errors import NodeNotFoundError

# ─────────────────────────── 提示词正文（全部自写） ───────────────────────────

PROMPTS: dict[str, dict] = {
    "novel.outline.generate": {
        "version": "1",
        "task_type": "大纲生成",
        "context_policy": "outline",
        "body": (
            "你是中文长篇小说的结构编辑。基于作者提供的灵感与设定，产出可直接执行的分卷大纲。\n"
            "要求：\n"
            "1. 给出 2-3 个**互不相同**的大纲候选，每个候选有清晰的主线冲突与成长弧；\n"
            "2. 每卷写明：卷名、核心事件、主角目标与阻力、卷末钩子；\n"
            "3. 只写结构与因果，不写抒情段落，不写正文；\n"
            "4. 若信息不足，先列出需要作者确认的关键问题（不超过 5 条），不要臆造设定。"
        ),
    },
    "novel.outline.refine": {
        "version": "1",
        "task_type": "大纲完善",
        "context_policy": "outline",
        "body": (
            "你是中文长篇小说的结构编辑。请完善作者指定卷的大纲：补齐因果链、"
            "冲突升级节奏与章末钩子类型，保持既有设定口径不变。\n"
            "只输出完善后的该卷大纲文本，不要解释你做了什么。"
        ),
    },
    "novel.chapter.contract": {
        "version": "1",
        "task_type": "章节合同",
        "context_policy": "contract",
        "body": (
            "你是中文小说的章节策划。请为指定章节产出一份**章节合同**，供作者确认后冻结。\n"
            "输出严格的 JSON（不要 Markdown 围栏、不要额外说明）：\n"
            "{\n"
            '  "情节点": ["必须发生的事件，按顺序，3-6 条"],\n'
            '  "字数预算": 3000,\n'
            '  "钩子类型": "悬念/反转/情绪/信息/危机 之一",\n'
            '  "涉及实体": ["本章涉及的角色与关键设定名"],\n'
            '  "必须承上": ["上一章遗留、本章必须接住的点"],\n'
            '  "禁止事项": ["本章不能发生的事（防止提前消费后续情节点）"]\n'
            "}\n"
            "约束：情节点必须可在正文中被逐项验证（具体、可观测），避免抽象描述。"
        ),
    },
    "novel.chapter.draft": {
        "version": "3",
        "task_type": "章节正文",
        "context_policy": "chapter_draft",
        "body": (
            "你是中文小说写作者。请按本章合同写作正文，直接产出可落盘的小说文本。\n"
            "硬约束（违反即视为失败）：\n"
            "1. 只写正文，不写标题、不写创作说明、不写「以下是」之类的引导语；\n"
            "2. 不使用破折号（——）堆叠，不写「不是……而是……」这类对仗句式；\n"
            "3. 不用「突然、忽然、仿佛、只见、缓缓」这类套话起句；\n"
            "4. 对话用中文引号，人物说话要有信息差与个性，不要人人都伶牙俐齿；\n"
            "5. 不写「他知道」「她明白」这种全知旁白来替代动作与细节；\n"
            "6. 场景与情绪用具体的动作、物件、感官细节承载；\n"
            "7. 章末必须落在合同指定的钩子类型上，不要总结全文。\n"
            "写出约 {字数预算} 字（允许 ±15%）。"
        ),
    },
    "novel.chapter.continue": {
        "version": "2",
        "task_type": "续写",
        "context_policy": "chapter_draft",
        "body": (
            "你是中文小说写作者。承接已写好的段落继续往下写，保持人称、语气、"
            "节奏与既有细节一致。\n"
            "只输出接续的正文（不要重复已有内容，不要写标题或说明）。\n"
            "同一条硬约束：不堆破折号、不用「不是……而是……」、不用套话起句、"
            "对话有个性、不写全知旁白总结。"
        ),
    },
    "novel.review.contract": {
        "version": "2",
        "task_type": "审稿",
        "context_policy": "review",
        "body": (
            "你是严格的小说审稿编辑。请对照**章节合同**逐项验收正文。\n"
            "对每一个情节点、每一条「必须承上」，判断：已完成 / 未完成 / 待核实。\n"
            "规则：\n"
            '1. 只有正文中有明确证据才算「已完成」，并在"证据"里引用原文片段（不超过 40 字）；\n'
            '2. 找不到证据的一律「待核实」，**不得**因为"看起来差不多"就算通过；\n'
            '3. 正文与合同冲突（写了禁止事项）直接判「未完成」并说明。\n'
            "输出严格 JSON：\n"
            "{\n"
            '  "逐项": [{"项": "情节点原文", "判定": "已完成/未完成/待核实", "证据": "正文引用或空"}],\n'
            '  "一致性": [{"类型": "数值/设定口径/细节连续", "问题": "", "证据": ""}],\n'
            '  "结论": "通过/不通过",\n'
            '  "修改指令": ["不通过时给作者或改写者的具体指令"]\n'
            "}"
        ),
    },
    "novel.review.consistency": {
        "version": "1",
        "task_type": "一致性检查",
        "context_policy": "review",
        "body": (
            "你是小说一致性校对员。对照给定的设定与状态，检查本章是否存在：\n"
            "1. 数值算术错误（资源、人数、时间跨度）；\n"
            "2. 设定口径漂移（能力/规则/称谓与设定不符）；\n"
            "3. 细节连续性断裂（位置、持有物、伤势、谁知道什么）。\n"
            "只报告你有证据的问题，没有证据的不要写。输出严格 JSON：\n"
            '{"问题": [{"类型": "", "说明": "", "证据": "", "旧值": "", "新值": ""}]}'
        ),
    },
    "novel.deslop.soft": {
        "version": "1",
        "task_type": "去AI味软审",
        "context_policy": "review",
        "body": (
            "你是中文小说的文字编辑，专门清理「AI 腔」。逐行检查以下问题并给出最小修改建议：\n"
            "1. 解释腔（把动作的含义写出来）；2. 自问自答；3. 段尾总结升华；\n"
            "4. 过度因果（事事都要交代原因）；5. 排比堆砌；6. 情绪直给（直接说情绪词）；\n"
            "7. 人物说话过于工整、信息过载。\n"
            "输出严格 JSON：\n"
            '{"建议": [{"行": 12, "问题": "", "原文": "", "建议": ""}]}\n'
            "只列真正有问题的行，没问题就返回空数组。"
        ),
    },
    "novel.structure.extract": {
        "version": "1",
        "task_type": "结构化抽取",
        "context_policy": "ingest",
        "body": (
            "你是小说信息抽取器。从给定章节正文中抽取结构化信息，只输出 JSON：\n"
            "{\n"
            '  "摘要": "本章发生了什么，150 字以内，按因果写",\n'
            '  "出场角色": ["角色名"],\n'
            '  "时间线索": [{"线索": "原文中的时间表述", "依据": "原文片段"}],\n'
            '  "状态变更": [{"角色": "", "字段": "位置/持有物/伤势/心理/关系/能力", "新值": "", "依据": ""}],\n'
            '  "伏笔": [{"内容": "", "依据": ""}],\n'
            '  "资源变更": [{"物品": "", "增减": 0, "依据": ""}]\n'
            "}\n"
            "规则：所有条目必须附原文依据；没有的类目返回空数组；不要臆测。"
        ),
    },
    "novel.card.complete": {
        "version": "1",
        "task_type": "卡片补全",
        "context_policy": "asset_card",
        "body": (
            "你是小说设定整理助手。请把给定的设定片段补全为结构化卡片字段，只输出 JSON：\n"
            "{\n"
            '  "名称": "", "分类": "人物/世界/势力/物品/技能/场景/伏笔",\n'
            '  "摘要": "一句话", "字段": {"性格": "", "目标": "", "关系": ""},\n'
            '  "来源": "author/model/unknown"\n'
            "}\n"
            "约束：不得编造原文没有的信息；不确定的字段留空并在「来源」标注 unknown。"
        ),
    },
    "novel.teardown.dimensions": {
        "version": "1",
        "task_type": "拆书",
        "context_policy": "teardown",
        "body": (
            "你是网文拆解分析师。请按以下**自定维度**拆解给定样章（维度为工作台自建，非外部模板）：\n"
            "1. 开篇钩子（前 300 字做了什么、何时给冲突）；\n"
            "2. 爽点/情绪节拍（每 500 字一个可命名的节拍）；\n"
            "3. 主角行动线（做了什么决定、付出什么代价）；\n"
            "4. 信息节奏（伏笔埋设与揭示的位置）；\n"
            "5. 语言特征（句长、对话占比、口语化程度）；\n"
            "6. 可迁移手法（能用于本书的具体写法，1-3 条）。\n"
            "输出严格 JSON：\n"
            '{"维度": [{"名称": "", "结论": "", "章节依据": "第N章/段", "原文摘录": ""}]}\n'
            "规则：每条结论必须附章节依据与原句摘录；缺依据的条目请在「章节依据」写「缺失」。"
        ),
    },
    "novel.chat.assistant": {
        "version": "1",
        "task_type": "对话",
        "context_policy": "chat",
        "body": (
            "你是这位作者的创作陪练。基于给定的作品上下文回答与协助。\n"
            "原则：\n"
            "1. 不用空话鼓励，直接给可执行的建议或候选方案；\n"
            "2. 需要改稿时，产出**完整替换文本**或明确的差异片段，便于收件箱应用；\n"
            "3. 涉及设定冲突时先指出冲突，再给两种取舍及各自代价；\n"
            "4. 无法从上下文确认的信息，直接问，不要编。"
        ),
    },
    "novel.stuck.prompt": {
        "version": "1",
        "task_type": "灵感追问",
        "context_policy": "chat",
        "body": (
            "作者卡文了。请基于当前章节上下文，给出 3 个**不同方向**的后续走向候选，"
            "每个候选写：走向一句话、需要的前置铺垫、可能的代价/风险、章末钩子。\n"
            "不要写正文。"
        ),
    },
}


# ─────────────────────────── 注册表操作 ───────────────────────────


def ensure_registry() -> int:
    """把代码内的提示词幂等同步进 ``prompt_registry`` 表，返回同步条数。"""
    count = 0
    with db.get_conn() as conn:
        for prompt_id, spec in PROMPTS.items():
            conn.execute(
                "INSERT INTO prompt_registry (prompt_id, version, task_type, context_policy, body)"
                " VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(prompt_id, version) DO UPDATE SET"
                " task_type = excluded.task_type, context_policy = excluded.context_policy,"
                " body = excluded.body",
                (
                    prompt_id,
                    spec["version"],
                    spec.get("task_type", ""),
                    spec.get("context_policy", ""),
                    spec["body"],
                ),
            )
            count += 1
            # 历史版本保留（不删除旧版本，便于追溯）
    return count


def get_prompt(prompt_id: str, version: str | None = None) -> dict:
    """取提示词：优先代码内定义；未知 id 时查库（支持用户自定义注册）。"""
    spec = PROMPTS.get(prompt_id)
    if spec is not None and version in (None, spec["version"]):
        return {
            "prompt_id": prompt_id,
            "version": spec["version"],
            "task_type": spec.get("task_type", ""),
            "context_policy": spec.get("context_policy", ""),
            "body": spec["body"],
            "source": "builtin",
        }

    with db.get_conn() as conn:
        if version:
            row = conn.execute(
                "SELECT prompt_id, version, task_type, context_policy, body"
                " FROM prompt_registry WHERE prompt_id = ? AND version = ?",
                (prompt_id, version),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT prompt_id, version, task_type, context_policy, body"
                " FROM prompt_registry WHERE prompt_id = ? ORDER BY id DESC LIMIT 1",
                (prompt_id,),
            ).fetchone()
    if row is None:
        raise NodeNotFoundError(f"未注册的提示词：{prompt_id}")
    return {
        "prompt_id": row["prompt_id"],
        "version": row["version"],
        "task_type": row["task_type"],
        "context_policy": row["context_policy"],
        "body": row["body"],
        "source": "registry",
    }


def find_by_task(task_type: str) -> dict | None:
    """按任务类型找提示词（决策表与管线用）。"""
    for prompt_id, spec in PROMPTS.items():
        if spec.get("task_type") == task_type:
            return get_prompt(prompt_id)
    return None


def list_prompts() -> list[dict]:
    """列出全部提示词（注册状态 + 版本）。"""
    ensure_registry()
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT prompt_id, version, task_type, context_policy, created_at"
            " FROM prompt_registry ORDER BY task_type, prompt_id, version"
        ).fetchall()
    return [
        {
            "prompt_id": row["prompt_id"],
            "version": row["version"],
            "task_type": row["task_type"],
            "context_policy": row["context_policy"],
            "created_at": row["created_at"],
            "current": PROMPTS.get(row["prompt_id"], {}).get("version") == row["version"],
        }
        for row in rows
    ]


def render(prompt_id: str, variables: dict | None = None) -> dict:
    """渲染提示词：替换 ``{变量}`` 占位（缺失变量保持原样，便于人工发现）。"""
    prompt = get_prompt(prompt_id)
    body = prompt["body"]
    for key, value in (variables or {}).items():
        body = body.replace("{" + str(key) + "}", str(value))
    return {**prompt, "body": body}


__all__ = [
    "PROMPTS",
    "ensure_registry",
    "find_by_task",
    "get_prompt",
    "list_prompts",
    "render",
]