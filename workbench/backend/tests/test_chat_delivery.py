"""Required artifact receipts are distinct from an executor's write scope."""
from __future__ import annotations

import pytest

from workbench.backend.services import chat_execution_service as policy
from workbench.backend.services import chat_run_service as runs, file_change_service as files
from workbench.backend.services import project_service
from workbench.backend.services import intent_service, chapter_service, settings_service
from workbench.backend import db
import json
from workbench.backend.tests.test_chat_runs import workspace, install, finish, plan_route, TWO_STEPS, FakeNative
from workbench.backend.tests.test_writing_prefs import CLEAN_BODY


@pytest.mark.parametrize("text,scope,context,expected", [
    ("根据大纲/评测依据.md 更新状态/评测连续性.md，记录当前地点和资源，不修改大纲，实际保存。",
     ["状态", "设定"], {}, ["状态/评测连续性.md"]),
    ("根据世界设定更新状态，记录资源账本，不修改设定。", ["状态", "设定"], {}, ["状态"]),
    ("创建设定/大纲草案.md，实际保存。", ["设定", "大纲"], {}, ["设定/大纲草案.md"]),
    ("先读取设定/世界设定.md，再创建状态/资源.md。", ["状态", "设定"], {}, ["状态/资源.md"]),
    ("请创建 `设定/Character Card.md` 并保存。", ["设定"], {}, ["设定/Character Card.md"]),
    ("创建设定/人物.md 和设定/规则.md，两份都保存。", ["设定"], {}, ["设定/人物.md", "设定/规则.md"]),
    ("先创建设定/规则.md，再创建大纲/任务.md，分别保存。", ["设定", "大纲"], {}, ["设定/规则.md", "大纲/任务.md"]),
    ("不改设定/人物.md，只更新状态/人物.md。", ["状态", "设定"], {}, ["状态/人物.md"]),
    ("把当前文件选区中的年龄十八改为十九，保留其余内容。", ["状态", "设定"],
     {"active_file": "设定/人物.md"}, ["设定/人物.md"]),
    ("根据当前文件列大纲并保存。", ["大纲"], {"active_file": "设定/人物.md"}, ["大纲"]),
    ("完善正文并保存。", ["章节"], {"target_chapter": "章节/第0001章.txt"}, ["章节/第0001章.txt"]),
])
def test_delivery_targets_follow_author_outputs_not_role_scope(text, scope, context, expected):
    assert policy.delivery_targets(text, scope, **context) == expected


def test_state_file_receipt_satisfies_broad_context_keeper_scope(workspace, monkeypatch):
    """Reproduce final real-model case 5: actual saved state was marked failed."""
    project, session, _ = workspace
    plan_route(monkeypatch, [], agent="context-keeper", write_targets=["状态", "设定"])
    install(monkeypatch, turns=[[("create_file", {"rel_path": "状态/评测连续性.md",
        "content": "当前地点：茶馆。铜币前值三枚，变动零，结余三枚。"}), "状态已保存。"]])
    result = finish(runs.create_run(session["id"], text=
        "根据大纲/评测依据.md 更新状态/评测连续性.md，记录当前地点和资源，不修改大纲，实际保存。"))
    assert result["status"] == "completed", result
    assert result["routing"]["write_targets"] == ["状态", "设定"]
    assert result["routing"]["delivery_targets"] == ["状态/评测连续性.md"]
    assert result["completion"]["status"] == "verified"
    receipt = result["completion"]["receipts"][0]
    assert files.file_state(project["id"], receipt["path"])["hash"] == receipt["after_hash"]
    assert all(change["path"].startswith("状态/") for change in result["changes"])


def test_receipt_verification_uses_journal_order_when_reuse_is_appended_last(workspace):
    project, session, _ = workspace
    first = files.apply_change(project["id"], run_id="first", session_id=session["id"],
        tool_call_id="one", operation="create", rel_path="状态/版本.md", content="已完成旧步骤。")
    second = files.apply_change(project["id"], run_id="second", session_id=session["id"],
        tool_call_id="two", operation="rewrite", rel_path="状态/版本.md", content="剩余步骤更新的新版本。",
        expected_hash=first["after_hash"])
    assert policy.verify_receipts(project["id"], policy.receipts([second, first]))


@pytest.mark.parametrize("path", ["状态/无关.md", "设定/无关.md"])
def test_other_allowed_file_cannot_satisfy_named_delivery(workspace, monkeypatch, path):
    _, session, _ = workspace
    plan_route(monkeypatch, [], agent="context-keeper", write_targets=["状态", "设定"])
    install(monkeypatch, turns=[[("create_file", {"rel_path": path, "content": "真实保存但目标错误。"}), "完成。"]])
    result = finish(runs.create_run(session["id"], text="请创建状态/指定.md 并实际保存。"))
    assert result["error_code"] == "DELIVERY_UNVERIFIED"
    assert result["changes"][0]["status"] == "applied"
    assert result["routing"]["delivery_targets"] == ["状态/指定.md"]


@pytest.mark.parametrize("complete", [False, True])
def test_two_named_files_in_same_material_each_require_a_receipt(workspace, monkeypatch, complete):
    _, session, _ = workspace
    plan_route(monkeypatch, [], agent="setting-keeper", write_targets=["设定"])
    actions = [("create_file", {"rel_path": "设定/人物.md", "content": "人物卡已保存。"})]
    if complete:
        actions.append(("create_file", {"rel_path": "设定/规则.md", "content": "规则已保存。"}))
    install(monkeypatch, turns=[[*actions, "两份均已完成。"]])
    result = finish(runs.create_run(session["id"], text="请创建设定/人物.md 和设定/规则.md，两份都必须实际保存。"))
    assert result["status"] == ("completed" if complete else "failed")
    if not complete:
        assert result["error_code"] == "DELIVERY_UNVERIFIED"
    assert result["routing"]["delivery_targets"] == ["设定/人物.md", "设定/规则.md"]


def test_multi_step_named_output_cannot_be_replaced_by_neighbor_file(workspace, monkeypatch):
    _, session, _ = workspace
    plan_route(monkeypatch, TWO_STEPS, write_targets=["设定", "大纲"])
    engine = install(monkeypatch, turns=[
        [("create_file", {"rel_path": "设定/无关.md", "content": "无关设定。"}), "已保存。"],
        [("create_file", {"rel_path": "大纲/指定.md", "content": "不应执行。"}), "已保存。"],
    ])
    result = finish(runs.create_run(session["id"], text="先创建设定/指定.md，再创建大纲/指定.md，两份都实际保存。"))
    assert result["error_code"] == "DELIVERY_UNVERIFIED" and len(engine.calls) == 1
    assert result["plan_state"]["steps"][0]["delivery_targets"] == ["设定/指定.md"]
    assert result["plan_state"]["steps"][1]["status"] == "pending"


def test_saved_named_output_changed_before_done_does_not_verify(workspace, monkeypatch):
    project, session, _ = workspace
    plan_route(monkeypatch, [], agent="context-keeper", write_targets=["状态", "设定"])
    _, root = project_service.get_project_dir(project["id"])
    class AuthorEditsNative(FakeNative):
        def stream(self, **kwargs):
            for event in super().stream(**kwargs):
                yield event
                if event.get("type") == "tool_result" and event.get("result", {}).get("ok"):
                    (root / "状态/指定.md").write_text("作者随后修改了当前版本。", encoding="utf-8")
    engine = AuthorEditsNative(turns=[[("create_file", {"rel_path": "状态/指定.md", "content": "模型初版。"}), "已保存。"]])
    monkeypatch.setattr(runs, "_runtime", engine)
    result = finish(runs.create_run(session["id"], text="请创建状态/指定.md 并实际保存。"))
    assert result["error_code"] == "DELIVERY_UNVERIFIED"
    assert not policy.verify_receipts(project["id"], result["completion"]["receipts"])


def test_precise_named_delivery_continuation_skips_only_verified_artifact(workspace, monkeypatch):
    project, session, _ = workspace
    plan_route(monkeypatch, TWO_STEPS, write_targets=["设定", "大纲"])
    engine = install(monkeypatch, turns=[
        [("create_file", {"rel_path": "设定/规则.md", "content": "铜币只来自委托。"}), "已保存。"],
        [{"type": "error", "code": "MOCK_ERROR", "message": "合成第二步连接故障"}],
        [("create_file", {"rel_path": "大纲/任务.md", "content": "按规则调查账本。"}), "已保存。"],
    ])
    first = finish(runs.create_run(session["id"], text="先创建设定/规则.md，再创建大纲/任务.md，两份实际保存。"))
    second = finish(runs.create_run(session["id"], text="继续完成未完成步骤", continue_from_run_id=first["id"]))
    assert second["status"] == "completed", second
    assert len(engine.calls) == 3
    assert second["plan_state"]["steps"][0]["reused"]
    assert policy.covers_targets(second["completion"]["receipts"], ["设定/规则.md", "大纲/任务.md"])
    assert policy.verify_receipts(project["id"], second["completion"]["receipts"])


REPORT_THEN_WRITE = [
    {"step": 1, "agent": "reviewer", "write_targets": [], "read_only_refs": ["章节"], "note": "审稿并报告问题"},
    {"step": 2, "agent": "writer", "write_targets": ["章节"], "read_only_refs": [], "note": "按审稿意见修正正文"},
]


def test_report_leader_keeps_later_authorized_writer_and_each_step_guard(workspace, monkeypatch):
    project, session, _ = workspace
    chapter = chapter_service.create_chapter(project["id"], title="合成审稿")
    _, root = project_service.get_project_dir(project["id"])
    rel = chapter["rel_path"]
    (root / rel).write_text(CLEAN_BODY, encoding="utf-8")
    settings_service.update_settings({"chat": {"scope_strictness": "reject"},
        "writing": {"auto_deslop": {"enabled": False}}})
    decision = {"agent": "reviewer", "write_targets": ["章节"], "plan": REPORT_THEN_WRITE}
    guarded = intent_service._guard_result(decision, "先审稿当前章节，再由写作角色修正正文并保存。")
    assert not guarded["read_only"] and guarded["write_targets"] == ["章节"]
    assert guarded["plan"][0]["write_targets"] == []
    plan_route(monkeypatch, guarded["plan"], agent="reviewer", write_targets=guarded["write_targets"])
    engine = install(monkeypatch, turns=[
        [("create_file", {"rel_path": "备忘录/审稿越权.md", "content": "不许写。"}), "审稿意见：调整码头称呼。"],
        [("read_file", {"rel_path": rel}),
         ("write_file", {"rel_path": rel, "content": CLEAN_BODY.replace("码头", "港口")}),
         ("create_file", {"rel_path": "设定/写作越权.md", "content": "不许跨域。"}), "正文已保存。"],
    ])
    result = finish(runs.create_run(session["id"], text="先审稿当前章节，再由写作角色修正正文并保存。",
        context={"active_file": rel, "target_chapter": rel}))
    assert result["status"] == "completed", result
    assert len(engine.calls) == 2 and not result["read_only"]
    readonly_tools = {(spec.get("function") or spec)["name"] for spec in engine.calls[0]["tool_specs"]}
    write_tools = {(spec.get("function") or spec)["name"] for spec in engine.calls[1]["tool_specs"]}
    assert not readonly_tools & {"create_file", "write_file", "new_chapter", "revert_last_changes"}
    assert "write_file" in write_tools
    assert result["plan_state"]["steps"][0]["status"] == "reported"
    assert result["plan_state"]["steps"][1]["status"] == "verified"
    assert not (root / "备忘录/审稿越权.md").exists()
    assert not (root / "设定/写作越权.md").exists()
    assert "港口" in (root / rel).read_text(encoding="utf-8")
    assert result["routing"]["delivery_targets"] == [rel]


def test_exclusive_report_instruction_still_denies_entire_proposed_plan(workspace, monkeypatch):
    _, session, _ = workspace
    guarded = intent_service._guard_result({"agent": "reviewer", "write_targets": ["章节"],
        "plan": REPORT_THEN_WRITE}, "只报告审稿意见，不要改正文。")
    assert guarded["read_only"] and guarded["write_targets"] == [] and guarded["plan"] == []
    plan_route(monkeypatch, REPORT_THEN_WRITE, agent="reviewer", write_targets=["章节"])
    engine = install(monkeypatch, turns=[["报告意见。"], ["不应执行写作。"]])
    result = finish(runs.create_run(session["id"], text="只报告审稿意见，不要改正文。"))
    assert result["status"] == "completed" and result["read_only"]
    assert len(engine.calls) == 1 and result["routing"]["plan"] == [] and not result["changes"]


@pytest.mark.parametrize("text,readonly", [
    ("先审稿，只报告正文问题；再由 writer 改写正文并保存。", False),
    ("先审稿，只报告问题，然后根据审稿意见修正正文。", False),
    ("先审稿，只报告问题，再讨论如何修改正文。", True),
    ("整个任务只报告问题，先审稿，再由 writer 改写正文。", True),
    ("只报告审稿意见，先审稿，再由 writer 改写正文。", True),
    ("不要修改任何文件，先审稿，再由 writer 改写正文。", True),
    ("只报告审稿意见，不要修改任何文件。", True),
])
def test_report_constraint_applies_to_its_stage_or_entire_task(text, readonly):
    assert runs.is_read_only({"discussion_only": False}, text) is readonly
    guarded = intent_service._guard_result({"agent": "reviewer", "write_targets": ["章节"], "plan": REPORT_THEN_WRITE}, text)
    assert guarded["read_only"] is readonly
    assert bool(guarded["plan"]) is not readonly
    assert runs.is_read_only({"discussion_only": True}, text)


def test_offline_fallback_keeps_ordered_review_and_revision_from_registry(workspace):
    from workbench.backend.services import routing_service
    result = routing_service.route(None, "先审稿当前章节，只报告问题；再由 writer 修正正文并保存。", record=False)
    assert result["source"] == "keyword" and not result["read_only"]
    assert [step["agent"] for step in result["plan"]] == ["reviewer", "writer"]
    assert result["plan"][0]["write_targets"] == []
    assert result["plan"][1]["write_targets"] == ["章节"]


@pytest.mark.parametrize("change", ["unchanged", "read-file", "injected-setting", "legacy-missing-evidence", "tampered-report", "unversioned-query", "no-reads"])
def test_completed_readonly_step_reuses_only_verified_report_inputs(workspace, monkeypatch, change):
    project, session, _ = workspace
    chapter = chapter_service.create_chapter(project["id"], title="报告续接")
    _, root = project_service.get_project_dir(project["id"])
    rel = chapter["rel_path"]
    (root / rel).write_text(CLEAN_BODY, encoding="utf-8")
    settings_service.update_settings({"writing": {"auto_deslop": {"enabled": False}}})
    plan_route(monkeypatch, REPORT_THEN_WRITE, agent="reviewer", write_targets=["章节"])
    repeated_report = [("read_file", {"rel_path": rel}), "已重新核对，建议修改码头称呼。"]
    writer = [("read_file", {"rel_path": rel}),
        ("write_file", {"rel_path": rel, "content": CLEAN_BODY.replace("码头", "港口")}), "正文已保存。"]
    first_report = ([] if change == "no-reads" else [("read_file", {"rel_path": rel})])
    if change == "unversioned-query":
        first_report.append(("search_files", {"query": "码头"}))
    engine = install(monkeypatch, turns=[
        [*first_report, "已核对当前正文，建议修改码头称呼。"],
        [{"type": "error", "code": "MOCK_ERROR", "message": "写作步骤连接故障"}],
        *([] if change == "unchanged" else [repeated_report]), writer,
    ])
    first = finish(runs.create_run(session["id"], text="先审稿，只报告正文问题；再由 writer 改写正文并保存。",
        context={"active_file": rel, "target_chapter": rel}))
    assert first["plan_state"]["steps"][0]["status"] == "reported"
    report = first["plan_state"]["steps"][0]["report"]
    assert bool(report["input_versions"]) is (change != "no-reads")
    assert report["context_fingerprint"] and report["text_hash"]
    if change == "read-file":
        (root / rel).write_text(CLEAN_BODY.replace("码头", "渡口"), encoding="utf-8")
    elif change == "injected-setting":
        (root / "设定/世界设定.md").write_text("港口是唯一渡海地点。", encoding="utf-8")
    elif change in {"legacy-missing-evidence", "tampered-report"}:
        with db.get_conn() as connection:
            row = connection.execute("SELECT meta FROM chat_messages WHERE id=?", (first["assistant_message_id"],)).fetchone()
            meta = json.loads(row[0])
            if change == "legacy-missing-evidence":
                meta["plan_state"]["steps"][0].pop("report")
            else:
                meta["plan_state"]["steps"][0]["report"]["text"] = "被手动篡改的未核实报告。"
            connection.execute("UPDATE chat_messages SET meta=? WHERE id=?", (json.dumps(meta, ensure_ascii=False), first["assistant_message_id"]))
            event = connection.execute("SELECT seq,payload FROM chat_run_events WHERE run_id=? AND json_extract(payload,'$.event')='plan_state' ORDER BY seq DESC LIMIT 1", (first["id"],)).fetchone()
            payload = json.loads(event[1])
            payload["plan_state"] = meta["plan_state"]
            connection.execute("UPDATE chat_run_events SET payload=? WHERE run_id=? AND seq=?", (json.dumps(payload, ensure_ascii=False), first["id"], event[0]))
    second = finish(runs.create_run(session["id"], text="继续完成未完成步骤", continue_from_run_id=first["id"]))
    assert second["status"] == "completed", second
    assert len(engine.calls) == (3 if change == "unchanged" else 4)
    assert bool(second["plan_state"]["steps"][0].get("reused")) is (change == "unchanged")
    if change == "unchanged":
        assert "上一步报告已核验" in engine.calls[-1]["context"]
