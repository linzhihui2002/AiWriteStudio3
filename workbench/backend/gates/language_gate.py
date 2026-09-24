#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""语言门（中文正文语言契约）—— 由 MIT 项目引入并改造。

来源声明（第三方引入登记见 docs/third-party-notices.md）
---------------------------------------------------------------------------
原项目   : qin1473692580-ux__oh-story-claudecode
许可证   : MIT License (Copyright (c) 2025-2026 oh-story-claudecode)
原文件   : projects/qin1473692580-ux__oh-story-claudecode/scripts/check-chinese-prose-contract.py
           （中文正文契约：语言门为第一关 / HTML 阻断 / 非叙事结构边界 / 外语须精确登记）
           projects/qin1473692580-ux__oh-story-claudecode/skills/story-deslop/scripts/language_gate.js
           （同一 MIT 项目中该契约的语言门实现；本模块即其 Python 移植）
引入日期 : 2026-09-19
修改说明 :
  1. 文件头补充本来源声明；
  2. **检测逻辑与阈值保持不变**：HTML 标签/注释/实体一律阻断；URL、邮箱、代码、
     路径、文件名等明确非叙事结构由检测器机械保护；其余外语字母 token 一律阻断，
     只有经用户单独确认并**精确登记**在 `.deslop-whitelist` 的条目才豁免；
  3. 保护区正则、标记正则与白名单边界规则抽为模块级常量并支持 `LanguageConfig` 覆盖；
  4. 新增 `run(text, ...) -> LanguageResult` 的 import 接口，同时保留原 CLI 的
     `--json` 输出与退出码语义（0 = 通过，2 = 有 finding，3 = 用法/读取错误）；
  5. 一并移植源契约文件的「文档契约漂移」检查为 `check_contract_docs()`。
许可全文 : projects/qin1473692580-ux__oh-story-claudecode/LICENSE
---------------------------------------------------------------------------

契约不变量（语言门是第一关）：
    * 正文必须为中文散文；外语 token 阻断；
    * HTML 标签、注释与实体一律阻断；
    * 只机械保护**明确非叙事结构**（代码、URL、邮箱、路径、文件名）；
    * 白名单只做**精确匹配**，不得由模型自行添加、泛化或子串匹配。

CLI 用法::

    python workbench/backend/gates/language_gate.py [--json] 正文.md

import 用法::

    from workbench.backend.gates import language_gate

    result = language_gate.run(text)                    # 无白名单
    result = language_gate.run(text, ["Alpha-7"])       # 精确登记的外语 token
    if not result.passed:
        print(result.report())
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

# ── 明确非叙事结构：机械保护，不算语言泄漏 ─────────────────────
PATH_PART = r"""[^\s/\\<>"'“”‘’「」『』【】()（）,，。；;：:!！?？、]+"""

PROTECTED_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"```[\s\S]*?```"),
    re.compile(r"`[^`\n]+`"),
    re.compile(r"https?://[^\s<>()]+", re.IGNORECASE),
    re.compile(r"\b[A-Za-z0-9_.+-]+@[A-Za-z0-9_.-]+\.[A-Za-z]{2,}\b"),
    re.compile(
        rf"""(?:[A-Za-z]:[\\/]|\.{{1,2}}[\\/]|/)(?:{PATH_PART}[\\/])*{PATH_PART}"""
    ),
    re.compile(
        r"""(?:^|[\s（(《“"'])[^\s]+\.(?:md|txt|json|ya?ml|toml|js|mjs|cjs|ts|tsx|jsx|py|sh|html|css)(?=$|[\s）)》”"'，。！？；：,.!?;:])""",
        re.IGNORECASE | re.MULTILINE,
    ),
)

# HTML 标记：标签 / 注释 / 实体，一律阻断
MARKUP_PATTERN = re.compile(
    r"<!--[\s\S]*?-->|</?[A-Za-z][^>]*>|<![A-Za-z][^>]*>|&(?:[A-Za-z][A-Za-z0-9]+|#\d+|#x[0-9A-Fa-f]+);"
)

WHITELIST_FILENAME = ".deslop-whitelist"

# 白名单条目的边界字符（命中即视为子串匹配，不予豁免）
BRIDGE_CHARS = "_'’./+#-"

# 明确非叙事结构的保护锚点（白名单条目不得越过这些边界）
WHITELIST_BOUNDARY_RE = re.compile(r"[0-9_.+/#-]")

# ── 契约漂移检查（移植自源契约文件的 require() 语义） ───────────
CONTRACT_REQUIRED: tuple[tuple[str, str], ...] = (
    ("语言门", "语言门为第一关"),
    ("HTML", "HTML 阻断契约"),
    ("非叙事", "非叙事结构边界"),
    ("精确登记", "外语 token 精确白名单契约"),
)

CONTRACT_STALE_PATTERNS: tuple[str, ...] = (
    r"中文正稿不接受外语白名单",
)


@dataclass(frozen=True)
class LanguageConfig:
    """语言门可配置参数。默认值即原脚本硬编码常量（保持不变）。"""

    protected_patterns: tuple[re.Pattern[str], ...] = PROTECTED_PATTERNS
    markup_pattern: re.Pattern[str] = MARKUP_PATTERN
    whitelist_filename: str = WHITELIST_FILENAME
    compact_limit: int = 120


@dataclass
class Finding:
    """单条语言门命中。severity 固定为 blocking（原脚本语义）。"""

    severity: str
    type: str
    text: str
    line: int
    column: int
    context: str
    action: str = "return_to_narrative_writer"

    def __str__(self) -> str:
        return f"{self.line}:{self.column} [{self.type}] {self.text}"


@dataclass
class LanguageResult:
    """语言门结果对象。`passed` 为 False 时 `findings` 非空。"""

    findings: list[Finding] = field(default_factory=list)
    whitelist_file: str | None = None
    whitelist_entries: list[str] = field(default_factory=list)
    text_length: int = 0

    @property
    def passed(self) -> bool:
        return not self.findings

    @property
    def blocking(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "blocking"]

    def report(self, name: str = "文本") -> str:
        if self.passed:
            return f"PASS: no foreign letters or forbidden markup in {name}"
        lines = [f"REJECTED: {len(self.findings)} language/markup finding(s) in {name}"]
        for finding in self.findings:
            lines.append(f"  {finding.line}:{finding.column} [{finding.type}] {finding.text}")
            lines.append(f"    {finding.context}")
        return "\n".join(lines)

    def to_dict(self, name: str = "文本") -> dict[str, object]:
        return {
            "status": "passed" if self.passed else "rejected",
            "file": name,
            "findings": [
                {
                    "severity": f.severity,
                    "type": f.type,
                    "text": f.text,
                    "line": f.line,
                    "column": f.column,
                    "context": f.context,
                    "action": f.action,
                }
                for f in self.findings
            ],
            "whitelist_file": self.whitelist_file,
            "next_action": (
                "continue_workflow"
                if self.passed
                else "revise_reported_sentences_and_rerun_gate"
            ),
        }

    def to_json(self, name: str = "文本") -> str:
        return json.dumps(self.to_dict(name), ensure_ascii=False, indent=2)


# ── 工具 ──────────────────────────────────────────────────────
def _compact(value: str, limit: int = 120) -> str:
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _locate(text: str, offset: int, limit: int = 120) -> tuple[int, int, str]:
    before = text[:offset]
    line = before.count("\n") + 1
    line_start = before.rfind("\n") + 1
    line_end_raw = text.find("\n", offset)
    line_end = len(text) if line_end_raw == -1 else line_end_raw
    return line, offset - line_start + 1, _compact(text[line_start:line_end], limit)


def _is_han(char: str) -> bool:
    """是否 Han 文字（与源项目 Python 侧 parity 实现同构）。"""
    point = ord(char)
    return (
        0x3400 <= point <= 0x4DBF
        or 0x4E00 <= point <= 0x9FFF
        or 0xF900 <= point <= 0xFAFF
        or 0x20000 <= point <= 0x323AF
    )


def _is_foreign_letter(char: str) -> bool:
    """非 Han 的字母（NFKC 归一后判定），如拉丁/西里尔/希腊字母。"""
    if not char:
        return False
    return any(
        unicodedata.category(unit).startswith("L") and not _is_han(unit)
        for unit in unicodedata.normalize("NFKC", char)
    )


def _is_bridge(char: str) -> bool:
    """可把外语 token 连成一个整体的连接字符。"""
    if not char:
        return False
    return (
        unicodedata.category(char)[:1] in ("M", "N")
        or char in BRIDGE_CHARS
    )


def _is_whitelist_token_at(text: str, index: int) -> bool:
    if index < 0 or index >= len(text):
        return False
    char = text[index]
    return _is_foreign_letter(char) or WHITELIST_BOUNDARY_RE.fullmatch(char) is not None


def parse_whitelist(content: str) -> list[str]:
    """解析 `.deslop-whitelist` 内容：去注释、去空行、去重保序。"""
    entries: list[str] = []
    for raw in str(content or "").splitlines():
        value = re.sub(r"\s+#.*$", "", raw).strip()
        if value and not value.startswith("#"):
            entries.append(value)
    return list(dict.fromkeys(entries))


def load_whitelist(input_path: str | Path) -> tuple[str | None, list[str]]:
    """从稿件所在目录向上查找 `.deslop-whitelist`。"""
    directory = Path(input_path).resolve().parent
    while True:
        candidate = directory / WHITELIST_FILENAME
        if candidate.is_file():
            try:
                content = candidate.read_text(encoding="utf-8")
            except OSError:
                return None, []
            return str(candidate), parse_whitelist(content)
        parent = directory.parent
        if parent == directory:
            return None, []
        directory = parent


# ── 检测 ──────────────────────────────────────────────────────
def _mask_protected_structures(text: str, mask: list[bool], config: LanguageConfig) -> None:
    for pattern in config.protected_patterns:
        for match in pattern.finditer(text):
            mask[match.start(): match.end()] = [True] * (match.end() - match.start())


def _find_forbidden_markup(text: str, mask: list[bool], config: LanguageConfig) -> list[Finding]:
    findings: list[Finding] = []
    for match in config.markup_pattern.finditer(text):
        start, end = match.span()
        if all(mask[start:end]):
            continue  # 整个标记都在保护区（如代码块内）
        mask[start:end] = [True] * (end - start)
        line, column, context = _locate(text, start, config.compact_limit)
        findings.append(
            Finding(
                severity="blocking",
                type="forbidden-markup",
                text=_compact(match.group(), config.compact_limit),
                line=line,
                column=column,
                context=context,
            )
        )
    return findings


def _mark_whitelist_entries(text: str, mask: list[bool], entries: list[str]) -> None:
    for entry in entries:
        if not entry:
            continue
        offset = 0
        while offset <= len(text) - len(entry):
            start = text.find(entry, offset)
            if start < 0:
                break
            end = start + len(entry)
            before_foreign = _is_whitelist_token_at(text, start - 1)
            after_foreign = _is_whitelist_token_at(text, end)
            whitespace = re.match(r"\s+", text[end:])
            followed_by_foreign_word = bool(
                whitespace
                and end + whitespace.end() < len(text)
                and _is_foreign_letter(text[end + whitespace.end()])
            )
            if not before_foreign and not after_foreign and not followed_by_foreign_word:
                mask[start:end] = [True] * (end - start)
            offset = start + max(len(entry), 1)


def _find_foreign_letters(text: str, mask: list[bool], config: LanguageConfig) -> list[Finding]:
    findings: list[Finding] = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if mask[index] or not _is_foreign_letter(char):
            index += 1
            continue

        start = index
        end = index + 1
        while end < length:
            if mask[end]:
                break
            nxt = text[end]
            if _is_foreign_letter(nxt) or _is_bridge(nxt):
                end += 1
                continue
            if nxt in " \t":
                cursor = end + 1
                while cursor < length and text[cursor] in " \t":
                    cursor += 1
                if cursor < length and not mask[cursor] and _is_foreign_letter(text[cursor]):
                    end = cursor
                    continue
            break

        line, column, context = _locate(text, start, config.compact_limit)
        findings.append(
            Finding(
                severity="blocking",
                type="mixed-language",
                text=text[start:end],
                line=line,
                column=column,
                context=context,
            )
        )
        index = end
    return findings


def scan(
    text: str,
    whitelist_entries: list[str] | None = None,
    config: LanguageConfig | None = None,
) -> list[Finding]:
    """扫描文本，返回按行列排序的 finding 列表（原 `scan()` 的 Python 版）。"""
    cfg = config or LanguageConfig()
    entries = whitelist_entries or []
    mask: list[bool] = [False] * len(text)
    _mask_protected_structures(text, mask, cfg)
    markup_findings = _find_forbidden_markup(text, mask, cfg)
    _mark_whitelist_entries(text, mask, entries)
    findings = markup_findings + _find_foreign_letters(text, mask, cfg)
    return sorted(findings, key=lambda f: (f.line, f.column))


def run(
    text: str,
    whitelist_entries: list[str] | None = None,
    config: LanguageConfig | None = None,
    **overrides: object,
) -> LanguageResult:
    """对正文执行语言门检查。

    :param text: 正文文本。
    :param whitelist_entries: 已精确登记的外语 token（用户单独确认过）。
    :param config: 完整配置；缺省使用模块级默认常量。
    :param overrides: 覆盖 `LanguageConfig` 单个字段。
    :return: :class:`LanguageResult`
    """
    cfg = config or LanguageConfig()
    if overrides:
        cfg = LanguageConfig(**{**cfg.__dict__, **overrides})

    entries = list(whitelist_entries or [])
    findings = scan(text, entries, cfg)
    return LanguageResult(
        findings=findings,
        whitelist_file=None,
        whitelist_entries=entries,
        text_length=len(text),
    )


# ── 文档契约漂移检查（源契约文件逻辑的移植） ────────────────────
def check_contract_docs(
    paths: list[str | Path],
    required: tuple[tuple[str, str], ...] = CONTRACT_REQUIRED,
    stale_patterns: tuple[str, ...] = CONTRACT_STALE_PATTERNS,
    root: str | Path | None = None,
) -> list[str]:
    """检查文档是否仍锁住中文正文契约。

    移植自源契约文件的 ``require()`` / stale-pattern 语义：缺少必需 needle
    或命中已废止模式即记一条错误。返回错误列表（空 = 通过）。

    :param paths: 待检查文档路径（相对 `root` 或绝对路径）。
    :param root: 相对路径基准目录；缺省为当前工作目录。
    """
    base = Path(root) if root is not None else Path.cwd()
    errors: list[str] = []
    for raw in paths:
        path = Path(raw)
        if not path.is_absolute():
            path = base / path
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as error:
            errors.append(f"{raw}: 无法读取：{error}")
            continue
        for needle, label in required:
            if needle not in text:
                errors.append(f"{raw}: 缺少{label}：{needle}")
        for pattern in stale_patterns:
            if re.search(pattern, text):
                errors.append(f"{raw}: 命中已废止契约 /{pattern}/")
    return errors


# ── CLI ───────────────────────────────────────────────────────
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="语言门：中文正文语言契约检查")
    parser.add_argument("path", help="正文文件路径")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出报告")
    parser.add_argument(
        "--whitelist",
        nargs="*",
        default=None,
        help="显式白名单条目（缺省自动向上查找 .deslop-whitelist）",
    )
    args = parser.parse_args(argv)

    input_path = Path(args.path).resolve()
    try:
        text = input_path.read_text(encoding="utf-8")
    except OSError as error:
        print(f"language_gate: cannot read {input_path}: {error}", file=sys.stderr)
        return 3

    if args.whitelist is not None:
        whitelist_file: str | None = None
        entries = args.whitelist
    else:
        whitelist_file, entries = load_whitelist(input_path)

    result = run(text, entries)
    result.whitelist_file = whitelist_file

    if args.json:
        print(result.to_json(str(input_path)))
    elif result.passed:
        print(result.report(str(input_path)))
    else:
        print(result.report(str(input_path)), file=sys.stderr)

    return 2 if result.findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
