#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""中文成稿硬禁令与模型化形状检查（prose gate）—— 由 MIT 项目引入并改造。

来源声明（第三方引入登记见 docs/third-party-notices.md）
---------------------------------------------------------------------------
原项目   : huahuakandaima__human-novel-writer
许可证   : MIT License (Copyright (c) 2026 huahuakandaima)
原文件   : projects/huahuakandaima__human-novel-writer/scripts/check_prose.py
引入日期 : 2026-09-19
修改说明 :
  1. 文件头补充本来源声明（原文件说明为「检查中文成稿的硬禁令与常见模型化形状。
     只报警，不自动改文。」）；
  2. **检测逻辑、词表与全部阈值保持不变**（阈值来自人工段 vs AI 段的同文对照实测）；
  3. 硬编码词表 / 阈值抽为模块级常量，并支持 `ProseConfig` 覆盖（配置化）；
  4. 新增 `run(text, config=None, **overrides) -> ProseResult` 的 import 接口，
     同时保留原 CLI 入口与退出码语义（0 = 无 failures，1 = 有 failures，2 = 读取失败）。
许可全文 : projects/huahuakandaima__human-novel-writer/LICENSE
---------------------------------------------------------------------------

CLI 用法::

    python workbench/backend/gates/prose_gate.py 稿件.md
    python workbench/backend/gates/prose_gate.py -        # 从标准输入读取

import 用法::

    from workbench.backend.gates import prose_gate

    result = prose_gate.run(text)
    if not result.passed:
        print(result.report())
"""

from __future__ import annotations

import argparse
import collections
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

# ── 词表（唯一事实源，改这里即改门禁） ─────────────────────────
HARD_STOPS: tuple[str, ...] = (
    "说白了",
    "说穿了",
    "先说结论",
)

HARD_JARGON: tuple[str, ...] = (
    "赋能",
    "抓手",
    "商业闭环",
    "价值闭环",
    "能力沉淀",
    "拉通",
    "底层逻辑",
    "顶层设计",
    "认知跃迁",
    "价值释放",
    "能力建设",
    "降本增效",
    "内容矩阵",
    "全链路",
    "组合拳",
    "打开想象空间",
    "结构性机会",
    "关键命题",
    "深层逻辑",
    "技术底座",
    "公共底座",
    "技术主权",
    "单点风险",
    "主脊柱",
    "材料锚点",
    "认知增量",
    "迭代闭环",
)

CONTEXT_JARGON: tuple[str, ...] = (
    "沉淀",
    "颗粒度",
    "对齐",
    "协同",
    "链路",
    "生态位",
    "心智",
    "范式",
    "方法论",
    "核心变量",
    "打法",
    "想象空间",
    "闭环",
    "不丢",
)

LYRIC_WORDS: tuple[str, ...] = (
    "安放",
    "抵达",
    "微光",
    "褶皱",
    "丰盈",
    "滚烫",
    "轻盈",
    "赤裸",
    "剥开",
)

ROAD_SIGNS: tuple[str, ...] = (
    "更微妙的是",
    "还有一层",
    "只说对了一半",
    "值得注意的是",
    "需要指出的是",
    "从某种意义上说",
)

ROAD_STRIP_CHARS = "。！？!? \n"

FORBIDDEN_PUNCTUATION: dict[str, str] = {
    "：": "中文冒号",
    ":": "英文冒号",
    "—": "破折号",
    "–": "连接号式破折号",
}

PIVOT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?:并)?不是[^。！？\n]{0,90}而是"),
    re.compile(r"并非[^。！？\n]{0,90}而是"),
    re.compile(r"不在于[^。！？\n]{0,90}而在于"),
    re.compile(r"与其说[^。！？\n]{0,90}(?:不如|毋宁|倒不如)"),
    re.compile(r"[。！？!?]\s*而是"),
    re.compile(r"表面(?:上)?[^。！？\n]{0,90}(?:其实|实际|实则)"),
    re.compile(r"看似[^。！？\n]{0,90}(?:其实|实际|实则)"),
)

SEMANTIC_PIVOT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?:总|一直|曾|都)?以为[^！？\n]{2,60}?(?:其实|才发现|才明白|才知道|后来才)"),
    re.compile(r"(?:总|都|一直)以为[^！？\n]{2,60}?[。，](?:可|但|其实)"),
    re.compile(r"回头(?:看|一看)?才(?:发现|明白|知道)"),
    re.compile(r"(?:并)?不是[^。！？\n]{1,40}，(?:更|才)?是[^，。！？\n]"),
    re.compile(r"从来(?:都)?(?:不是|与[^。！？，\n]{1,12}无关)"),
    re.compile(r"答案(?:是否定的|恰恰相反)|恰恰相反"),
    re.compile(r"表面(?:上)?[^！？\n]{0,60}。[^！？\n]{0,12}(?:其实|实际|实则)"),
    re.compile(r"看似[^！？\n]{0,60}。[^！？\n]{0,12}(?:其实|实际|实则)"),
    re.compile(r"[^，。！？\n]{1,12}不重要，(?:重要|要紧)的是"),
    re.compile(r"真正[^，。！？\n]{0,16}的(?:，)?是"),
    re.compile(r"不只(?:是)?[^。！？\n]{0,90}(?:还|也)"),
)

NOMINALIZATION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"进行(?:了|一次|一场|着)?[^。，！？\n]{0,10}"
        r"(?:调整|优化|升级|分析|讨论|沟通|梳理|复盘|迭代|探索|尝试|思考|规划|布局)"
    ),
    re.compile(r"实现了?[^。，！？\n]{0,14}的?[^。，！？\n]{0,6}(?:提升|增长|突破|转变|跃升|落地)"),
    re.compile(r"完成了?对[^。，！？\n]{0,16}的"),
    re.compile(r"起到了?[^。，！？\n]{0,12}的?作用"),
    re.compile(r"具有[^。，！？\n]{0,10}(?:意义|价值)"),
)

CONJUNCTIONS: tuple[str, ...] = (
    "因为",
    "所以",
    "但是",
    "然而",
    "同时",
    "此外",
    "而且",
    "并且",
    "因此",
    "不仅",
)

ROAD_SIGN_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        rf"(?:^|[。！？!?]\s*){re.escape(ROAD_SIGNS[0])}[^。！？!?\n]{{0,24}}",
        re.MULTILINE,
    ),
    re.compile(
        rf"(?:^|[。！？!?]\s*){re.escape(ROAD_SIGNS[1])}"
        r"(?=(?:更|原因|问题|意思|考虑|变化|逻辑|价值|作用|风险|影响|值得|很少|不容易|常被|往往))"
        r"[^。！？!?\n]{0,24}",
        re.MULTILINE,
    ),
    *(
        re.compile(
            rf"(?:^|[。！？!?]\s*){re.escape(phrase)}[^。！？!?\n]{{0,24}}",
            re.MULTILINE,
        )
        for phrase in ROAD_SIGNS[2:]
    ),
)

SOFT_MARKERS: tuple[str, ...] = (
    "真正",
    "本质上",
    "更深层次",
    "归根结底",
    "换句话说",
    "不可否认",
    "核心是",
    "关键在于",
    "这意味着",
)

REPEATED_OPENERS: tuple[str, ...] = (
    "其实",
    "不过",
    "当然",
    "所以",
    "但是",
    "后来",
    "当时",
    "很多人",
    "问题是",
    "更重要的是",
    "说到这里",
)

LEFT_BRANCH_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?:^|[。！？]\s*)在[^，。！？\n]{12,70}(?:以后|之后|之前|以前|过程中|情况下|背景下)，"),
    re.compile(r"(?:^|[。！？]\s*)那些[^，。！？\n]{10,60}的[^，。！？\n]{2,30}[，。]"),
    re.compile(r"(?:^|[。！？]\s*)(?:真正|最终|最后)让[^，。！？\n]{8,70}的，是"),
)

METAPHOR_FIELDS: dict[str, tuple[str, ...]] = {
    "温度": ("降温", "升温", "冷却", "余温", "温度最高"),
    "生死战争": ("杀死", "死因", "枪响", "开火", "战场", "引爆", "弹药"),
    "建筑灾害": ("坍塌", "崩塌", "地基", "砖头", "支柱", "废墟"),
    "仓储租赁": ("仓库", "库房", "租金", "取货", "入库", "库存"),
    "道路竞赛": ("赛道", "跑道", "岔路", "十字路口", "终点线", "门票"),
    "机器器官": ("齿轮", "引擎", "发动机", "血管", "骨架", "肌肉"),
    "海洋航行": ("蓝海", "浪潮", "潮水", "航船", "灯塔", "彼岸"),
}

HAN_RE = re.compile(r"[\u4e00-\u9fff]")
SENTENCE_RE = re.compile(r"[^。！？!?\n]+(?:[。！？!?]|$)")


@dataclass(frozen=True)
class ProseConfig:
    """prose 门禁可配置参数。默认值即原脚本硬编码阈值（保持不变）。"""

    # 排比 / 长句
    anaphora_min: int = 3
    heavy_de_han: int = 38
    heavy_de_count: int = 4
    # 句长变异系数
    cv_min_sentences: int = 12
    cv_min_sentence_han: int = 4
    sentence_cv_min: float = 0.42
    # 密度类（每千字 / 每 N 字）
    conjunction_min_han: int = 600
    conjunction_per_1000: float = 7.0
    highlight_base: int = 3
    highlight_per_han: int = 700
    marker_base: int = 2
    marker_per_han: int = 900
    left_branch_base: int = 2
    left_branch_per_han: int = 1200
    dense_de_base: int = 1
    dense_de_per_han: int = 1500
    # 段落形态
    one_sentence_min_paragraphs: int = 10
    one_sentence_ratio: float = 0.75
    short_streak_limit: int = 4
    short_para_han: int = 24
    short_para_sentences: int = 1
    repeated_opener_min: int = 4
    # 借喻聚簇
    metaphor_distance: int = 800
    metaphor_fields_min: int = 3
    # 报告截断
    max_anaphora_reports: int = 4
    max_nominalization_reports: int = 4


@dataclass
class ProseResult:
    """prose 门禁结果对象。`passed` 为 False 时 `failures` 非空。"""

    han_count: int
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    counters: dict[str, int] = field(default_factory=dict)
    config: ProseConfig = field(default_factory=ProseConfig)

    @property
    def passed(self) -> bool:
        return not self.failures

    def report(self) -> str:
        """渲染与原 CLI 一致的报告文本（不含退出码）。"""
        c = self.counters
        lines: list[str] = [
            f"汉字数 {self.han_count}",
            (
                f"翻案句 {c.get('pivots', 0)}，翻案腔变形 {c.get('semantic_pivots', 0)}，"
                f"同构排比 {c.get('anaphoras', 0)}，名词化 {c.get('nominalizations', 0)}，"
                f"黑话 {c.get('jargon', 0)}，硬停词 {c.get('hard_stops', 0)}，"
                f"模型路标 {c.get('road_signs', 0)}，需辨语境词 {c.get('context_jargon', 0)}，"
                f"抒情词 {c.get('lyric', 0)}，洞察路标 {c.get('markers', 0)}，"
                f"长前置成分 {c.get('left_branches', 0)}，重定语句 {c.get('dense_de', 0)}"
            ),
        ]
        if self.failures:
            lines.append("")
            lines.append("需要修改")
            lines.extend(f"- {item}" for item in self.failures)
        if self.warnings:
            lines.append("")
            lines.append("需要人工判断")
            lines.extend(f"- {item}" for item in self.warnings)
        if not self.failures and not self.warnings:
            lines.append("")
            lines.append("未发现这份检查器覆盖的问题。")
        return "\n".join(lines)


@dataclass
class Paragraph:
    position: int
    text: str
    han: int
    sentences: int


# ── 基础工具 ──────────────────────────────────────────────────
def han_count(text: str) -> int:
    return len(HAN_RE.findall(text))


def line_number(text: str, position: int) -> int:
    return text.count("\n", 0, position) + 1


def excerpt(value: str, width: int = 72) -> str:
    value = re.sub(r"\s+", " ", value).strip()
    return value if len(value) <= width else value[: width - 1] + "…"


def mask_non_prose(text: str) -> str:
    """屏蔽代码、网址和机器元数据，同时保留字符位置与换行。"""

    def mask(match: re.Match[str]) -> str:
        return "".join("\n" if char == "\n" else " " for char in match.group())

    patterns = (
        re.compile(r"\A---\s*\n.*?\n---\s*(?:\n|\Z)", re.DOTALL),
        re.compile(r"```.*?```", re.DOTALL),
        re.compile(r"`[^`\n]*`"),
        re.compile(r"\]\([^\n)]*\)"),
        re.compile(r"https?://[^\s)>]+"),
        re.compile(r"<[^>\n]+>"),
    )
    masked = text
    for pattern in patterns:
        masked = pattern.sub(mask, masked)
    return masked


def non_overlapping_terms(text: str, terms: tuple[str, ...]) -> list[tuple[int, str]]:
    matches: list[tuple[int, str]] = []
    occupied: list[tuple[int, int]] = []
    for term in sorted(terms, key=len, reverse=True):
        for match in re.finditer(re.escape(term), text):
            start, end = match.span()
            if any(start < old_end and end > old_start for old_start, old_end in occupied):
                continue
            matches.append((start, term))
            occupied.append((start, end))
    return sorted(matches)


def all_matches(text: str, patterns: tuple[re.Pattern[str], ...]) -> list[re.Match[str]]:
    matches: list[re.Match[str]] = []
    for pattern in patterns:
        matches.extend(pattern.finditer(text))
    return sorted(matches, key=lambda match: match.start())


def heavy_de_sentences(text: str, config: ProseConfig):
    """找出主干可能被多个“的”压到后面的长句。"""

    matches = []
    for match in SENTENCE_RE.finditer(text):
        value = match.group()
        if han_count(value) >= config.heavy_de_han and value.count("的") >= config.heavy_de_count:
            matches.append(match)
    return matches


def anaphora_runs(text: str, minimum: int = 3):
    """找出同一句里三个以上小句用同一个开头的排比。"""

    matches = []
    for sentence in SENTENCE_RE.finditer(text):
        clauses = [
            clause.strip()
            for clause in re.split(r"[，、；,;]", sentence.group())
            if han_count(clause) >= 3
        ]
        if len(clauses) < minimum:
            continue
        run = 1
        for previous, current in zip(clauses, clauses[1:]):
            if previous[:2] == current[:2] and re.match(r"[一-鿿]{2}", current):
                run += 1
                if run >= minimum:
                    matches.append(sentence)
                    break
            else:
                run = 1
    return matches


def sentence_length_cv(text: str, config: ProseConfig):
    """句长变异系数。人写的长短句差距大，模型的句长彼此接近。"""

    lengths = [
        han_count(match.group())
        for match in re.finditer(r"[^。！？!?\n]+[。！？!?]", text)
        if han_count(match.group()) >= config.cv_min_sentence_han
    ]
    if len(lengths) < config.cv_min_sentences:
        return None
    mean = sum(lengths) / len(lengths)
    if mean == 0:
        return None
    variance = sum((value - mean) ** 2 for value in lengths) / len(lengths)
    return (variance ** 0.5) / mean, len(lengths)


def bracket_highlights(text: str):
    """「」括起来的短语。太密说明在批量造金句。"""

    return list(re.finditer(r"[「『][^」』\n]{1,6}[」』]", text))


def prose_paragraphs(text: str) -> list[Paragraph]:
    paragraphs = []
    cursor = 0
    for block in re.split(r"\n\s*\n", text):
        position = text.find(block, cursor)
        cursor = max(position + len(block), cursor)
        clean = re.sub(r"[>*_`]", "", block).strip()
        if not clean or clean.startswith(("#", "http", "![", "```")):
            continue
        if re.match(r"^(?:[-+*]|\d+[.、])\s", clean):
            continue
        count = han_count(clean)
        if count < 4:
            continue
        sentences = max(1, len(re.findall(r"[。！？!?]", clean)))
        paragraphs.append(Paragraph(position, clean, count, sentences))
    return paragraphs


def metaphor_cluster(text: str, distance: int = 800, fields_min: int = 3):
    hits = []
    for field, words in METAPHOR_FIELDS.items():
        for word in words:
            for match in re.finditer(re.escape(word), text):
                hits.append((match.start(), field, word))
    hits.sort()
    for index, (start, _, _) in enumerate(hits):
        window = [hit for hit in hits[index:] if hit[0] - start <= distance]
        fields = {hit[1] for hit in window}
        if len(fields) >= fields_min:
            return window, fields
    return None


def short_streak(paragraphs: list[Paragraph], config: ProseConfig):
    streak = []
    for paragraph in paragraphs:
        if paragraph.han <= config.short_para_han and paragraph.sentences <= config.short_para_sentences:
            streak.append(paragraph)
            if len(streak) >= config.short_streak_limit:
                return streak
        else:
            streak = []
    return None


def opener_counts(paragraphs: list[Paragraph]):
    counts: collections.Counter[str] = collections.Counter()
    examples: dict[str, int] = {}
    for paragraph in paragraphs:
        value = paragraph.text.lstrip("“‘\"（(")
        for opener in REPEATED_OPENERS:
            if value.startswith(opener):
                counts[opener] += 1
                examples.setdefault(opener, paragraph.position)
                break
    return counts, examples


def dialogue_attribution_warnings(text: str) -> list[str]:
    """对话归属启发式：无说话人标签的对话行，且上一行也是对话行时报警。

    只报警不判错（对话归属最终靠人工确认）：连续两个对话回合中，
    第二个回合若无"说/问/喊/道/声音"等线索，说话人切换时读者会误读。
    实例：'主角？' 无标签 → 读者默认归上一个说话人，实为另一角色发问。
    """
    hints = re.compile(r"[说问道喊答开口嗓子声音顿了顿接话沉吟喃喃低语嘀咕]")
    warnings: list[str] = []
    lines = text.splitlines()
    prev_was_dialog = False
    for idx, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped:
            continue  # 空行不重置对话状态（对话段落间常有空行）
        if not stripped.startswith(("“", "「", "‘", '"', "'")):
            prev_was_dialog = False
            continue
        has_hint = bool(hints.search(line))
        if prev_was_dialog and not has_hint:
            warnings.append(
                f"第 {idx} 行对话无说话人标签，且上一行也是对话——"
                f"若说话人已切换，必须补标签（名字优先）。"
            )
        prev_was_dialog = True
    return warnings


# ── 主检测 ────────────────────────────────────────────────────
def run(
    text: str,
    config: ProseConfig | None = None,
    **overrides: object,
) -> ProseResult:
    """对中文成稿执行硬禁令与模型化形状检查。

    :param text: 稿件原文（Markdown 或纯文本）。
    :param config: 完整配置；缺省使用模块级默认常量。
    :param overrides: 覆盖 `ProseConfig` 单个字段。
    :return: :class:`ProseResult`
    """
    cfg = config or ProseConfig()
    if overrides:
        cfg = ProseConfig(**{**cfg.__dict__, **overrides})

    prose = mask_non_prose(text)
    total_han = han_count(prose)
    if total_han == 0:
        return ProseResult(
            han_count=0,
            failures=["没有检测到汉字。"],
            warnings=[],
            counters={},
            config=cfg,
        )

    failures: list[str] = []
    warnings: list[str] = []

    # 1. 标点（冒号/破折号降为提示；密度红线由 density_gate 统一判定）
    quote_colons = []
    for symbol, label in FORBIDDEN_PUNCTUATION.items():
        matches = list(re.finditer(re.escape(symbol), prose))
        if symbol in ("：", ":"):
            hard = []
            for match in matches:
                tail = prose[match.end(): match.end() + 2].lstrip()
                if tail[:1] in ("「", "『", "“", "‘", '"'):
                    quote_colons.append(match)
                else:
                    hard.append(match)
            matches = hard
        if matches:
            lines = "、".join(str(line_number(text, match.start())) for match in matches[:8])
            warnings.append(
                f"{label}共 {len(matches)} 处（作为标点提示，密度红线以 density_check 为准），"
                f"出现在第 {lines} 行。"
            )
    if quote_colons:
        lines = "、".join(
            str(line_number(text, match.start())) for match in quote_colons[:8]
        )
        warnings.append(
            f"引出原话的冒号 {len(quote_colons)} 处，第 {lines} 行。"
            "确认引号里确实是原话，且不是提示性用法。"
        )

    # 2. 硬停词
    stop_matches = non_overlapping_terms(prose, HARD_STOPS)
    for position, phrase in stop_matches:
        failures.append(f"硬停词，第 {line_number(text, position)} 行，{phrase}")

    # 3. 黑话（硬）
    jargon_matches = non_overlapping_terms(prose, HARD_JARGON)
    for position, phrase in jargon_matches:
        failures.append(f"黑话，第 {line_number(text, position)} 行，{phrase}")

    # 4. 需辨语境词（软）
    context_jargon_matches = non_overlapping_terms(prose, CONTEXT_JARGON)
    hard_spans = [
        (position, position + len(phrase)) for position, phrase in jargon_matches
    ]
    context_jargon_matches = [
        (position, phrase)
        for position, phrase in context_jargon_matches
        if not any(
            position < end and position + len(phrase) > start
            for start, end in hard_spans
        )
    ]
    if context_jargon_matches:
        samples = "、".join(
            dict.fromkeys(phrase for _, phrase in context_jargon_matches)
        )
        lines = "、".join(
            dict.fromkeys(
                str(line_number(text, position))
                for position, _ in context_jargon_matches[:8]
            )
        )
        warnings.append(
            f"有 {len(context_jargon_matches)} 处词语需要结合语境判断。"
            f"第 {lines} 行出现 {samples}。本义准确时保留，用来抬价时改写。"
        )

    # 5. 模型路标
    road_signs = all_matches(prose, ROAD_SIGN_PATTERNS)
    for match in road_signs:
        failures.append(
            f"模型路标，第 {line_number(text, match.start())} 行，"
            f"“{excerpt(match.group().lstrip(ROAD_STRIP_CHARS))}”"
        )

    # 6. 翻案句（硬）
    pivots = all_matches(prose, PIVOT_PATTERNS)
    for match in pivots:
        failures.append(
            f"禁用翻案句，第 {line_number(text, match.start())} 行，"
            f"“{excerpt(match.group())}”"
        )

    # 7. 翻案腔变形（软）
    occupied_spans = [match.span() for match in pivots]
    semantic_pivots = []
    for match in all_matches(prose, SEMANTIC_PIVOT_PATTERNS):
        if any(
            match.start() < end and match.end() > start
            for start, end in occupied_spans
        ):
            continue
        semantic_pivots.append(match)
        occupied_spans.append(match.span())
    for match in semantic_pivots:
        warnings.append(
            f"疑似翻案腔变形，第 {line_number(text, match.start())} 行，"
            f"“{excerpt(match.group(), 44)}”。先立误解再推翻就改成正面陈述，正常用法保留。"
        )

    # 8. 同构排比（软）
    anaphoras = anaphora_runs(prose, cfg.anaphora_min)
    for match in anaphoras[: cfg.max_anaphora_reports]:
        warnings.append(
            f"三连以上同构排比，第 {line_number(text, match.start())} 行，"
            f"“{excerpt(match.group(), 44)}”。留两项，第三项换说法或删掉。"
        )

    # 9. 抒情词（软）
    lyric_matches = non_overlapping_terms(prose, LYRIC_WORDS)
    if len(lyric_matches) >= 2:
        samples = "、".join(dict.fromkeys(term for _, term in lyric_matches))
        warnings.append(
            f"模型偏爱的抒情词 {len(lyric_matches)} 处。{samples}。"
            "写具体事物时保留，给抽象概念穿衣服时删掉。"
        )

    # 10. 名词化（软）
    nominalizations = all_matches(prose, NOMINALIZATION_PATTERNS)
    for match in nominalizations[: cfg.max_nominalization_reports]:
        warnings.append(
            f"名词化句式，第 {line_number(text, match.start())} 行，"
            f"“{excerpt(match.group(), 36)}”。还原成直接的动词。"
        )

    # 11. 连词密度（软）
    conjunction_hits = non_overlapping_terms(prose, CONJUNCTIONS)
    if (
        total_han >= cfg.conjunction_min_han
        and len(conjunction_hits) * 1000 / total_han > cfg.conjunction_per_1000
    ):
        samples = "、".join(
            f"{term} {count} 次"
            for term, count in collections.Counter(
                term for _, term in conjunction_hits
            ).most_common(4)
        )
        warnings.append(
            f"连词密度偏高，每千字 {len(conjunction_hits) * 1000 // total_han} 个。{samples}。"
            "中文小句靠语序和事理相接，删掉一半试试。"
        )

    # 12. 金句括注（软）
    highlights = bracket_highlights(prose)
    highlight_limit = max(cfg.highlight_base, total_han // cfg.highlight_per_han)
    if len(highlights) > highlight_limit:
        samples = "、".join(dict.fromkeys(match.group() for match in highlights[:6]))
        warnings.append(
            f"「」括起的短语共 {len(highlights)} 处。{samples}。太密说明在批量造金句。"
        )

    # 13. 句长变异系数（软）
    cv_result = sentence_length_cv(prose, cfg)
    if cv_result and cv_result[0] < cfg.sentence_cv_min:
        warnings.append(
            f"全文 {cv_result[1]} 个句子长度过于接近（变异系数 {cv_result[0]:.2f}）。"
            "人写的段落里十个字的句子会挨着四十个字的句子，放开几句，压短几句。"
        )

    # 14. 洞察路标（软）
    marker_matches = non_overlapping_terms(prose, SOFT_MARKERS)
    marker_limit = max(cfg.marker_base, total_han // cfg.marker_per_han)
    if len(marker_matches) > marker_limit:
        samples = "、".join(dict.fromkeys(term for _, term in marker_matches))
        warnings.append(
            f"洞察路标共 {len(marker_matches)} 处，当前提醒线为 {marker_limit} 处。"
            f"重点检查 {samples}。"
        )

    # 15. 长前置成分（软）
    left_branches = all_matches(prose, LEFT_BRANCH_PATTERNS)
    left_limit = max(cfg.left_branch_base, total_han // cfg.left_branch_per_han)
    if len(left_branches) > left_limit:
        samples = "；".join(
            f"第 {line_number(text, match.start())} 行“{excerpt(match.group(), 44)}”"
            for match in left_branches[:4]
        )
        warnings.append(
            f"长前置成分共 {len(left_branches)} 处，可能让主干来得太晚。{samples}"
        )

    # 16. 重定语句（软）
    dense_de = heavy_de_sentences(prose, cfg)
    dense_de_limit = max(cfg.dense_de_base, total_han // cfg.dense_de_per_han)
    if len(dense_de) > dense_de_limit:
        samples = "；".join(
            f"第 {line_number(text, match.start())} 行“{excerpt(match.group(), 44)}”"
            for match in dense_de[:4]
        )
        warnings.append(
            f"有 {len(dense_de)} 个长句包含四个以上的“的”，可能要先交代人和动作。{samples}"
        )

    # 17. 段落形态（软）
    paragraphs = prose_paragraphs(prose)
    if len(paragraphs) >= cfg.one_sentence_min_paragraphs:
        one_sentence = sum(paragraph.sentences <= 1 for paragraph in paragraphs)
        ratio = one_sentence / len(paragraphs)
        if ratio >= cfg.one_sentence_ratio:
            warnings.append(
                f"可识别段落中有 {ratio:.0%} 只有一句话，可能形成统一的短段鼓点。"
            )

    streak = short_streak(paragraphs, cfg)
    if streak:
        first = streak[0]
        warnings.append(
            f"从第 {line_number(text, first.position)} 行起连续出现 {len(streak)} 个短促单句段，"
            "检查是否在排队喊结论。"
        )

    counts, opener_examples = opener_counts(paragraphs)
    repeated = [
        (opener, count) for opener, count in counts.items() if count >= cfg.repeated_opener_min
    ]
    if repeated:
        details = "、".join(f"{opener} {count} 次" for opener, count in repeated)
        first_position = min(opener_examples[opener] for opener, _ in repeated)
        warnings.append(
            f"段落开场重复，从第 {line_number(text, first_position)} 行附近开始。{details}。"
        )

    # 18. 借喻聚簇（软）
    metaphors = metaphor_cluster(prose, cfg.metaphor_distance, cfg.metaphor_fields_min)
    if metaphors:
        window, fields = metaphors
        samples = "、".join(dict.fromkeys(hit[2] for hit in window))
        warnings.append(
            f"八百字内出现 {len(fields)} 套借喻。{'、'.join(sorted(fields))}。"
            f"例词有 {samples}。"
        )

    # 19. 对话归属（软）
    warnings.extend(dialogue_attribution_warnings(text))

    counters = {
        "pivots": len(pivots),
        "semantic_pivots": len(semantic_pivots),
        "anaphoras": len(anaphoras),
        "nominalizations": len(nominalizations),
        "jargon": len(jargon_matches),
        "hard_stops": len(stop_matches),
        "road_signs": len(road_signs),
        "context_jargon": len(context_jargon_matches),
        "lyric": len(lyric_matches),
        "markers": len(marker_matches),
        "left_branches": len(left_branches),
        "dense_de": len(dense_de),
    }

    return ProseResult(
        han_count=total_han,
        failures=failures,
        warnings=warnings,
        counters=counters,
        config=cfg,
    )


def read_text(path: str) -> str:
    """读取稿件；``-`` 表示标准输入。"""
    if path == "-":
        return sys.stdin.read()
    return Path(path).read_text(encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="检查中文成稿的硬禁令与模型化形状")
    parser.add_argument("path", help="Markdown 或文本文件路径。使用 - 从标准输入读取")
    args = parser.parse_args(argv)

    try:
        text = read_text(args.path)
    except (OSError, UnicodeError) as error:
        print(f"无法读取稿件。{error}", file=sys.stderr)
        return 2

    result = run(text)
    if result.han_count == 0:
        print("没有检测到汉字。", file=sys.stderr)
        return 2

    print(result.report())
    return 1 if result.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
