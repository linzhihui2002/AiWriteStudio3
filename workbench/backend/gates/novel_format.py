#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""小说正文格式门禁（番茄纯文本规范）—— 自写模块（非第三方引入）。

背景
----
章节正文以 ``章节/第NNNN章.txt`` **纯正文**存储（无 frontmatter），上传番茄前
必须保持"纯中文小说文本"形态：标题 / 状态 / 合同ID / 字数全部以数据库为准。
本门禁只做**确定性**判定，命中即阻断落盘，并给出定位清单（行号 + 命中片段）。

判定项
------
1. **Markdown 标记泄漏**（阻断）：行首 ``#`` 标题、``**加粗**`` / ``*斜体*``、
   行首 ``- `` / ``* `` / ``1. `` 列表、行首 ``> `` 引用、单独 ``---`` / ``***``
   分隔线、```` ``` ```` 围栏、行内 `` `code` ``、``[]()`` 链接、``<tag>`` HTML 标签、
   ``&nbsp;`` 之类 HTML 实体。理由：这些标记会被任何 Markdown 渲染器解释，
   渲染层可能顺手"改造"标点与排版（破折号 / 省略号 / 引号），番茄也按字面收，
   必须在落盘与上传前拦掉。
2. **正文不该出现的东西**（阻断）：正文里又写一遍「第12章 xxx」当标题——
   标题是元数据，正文重复既冗余又会被上传端识别成章节标题行。
   *规划记号 / 任务指令泄漏不在本模块*：那份词表（``MARKER_PATTERNS``）的唯一
   事实源在 ``review_service``，「记号泄漏」门与「格式门」在 ``run_hard_gates`` 里
   并列执行；在本模块重抄一份只会造成两处词表漂移。
3. **基本形状**（仅确定性可判定项）：
   - 成对引号 ``「」`` / ``『』`` / ``“”`` / ``‘’`` 必须成对（数量不等即判失败）；
   - 半角 / 异常标点混用（``...`` 代替 ``……``、``--`` 代替 ``——``、汉字后直接跟
     半角逗号 / 句号）判失败；
   - 连续多行无空行的长块只给**建议级**提示（不阻断）：纯文本小说靠空行分段，
     长块可能是漏了分段，也可能是有意为之，交人工判断。

阈值与词表是本模块的模块级常量（唯一事实源，改这里即改门禁）。

CLI 用法::

    python workbench/backend/gates/novel_format.py 稿件.txt

import 用法::

    from workbench.backend.gates import novel_format

    result = novel_format.run(text)
    if not result.passed:
        print(result.report())
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

# ── 阈值（唯一事实源） ────────────────────────────────────────
BLOCK_WARN_LINES = 3        # 连续无空行多少行起提示（纯文本小说以空行分段）
BLOCK_WARN_CHARS = 60       # 且该块至少多少字符才提示（少于这个数不值得打断作者）
EXCERPT_WIDTH = 40          # 定位片段的字符上限（≤40 字）
MAX_FINDINGS = 50           # 单次检查报告的命中上限（避免刷屏）

# 行首 / 整行级别的 Markdown 标记（按行匹配，便于给出行号）
LINE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^\s{0,3}#{1,6}\s+\S.*$"), "Markdown 标题行"),
    (re.compile(r"^\s{0,3}(?:-{3,}|\*{3,}|_{3,})\s*$"), "Markdown 分隔线"),
    (re.compile(r"^\s{0,3}(?:```|~~~)"), "Markdown 代码围栏"),
    (re.compile(r"^\s{0,3}>\s?\S"), "Markdown 引用"),
    (re.compile(r"^\s{0,3}[-+*]\s+\S"), "Markdown 无序列表"),
    (re.compile(r"^\s{0,3}\d+[.)]\s+\S"), "Markdown 有序列表"),
)

# 行内 Markdown / HTML 标记（全篇匹配）
INLINE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\*\*[^*\n]+\*\*"), "Markdown 加粗"),
    (re.compile(r"__[^_\n]+__"), "Markdown 加粗"),
    (re.compile(r"(?<!\*)\*(?!\s)[^*\n]{1,80}(?<!\s)\*(?!\*)"), "Markdown 斜体"),
    (re.compile(r"`[^`\n]+`"), "行内代码"),
    (re.compile(r"!?\[[^\]\n]*\]\([^)\n]*\)"), "Markdown 链接"),
    (re.compile(r"</?[a-zA-Z][a-zA-Z0-9]*(?:\s[^<>\n]*)?/?>"), "HTML 标签"),
    (re.compile(r"&(?:[a-zA-Z][a-zA-Z0-9]{1,10}|#\d{1,6}|#x[0-9a-fA-F]{1,6});"), "HTML 实体"),
)

# 正文重复章节标题：整行就是「第12章 xxx」
TITLE_LINE_PATTERN = re.compile(
    r"^\s{0,3}第\s*(?:\d{1,6}|[一二三四五六七八九十百千零两]{1,8})\s*章"
    r"(?:\s+\S[^\n]{0,30})?\s*$"
)
TITLE_LINE_REASON = "正文重复章节标题行"

# 半角 / 异常标点（确定性可判定的几种）
PUNCTUATION_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\.{3,}"), "半角省略号（应用「……」）"),
    (re.compile(r"-{2,}"), "半角破折号（应用「——」）"),
    (re.compile(r"[\u4e00-\u9fff][,;:!?]"), "汉字后接半角标点"),
    (re.compile(r"[\u4e00-\u9fff]\.(?![0-9A-Za-z])"), "汉字后接半角句号"),
)

# 成对引号（开 / 闭 / 名称）
PAIRED_QUOTES: tuple[tuple[str, str, str], ...] = (
    ("「", "」", "直角引号"),
    ("『", "』", "直角单引号"),
    ("“", "”", "弯引号"),
    ("‘", "’", "弯单引号"),
)

QUOTE_UNMATCHED_REASON = "成对引号不配对"


@dataclass(frozen=True)
class NovelFormatConfig:
    """格式门禁可配置参数。默认值即模块级常量（保持不变）。"""

    block_warn_lines: int = BLOCK_WARN_LINES
    block_warn_chars: int = BLOCK_WARN_CHARS
    excerpt_width: int = EXCERPT_WIDTH
    max_findings: int = MAX_FINDINGS
    paired_quotes: tuple[tuple[str, str, str], ...] = PAIRED_QUOTES


@dataclass(frozen=True)
class FormatFinding:
    """一条定位记录：行号 + 命中片段（≤40 字）+ 原因。"""

    line: int
    column: int
    text: str
    reason: str


@dataclass
class NovelFormatResult:
    """格式门禁结果对象。``passed`` 为 False 时 ``findings`` 非空。"""

    total: int
    line_total: int
    findings: list[FormatFinding] = field(default_factory=list)
    warnings: list[FormatFinding] = field(default_factory=list)
    config: NovelFormatConfig = field(default_factory=NovelFormatConfig)

    @property
    def passed(self) -> bool:
        return not self.findings

    @property
    def failures(self) -> list[str]:
        """与 prose 门禁同形的失败项文本（行号 + 原因 + 片段）。"""
        return [f"第 {item.line} 行，{item.reason}：{item.text}" for item in self.findings]

    def report(self, name: str = "正文") -> str:
        lines: list[str] = [
            f"=== 小说正文格式检查: {name} "
            f"({self.line_total} 行 / {self.total} 字符) ==="
        ]
        if self.findings:
            lines.append(f"格式硬伤: {len(self.findings)} 处（阻断）")
            lines.extend(
                f"- 第 {item.line} 行第 {item.column} 列，{item.reason}：{item.text}"
                for item in self.findings[:20]
            )
        else:
            lines.append("格式硬伤: 0 处 ✓（无 Markdown 标记泄漏 / 无重复标题行）")
        if self.warnings:
            lines.append(f"建议: {len(self.warnings)} 处（不阻断，交人工判断）")
            lines.extend(
                f"- 第 {item.line} 行，{item.reason}：{item.text}"
                for item in self.warnings[:10]
            )
        lines.append("")
        lines.append(
            "结论: 纯文本格式通过 ✓"
            if self.passed
            else f"结论: {len(self.findings)} 处需修改（不落盘）"
        )
        return "\n".join(lines)


def _clip(value: str, width: int) -> str:
    value = re.sub(r"\s+", " ", value).strip()
    return value if len(value) <= width else value[: width - 1] + "…"


def _line_number(source: str, position: int) -> int:
    return source.count("\n", 0, position) + 1


def _column(source: str, position: int) -> int:
    return position - source.rfind("\n", 0, position)


def _excerpt_around(source: str, position: int, width: int) -> str:
    """命中位置附近 ≤width 字的片段（去掉换行，便于在报告里单行显示）。"""
    start = max(0, position - width // 2)
    end = min(len(source), position + width // 2)
    return _clip(source[start:end], width)


def run(
    text: str,
    config: NovelFormatConfig | None = None,
    **overrides: object,
) -> NovelFormatResult:
    """对章节纯正文执行小说格式检查。

    :param text: 纯正文文本（已剔除 frontmatter 的正文）。
    :param config: 完整配置；缺省使用模块级默认常量。
    :param overrides: 覆盖 `NovelFormatConfig` 单个字段。
    :return: :class:`NovelFormatResult`
    """
    cfg = config or NovelFormatConfig()
    if overrides:
        cfg = NovelFormatConfig(**{**cfg.__dict__, **overrides})

    source = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = source.split("\n")
    findings: list[FormatFinding] = []
    warnings: list[FormatFinding] = []

    def record(line: int, column: int, snippet: str, reason: str, *, blocking: bool) -> None:
        text_value = _clip(snippet, cfg.excerpt_width)
        bucket = findings if blocking else warnings
        # 同一处命中可能同时被"整行标记"与"标点混用"识别，只报一次
        if any(item.line == line and item.text == text_value for item in bucket):
            return
        if len(bucket) >= cfg.max_findings:
            return
        bucket.append(FormatFinding(line=line, column=column, text=text_value, reason=reason))

    # 1) 行首 / 整行 Markdown 标记 + 重复章节标题行
    for index, line in enumerate(lines, start=1):
        for pattern, reason in LINE_PATTERNS:
            match = pattern.match(line)
            if match:
                record(index, match.start() + 1, match.group(0), reason, blocking=True)
        title = TITLE_LINE_PATTERN.match(line)
        if title:
            record(index, title.start() + 1, title.group(0), TITLE_LINE_REASON, blocking=True)

    # 2) 行内 Markdown / HTML 标记
    for pattern, reason in INLINE_PATTERNS:
        for match in pattern.finditer(source):
            record(_line_number(source, match.start()), _column(source, match.start()),
                   match.group(0), reason, blocking=True)

    # 3) 半角 / 异常标点
    for pattern, reason in PUNCTUATION_PATTERNS:
        for match in pattern.finditer(source):
            record(_line_number(source, match.start()), _column(source, match.start()),
                   match.group(0), reason, blocking=True)

    # 4) 成对引号必须配对（多出的那一侧报"最后一次出现"的位置）
    for open_quote, close_quote, label in cfg.paired_quotes:
        opens = source.count(open_quote)
        closes = source.count(close_quote)
        if opens == closes:
            continue
        position = source.rfind(open_quote if opens > closes else close_quote)
        record(
            _line_number(source, position) if position >= 0 else 0,
            _column(source, position) if position >= 0 else 0,
            _excerpt_around(source, position, cfg.excerpt_width) if position >= 0 else "",
            f"{QUOTE_UNMATCHED_REASON}（{label} {opens} 开 / {closes} 闭）",
            blocking=True,
        )

    # 5) 段落空行分隔（建议级：纯文本小说靠空行分段）
    run_lines = 0
    run_start = 0
    run_chars = 0
    for index, line in enumerate(lines + [""], start=1):
        if line.strip():
            if run_lines == 0:
                run_start = index
            run_lines += 1
            run_chars += len(line.strip())
            continue
        if run_lines >= cfg.block_warn_lines and run_chars >= cfg.block_warn_chars:
            record(run_start, 1, f"连续 {run_lines} 行无空行（约 {run_chars} 字）",
                   "疑似漏了分段空行", blocking=False)
        run_lines = 0
        run_chars = 0

    return NovelFormatResult(
        total=len(source),
        line_total=len(lines),
        findings=findings,
        warnings=warnings,
        config=cfg,
    )


def read_text(path: str) -> str:
    """读取稿件；``-`` 表示标准输入。"""
    if path == "-":
        return sys.stdin.read()
    return Path(path).read_text(encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="小说正文格式检查（番茄纯文本规范）")
    parser.add_argument("path", help="文本文件路径。使用 - 从标准输入读取")
    args = parser.parse_args(argv)

    try:
        text = read_text(args.path)
    except (OSError, UnicodeError) as error:
        print(f"无法读取稿件。{error}", file=sys.stderr)
        return 2

    name = Path(args.path).name if args.path != "-" else "<stdin>"
    result = run(text)
    print(result.report(name))
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())