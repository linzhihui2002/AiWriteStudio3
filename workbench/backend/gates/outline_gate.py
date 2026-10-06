#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""大纲与章纲校验门禁（只报告不改写）—— 自写模块（非第三方引入）。

结构事实源：``skills/novel-planning/references/大纲结构规范.md``

* 章级（``大纲/章纲.md``）「每章一行，字段固定」，
  并要求「**章号连续且唯一**：跳号或重号要在结账行报出，不自行补齐」；
* 卷级（``大纲/大纲.md``）每卷含「卷级目标」（卷首）与「卷末钩子」，
  且「钩子必须与**下一卷的卷级目标**相接」。

校验项（可机械判定的部分）
--------------------------
1. **章号唯一**：重复 → ``severity: "error"``（判失败）；
2. **章号连续**：跳号 / 回退 → ``severity: "warn"``（只报告，不判失败）；
3. **章行要素**：章号后面没有一句话事件描述 → 报出；
4. **卷首目标与卷末钩子成对**：有其一缺另一 → ``severity: "warn"``。

``passed`` 只在出现 ``error`` 时为 ``False``（跳号这类按告警级报告）。
本模块**只报告，不改动任何文件**。
"""

from __future__ import annotations

import re

# ── 判定常量（唯一事实源） ────────────────────────────────────
SEVERITY_ERROR = "error"
SEVERITY_WARN = "warn"

#: 章号写法：``第12章`` / ``第0012章`` / ``第 12 章``
CHAPTER_RE = re.compile(r"第\s*(\d{1,6})\s*章")
#: 卷标题：``## 第1卷 起风``
VOLUME_HEADING_RE = re.compile(r"^\s{0,3}#{1,4}\s*第\s*(\d{1,6})\s*卷")
#: 卷末钩子小节 / 字段（``卷末钩子：`` / ``- 钩子：``）
HOOK_LINE_RE = re.compile(r"^\s{0,3}[-*•]?\s*(?:卷末)?钩子\s*[:：]")
#: 卷首 / 卷级目标小节 / 字段（``卷首目标：`` / ``- 卷级目标：``）
GOAL_LINE_RE = re.compile(r"^\s{0,3}[-*•]?\s*(?:卷首|卷级)?目标\s*[:：]")

_LIST_MARKER_RE = re.compile(r"^(?:[-*•·>|]+|\d+[.)])\s*")
_EVENT_STRIP_CHARS = " \t｜|，,。;；:：-—>"


def _chapter_match(line: str) -> re.Match[str] | None:
    """行首（剥掉列表 / 表格 / 引用记号后）是否就是章号写法。"""
    return CHAPTER_RE.match(_LIST_MARKER_RE.sub("", line.strip()))


def _next_content_index(lines: list[str], start: int) -> int | None:
    """从 0 基下标 ``start`` 起第一行非空行的下标（跳过空行）。"""
    for index in range(start, len(lines)):
        if lines[index].strip():
            return index
    return None


def _list_block_findings(lines: list[str]) -> list[dict]:
    """卷首目标与卷末钩子成对检查：有卷标题时按卷分块，否则整篇一块。"""
    headings = [index for index, line in enumerate(lines, start=1)
                if VOLUME_HEADING_RE.match(line)]
    if headings:
        blocks: list[tuple[int, list[str]]] = []
        for position, start in enumerate(headings):
            end = headings[position + 1] - 1 if position + 1 < len(headings) else len(lines)
            blocks.append((start, lines[start - 1:end]))
    else:
        blocks = [(1, lines)]

    findings: list[dict] = []
    for start, block_lines in blocks:
        has_hook = any(HOOK_LINE_RE.match(line) for line in block_lines)
        has_goal = any(GOAL_LINE_RE.match(line) for line in block_lines)
        if has_hook and not has_goal:
            findings.append({"line": start, "message": "有卷末钩子但缺卷首/卷级目标",
                             "severity": SEVERITY_WARN})
        elif has_goal and not has_hook:
            findings.append({"line": start, "message": "有卷首/卷级目标但缺卷末钩子",
                             "severity": SEVERITY_WARN})
    return findings


def run(text: str) -> dict:
    """对大纲 / 章纲文本执行章号与钩子检查。

    :param text: ``大纲/章纲.md``（或 ``大纲/大纲.md``）的文本内容。
    :return: ``{"key", "passed", "detail", "findings"}``；
             ``findings`` 每项含 ``line`` / ``message`` / ``severity``。
    """
    source = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = source.split("\n")

    findings: list[dict] = []
    chapters: list[tuple[int, int, str]] = []      # (行号, 章号, 原文写法)
    for index, raw in enumerate(lines, start=1):
        match = _chapter_match(raw)
        if match is None:
            continue
        token = match.group(0)
        chapters.append((index, int(match.group(1)), token))
        rest = _LIST_MARKER_RE.sub("", raw.strip())[match.end():].strip(_EVENT_STRIP_CHARS)
        if rest:
            continue
        following = _next_content_index(lines, index)
        if following is not None and _chapter_match(lines[following]) is None:
            continue  # 事件写在章号下一行，也算有描述
        findings.append({"line": index,
                         "message": f"{token} 后面没有一句话事件描述",
                         "severity": SEVERITY_ERROR})

    seen: dict[int, int] = {}
    for line, number, token in chapters:
        if number in seen:
            findings.append({"line": line,
                             "message": f"章号重复：{token} 已在第 {seen[number]} 行出现",
                             "severity": SEVERITY_ERROR})
        else:
            seen[number] = line

    gaps = 0
    for (_, prev, prev_token), (line, number, token) in zip(chapters, chapters[1:]):
        if number == prev:
            continue  # 重复已按 error 报出
        if number < prev:
            gaps += 1
            findings.append({"line": line,
                             "message": f"章号回退：{prev_token} 之后出现 {token}",
                             "severity": SEVERITY_WARN})
        elif number - prev > 1:
            gaps += 1
            missing = (f"{prev + 1:04d}" if number - prev == 2 else f"{prev + 1:04d}-{number - 1:04d}")
            findings.append({"line": line,
                             "message": f"章号跳号：{prev_token} 之后是 {token}（缺 {missing}）",
                             "severity": SEVERITY_WARN})

    pair_findings = _list_block_findings(lines)
    findings.extend(pair_findings)

    duplicates = sum(1 for item in findings if item["severity"] == SEVERITY_ERROR
                     and item["message"].startswith("章号重复"))
    no_event = sum(1 for item in findings if item["severity"] == SEVERITY_ERROR
                   and "没有一句话事件描述" in item["message"])
    return {
        "key": "大纲校验",
        "passed": not any(item["severity"] == SEVERITY_ERROR for item in findings),
        "detail": (f"共 {len(chapters)} 章：重复 {duplicates} 处，跳号/回退 {gaps} 处，"
                   f"缺事件 {no_event} 处，钩子目标缺对 {len(pair_findings)} 处"),
        "findings": findings,
        "chapter_count": len(chapters),
    }
