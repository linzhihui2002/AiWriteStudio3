"""候选审稿绑定正文/合同版本，遗漏与伪证据均不得通过。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from workbench.backend import config, db
from workbench.backend.services import (
    chapter_service,
    contract_service,
    generation_service,
    knowledge_service,
    project_service,
    prompt_registry_service,
    review_service,
)
from workbench.backend.services.errors import InvalidOperationError


BODY = (
    "陆衡把六枚铜钱推到桌边。\n"
    "“照旧规矩，先付船资。”\n"
    "老船工抽走纸上的欠条，把夜里答应留下的木箱递给他。\n"
    "“钥匙在底下，别丢了。”"
)
PLOT = "陆衡支付船资"
CONNECT = "船工兑现前章留下木箱的承诺"


@pytest.fixture()
def book(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setattr(config, "RUNTIME_DIR", tmp_path / ".workbench")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / ".workbench/workbench.db")
    db.init_db()
    config.ensure_runtime_dirs()
    prompt_registry_service.ensure_registry()
    monkeypatch.setattr(knowledge_service, "enqueue", lambda *args, **kwargs: None)
    monkeypatch.setattr(knowledge_service, "search", lambda *args, **kwargs: {
        "hits": [], "degradation": []})
    # These tests exercise evidence/version acceptance with a short scene. The
    # production thresholds remain unchanged, and the real gates still run.
    monkeypatch.setattr(review_service, "chapter_word_range", lambda _project: (1, 1000))
    template = tmp_path / "test-template"
    for directory in ("章节", "设定", "大纲", "状态"):
        (template / directory).mkdir(parents=True)
    project = project_service.create_project("候选审稿测试", template=template)
    _, root = project_service.get_project_dir(project["id"])
    chapter = chapter_service.create_chapter(project["id"], "木箱交接")
    contract_service.generate_contract(project["id"], chapter["rel_path"], use_ai=False)
    contract = contract_service.update_contract(project["id"], chapter["rel_path"], {
        "plot_points": [PLOT], "must_connect": [CONNECT], "constraints": []})
    return SimpleNamespace(id=project["id"], root=root, rel=chapter["rel_path"],
                           path=root / chapter["rel_path"], contract=contract)


def _acceptance() -> dict:
    return {"逐项": [
        {"项": PLOT, "判定": "已完成", "证据": "陆衡把六枚铜钱推到桌边。"},
        {"项": CONNECT, "判定": "已完成", "证据": "把夜里答应留下的木箱递给他。"},
    ], "一致性": [], "结论": "通过", "修改指令": []}


def _model(monkeypatch: pytest.MonkeyPatch, payload: dict) -> list[dict]:
    calls: list[dict] = []

    def run(**kwargs):
        calls.append(kwargs)
        return {"ok": True, "text": json.dumps(payload, ensure_ascii=False)}

    monkeypatch.setattr(generation_service, "run_task", run)
    return calls


def test_grounded_expression_advice_is_separate_from_contract_acceptance(book, monkeypatch):
    payload = _acceptance()
    payload["表达建议"] = [{"类型": "模板表达", "证据": "“照旧规矩，先付船资。”",
                          "问题": "船工这一句可以结合双方关系检查是否过于通用。",
                          "建议": "结合既有说话习惯判断，原句有作用时保留。", "行": 999}]
    _model(monkeypatch, payload)
    result = review_service.review_text(book.id, book.rel, BODY)
    assert result["verdict"] == "通过"
    assert result["revise_instructions"] == [] and result["consistency"] == []
    assert result["counts"] == {"已完成": 2, "未完成": 0, "待核实": 0}
    suggestion = result["prose_review"][0]
    assert suggestion["line"] == 2 and suggestion["column"] == 1
    assert suggestion["evidence"] in BODY
    assert review_service.list_reviews(book.id)[0]["payload"]["prose_review"] == result["prose_review"]
    assert review_service.list_debts(book.id) == []


@pytest.mark.parametrize("advice", [
    "不是数组", {}, [None],
    [{"类型": "模板表达", "证据": "正文不存在的证据", "问题": "重复", "建议": "删减"}],
    [{"类型": "随意类型", "证据": "陆衡把六枚铜钱推到桌边。", "问题": "重复", "建议": "删减"}],
    [{"类型": "模板表达", "证据": ["陆衡"], "问题": "重复", "建议": "删减"}],
    [{"类型": "模板表达", "证据": "陆衡", "问题": "太短", "建议": "改名"}],
    [{"类型": "模板表达", "证据": "陆衡把六枚铜钱推到桌边。", "问题": "", "建议": "删减"}],
])
def test_unverified_expression_advice_is_dropped_without_changing_verdict(book, monkeypatch, advice):
    payload = _acceptance()
    payload["表达建议"] = advice
    _model(monkeypatch, payload)
    result = review_service.review_text(book.id, book.rel, BODY)
    assert result["verdict"] == "通过"
    assert result["prose_review"] == []
    assert result["rejected_expression_suggestions"] >= 1
    assert result["revise_instructions"] == []


def test_repeated_paragraphs_are_located_without_adding_a_hard_gate(book, monkeypatch):
    paragraph = (
        "船工把桌上的木箱转了半圈，将箱底那道划痕朝向窗边的光，拿起麻绳绕过铜扣，"
        "绳头穿进扣眼后留在掌心，他用空着的手按住箱盖，等陆衡收起欠条，再把箱子推到桌沿，"
        "靠墙的凳子挡住过道，他将凳子移到一边，留出能抱着箱子走过去的位置。"
    )
    body = BODY + "\n“箱子上岸后，记得把旧绳还回来。”\n" + paragraph + "\n" + paragraph
    calls = _model(monkeypatch, _acceptance())
    result = review_service.review_text(book.id, book.rel, body)
    assert result["hard_gates"]["passed"] and result["verdict"] == "通过"
    assert result["prose_quality"]["blocking"] is False
    finding = next(item for item in result["prose_quality"]["findings"]
                   if item["kind"] == "repeated_paragraph")
    assert finding["line"] == 7 and finding["related_locations"][0]["line"] == 6
    assert finding["text"] == paragraph
    assert "表达线索，仅供判断" in calls[0]["messages"][-1]["content"]
    assert calls[0]["context_snapshot"]["prose_diagnostics_version"] == 1
    assert result["revise_instructions"] == []


def test_expression_advice_rejects_ambiguous_and_duplicate_quotes():
    entry = {"类型": "重复解释", "证据": "这句话用来交代唯一的事实。", "问题": "检查解释。", "建议": "有事实作用则保留。"}
    checked, rejected = review_service._checked_expression_suggestions([entry, entry], entry["证据"])
    assert len(checked) == 1 and rejected == 1
    checked, rejected = review_service._checked_expression_suggestions([entry], entry["证据"] * 2)
    assert checked == [] and rejected == 1


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_expression_evidence_positions_follow_original_line_endings(newline):
    entry = {"类型": "模板表达", "证据": "他把铜钱推回桌边。", "问题": "检查人物表达。", "建议": "有作用可保留。"}
    body = "门口的船工还在等。" + newline + "  " + entry["证据"]
    checked, rejected = review_service._checked_expression_suggestions([entry], body)
    assert rejected == 0
    assert checked[0]["line"] == 2 and checked[0]["column"] == 3


def test_candidate_is_reviewed_without_reading_disk_draft(book, monkeypatch):
    old = "旧稿中陆衡拒绝付钱，船工扣住了木箱。"
    book.path.write_text(old, encoding="utf-8")
    calls = _model(monkeypatch, _acceptance())
    should_cancel = lambda: False
    result = review_service.review_text(book.id, book.rel, BODY,
                                        should_cancel=should_cancel)
    assert result["verdict"] == "通过" and result["ai_used"] is True
    assert result["hard_gates"]["passed"] is True
    assert calls[0]["should_cancel"] is should_cancel
    assert calls[0]["messages"][-1]["content"].endswith("【待审正文】\n" + BODY)
    assert old not in calls[0]["messages"][-1]["content"]
    assert PLOT in calls[0]["messages"][-1]["content"]
    assert CONNECT in calls[0]["messages"][-1]["content"]
    assert book.path.read_text(encoding="utf-8") == old
    assert result["candidate_hash"] == hashlib.sha256(BODY.encode("utf-8")).hexdigest()
    assert result["contract_hash"] == review_service.contract_fingerprint(book.contract)
    assert calls[0]["context_snapshot"]["candidate_hash"] == result["candidate_hash"]
    assert calls[0]["context_snapshot"]["contract_hash"] == result["contract_hash"]
    assert result["items"][0]["证据行"] == 1
    assert result["items"][1]["证据行"] == 3
    stored = review_service.list_reviews(book.id)[0]["payload"]
    assert stored["candidate_hash"] == result["candidate_hash"]
    assert review_service.list_debts(book.id) == []


def test_empty_new_chapter_still_has_ai_review(book, monkeypatch):
    assert book.path.read_text(encoding="utf-8") == ""
    calls = _model(monkeypatch, _acceptance())
    result = review_service.review_text(book.id, book.rel, BODY)
    assert result["ai_used"] is True and result["verdict"] == "通过"
    assert len(calls) == 1
    assert book.path.read_text(encoding="utf-8") == ""


def test_review_chapter_reuses_candidate_checks(book, monkeypatch):
    book.path.write_text(BODY, encoding="utf-8")
    _model(monkeypatch, _acceptance())
    result = review_service.review_chapter(book.id, book.rel)
    assert result["verdict"] == "通过"
    assert result["candidate_hash"] == hashlib.sha256(BODY.encode("utf-8")).hexdigest()


@pytest.mark.parametrize("response", [
    {"ok": False, "text": ""},
    {"ok": True, "text": "这章已经通过。"},
    {"ok": True, "text": "{}"},
    {"ok": True, "text": '{"逐项": [], "结论": "通过"}'},
    {"ok": True, "text": '{"逐项": [{"项":"支付船资"}], "一致性": 1}'},
])
def test_ai_failure_or_empty_result_is_unverified(book, monkeypatch, response):
    monkeypatch.setattr(generation_service, "run_task", lambda **kwargs: response)
    result = review_service.review_text(book.id, book.rel, BODY)
    assert result["hard_gates"]["passed"] is True
    assert result["verdict"] == "不通过" and result["ai_used"] is False
    assert result["counts"]["待核实"] >= 2
    assert result["ai_error"]


def test_ai_exception_is_unverified(book, monkeypatch):
    def unavailable(**kwargs):
        raise RuntimeError("model unavailable")

    monkeypatch.setattr(generation_service, "run_task", unavailable)
    result = review_service.review_text(book.id, book.rel, BODY)
    assert result["ai_used"] is False and result["verdict"] == "不通过"
    assert result["counts"]["待核实"] >= 2


@pytest.mark.parametrize("omitted", [PLOT, CONNECT])
def test_each_plot_and_must_connect_item_is_required(book, monkeypatch, omitted):
    payload = _acceptance()
    payload["逐项"] = [item for item in payload["逐项"] if item["项"] != omitted]
    _model(monkeypatch, payload)
    result = review_service.review_text(book.id, book.rel, BODY)
    assert result["verdict"] == "不通过"
    missing = next(item for item in result["items"] if item["项"] == omitted)
    assert missing["判定"] == "待核实" and missing["证据"] == ""


@pytest.mark.parametrize("evidence", ["", "陆衡缴清了所有欠款。", "陆衡把六枚……推到桌边。"])
def test_fabricated_or_missing_quote_cannot_complete_item(book, monkeypatch, evidence):
    payload = _acceptance()
    payload["逐项"][0]["证据"] = evidence
    _model(monkeypatch, payload)
    result = review_service.review_text(book.id, book.rel, BODY)
    assert result["verdict"] == "不通过"
    assert result["items"][0]["判定"] == "待核实"
    assert result["items"][0]["核验说明"]


def test_conflicting_duplicate_contract_items_cannot_pass(book, monkeypatch):
    payload = _acceptance()
    payload["逐项"].append({"项": PLOT, "判定": "未完成", "证据": ""})
    _model(monkeypatch, payload)
    result = review_service.review_text(book.id, book.rel, BODY)
    assert result["verdict"] == "不通过"
    assert result["items"][0]["判定"] == "未完成"


def test_hash_mismatch_rejected_before_model_or_review_record(book, monkeypatch):
    calls = _model(monkeypatch, _acceptance())
    with pytest.raises(InvalidOperationError, match="hash 不匹配"):
        review_service.review_text(book.id, book.rel, BODY, candidate_hash="stale")
    assert calls == [] and review_service.list_reviews(book.id) == []


def test_original_input_hash_does_not_normalize_frontmatter_or_newline(book, monkeypatch):
    text = "---\n标题: 旧版兼容\n---\n" + BODY.replace("\n", "\r\n")
    _model(monkeypatch, _acceptance())
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    result = review_service.review_text(book.id, book.rel, text, candidate_hash=digest)
    assert result["candidate_hash"] == digest
    assert digest != hashlib.sha256(BODY.encode("utf-8")).hexdigest()


def test_contract_change_during_review_requires_new_review(book, monkeypatch):
    def run(**kwargs):
        contract_service.update_contract(book.id, book.rel, {"must_connect": ["新增承上要求"]})
        return {"ok": True, "text": json.dumps(_acceptance(), ensure_ascii=False)}

    monkeypatch.setattr(generation_service, "run_task", run)
    result = review_service.review_text(book.id, book.rel, BODY)
    assert result["contract_changed"] is True
    assert result["verdict"] == "不通过"
    assert result["contract_hash"] == review_service.contract_fingerprint(book.contract)
    assert any(item["项"] == "章节合同版本" and item["判定"] == "待核实"
               for item in result["items"])


def test_contract_fingerprint_is_stable_for_key_order():
    assert review_service.contract_fingerprint({"plot_points": ["甲"], "word_budget": 20}) == \
        review_service.contract_fingerprint({"word_budget": 20, "plot_points": ["甲"]})
    assert review_service.contract_fingerprint(None) == ""


def test_candidate_review_does_not_certify_disk_version_in_quality_matrix(book, monkeypatch):
    book.path.write_text("旧稿。", encoding="utf-8")
    _model(monkeypatch, _acceptance())
    review_service.review_text(book.id, book.rel, BODY)
    matrix = review_service.quality_matrix(book.id)
    assert matrix["chapters"][0]["cells"]["逐项审稿"] == "未跑"
    book.path.write_text(BODY, encoding="utf-8")
    matrix = review_service.quality_matrix(book.id)
    assert matrix["chapters"][0]["cells"]["逐项审稿"] == "通过"


def test_candidate_review_retains_chapter_path_boundary(book, monkeypatch):
    calls = _model(monkeypatch, _acceptance())
    with pytest.raises(InvalidOperationError):
        review_service.review_text(book.id, "设定/人物设定.md", BODY)
    with pytest.raises(InvalidOperationError):
        review_service.review_text(book.id, "章节/../" + book.rel, BODY)
    assert calls == []


def test_quality_matrix_does_not_reuse_review_of_previous_contract(book, monkeypatch):
    book.path.write_text(BODY, encoding="utf-8")
    _model(monkeypatch, _acceptance())
    review_service.review_chapter(book.id, book.rel)
    contract_service.update_contract(book.id, book.rel, {"must_connect": ["船工须说明钥匙来历"]})
    matrix = review_service.quality_matrix(book.id)
    assert matrix["chapters"][0]["cells"]["逐项审稿"] == "未跑"
