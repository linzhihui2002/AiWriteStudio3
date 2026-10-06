"""正文重复表达诊断：纯函数、只提示，独立于章节硬门禁。

这不是 AI 文本检测器，也不评价整章质量。重复可能是作者有意安排的
复沓、回声或人物语言；报告只建议核对，不能据此拒收或自动改稿。

保守检测口径：
* 每个非空物理行视为一个段落；段落至少 80 个非空白字符、50 个汉字。
* 句子在 。！？!? 后结束，保留相邻终止标点和闭引号；至少 40 个
  非空白字符、28 个汉字。完整正文末尾无句号的句子也可参与检查。
* 比较只移除 Unicode 空白，保留所有标点、大小写、数字及原字形。
  不做全半角转换、标点删除、繁简转换或近似语义判断。
* 同一重复段落涉及的句子不重复报告；短对白、短口头禅不达阈值。

所有行列均为原始正文的 1-based 位置，末位置为包含式。输入最多检查
120000 个字符，最多报告 24 个重复组，每组最多 4 个关联位置，证据片段
最多 320 个原文字符。任何信息省略都会置 truncated=True，未检查的尾部
不能被视为无问题。没有磁盘、数据库、网络或模型调用。
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
import hashlib
import re
from typing import Any, Iterator


VERSION = 1
MAX_INPUT_CHARACTERS = 120_000
MAX_FINDINGS = 24
MAX_RELATED_LOCATIONS = 4
MAX_EVIDENCE_CHARACTERS = 320
MAX_REFERENCE_SOURCES = 3
MAX_REFERENCE_CHARACTERS = 30_000
MAX_TOTAL_REFERENCE_CHARACTERS = 90_000
MAX_RAW_REFERENCE_CHARACTERS = 120_000
MIN_PARAGRAPH_CHARACTERS = 80
MIN_PARAGRAPH_HAN = 50
MIN_SENTENCE_CHARACTERS = 40
MIN_SENTENCE_HAN = 28

_LINE_BREAK = re.compile(r"\r\n|\r|\n")
_NONEMPTY_LINE = re.compile(r"[^\r\n]+")
_HAN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\U00020000-\U0002fa1f]")
_SENTENCE_ENDS = frozenset("。！？!?")
_CLOSING_QUOTES = frozenset("」』”’\"')）】")
_FRONTMATTER = re.compile(r"^---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|$)", re.DOTALL)
_REFERENCE_PATH = re.compile(r"^章节/第[0-9]{4,12}章\.(?:txt|md)$")


@dataclass(frozen=True)
class _Unit:
    start: int
    end: int  # 原文 exclusive offset；返回位置会转为包含式。
    key: str


def _unit(text: str, start: int, end: int) -> _Unit | None:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    if start == end:
        return None
    key = "".join(character for character in text[start:end] if not character.isspace())
    return _Unit(start, end, key)


def _eligible(unit: _Unit, minimum_characters: int, minimum_han: int) -> bool:
    return len(unit.key) >= minimum_characters and len(_HAN.findall(unit.key)) >= minimum_han


def _sentence_units(text: str, paragraph: _Unit, *, complete: bool) -> Iterator[_Unit]:
    start = paragraph.start
    cursor = start
    while cursor < paragraph.end:
        if text[cursor] not in _SENTENCE_ENDS:
            cursor += 1
            continue
        end = cursor + 1
        while end < paragraph.end and text[end] in _SENTENCE_ENDS:
            end += 1
        while end < paragraph.end and text[end] in _CLOSING_QUOTES:
            end += 1
        sentence = _unit(text, start, end)
        if sentence is not None:
            yield sentence
        start = end
        cursor = end
    if complete:
        sentence = _unit(text, start, paragraph.end)
        if sentence is not None:
            yield sentence


def _duplicate_groups(units: list[_Unit]) -> list[list[_Unit]]:
    groups: dict[str, list[_Unit]] = {}
    for unit in units:
        groups.setdefault(unit.key, []).append(unit)
    return [group for group in groups.values() if len(group) > 1]


def diagnose_prose(text: str) -> dict[str, Any]:
    """返回有上限的重复表达建议；``blocking`` 恒为 False。

    ``repeated_paragraphs`` / ``repeated_sentences`` 统计重复组数，而非
    失败数或多出来的副本数。短输入、无发现及截断均不是质量通过判定。
    """
    if not isinstance(text, str):
        raise TypeError("text must be a string")

    scanned = text[:MAX_INPUT_CHARACTERS]
    input_truncated = len(scanned) < len(text)
    truncated = input_truncated
    line_starts = [0, *(match.end() for match in _LINE_BREAK.finditer(scanned))]

    def location(unit: _Unit) -> dict[str, int]:
        first_line = bisect_right(line_starts, unit.start) - 1
        final_line = bisect_right(line_starts, unit.end - 1) - 1
        return {
            "line": first_line + 1,
            "column": unit.start - line_starts[first_line] + 1,
            "end_line": final_line + 1,
            "end_column": unit.end - line_starts[final_line],
        }

    paragraph_count = 0
    sentence_count = 0
    paragraphs: list[_Unit] = []
    sentences: list[_Unit] = []
    for match in _NONEMPTY_LINE.finditer(scanned):
        paragraph = _unit(scanned, match.start(), match.end())
        if paragraph is None:
            continue
        paragraph_count += 1
        # 被字符上限切断的最后一行不能当成一个完整段落或无标点句子。
        complete = not (input_truncated and match.end() == len(scanned))
        if complete and _eligible(paragraph, MIN_PARAGRAPH_CHARACTERS, MIN_PARAGRAPH_HAN):
            paragraphs.append(paragraph)
        for sentence in _sentence_units(scanned, paragraph, complete=complete):
            sentence_count += 1
            if _eligible(sentence, MIN_SENTENCE_CHARACTERS, MIN_SENTENCE_HAN):
                sentences.append(sentence)

    paragraph_groups = _duplicate_groups(paragraphs)
    covered = sorted((unit.start, unit.end) for group in paragraph_groups for unit in group)
    covered_starts = [start for start, _ in covered]

    def already_reported(sentence: _Unit) -> bool:
        index = bisect_right(covered_starts, sentence.start) - 1
        return index >= 0 and sentence.end <= covered[index][1]

    sentence_groups = _duplicate_groups([unit for unit in sentences if not already_reported(unit)])
    candidates = [
        ("repeated_paragraph", group) for group in paragraph_groups
    ] + [("repeated_sentence", group) for group in sentence_groups]
    candidates.sort(key=lambda item: (item[1][1].start, item[0]))
    if len(candidates) > MAX_FINDINGS:
        truncated = True

    findings: list[dict[str, Any]] = []
    for kind, group in candidates[:MAX_FINDINGS]:
        primary = group[1]
        related = [group[0], *group[2:]]
        if len(related) > MAX_RELATED_LOCATIONS:
            truncated = True
        evidence = scanned[primary.start:primary.end]
        evidence_truncated = len(evidence) > MAX_EVIDENCE_CHARACTERS
        truncated = truncated or evidence_truncated
        label = "长段落" if kind == "repeated_paragraph" else "长句"
        findings.append({
            "kind": kind,
            "severity": "warning",
            **location(primary),
            "text": evidence[:MAX_EVIDENCE_CHARACTERS],
            "text_truncated": evidence_truncated,
            "related_locations": [location(unit) for unit in related[:MAX_RELATED_LOCATIONS]],
            "reason": f"本章有 {len(group)} 处{label}去除空白后完全相同，标点也相同；请核对是否误粘贴或重复交代。",
            "suggestion": "由作者复核是否需要删减或改变信息；有意复沓、回声或人物语言可保留。本项仅为表达建议，不拒收，也不自动修改。",
        })

    return {
        "version": VERSION,
        "blocking": False,
        "findings": findings,
        "counters": {
            "input_characters": len(text),
            "scanned_characters": len(scanned),
            "paragraphs": paragraph_count,
            "sentences": sentence_count,
            "repeated_paragraphs": len(paragraph_groups),
            "repeated_sentences": len(sentence_groups),
            "reported_findings": len(findings),
        },
        "truncated": truncated,
    }


def _comparison_body(text: str) -> str:
    """完整 frontmatter 只用于来源版本，不参加正文比较；位置以正文计。"""
    match = _FRONTMATTER.match(text)
    return text[match.end():] if match else text


def _comparison_units(text: str, limit: int) -> tuple[str, list[_Unit], list[_Unit], bool]:
    body = _comparison_body(text)
    scanned = body[:limit]
    truncated = len(body) > len(scanned)
    paragraphs: list[_Unit] = []
    sentences: list[_Unit] = []
    for match in _NONEMPTY_LINE.finditer(scanned):
        paragraph = _unit(scanned, match.start(), match.end())
        if paragraph is None:
            continue
        complete = not (truncated and match.end() == len(scanned))
        if complete and _eligible(paragraph, MIN_PARAGRAPH_CHARACTERS, MIN_PARAGRAPH_HAN):
            paragraphs.append(paragraph)
        sentences.extend(sentence for sentence in _sentence_units(scanned, paragraph, complete=complete)
                         if _eligible(sentence, MIN_SENTENCE_CHARACTERS, MIN_SENTENCE_HAN))
    return scanned, paragraphs, sentences, truncated


def _source_location(body: str, unit: _Unit) -> dict[str, int]:
    line_starts = [0, *(match.end() for match in _LINE_BREAK.finditer(body))]
    first = bisect_right(line_starts, unit.start) - 1
    final = bisect_right(line_starts, unit.end - 1) - 1
    return {"line": first + 1, "column": unit.start - line_starts[first] + 1,
            "end_line": final + 1, "end_column": unit.end - line_starts[final]}


def diagnose_cross_chapter(text: str, references: list[dict]) -> dict[str, Any]:
    """对原稿及最多三个历史原稿做精确重复建议，不推断是否已发生事件。

    ``references`` 为 ``{rel_path, text, content_hash}``，摘要必须匹配完整
    原文 UTF-8，含 frontmatter / 原换行。纯函数不读取路径；相对章节路径
    校验不能替代调用方的同书/状态/目录边界校验。双方位置均为正文行列。
    历史正文每源最多比较 30000 字符，总计最多 90000；单源完整原文超过
    120000 字符直接排除，避免对任意巨型输入计算全文 hash。所有省略可见。
    """
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    if not isinstance(references, list):
        raise TypeError("references must be a list")
    body, paragraphs, sentences, truncated = _comparison_units(text, MAX_INPUT_CHARACTERS)
    truncated = truncated or len(references) > MAX_REFERENCE_SOURCES
    valid: list[dict] = []
    excluded: list[dict] = []
    seen: set[str] = set()
    paragraph_matches: dict[str, list[tuple[dict, _Unit]]] = {}
    sentence_matches: dict[str, list[tuple[dict, _Unit]]] = {}
    for reference in references[:MAX_REFERENCE_SOURCES]:
        path = reference.get("rel_path") if isinstance(reference, dict) else None
        raw = reference.get("text") if isinstance(reference, dict) else None
        digest = reference.get("content_hash") if isinstance(reference, dict) else None
        reason = ""
        if not isinstance(path, str) or not _REFERENCE_PATH.fullmatch(path):
            reason = "历史源路径不规范"
        elif path in seen:
            reason = "历史源重复"
        elif not isinstance(raw, str) or len(raw) > MAX_RAW_REFERENCE_CHARACTERS:
            reason = "历史原文无效或超过完整输入上限"
        elif not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            reason = "历史源缺少完整正文版本摘要"
        elif hashlib.sha256(raw.encode("utf-8")).hexdigest() != digest:
            reason = "历史原文与版本摘要不匹配"
        if reason:
            excluded.append({"rel_path": path if isinstance(path, str) else "", "reason": reason})
            truncated = True
            continue
        seen.add(path)
        source_body, source_paragraphs, source_sentences, source_truncated = _comparison_units(
            raw, MAX_REFERENCE_CHARACTERS)
        source = {"rel_path": path, "content_hash": digest, "body": source_body}
        valid.append(source)
        truncated = truncated or source_truncated
        for units, matches in ((source_paragraphs, paragraph_matches), (source_sentences, sentence_matches)):
            for unit in units:
                matches.setdefault(unit.key, []).append((source, unit))

    matched_paragraphs = [unit for unit in paragraphs if unit.key in paragraph_matches]
    covered = [(unit.start, unit.end) for unit in matched_paragraphs]
    covered_starts = [start for start, _ in covered]

    def covered_by_paragraph(unit: _Unit) -> bool:
        index = bisect_right(covered_starts, unit.start) - 1
        return index >= 0 and unit.end <= covered[index][1]

    matched_sentences = [unit for unit in sentences
                         if unit.key in sentence_matches and not covered_by_paragraph(unit)]
    candidates: list[tuple[str, _Unit, list[tuple[dict, _Unit]]]] = []
    for kind, units, matches in (("cross_chapter_paragraph", matched_paragraphs, paragraph_matches),
                                ("cross_chapter_sentence", matched_sentences, sentence_matches)):
        reported: set[str] = set()
        for unit in units:
            if unit.key not in reported:
                candidates.append((kind, unit, matches[unit.key]))
                reported.add(unit.key)
    candidates.sort(key=lambda item: (item[1].start, item[0]))
    truncated = truncated or len(candidates) > MAX_FINDINGS
    findings: list[dict] = []
    for kind, unit, matches in candidates[:MAX_FINDINGS]:
        evidence = body[unit.start:unit.end]
        text_truncated = len(evidence) > MAX_EVIDENCE_CHARACTERS
        truncated = truncated or text_truncated or len(matches) > MAX_RELATED_LOCATIONS
        sources = []
        for source, source_unit in matches[:MAX_RELATED_LOCATIONS]:
            quote = source["body"][source_unit.start:source_unit.end]
            quote_truncated = len(quote) > MAX_EVIDENCE_CHARACTERS
            truncated = truncated or quote_truncated
            sources.append({"rel_path": source["rel_path"], "content_hash": source["content_hash"],
                            **_source_location(source["body"], source_unit),
                            "text": quote[:MAX_EVIDENCE_CHARACTERS], "text_truncated": quote_truncated})
        label = "长段落" if kind == "cross_chapter_paragraph" else "长句"
        findings.append({"kind": kind, "severity": "warning", **_source_location(body, unit),
                         "text": evidence[:MAX_EVIDENCE_CHARACTERS], "text_truncated": text_truncated,
                         "related_locations": [], "related_sources": sources,
                         "reason": f"本章{label}与此前已完成章节的原文去除空白后完全相同，标点也相同。",
                         "suggestion": "作者可核对是否重复交代或把已经发生的动作写成再次发生；仅凭相同文字不能确定事件重复。有意复沓、引述与意象回声可保留。本项不拒收，也不自动修改。"})
    return {"version": VERSION, "blocking": False, "findings": findings,
            "counters": {"input_characters": len(_comparison_body(text)), "scanned_characters": len(body),
                         "reference_sources": len(valid), "reference_characters": sum(len(item["body"]) for item in valid),
                         "repeated_paragraphs": len({unit.key for unit in matched_paragraphs}),
                         "repeated_sentences": len({unit.key for unit in matched_sentences}),
                         "reported_findings": len(findings)},
            "excluded": excluded, "truncated": truncated}
