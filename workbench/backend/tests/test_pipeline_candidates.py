"""候选审稿、局部修订和应用之间的集成回归；不调用真实模型。"""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from workbench.backend import config, db
from workbench.backend.services import (
    agent_service,
    chapter_service,
    contract_service,
    generation_service,
    file_change_service,
    knowledge_service,
    pipeline_service,
    project_service,
    prompt_registry_service,
    proposal_service,
    review_service,
    writing_preference_service,
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
    agent_service.ensure_builtin_agents()
    monkeypatch.setattr(knowledge_service, "enqueue", lambda *args, **kwargs: None)
    monkeypatch.setattr(knowledge_service, "search", lambda *args, **kwargs: {
        "hits": [], "degradation": []})
    template = tmp_path / "test-template"
    for directory in ("章节", "设定", "大纲", "状态"):
        (template / directory).mkdir(parents=True)
    project = project_service.create_project("候选管线测试", template=template)
    _, root = project_service.get_project_dir(project["id"])
    chapter = chapter_service.create_chapter(project["id"], "木箱交接")
    # The book's real preference path supplies the short-scene word range to
    # both review and file-change gates; no production gate is bypassed.
    writing_preference_service.update_writing_prefs(
        project["id"], chapter_min_words=1, chapter_max_words=1000)
    (root / "大纲/大纲.md").write_text("卷一，陆衡付钱取回木箱。", encoding="utf-8")
    contract_service.freeze_outline(project["id"])
    contract_service.generate_contract(project["id"], chapter["rel_path"], use_ai=False)
    contract_service.update_contract(project["id"], chapter["rel_path"], {
        "plot_points": [PLOT], "must_connect": [CONNECT], "constraints": []})
    contract_service.freeze_contract(project["id"], chapter["rel_path"])
    draft = config.runtime_dir() / "drafts" / f"{project['id']}-{Path(chapter['rel_path']).stem}.md"
    draft.parent.mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(id=project["id"], root=root, rel=chapter["rel_path"],
                           path=root / chapter["rel_path"], draft=draft)


def _review_payload(body: str = BODY) -> dict:
    return {"逐项": [
        {"项": PLOT, "判定": "已完成", "证据": body.splitlines()[0]},
        {"项": CONNECT, "判定": "已完成", "证据": "把夜里答应留下的木箱递给他。"},
    ], "一致性": [], "结论": "通过", "修改指令": []}


def _install_model(monkeypatch, *, revise=None, reviewer=None, on_task=None) -> list[dict]:
    calls = []

    def run(**kwargs):
        calls.append(kwargs)
        task = kwargs["task_type"]
        if task == "章节正文":
            text = BODY
        elif task == "审稿":
            body = kwargs["messages"][-1]["content"].split("【待审正文】\n", 1)[1]
            text = json.dumps(reviewer(body) if reviewer else _review_payload(body), ensure_ascii=False)
        elif task == "章节修订":
            text = json.dumps(revise or {"patches": []}, ensure_ascii=False)
        elif task == "结构化抽取":
            text = "{}"
        else:
            raise AssertionError(f"Unexpected real-model task: {task}")
        if on_task:
            on_task(task)
        return {"ok": True, "text": text, "task_id": None, "engine": "fake", "model": "test"}

    monkeypatch.setattr(generation_service, "run_task", run)
    return calls


def _candidate(book, monkeypatch, *, review=True):
    _install_model(monkeypatch)
    data = {}
    result = pipeline_service._draft(book.id, book.rel, book.draft, data, use_ai=True)
    assert result["ok"]
    data.update(result)
    if review:
        reviewed = review_service.review_text(book.id, book.rel, BODY)
        assert reviewed["verdict"] == "通过"
        proposal_service.record_pipeline_review(result["proposal_id"], reviewed)
    return data


def _revision_data(book, monkeypatch):
    data = _candidate(book, monkeypatch)
    data.update(revise_round=1, last_review={"verdict": "不通过", "ai_review": {
        "revise_instructions": ["只将支付数量从六枚改为七枚，保留其余原文。"]}})
    return data


def test_author_edited_candidate_can_be_reviewed_in_inbox_then_applied(book, monkeypatch):
    data = _candidate(book, monkeypatch)
    edited = BODY.replace("六枚", "七枚")
    proposal_service.update_proposal_content(data["proposal_id"], edited)
    with pytest.raises(InvalidOperationError):
        proposal_service.apply_proposal(data["proposal_id"])
    reviewed = proposal_service.review_pipeline_candidate(data["proposal_id"])
    assert reviewed["verdict"] == "通过"
    proposal_service.apply_proposal(data["proposal_id"])
    assert book.path.read_text(encoding="utf-8") == edited


def test_completed_checkpoint_cannot_reuse_completion_after_author_edit(book, monkeypatch):
    _install_model(monkeypatch)
    assert pipeline_service.run_pipeline(book.id, book.rel)["status"] == "done"
    edited = BODY.replace("六枚", "七枚")
    chapter_service.save_chapter(book.id, book.rel, edited)
    result = pipeline_service.run_pipeline(book.id, book.rel)
    assert result["status"] == "blocked"
    assert book.path.read_text(encoding="utf-8") == edited


def test_inbox_review_does_not_bind_a_concurrently_edited_candidate(book, monkeypatch):
    data = _candidate(book, monkeypatch)
    _install_model(monkeypatch, on_task=lambda task: proposal_service.update_proposal_content(
        data["proposal_id"], BODY.replace("六枚", "七枚")) if task == "审稿" else None)
    with pytest.raises(InvalidOperationError, match="版本不一致"):
        proposal_service.review_pipeline_candidate(data["proposal_id"])
    with pytest.raises(InvalidOperationError):
        proposal_service.apply_proposal(data["proposal_id"])
    assert book.path.read_text(encoding="utf-8") == ""


def test_inbox_review_does_not_apply_or_pass_when_reviewer_unavailable(book, monkeypatch):
    data = _candidate(book, monkeypatch, review=False)
    monkeypatch.setattr(generation_service, "run_task", lambda **kwargs: {"ok": False})
    result = proposal_service.review_pipeline_candidate(data["proposal_id"])
    assert result["verdict"] != "通过"
    with pytest.raises(InvalidOperationError):
        proposal_service.apply_proposal(data["proposal_id"])
    assert book.path.read_text(encoding="utf-8") == ""


def test_empty_target_reviews_candidate_then_applies_that_text(book, monkeypatch):
    calls = _install_model(monkeypatch)
    assert book.path.read_text(encoding="utf-8") == ""
    result = pipeline_service.run_pipeline(book.id, book.rel)
    assert result["status"] == "done"
    assert book.path.read_text(encoding="utf-8") == BODY
    assert chapter_service.read_chapter(book.id, book.rel)["status"] == "完成"
    assert [call["task_type"] for call in calls].count("审稿") == 1
    review_call = next(call for call in calls if call["task_type"] == "审稿")
    assert review_call["messages"][-1]["content"].endswith("【待审正文】\n" + BODY)
    assert proposal_service.get_proposal(result["proposal_id"])["status"] == "applied"


def test_expression_advice_alone_never_triggers_a_revision_round(book, monkeypatch):
    def reviewer(body):
        payload = _review_payload(body)
        payload["表达建议"] = [{"类型": "模板表达", "证据": "照旧规矩，先付船资。",
                              "问题": "检查这一句是否符合船工既有说话习惯。", "建议": "由作者判断是否保留。"}]
        return payload

    calls = _install_model(monkeypatch, reviewer=reviewer)
    result = pipeline_service.run_pipeline(book.id, book.rel)
    assert result["status"] == "done"
    assert not any(call["task_type"] == "章节修订" for call in calls)
    draft = next(call for call in calls if call["task_type"] == "章节正文")
    assert prompt_registry_service.PROSE_CRAFT_GUIDANCE in draft["system"]
    assert book.path.read_text(encoding="utf-8") == BODY
    assert result["review"]["ai_review"]["prose_review"]


def test_required_revision_gets_optional_advice_without_widening_the_patch(book, monkeypatch):
    data = _revision_data(book, monkeypatch)
    data["last_review"]["ai_review"]["prose_review"] = [{
        "line": 2, "evidence": "照旧规矩，先付船资。", "reason": "检查人物声音。", "suggestion": "有作用则保留。"}]
    calls = _install_model(monkeypatch, revise={"patches": [{"original": "六枚", "replacement": "七枚"}]})
    result = pipeline_service._draft(book.id, book.rel, book.draft, data, use_ai=True)
    assert result["ok"]
    assert book.draft.read_text(encoding="utf-8") == BODY.replace("六枚", "七枚")
    revision = next(call for call in calls if call["task_type"] == "章节修订")
    assert prompt_registry_service.PROSE_CRAFT_GUIDANCE in revision["system"]
    assert any("可选表达参考，不能扩大补丁范围" in message["content"] for message in revision["messages"])


def test_unapplied_candidate_preserves_old_chapter_and_status(book, monkeypatch):
    old = "“先等天亮。”陆衡把铜钱放回袋里。"
    book.path.write_text(old, encoding="utf-8")
    calls = _install_model(monkeypatch)
    result = pipeline_service.run_pipeline(book.id, book.rel, auto_apply=False)
    assert result["status"] == "done"
    assert book.path.read_text(encoding="utf-8") == old
    assert chapter_service.read_chapter(book.id, book.rel)["status"] == "草稿"
    assert proposal_service.get_proposal(result["proposal_id"])["status"] == "pending"
    assert not any(call["task_type"] == "结构化抽取" for call in calls)


@pytest.mark.parametrize("mutation", ["edit_proposal", "apply_override", "change_contract"])
def test_post_review_candidate_or_contract_change_blocks_application(book, monkeypatch, mutation):
    data = _candidate(book, monkeypatch)
    proposal_id = data["proposal_id"]
    arguments = {}
    if mutation == "edit_proposal":
        proposal_service.update_proposal_content(proposal_id, BODY.replace("六枚", "七枚"))
    elif mutation == "apply_override":
        arguments["content"] = BODY.replace("六枚", "七枚")
    else:
        contract_service.update_contract(book.id, book.rel,
                                         {"must_connect": ["解释木箱内物品"]}, confirm=True)
    with pytest.raises(InvalidOperationError):
        proposal_service.apply_proposal(proposal_id, **arguments)
    assert book.path.read_text(encoding="utf-8") == ""
    assert proposal_service.get_proposal(proposal_id)["status"] == "pending"


def test_pipeline_candidate_without_ai_review_cannot_be_applied(book, monkeypatch):
    data = _candidate(book, monkeypatch, review=False)
    with pytest.raises(InvalidOperationError, match="审稿"):
        proposal_service.apply_proposal(data["proposal_id"])
    assert book.path.read_text(encoding="utf-8") == ""


def test_revision_applies_precise_patch_and_keeps_every_other_character(book, monkeypatch):
    data = _revision_data(book, monkeypatch)
    calls = _install_model(monkeypatch, revise={"patches": [{
        "original": "六枚铜钱", "replacement": "七枚铜钱"}]})
    result = pipeline_service._draft(book.id, book.rel, book.draft, data, use_ai=True)
    assert result["ok"] is True
    revised = BODY.replace("六枚铜钱", "七枚铜钱")
    assert book.draft.read_text(encoding="utf-8") == revised
    assert proposal_service.get_proposal(result["proposal_id"])["content"] == revised
    assert book.path.read_text(encoding="utf-8") == ""
    assert Path(data["candidate_versions"][0]["path"]).read_text(encoding="utf-8") == BODY
    assert "【候选原稿，补丁只能定位此版本】\n" + BODY in \
        "\n".join(message["content"] for message in calls[0]["messages"])


def test_failed_review_revises_and_reviews_new_candidate_before_application(book, monkeypatch):
    inspected = []

    def reviewer(body):
        inspected.append(body)
        payload = _review_payload(body)
        if "六枚铜钱" in body:
            payload["逐项"][0].update(判定="未完成", 证据="六枚铜钱")
            payload.update(结论="不通过", 修改指令=["把六枚铜钱改为七枚铜钱，其余内容保留。"])
        return payload

    calls = _install_model(monkeypatch, reviewer=reviewer, revise={"patches": [{
        "original": "六枚铜钱", "replacement": "七枚铜钱"}]})
    result = pipeline_service.run_pipeline(book.id, book.rel)
    revised = BODY.replace("六枚铜钱", "七枚铜钱")
    assert result["status"] == "done"
    assert inspected == [BODY, revised]
    assert [call["task_type"] for call in calls].count("章节正文") == 1
    assert [call["task_type"] for call in calls].count("章节修订") == 1
    assert book.path.read_text(encoding="utf-8") == revised
    assert chapter_service.read_chapter(book.id, book.rel)["status"] == "完成"


@pytest.mark.parametrize("failure", ["model_failure", "wrong_format"])
def test_failed_revision_preserves_prior_draft_and_proposal(book, monkeypatch, failure):
    data = _revision_data(book, monkeypatch)
    prior_proposal = data["proposal_id"]
    before_count = len(proposal_service.list_proposals(book.id))
    monkeypatch.setattr(generation_service, "run_task", lambda **kwargs: {
        "ok": failure != "model_failure", "text": "模型开始重写却没有给出精确补丁。"})
    if failure == "model_failure":
        assert pipeline_service._draft(book.id, book.rel, book.draft, data, use_ai=True)["ok"] is False
    else:
        with pytest.raises(InvalidOperationError):
            pipeline_service._draft(book.id, book.rel, book.draft, data, use_ai=True)
    assert book.draft.read_text(encoding="utf-8") == BODY
    assert proposal_service.get_proposal(prior_proposal)["content"] == BODY
    assert len(proposal_service.list_proposals(book.id)) == before_count


@pytest.mark.parametrize("body,patches", [
    (BODY, [{"original": "六枚铜钱", "replacement": "七枚铜钱"},
            {"original": "铜钱推到", "replacement": "铜钱移到"}]),
    (BODY + "\n陆衡把六枚铜钱收回来。", [{"original": "六枚铜钱", "replacement": "七枚铜钱"}]),
    (BODY, [{"original": BODY, "replacement": "全新的正文。"}]),
])
def test_ambiguous_overlapping_or_whole_chapter_patch_is_rejected(body, patches):
    with pytest.raises(InvalidOperationError):
        pipeline_service._apply_revision(body, json.dumps({"patches": patches}, ensure_ascii=False))


def test_cancellation_after_ai_review_does_not_apply_or_complete_chapter(book, monkeypatch):
    state = {"cancel": False}
    _install_model(monkeypatch, on_task=lambda task: state.update(cancel=True) if task == "审稿" else None)
    result = pipeline_service.run_pipeline(book.id, book.rel,
                                            should_cancel=lambda: state["cancel"])
    assert result["status"] == "paused"
    assert book.path.read_text(encoding="utf-8") == ""
    assert chapter_service.read_chapter(book.id, book.rel)["status"] == "草稿"
    assert all(proposal["status"] == "pending" for proposal in proposal_service.list_proposals(book.id))


def test_cancelled_revision_keeps_previous_draft(book, monkeypatch):
    data = _revision_data(book, monkeypatch)
    state = {"cancel": False}
    _install_model(monkeypatch, revise={"patches": [{
        "original": "六枚铜钱", "replacement": "七枚铜钱"}]},
        on_task=lambda task: state.update(cancel=True) if task == "章节修订" else None)
    result = pipeline_service._draft(book.id, book.rel, book.draft, data,
                                     use_ai=True, should_cancel=lambda: state["cancel"])
    assert result["ok"] is False
    assert book.draft.read_text(encoding="utf-8") == BODY
    assert book.path.read_text(encoding="utf-8") == ""


def test_legacy_checkpoint_runs_new_review_before_apply(book, monkeypatch):
    data = _candidate(book, monkeypatch, review=False)
    data.update(steps={step: {"status": "done"} for step in pipeline_service.STEPS},
                last_review={"verdict": "通过", "ai_review": {"ai_used": True}}, protocol=1)
    pipeline_service.save_checkpoint(book.id, book.rel, data)
    calls = _install_model(monkeypatch)
    result = pipeline_service.run_pipeline(book.id, book.rel)
    assert result["status"] == "done" and result["resumed_from"] == "REVIEW"
    assert [call["task_type"] for call in calls].count("审稿") == 1
    assert not any(call["task_type"] == "章节正文" for call in calls)
    assert book.path.read_text(encoding="utf-8") == BODY


def test_processed_legacy_checkpoint_requires_new_candidate(book, monkeypatch):
    _install_model(monkeypatch)
    result = pipeline_service.run_pipeline(book.id, book.rel)
    assert result["status"] == "done"
    checkpoint = pipeline_service.load_checkpoint(book.id, book.rel)
    checkpoint["protocol"] = 1
    pipeline_service.save_checkpoint(book.id, book.rel, checkpoint)
    calls = _install_model(monkeypatch)
    resumed = pipeline_service.run_pipeline(book.id, book.rel)
    assert resumed["status"] == "blocked"
    assert calls == []
    assert book.path.read_text(encoding="utf-8") == BODY


@pytest.mark.parametrize("response", [
    {"ok": False, "text": ""},
    {"ok": True, "text": "审稿报告服务未能输出 JSON。"},
])
def test_invalid_ai_review_preserves_candidate_without_requesting_revision(book, monkeypatch, response):
    _install_model(monkeypatch)
    fake = generation_service.run_task
    tasks = []

    def run(**kwargs):
        tasks.append(kwargs["task_type"])
        return response if kwargs["task_type"] == "审稿" else fake(**kwargs)

    monkeypatch.setattr(generation_service, "run_task", run)
    result = pipeline_service.run_pipeline(book.id, book.rel)
    assert result["status"] == "manual"
    assert tasks == ["章节正文", "审稿"]
    assert book.draft.read_text(encoding="utf-8") == BODY
    assert book.path.read_text(encoding="utf-8") == ""
    candidates = proposal_service.list_proposals(book.id)
    assert len(candidates) == 1
    assert candidates[0]["content"] == BODY and candidates[0]["status"] == "pending"


def test_cancel_during_application_prevents_manuscript_commit(book, monkeypatch):
    _install_model(monkeypatch)
    state = {"cancel": False}
    apply = proposal_service.apply_proposal

    def cancelled_apply(*args, **kwargs):
        # A stop can arrive after UPDATE's step-boundary check, while the commit
        # is entering its book lock. The file-change layer must see that stop.
        state["cancel"] = True
        return apply(*args, **kwargs)

    monkeypatch.setattr(proposal_service, "apply_proposal", cancelled_apply)
    pipeline_service.run_pipeline(book.id, book.rel, should_cancel=lambda: state["cancel"])
    assert book.path.read_text(encoding="utf-8") == ""
    assert chapter_service.read_chapter(book.id, book.rel)["status"] == "草稿"


def test_retry_after_ai_failure_reviews_existing_candidate_without_rewriting(book, monkeypatch):
    _install_model(monkeypatch)
    fake = generation_service.run_task
    monkeypatch.setattr(generation_service, "run_task", lambda **kwargs: (
        {"ok": False, "text": ""} if kwargs["task_type"] == "审稿" else fake(**kwargs)))
    first = pipeline_service.run_pipeline(book.id, book.rel)
    assert first["status"] == "manual"
    calls = _install_model(monkeypatch)
    retried = pipeline_service.run_pipeline(book.id, book.rel)
    assert retried["status"] == "done"
    assert [call["task_type"] for call in calls].count("审稿") == 1
    assert not any(call["task_type"] in {"章节正文", "章节修订"} for call in calls)
    assert book.path.read_text(encoding="utf-8") == BODY


def test_discard_while_application_waits_for_lock_does_not_get_applied(book, monkeypatch):
    data = _candidate(book, monkeypatch)
    proposal_id = data["proposal_id"]
    original_lock = file_change_service.project_lock
    state = {"discarded": False}

    @contextmanager
    def discard_before_acquiring(project_id):
        if not state["discarded"]:
            state["discarded"] = True
            # The author discards after apply read 'pending', before apply can
            # acquire its book lock. A status-only edit leaves raw payload equal.
            proposal_service.discard_proposal(proposal_id)
        with original_lock(project_id):
            yield

    monkeypatch.setattr(file_change_service, "project_lock", discard_before_acquiring)
    with pytest.raises(InvalidOperationError):
        proposal_service.apply_proposal(proposal_id)
    assert book.path.read_text(encoding="utf-8") == ""
    assert proposal_service.get_proposal(proposal_id)["status"] == "discarded"


def test_recovery_after_apply_does_not_rewrite_manuscript(book, monkeypatch):
    _install_model(monkeypatch)
    finished = pipeline_service.run_pipeline(book.id, book.rel)
    assert finished["status"] == "done"
    checkpoint = pipeline_service.load_checkpoint(book.id, book.rel)
    checkpoint["steps"]["UPDATE"] = {"status": "pending"}
    checkpoint["steps"]["DECIDE"] = {"status": "pending"}
    pipeline_service.save_checkpoint(book.id, book.rel, checkpoint)

    def forbidden_reapply(*args, **kwargs):
        raise AssertionError("Committed manuscript must not be applied again")

    monkeypatch.setattr(proposal_service, "apply_proposal", forbidden_reapply)
    resumed = pipeline_service.run_pipeline(book.id, book.rel)
    assert resumed["status"] == "done"
    assert book.path.read_text(encoding="utf-8") == BODY


def test_author_edit_during_generation_preserves_editor_text_and_previous_draft(book, monkeypatch):
    data = _candidate(book, monkeypatch, review=False)
    edited = "“我今天不取货。”陆衡把船票撕开，留下铜钱。"
    _install_model(monkeypatch, on_task=lambda task: chapter_service.save_chapter(
        book.id, book.rel, edited) if task == "章节正文" else None)
    with pytest.raises(InvalidOperationError):
        pipeline_service._draft(book.id, book.rel, book.draft, {}, use_ai=True)
    assert book.path.read_text(encoding="utf-8") == edited
    assert book.draft.read_text(encoding="utf-8") == BODY
    assert proposal_service.get_proposal(data["proposal_id"])["content"] == BODY
    assert len(proposal_service.list_proposals(book.id)) == 1


def test_author_edit_after_proposal_prevents_revision_model_call(book, monkeypatch):
    data = _revision_data(book, monkeypatch)
    edited = "“明天再来。”船工收起木箱，合上舱门。"
    chapter_service.save_chapter(book.id, book.rel, edited)
    calls = _install_model(monkeypatch, revise={"patches": [{
        "original": "六枚铜钱", "replacement": "七枚铜钱"}]})
    with pytest.raises(InvalidOperationError):
        pipeline_service._draft(book.id, book.rel, book.draft, data, use_ai=True)
    assert calls == []
    assert book.path.read_text(encoding="utf-8") == edited
    assert book.draft.read_text(encoding="utf-8") == BODY


def test_proposal_creation_failure_does_not_replace_previous_canonical_draft(book, monkeypatch):
    data = _candidate(book, monkeypatch, review=False)
    revised = BODY.replace("六枚铜钱", "七枚铜钱")
    monkeypatch.setattr(generation_service, "run_task", lambda **kwargs: {
        "ok": True, "text": revised, "task_id": None})

    def unavailable(**kwargs):
        raise RuntimeError("proposal database unavailable")

    monkeypatch.setattr(proposal_service, "create_proposal", unavailable)
    with pytest.raises(RuntimeError, match="database unavailable"):
        pipeline_service._draft(book.id, book.rel, book.draft, {}, use_ai=True)
    assert book.draft.read_text(encoding="utf-8") == BODY
    assert proposal_service.get_proposal(data["proposal_id"])["content"] == BODY
    assert book.path.read_text(encoding="utf-8") == ""


def test_recovery_does_not_ingest_author_edit_as_the_reviewed_candidate(book, monkeypatch):
    _install_model(monkeypatch)
    pipeline_service.run_pipeline(book.id, book.rel)
    checkpoint = pipeline_service.load_checkpoint(book.id, book.rel)
    checkpoint["steps"]["UPDATE"] = {"status": "pending"}
    checkpoint["steps"]["DECIDE"] = {"status": "pending"}
    pipeline_service.save_checkpoint(book.id, book.rel, checkpoint)
    edited = "“钱留下，木箱我不要了。”陆衡在舱门上刻了一道划痕。"
    chapter_service.save_chapter(book.id, book.rel, edited)
    calls = _install_model(monkeypatch)
    resumed = pipeline_service.run_pipeline(book.id, book.rel)
    assert resumed["status"] == "blocked"
    assert book.path.read_text(encoding="utf-8") == edited
    assert calls == []
