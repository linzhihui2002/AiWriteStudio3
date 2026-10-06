"""Recent-chapter repetition stays advisory and generation audits reflect actual input."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from workbench.backend import config, db
from workbench.backend.api import writing
from workbench.backend.services import (
    agent_service,
    chapter_service,
    context_service,
    contract_service,
    generation_service,
    knowledge_service,
    pipeline_service,
    project_service,
    prompt_registry_service,
    proposal_service,
    review_service,
    writing_preference_service,
)


SENTENCE = (
    "陆衡沿着仓库的货架逐项清点船上卸下的木箱，红笔停在第三栏，"
    "清单上的数字与昨夜留下的底单对不上。"
)
PAY = "陆衡把六枚铜钱推到桌边。"
CONNECT_EVIDENCE = "把夜里答应留下的木箱递给他。"
PLOT = "陆衡支付六枚铜钱船资"
CONNECT = "船工兑现前章留下木箱的承诺"
BODY = (
    SENTENCE + "\n" + PAY + "\n“照旧规矩，先付船资。”\n"
    "船工抽走纸上的欠条，把夜里答应留下的木箱递给他。\n"
    "“钥匙在底下，别丢了。”"
)
PREVIOUS = "船工翻过清单。\n" + SENTENCE + "\n“木箱按约定留下，等你来取。”"
OLD_DISK = "旧稿中陆衡没有付船资，船工把木箱锁回柜子。"


@pytest.fixture()
def book(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setattr(config, "RUNTIME_DIR", tmp_path / ".workbench")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / ".workbench/test.db")
    db.init_db()
    config.ensure_runtime_dirs()
    prompt_registry_service.ensure_registry()
    agent_service.ensure_builtin_agents()
    monkeypatch.setattr(knowledge_service, "enqueue", lambda *args, **kwargs: None)
    monkeypatch.setattr(knowledge_service, "search", lambda *args, **kwargs: {
        "hits": [], "degradation": []})

    def no_real_model(*args, **kwargs):
        raise AssertionError("Integration fixtures must never use a real model")

    monkeypatch.setattr(generation_service, "run_task", no_real_model)
    monkeypatch.setattr(generation_service, "stream_task", no_real_model)
    template = tmp_path / "template"
    for directory in ("章节", "设定", "大纲", "状态"):
        (template / directory).mkdir(parents=True)
    project = project_service.create_project("跨章表达集成隔离书", template=template)
    ident = project["id"]
    _, root = project_service.get_project_dir(ident)
    previous = chapter_service.create_chapter(ident, "仓库清点")
    previous_path = root / previous["rel_path"]
    previous_path.write_text(PREVIOUS, encoding="utf-8")
    chapter_service.set_chapter_meta(ident, previous["rel_path"], status="完成")
    target = chapter_service.create_chapter(ident, "木箱交接")
    target_path = root / target["rel_path"]
    target_path.write_text(OLD_DISK, encoding="utf-8")
    writing_preference_service.update_writing_prefs(
        ident, chapter_min_words=1, chapter_max_words=1000)
    (root / "大纲/大纲.md").write_text("卷一，陆衡支付船资，取回木箱。", encoding="utf-8")
    contract_service.freeze_outline(ident)
    contract_service.generate_contract(ident, target["rel_path"], use_ai=False)
    contract_service.update_contract(ident, target["rel_path"], {
        "plot_points": [PLOT], "must_connect": [CONNECT], "constraints": []})
    contract_service.freeze_contract(ident, target["rel_path"])
    app = FastAPI()
    app.include_router(writing.router)
    return SimpleNamespace(id=ident, root=root, rel=target["rel_path"], path=target_path,
                           previous=previous["rel_path"], previous_path=previous_path,
                           client=TestClient(app))


def _acceptance() -> dict:
    return {"逐项": [
        {"项": PLOT, "判定": "已完成", "证据": PAY},
        {"项": CONNECT, "判定": "已完成", "证据": CONNECT_EVIDENCE},
    ], "一致性": [], "结论": "通过", "修改指令": [], "表达建议": []}


def _model(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    calls = []

    def run(**kwargs):
        calls.append(kwargs)
        task = kwargs["task_type"]
        if task == "审稿":
            text = json.dumps(_acceptance(), ensure_ascii=False)
        elif task == "结构化抽取":
            text = "{}"
        elif task == "章节修订":
            raise AssertionError("Soft history findings must not start a revision")
        else:
            text = BODY
        return {"ok": True, "text": text, "task_id": None, "engine": "fake", "model": "test"}

    monkeypatch.setattr(generation_service, "run_task", run)
    return calls


def _blocks(assembly: dict) -> list[dict]:
    return [block for block in assembly["blocks"] if block.get("type") == "prose_history"]


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_candidate_history_report_uses_candidate_instead_of_old_disk_and_is_not_a_gate(book, monkeypatch):
    calls = _model(monkeypatch)
    result = review_service.review_text(book.id, book.rel, BODY, register_debt=True)
    history = result["prose_history"]
    assert result["candidate_hash"] == _hash(BODY)
    assert result["verdict"] == "通过" and result["hard_gates"]["passed"] is True
    assert history["version"] == 1 and history["blocking"] is False
    assert history["status"] == "available" and history["reference_hash"]
    finding = next(item for item in history["findings"]
                   if item["kind"] == "cross_chapter_sentence")
    assert finding["text"] == SENTENCE and finding["line"] == 1
    source = finding["related_sources"][0]
    assert source["rel_path"] == book.previous
    assert source["content_hash"] == _hash(PREVIOUS)
    assert source["text"] == SENTENCE and source["line"] == 2
    assert not ({"score", "passed", "ai_probability"} & history.keys())
    assert all("跨章" not in gate["key"] for gate in result["hard_gates"]["gates"])
    assert review_service.list_debts(book.id) == []
    assert len(calls) == 1 and calls[0]["task_type"] == "审稿"
    assert book.path.read_text(encoding="utf-8") == OLD_DISK


def test_review_model_receives_versioned_history_evidence_and_snapshot(book, monkeypatch):
    calls = _model(monkeypatch)
    result = review_service.review_text(book.id, book.rel, BODY)
    text = "\n".join(message["content"] for message in calls[0]["messages"])
    assert book.previous in text and _hash(PREVIOUS) in text
    assert SENTENCE in text
    assert calls[0]["messages"][-1]["content"].endswith("【待审正文】\n" + BODY)
    snapshot = calls[0]["context_snapshot"]
    assert snapshot["candidate_hash"] == _hash(BODY)
    assert snapshot["prose_history"]["reference_hash"] == result["prose_history"]["reference_hash"]
    assert snapshot["prose_history"]["sources"] == result["prose_history"]["sources"]
    assert snapshot["prose_history"]["status"] == result["prose_history"]["status"]
    assert snapshot["prose_history"]["truncated"] is result["prose_history"]["truncated"]
    stored = review_service.list_reviews(book.id)[0]["payload"]
    assert stored["prose_history"] == result["prose_history"]


def test_offline_history_diagnosis_does_not_require_model_or_certify_contract(book):
    result = review_service.review_text(book.id, book.rel, BODY, use_ai=False)
    assert result["ai_used"] is False
    assert result["prose_history"]["findings"] and result["prose_history"]["blocking"] is False
    assert result["hard_gates"]["passed"] is True
    assert result["counts"]["待核实"] > 0 and result["verdict"] == "不通过"


def test_context_does_not_load_history_unless_explicitly_enabled(book):
    assembly = context_service.assemble(book.id, chapter_rel=book.rel, use_retrieval=False)
    assert not _blocks(assembly)
    assert assembly["prose_history_reference"]["enabled"] is False
    assert assembly["prose_history_reference"]["injected"] is False


def test_history_context_is_distinct_from_author_approved_style_and_audits_actual_source(book):
    assembly = context_service.assemble(book.id, chapter_rel=book.rel, use_retrieval=False,
                                        include_prose_history=True)
    blocks = _blocks(assembly)
    assert len(blocks) == 1 and blocks[0]["level"] == 10
    assert blocks[0]["mandatory"] is False
    assert not any(block.get("type") == "style" for block in assembly["blocks"])
    audit = assembly["prose_history_reference"]
    assert audit["enabled"] is True and audit["injected"] is True
    assert audit["samples"] and audit["reference_hash"] and audit["tokens"] > 0
    assert all(sample["rel_path"] == book.previous for sample in audit["samples"])
    assert all(sample["content_hash"] == _hash(PREVIOUS) for sample in audit["samples"])
    assert audit["selected_samples"] == audit["samples"]
    _, messages = context_service.to_messages(assembly)
    assert blocks[0]["text"] in messages[0]["content"]
    for sample in audit["samples"]:
        assert sample["text"] in messages[0]["content"]
    assert assembly["preview"]["prose_history_reference"] == audit
    assert any(item.get("type") == "prose_history" for item in assembly["preview"]["items"])
    saved = context_service.record_assembly(book.id, book.rel, assembly)
    stored = json.loads(Path(saved).read_text(encoding="utf-8"))
    assert stored["prose_history_reference"] == audit


def test_history_is_trimmed_before_protected_facts_without_false_injection_receipt(book):
    assembly = context_service.assemble(book.id, chapter_rel=book.rel, use_retrieval=False,
                                        include_prose_history=True, budget_tokens=350,
                                        exclude_levels=[2, 3, 4, 5, 6, 7, 9],
                                        explicit=[{"text": "事实材料" * 80, "protected": True}])
    audit = assembly["prose_history_reference"]
    assert audit["selected_samples"] and audit["selected_reference_hash"]
    assert audit["selected_tokens"] > 0
    assert audit["injected"] is False and audit["samples"] == []
    assert audit["reference_hash"] == "" and audit["tokens"] == 0
    assert not _blocks(assembly)
    assert any(event["level"] == 10 for event in assembly["degradation"])
    assert any(block["level"] == 1 for block in assembly["blocks"])
    assert assembly["total_tokens"] <= 350
    assert assembly["preview"]["prose_history_reference"]["injected"] is False


def test_excluding_history_level_clears_actual_receipt(book):
    assembly = context_service.assemble(book.id, chapter_rel=book.rel, use_retrieval=False,
                                        include_prose_history=True, exclude_levels=[10])
    assert not _blocks(assembly)
    audit = assembly["prose_history_reference"]
    assert audit["injected"] is False and audit["samples"] == [] and audit["reference_hash"] == ""


def test_pipeline_does_not_revise_or_block_application_for_history_warning(book, monkeypatch):
    calls = _model(monkeypatch)
    result = pipeline_service.run_pipeline(book.id, book.rel, use_ai=True, auto_apply=True)
    assert result["status"] == "done" and result["revise_rounds"] == 0
    assert not any(call["task_type"] == "章节修订" for call in calls)
    assert sum(call["task_type"] == "章节正文" for call in calls) == 1
    assert sum(call["task_type"] == "审稿" for call in calls) == 1
    assert book.path.read_text(encoding="utf-8") == BODY
    assert review_service.list_debts(book.id) == []
    draft = next(call for call in calls if call["task_type"] == "章节正文")
    audit = draft["context_snapshot"]["prose_history_reference"]
    assert audit["injected"] is True and audit["samples"]
    generated_input = "\n".join(message["content"] for message in draft["messages"])
    assert all(sample["text"] in generated_input for sample in audit["samples"])
    proposal = next(item for item in proposal_service.list_proposals(book.id, status=None)
                    if item["kind"] == "chapter_draft")
    assert proposal["status"] == "applied"


@pytest.mark.parametrize("task,profile,expect_history", [
    ("章节正文", "off", True), ("续写", "off", True), ("章节修订", "off", True),
    ("审稿", "off", False), ("大纲生成", "off", False),
    ("章节正文", "review", False), ("章节正文", "teardown", False),
])
def test_generate_only_writing_tasks_receive_history_reference(book, monkeypatch, task, profile, expect_history):
    calls = []

    def run(**kwargs):
        calls.append(kwargs)
        return {"ok": True, "text": BODY, "task_id": None}

    monkeypatch.setattr(generation_service, "run_task", run)
    response = book.client.post(f"/api/projects/{book.id}/generate", json={
        "chapter_rel": book.rel, "task_type": task, "retrieval_profile": profile,
        "instruction": "承接木箱交接，避免重演上一章事件。"})
    assert response.status_code == 200 and len(calls) == 1
    audit = calls[0]["context_snapshot"]["prose_history_reference"]
    assert audit["injected"] is expect_history
    if expect_history:
        text = "\n".join(message["content"] for message in calls[0]["messages"])
        assert audit["samples"] and all(sample["text"] in text for sample in audit["samples"])
    else:
        assert audit["samples"] == [] and audit["reference_hash"] == ""


@pytest.mark.parametrize("route,payload", [
    ("local-op", {"selection": OLD_DISK, "operation": "polish"}),
    ("ghost-text", {"prefix": "陆衡把清单折好。", "suffix": "船工松开木箱。"}),
])
def test_brief_writing_context_preserves_the_history_it_claims_to_inject(book, monkeypatch, route, payload):
    calls = _model(monkeypatch)
    response = book.client.post(f"/api/projects/{book.id}/{route}", json={
        "chapter_rel": book.rel, **payload})
    assert response.status_code == 200 and len(calls) == 1
    audit = calls[0]["context_snapshot"]["prose_history_reference"]
    text = "\n".join(message["content"] for message in calls[0]["messages"])
    assert audit["injected"] is True and audit["samples"]
    assert all(sample["text"] in text for sample in audit["samples"])
    assert response.json()["context_preview"]["prose_history_reference"] == audit


def test_stream_uses_same_actual_history_receipt_in_preview_and_task(book, monkeypatch):
    calls = []

    def stream(**kwargs):
        calls.append(kwargs)
        yield {"delta": BODY, "task_id": 8}
        yield {"done": True, "text": BODY, "task_id": 8, "ok": True}

    monkeypatch.setattr(generation_service, "stream_task", stream)
    response = book.client.post(f"/api/projects/{book.id}/generate/stream", json={
        "chapter_rel": book.rel, "task_type": "续写", "retrieval_profile": "off"})
    assert response.status_code == 200 and len(calls) == 1
    audit = calls[0]["context_snapshot"]["prose_history_reference"]
    assert audit["injected"] is True and audit["samples"]
    text = "\n".join(message["content"] for message in calls[0]["messages"])
    assert all(sample["text"] in text for sample in audit["samples"])
    events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
    assert events[0]["preview"]["prose_history_reference"] == audit


def test_context_preview_history_can_be_explicitly_excluded_and_is_not_for_planning(book):
    path = f"/api/projects/{book.id}/context/preview"
    base = {"chapter_rel": book.rel, "use_retrieval": False}
    response = book.client.post(path, json=base)
    assert response.status_code == 200
    included = response.json()
    assert included["prose_history_reference"]["injected"] is True
    assert any(block.get("type") == "prose_history" for block in included["blocks"])
    for change in ({"include_prose_history": False}, {"exclude_levels": [10]},
                   {"task_type": "大纲生成"}, {"task_type": "审稿"}):
        excluded = book.client.post(path, json={**base, **change}).json()
        assert excluded["prose_history_reference"]["injected"] is False
        assert excluded["prose_history_reference"]["samples"] == []

