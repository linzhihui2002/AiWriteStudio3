#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""账本与追踪校验门禁（只报告不改写）—— 自写模块（非第三方引入）。

字段与格式事实源
----------------
* **资源账本行格式**：``skills/novel-setting/references/状态维护细则.md``
  「每行字段：``第NNNN章 ｜ 项目 ｜ 前值 ＋ 变动 ＝ 结余 ｜ 依据``」，
  并规定「算术必须成立，逐行可验」「校验失败**只报不改**：给出不满足的行号与差值」。
* **条目 ID**：同文件（台账 ID 唯一且不复用）与
  ``skills/foreshadow-check/references/伏笔台账规范.md``
  （「伏笔ID｜唯一且不复用」「台账缺 ID 或 ID 复用 → 无法追溯」）。
  前缀表是模块级常量 :data:`ID_PREFIXES`（唯一事实源，按需增补）。

校验项（确定性，只报告）
------------------------
1. :func:`run_ledger` —— 逐行校验「前值 + 变动 = 结余」，给出不满足的行号与差值；
   写得不规整、解析不出来的**候选行**只列入 ``unparsed``，不判失败
   （作者手写流水允许「待核实」这类占位）。
2. :func:`run_ids` —— 条目 ID 的唯一性与格式（重复 / 缺分隔符 / 编号非数字 / 超长）。
3. :func:`run` —— 依次跑上面两项并汇总，便于统一调用。

本模块**只报告，不改动任何文件**。
"""

from __future__ import annotations

import re

# ── 条目 ID（唯一事实源：改这里即改判定） ─────────────────────
ID_PREFIXES: tuple[str, ...] = ("FACT", "FORESHADOW", "EVENT", "FX")
ID_MIN_DIGITS = 1
ID_MAX_DIGITS = 6

# ── 账本判定常量 ──────────────────────────────────────────────
LEDGER_BALANCE_KEY = "结余"
LEDGER_BASE_KEY = "前值"
ARITHMETIC_TOLERANCE = 1e-9   # 浮点比较容差（账本允许小数）

# 全角数字与算术符号 → 半角（作者常在中文输入法下写流水）
_FULLWIDTH_MAP = str.maketrans({
    **{chr(0xFF10 + digit): str(digit) for digit in range(10)},
    "＋": "+", "－": "-", "−": "-", "＝": "=", "　": " ",
})

_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s")
_TABLE_RULE_RE = re.compile(r"^\s*\|?[\s:|\-]+\|?\s*$")
_LIST_MARKER_RE = re.compile(r"^(?:[-*•·>|]+|\d+[.)])\s*")
_CHAPTER_TOKEN_RE = re.compile(r"第\s*\d+\s*[章卷]")
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")
_SIGN_SPLIT_RE = re.compile(r"([+\-])")
_BASE_VALUE_RE = re.compile(r"前值[^\d]{0,8}(\d+(?:\.\d+)?)")

_VALID_ID_RE = re.compile(r"\b(FACT|FORESHADOW|EVENT|FX)-(\d{1,6})\b")
_MISSING_SEP_ID_RE = re.compile(r"\b(FACT|FORESHADOW|EVENT|FX)(\d{2,6})\b")
_LONG_ID_RE = re.compile(r"\b(FACT|FORESHADOW|EVENT|FX)-(\d{7,})\b")
_BAD_TAIL_ID_RE = re.compile(r"\b(FACT|FORESHADOW|EVENT|FX)-([0-9A-Za-z]*[A-Za-z][0-9A-Za-z]*)\b")
_TAILLESS_ID_RE = re.compile(r"\b(FACT|FORESHADOW|EVENT|FX)-(?![0-9A-Za-z])")


def _fmt(value: float) -> str:
    """数字渲染：整数去掉小数点，小数最多留两位。"""
    if abs(value - round(value)) < ARITHMETIC_TOLERANCE:
        return str(int(round(value)))
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _format_change(change: float) -> str:
    return f"+{_fmt(change)}" if change >= 0 else f"-{_fmt(-change)}"


def _skip_line(line: str) -> bool:
    """跳过空行 / frontmatter 分隔线 / 标题行 / 表格分隔行。"""
    stripped = line.strip()
    if not stripped or stripped == "---":
        return True
    return bool(_HEADING_RE.match(line) or _TABLE_RULE_RE.match(line))


def _parse_ledger_line(line: str) -> tuple[float, float, float] | None:
    """解析一行账本流水；解析不出来返回 ``None``（调用方列入 unparsed）。"""
    if "=" in line:
        left, _, right = line.partition("=")
    elif LEDGER_BALANCE_KEY in line:
        left, _, after = line.partition(LEDGER_BALANCE_KEY)
        right = LEDGER_BALANCE_KEY + after
    else:
        return None

    left_body = _CHAPTER_TOKEN_RE.sub(" ", _LIST_MARKER_RE.sub("", left.strip()))
    right_body = _CHAPTER_TOKEN_RE.sub(" ", right)

    base_match = _BASE_VALUE_RE.search(left_body)
    if base_match:
        base = float(base_match.group(1))
        tail = left_body[base_match.end():]
    else:
        head = _SIGN_SPLIT_RE.split(left_body, maxsplit=1)[0]
        number = _NUMBER_RE.search(head)
        if number is None:
            return None
        base = float(number.group(0))
        tail = left_body[number.end():]

    change = 0.0
    parts = _SIGN_SPLIT_RE.split(tail)
    for sign, segment in zip(parts[1::2], parts[2::2]):
        numbers = _NUMBER_RE.findall(segment)
        if not numbers:
            continue
        if len(numbers) > 1:
            return None  # 变动段里出现多个数字，无法确定加减哪一项
        change += float(numbers[0]) * (1 if sign == "+" else -1)

    if LEDGER_BALANCE_KEY in right_body:
        after = right_body.split(LEDGER_BALANCE_KEY, 1)[1]
        number = _NUMBER_RE.search(after)
    else:
        number = _NUMBER_RE.search(right_body)
    if number is None:
        return None
    return base, change, float(number.group(0))


def run_ledger(text: str) -> dict:
    """逐行校验资源账本的「前值 + 变动 = 结余」。

    :param text: ``状态/资源账本.md`` 的文本内容。
    :return: ``{"key", "passed", "detail", "findings", "unparsed"}``；
             ``findings`` 每项含行号 / 说明 / 应为值 / 实际值 / 差值，
             ``unparsed`` 是解析不出来的行号（不判失败）。
    """
    findings: list[dict] = []
    unparsed: list[int] = []
    checked = 0
    for index, raw in enumerate(str(text or "").splitlines(), start=1):
        if _skip_line(raw):
            continue
        line = raw.translate(_FULLWIDTH_MAP)
        if "=" not in line and LEDGER_BALANCE_KEY not in line:
            continue
        if not any(char.isdigit() for char in line):
            continue
        checked += 1
        parsed = _parse_ledger_line(line)
        if parsed is None:
            unparsed.append(index)
            continue
        base, change, balance = parsed
        expected = base + change
        if abs(expected - balance) < ARITHMETIC_TOLERANCE:
            continue
        findings.append({
            "line": index,
            "message": (f"前值 {_fmt(base)} {_format_change(change)} 应为 {_fmt(expected)}，"
                        f"结余写的是 {_fmt(balance)}（相差 {_fmt(balance - expected)}）"),
            "expected": _fmt(expected),
            "actual": _fmt(balance),
            "diff": _fmt(balance - expected),
        })
    return {
        "key": "账本算术",
        "passed": not findings,
        "detail": f"检查 {checked} 行：{len(findings)} 行不满足，{len(unparsed)} 行无法解析",
        "findings": findings,
        "unparsed": unparsed,
    }


def run_ids(text: str) -> dict:
    """校验条目 ID 的唯一性与格式（只看传入的这一个文件）。

    :param text: 含条目 ID 的文件内容（如资源账本、伏笔台账、事实卡）。
    :return: ``{"key", "passed", "detail", "findings", "unparsed"}``。
    """
    findings: list[dict] = []
    unparsed: list[int] = []
    occurrences: dict[str, list[int]] = {}
    issues = 0
    for index, raw in enumerate(str(text or "").splitlines(), start=1):
        if _skip_line(raw):
            continue
        line = raw.translate(_FULLWIDTH_MAP)
        for match in _VALID_ID_RE.finditer(line):
            occurrences.setdefault(match.group(0), []).append(index)
        for match in _MISSING_SEP_ID_RE.finditer(line):
            issues += 1
            findings.append({
                "line": index,
                "message": f"ID 缺分隔符：{match.group(0)}（应写作 {match.group(1)}-{match.group(2)}）",
                "expected": f"{match.group(1)}-{match.group(2)}",
                "actual": match.group(0),
            })
        for match in _LONG_ID_RE.finditer(line):
            issues += 1
            findings.append({
                "line": index,
                "message": f"ID 编号超长：{match.group(0)}（编号 {ID_MIN_DIGITS}-{ID_MAX_DIGITS} 位）",
                "expected": f"{match.group(1)}-{match.group(2)[:ID_MAX_DIGITS]}",
                "actual": match.group(0),
            })
        for match in _BAD_TAIL_ID_RE.finditer(line):
            issues += 1
            findings.append({
                "line": index,
                "message": f"ID 格式不合规：{match.group(0)}（应为 前缀-数字）",
                "expected": f"{match.group(1)}-<数字>",
                "actual": match.group(0),
            })
        if _TAILLESS_ID_RE.search(line):
            unparsed.append(index)

    duplicated = 0
    for token, lines in sorted(occurrences.items()):
        if len(lines) <= 1:
            continue
        duplicated += 1
        findings.append({
            "line": lines[1],
            "message": f"条目 ID 重复：{token} 共出现 {len(lines)} 次（首见第 {lines[0]} 行）",
            "expected": "唯一 ID（不复用）",
            "actual": token,
        })
    unparsed = list(dict.fromkeys(unparsed))
    return {
        "key": "条目ID",
        "passed": not findings,
        "detail": (f"检查 {len(occurrences)} 个 ID：{duplicated} 个重复，"
                   f"{issues} 处格式不合规，{len(unparsed)} 行无法解析"),
        "findings": findings,
        "unparsed": unparsed,
    }


def run(text: str) -> dict:
    """依次跑账本算术与条目 ID 校验并汇总（统一调用入口）。"""
    ledger = run_ledger(text)
    ids = run_ids(text)
    return {
        "key": "追踪校验",
        "passed": ledger["passed"] and ids["passed"],
        "detail": f"账本算术：{ledger['detail']}；条目ID：{ids['detail']}",
        "checks": [ledger, ids],
    }
