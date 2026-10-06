#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""设定与状态卡校验门禁（只报告不改写）—— 自写模块（非第三方引入）。

字段事实源
----------
* **设定卡必填字段**：``skills/novel-setting/references/设定字段规范.md``
  （人物 / 世界规则 / 势力 / 物品 / 技能 / 场景各卡的字段表，
  以及「来源三级：``author`` ＝ 作者直接给的 / ``model`` ＝ 从正文提炼的 /
  ``unknown`` ＝ 无法判定」）；伏笔卡字段取
  ``skills/foreshadow-check/references/伏笔台账规范.md``；
* **状态卡字段**（``状态`` 分类）：``skills/novel-setting/references/状态维护细则.md``
  「角色状态：位置、伤势与体力、当前心理、随身物、对外关系值、缺口、最后更新章」；
* **分类与文件名**：与 ``templates/默认模板/设定/*.md``、``templates/默认模板/状态/*.md``
  的标题一致（模板正文只有 frontmatter、没有字段示例，因此字段清单以 references 明细为准；
  模板只提供分类与文件名这一事实源）。
  亦与 ``templates`` 对应的 ``asset_card_service.BASE_CATEGORIES`` 一致，另加 ``状态``。

校验项（确定性，只报告）
------------------------
1. 每个条目块缺少必填字段 → ``severity: "error"``（判失败）；
2. 缺少来源分级标注（或取值不在三级内）→ ``severity: "warn"``
   （作者可能还没标，只提示，不判失败）；
3. ``category`` 为空或未知 → 返回 ``passed: False`` 并在 ``detail`` 里列出可选分类
   （不抛异常，交给对话层转述）。

本模块**只报告，不改动任何文件**。
"""

from __future__ import annotations

import re
from pathlib import Path

# ── 判定常量（唯一事实源） ────────────────────────────────────
SEVERITY_ERROR = "error"
SEVERITY_WARN = "warn"

#: 来源分级取值（见 设定字段规范.md「来源三级」）
SOURCE_GRADES: tuple[str, ...] = ("author", "model", "unknown")

#: 分类 → 对应文件（与 templates/默认模板 下的实际文件同名）
CATEGORY_FILES: dict[str, str] = {
    "人物": "设定/人物设定.md",
    "世界": "设定/世界设定.md",
    "势力": "设定/势力设定.md",
    "物品": "设定/物品设定.md",
    "技能": "设定/技能设定.md",
    "场景": "设定/场景设定.md",
    "伏笔": "设定/伏笔管理.md",
    "状态": "状态/角色状态.md",
}

#: 分类 → 必填字段清单（对齐 references 明细，是本门禁的唯一事实源）。
#: 注意：``来源`` / ``来源/依据`` 不列入必填字段，改由「来源分级」检查覆盖
#: （缺失按 warn 报告），避免同一件事报两遍、severity 打架。
REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "人物": ("姓名", "别名", "身份", "目标", "动机", "能力边界", "关系",
             "语言习惯", "已知信息边界", "状态锚点"),
    "世界": ("规则陈述", "适用范围", "代价", "例外", "首次出现"),
    "势力": ("名称", "性质", "当前目标", "资源与地盘", "内部派系",
             "与主角的关系", "首次出现"),
    "物品": ("名称", "来历", "当前持有人", "当前所在地", "关键属性", "首次出现"),
    "技能": ("名称", "境界", "能做什么", "代价", "已知掌握者", "首次出现"),
    "场景": ("地名", "地理关系", "气候与地貌", "当地势力", "功能", "首次出现"),
    "伏笔": ("伏笔ID", "内容", "埋设章", "依据原文", "回收状态", "计划回收章"),
    "状态": ("位置", "伤势", "当前心理", "随身物", "关系值", "缺口", "最后更新章"),
}

#: 这些标题的块不是「条目」（规则 / 说明 / 示例小节），不参与字段完整性检查
NON_ENTRY_TITLES: tuple[str, ...] = (
    "总则", "通用规则", "规则说明", "字段清单", "来源分级", "示例", "正反例",
    "注意", "备注", "待定", "结账", "自查", "验收", "常见失败模式",
)

_ENTRY_HEADING_RE = re.compile(r"^\s{0,3}(#{2,4})\s*(\S[^\n#]{0,40}?)\s*$")
_ENTRY_HEADING_H1_RE = re.compile(r"^\s{0,3}#\s*(\S[^\n#]{0,40}?)\s*$")
_SOURCE_RE = re.compile(r"来源\s*[:：]\s*([A-Za-z]+)")
_FIELD_SEPARATOR_RE = re.compile(r"[:：]|｜|\|")


def category_from_path(rel_path: str) -> str:
    """按文件名确定性推断分类（``设定/人物设定.md`` → ``人物``）；推断不出返回空串。"""
    name = Path(str(rel_path or "").replace("\\", "/")).name
    for category, path in CATEGORY_FILES.items():
        if name == Path(path).name:
            return category
    stem = name[:-3] if name.endswith(".md") else name
    if stem.endswith("设定") and stem[:-2] in REQUIRED_FIELDS:
        return stem[:-2]
    return ""


def _split_entries(text: str) -> list[tuple[int, str, str]]:
    """把文件切成条目块：优先 H2–H4，其次 H1，最后退化为逐行条目。"""
    lines = str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    heading_indexes = [index for index, line in enumerate(lines) if _ENTRY_HEADING_RE.match(line)]
    pattern = _ENTRY_HEADING_RE
    if not heading_indexes:
        heading_indexes = [index for index, line in enumerate(lines)
                           if _ENTRY_HEADING_H1_RE.match(line)]
        pattern = _ENTRY_HEADING_H1_RE
    if not heading_indexes:
        return [(index, line.strip()[:20], line.strip())
                for index, line in enumerate(lines, start=1) if line.strip()]

    entries: list[tuple[int, str, str]] = []
    for position, index in enumerate(heading_indexes):
        end = heading_indexes[position + 1] if position + 1 < len(heading_indexes) else len(lines)
        match = pattern.match(lines[index])
        title = (match.group(2) if pattern is _ENTRY_HEADING_RE else match.group(1)).strip()
        entries.append((index + 1, title, "\n".join(lines[index + 1:end])))
    return entries


def run(text: str, *, category: str = "") -> dict:
    """按分类校验设定 / 状态卡的字段完整性与来源分级标注。

    :param text: 目标文件（如 ``设定/人物设定.md``）的文本内容。
    :param category: ``人物`` / ``世界`` / ``势力`` / ``物品`` / ``技能`` / ``场景`` /
                     ``伏笔`` / ``状态``；为空或未知时返回 ``passed: False`` 并列出可选分类。
    :return: ``{"key", "passed", "category", "detail", "findings"}``；
             ``findings`` 每项含 ``line`` / ``message`` / ``severity`` / ``field``。
    """
    name = str(category or "").strip()
    options = "、".join(REQUIRED_FIELDS)
    if name not in REQUIRED_FIELDS:
        return {
            "key": "设定校验",
            "passed": False,
            "category": name,
            "detail": f"未指定可用分类「{name}」：请传 category=（{options}）",
            "findings": [],
        }

    required = REQUIRED_FIELDS[name]
    findings: list[dict] = []
    checked = 0
    for line_number, title, body in _split_entries(text):
        if not body.strip():
            continue  # 空块（只有标题 / 空文件）没有可校验的字段
        if any(word in title for word in NON_ENTRY_TITLES):
            continue
        checked += 1
        missing = [field for field in required if field not in body]
        if missing and len(missing) == len(required) and not _FIELD_SEPARATOR_RE.search(body):
            findings.append({"line": line_number, "severity": SEVERITY_ERROR, "field": "",
                             "message": f"条目「{title}」未按字段写法给出任何必填字段"})
        else:
            for field in missing:
                findings.append({"line": line_number, "severity": SEVERITY_ERROR, "field": field,
                                 "message": f"条目「{title}」缺少必填字段：{field}"})
        source = _SOURCE_RE.search(body)
        if source is None:
            findings.append({"line": line_number, "severity": SEVERITY_WARN, "field": "来源",
                             "message": f"条目「{title}」缺少来源分级标注（{'/'.join(SOURCE_GRADES)}）"})
        elif source.group(1).lower() not in SOURCE_GRADES:
            findings.append({"line": line_number, "severity": SEVERITY_WARN, "field": "来源",
                             "message": (f"条目「{title}」来源分级取值不规范：{source.group(1)}"
                                         f"（只允许 {'/'.join(SOURCE_GRADES)}）")})

    errors = sum(1 for item in findings if item["severity"] == SEVERITY_ERROR)
    warns = len(findings) - errors
    return {
        "key": "设定校验",
        "passed": errors == 0,
        "category": name,
        "detail": f"分类「{name}」：检查 {checked} 个条目，缺必填字段 {errors} 项，来源分级待补 {warns} 项",
        "findings": findings,
    }
