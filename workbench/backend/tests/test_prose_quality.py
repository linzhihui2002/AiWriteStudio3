"""纯正文诊断的正反例；无模型、无小说目录或持久化访问。"""

from __future__ import annotations

import pytest

from workbench.backend.gates import prose_quality


SENTENCE = "他把旧账簿压在桌角，逐行核对船上卸下的货物，红笔停在第三栏，那个数字与仓库留下的底单对不上。"
PARAGRAPH = (
    "老周推开仓库侧门，把钥匙放在木箱上，先检查封条，又沿着窗台摸了一圈，指尖沾满了黑色灰尘。"
    "桌上摊着昨晚留下的清单，每张纸角都压着一块石头，靠近门口的那张多了一道鞋印，下面的两行数字被水泡开了。"
)


def test_empty_is_advisory_not_a_quality_verdict() -> None:
    result = prose_quality.diagnose_prose("")
    assert result["version"] == 1
    assert result["blocking"] is False
    assert result["findings"] == []
    assert result["truncated"] is False
    assert result["counters"]["scanned_characters"] == 0
    assert not ({"passed", "score", "ai_probability"} & result.keys())


def test_repeated_paragraph_is_one_group_with_original_locations() -> None:
    result = prose_quality.diagnose_prose(f"头\n  {PARAGRAPH}\r\n\r\n\t {PARAGRAPH}  ")
    assert result["counters"]["repeated_paragraphs"] == 1
    assert result["counters"]["repeated_sentences"] == 0
    assert len(result["findings"]) == 1
    finding = result["findings"][0]
    assert finding["kind"] == "repeated_paragraph"
    assert (finding["line"], finding["column"], finding["end_line"], finding["end_column"]) == (4, 3, 4, len(PARAGRAPH) + 2)
    assert finding["text"] == PARAGRAPH
    assert finding["related_locations"] == [{"line": 2, "column": 3, "end_line": 2, "end_column": len(PARAGRAPH) + 2}]
    assert "有意复沓" in finding["suggestion"]
    assert result["blocking"] is False


def test_unicode_whitespace_differences_are_ignored_without_changing_evidence() -> None:
    spaced = PARAGRAPH[:12] + "\u3000\t" + PARAGRAPH[12:]
    result = prose_quality.diagnose_prose(f"{PARAGRAPH}\n{spaced}")
    assert result["findings"][0]["text"] == spaced
    assert result["findings"][0]["end_column"] == len(spaced)


def test_punctuation_and_character_changes_are_not_approximate_matches() -> None:
    # 一个逗号变为句号，一个人名改变；保守精确检查不会把它们当作重复段落。
    changed = PARAGRAPH.replace("老周", "老吴").replace("，", "；")
    assert prose_quality.diagnose_prose(f"{PARAGRAPH}\n{changed}")["findings"] == []


def test_repeated_long_sentence_in_distinct_paragraphs() -> None:
    result = prose_quality.diagnose_prose(f"前门落了锁。{SENTENCE}\n{SENTENCE}后门还有人。")
    finding = result["findings"][0]
    assert finding["kind"] == "repeated_sentence"
    assert finding["text"] == SENTENCE
    assert (finding["line"], finding["column"]) == (2, 1)
    assert finding["related_locations"][0]["column"] == len("前门落了锁。") + 1
    assert result["counters"]["repeated_sentences"] == 1


@pytest.mark.parametrize("quote", ["「好。」", "“知道了。”", "「不。」「不。」「不。」"])
def test_short_dialogue_and_intentional_short_refrains_are_not_reported(quote: str) -> None:
    assert prose_quality.diagnose_prose("\n".join([quote] * 50))["findings"] == []


def test_terminal_punctuation_and_closing_quotes_remain_in_sentence_evidence() -> None:
    quoted = "「" + SENTENCE[:-1] + "！？」"
    result = prose_quality.diagnose_prose(f"她读了一遍。{quoted}\n他照着念。{quoted}")
    assert result["findings"][0]["text"] == quoted
    assert result["findings"][0]["end_column"] == len("他照着念。") + len(quoted)


@pytest.mark.parametrize("length, expected", [(79, 0), (80, 1)])
def test_paragraph_character_threshold(length: int, expected: int) -> None:
    paragraph = "甲" * (length - 1) + "。"
    result = prose_quality.diagnose_prose(f"{paragraph}\n{paragraph}")
    assert result["counters"]["repeated_paragraphs"] == expected


@pytest.mark.parametrize("han, expected", [(49, 0), (50, 1)])
def test_paragraph_requires_sufficient_chinese_text(han: int, expected: int) -> None:
    paragraph = "甲" * han + "1" * (79 - han) + "。"
    result = prose_quality.diagnose_prose(f"{paragraph}\n{paragraph}")
    assert result["counters"]["repeated_paragraphs"] == expected


@pytest.mark.parametrize("length, expected", [(39, 0), (40, 1)])
def test_sentence_character_threshold(length: int, expected: int) -> None:
    sentence = "甲" * (length - 1) + "。"
    result = prose_quality.diagnose_prose(f"{sentence}\n{sentence}")
    assert result["counters"]["repeated_sentences"] == expected


@pytest.mark.parametrize("han, expected", [(27, 0), (28, 1)])
def test_sentence_requires_sufficient_chinese_text(han: int, expected: int) -> None:
    sentence = "甲" * han + "1" * (39 - han) + "。"
    result = prose_quality.diagnose_prose(f"{sentence}\n{sentence}")
    assert result["counters"]["repeated_sentences"] == expected


def test_distinct_sentences_with_shared_short_opening_are_not_reported() -> None:
    text = (
        "他把门推开，墙上挂着旧地图，桌上那封信已经拆开，信纸的边角还带着雨水。\n"
        "他把门推开，床边留下的是一只破皮箱，箱盖扣得很紧，钥匙就在窗台下面。"
    )
    assert prose_quality.diagnose_prose(text)["findings"] == []


def test_full_document_without_final_punctuation_can_be_diagnosed() -> None:
    sentence = SENTENCE[:-1]
    result = prose_quality.diagnose_prose(f"{sentence}\n{sentence}")
    assert result["findings"][0]["text"] == sentence


def test_input_limit_does_not_report_incomplete_final_paragraph() -> None:
    prefix = "甲" * 100 + "\n"
    partial = "甲" * 100
    padded = "x" * (prose_quality.MAX_INPUT_CHARACTERS - len(prefix) - len(partial) - 1) + "\n"
    result = prose_quality.diagnose_prose(prefix + padded + partial + "还没有写完。")
    assert result["counters"]["scanned_characters"] == prose_quality.MAX_INPUT_CHARACTERS
    assert result["truncated"] is True
    assert result["findings"] == []


def test_complete_sentences_before_input_cutoff_still_participate() -> None:
    prefix = SENTENCE + "\n"
    tail = SENTENCE + "乙"
    padded = "x" * (prose_quality.MAX_INPUT_CHARACTERS - len(prefix) - len(tail) - 1) + "\n"
    result = prose_quality.diagnose_prose(prefix + padded + tail + "还有后续。")
    assert result["truncated"] is True
    assert result["counters"]["repeated_sentences"] == 1
    assert result["findings"][0]["text"] == SENTENCE


def test_findings_limit_preserves_total_detected_group_count() -> None:
    paragraphs = [f"第{number}项：" + "甲" * 80 + "。" for number in range(30)]
    text = "\n".join(paragraph for paragraph in paragraphs for _ in range(2))
    result = prose_quality.diagnose_prose(text)
    assert result["counters"]["repeated_paragraphs"] == 30
    assert len(result["findings"]) == prose_quality.MAX_FINDINGS
    assert result["truncated"] is True


def test_related_location_limit_keeps_original_and_one_repeated_position() -> None:
    result = prose_quality.diagnose_prose("\n".join([PARAGRAPH] * 7))
    finding = result["findings"][0]
    assert finding["line"] == 2
    assert finding["related_locations"][0]["line"] == 1
    assert len(finding["related_locations"]) == prose_quality.MAX_RELATED_LOCATIONS
    assert result["truncated"] is True


def test_evidence_limit_is_explicit_and_stays_an_original_contiguous_quote() -> None:
    paragraph = "甲" * 400 + "。"
    result = prose_quality.diagnose_prose(f"{paragraph}\n{paragraph}")
    finding = result["findings"][0]
    assert finding["text"] == paragraph[:prose_quality.MAX_EVIDENCE_CHARACTERS]
    assert finding["text_truncated"] is True
    assert finding["end_column"] == len(paragraph)
    assert result["truncated"] is True


def test_output_is_deterministic_and_is_independent_per_call() -> None:
    text = f"{PARAGRAPH}\n{PARAGRAPH}"
    first = prose_quality.diagnose_prose(text)
    assert first == prose_quality.diagnose_prose(text)
    first["findings"].clear()
    assert len(prose_quality.diagnose_prose(text)["findings"]) == 1


def test_non_text_input_is_rejected_explicitly() -> None:
    with pytest.raises(TypeError, match="text must be a string"):
        prose_quality.diagnose_prose(None)  # type: ignore[arg-type]
