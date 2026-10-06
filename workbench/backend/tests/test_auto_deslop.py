"""落盘前自动去味重写 + 对话回复体检。"""

from __future__ import annotations

import itertools
from pathlib import Path
from types import SimpleNamespace

import pytest

from workbench.backend import config, db
from workbench.backend.services import (
    chapter_service, chat_run_service as runs, chat_workspace_tools as tools,
    file_change_service as changes, generation_service, project_service,
    settings_service, style_service, chat_preference_service,
)
from workbench.backend.services.errors import InvalidOperationError

# 干净长正文（含对话、≥2000 汉字，可通过全部门禁）
CLEAN_BODY = (
    "码头上的风带着咸味。老周把缆绳在桩子上绕了两圈，手背上的旧伤被磨得发亮。\n"
    "“今夜潮水涨得早。”他抬头看了一眼天。\n"
    "沈砚没答话，他从怀里摸出半块干饼，掰了一角递过去。"
    "“你上次说，北边那批货晚三天到。”\n"
    "“晚三天。”老周接了饼，没吃，捏在手里，“船老大的儿子病了，"
    "耽误了两日。第三日是逆风。”\n"
) * 30

# 去 AI 味类门禁失败：禁词（突然 / 仿佛）+ 三连排比
DIRTY_BODY = CLEAN_BODY + "\n突然，他仿佛看见了什么。不是恐惧，不是惊讶，而是麻木。\n"
# 重写结果：干净正文，可通过门禁
REWRITTEN_BODY = CLEAN_BODY + "\n他停在门口，把伞收好，抖了抖上面的水。\n"
# 回复体检：单行成段正文（≥150 汉字）且命中禁词
DIRTY_REPLY = "他突然站着，仿佛灯亮了起来。" * 30


@pytest.fixture()
def book(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "books")
    monkeypatch.setattr(config, "RUNTIME_DIR", tmp_path / ".workbench")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / ".workbench" / "test.db")
    db.init_db()
    config.ensure_runtime_dirs()
    project = project_service.create_project(name="自动去味书")
    _, root = project_service.get_project_dir(project["id"])
    ctx = {"project_id": project["id"], "session_id": 1, "run_id": "run-deslop",
           "tool_call_id": "call-0", "auto_apply": True, "read_only": False,
           "read_hashes": {}, "change_ids": [], "proposals": [],
           "provider": "test-provider", "model": "test-model"}
    calls = itertools.count(1)

    def run_tool(name: str, args: dict) -> dict:
        ctx["tool_call_id"] = f"call-{next(calls)}"
        return tools.execute(name, args, ctx)

    return SimpleNamespace(id=project["id"], root=root, ctx=ctx, run_tool=run_tool)


def _fake_model(monkeypatch, text: str) -> list[dict]:
    calls: list[dict] = []

    def run_task(**kwargs):
        calls.append(kwargs)
        return {"ok": True, "text": text}

    monkeypatch.setattr(generation_service, "run_task", run_task)
    return calls


# ─────────────────────────── 自动去味落盘 ───────────────────────────


def test_gate_failure_triggers_rewrite_and_applies_rewritten_text(book, monkeypatch):
    calls = _fake_model(monkeypatch, REWRITTEN_BODY)

    result = book.run_tool("new_chapter", {"title": "去味稿", "content": DIRTY_BODY})

    assert result["ok"], result
    assert calls and calls[0]["task_type"] == "去AI味重写"
    assert calls[0]["provider"] == "test-provider" and calls[0]["model"] == "test-model"
    from workbench.backend.services.prompt_registry_service import PROSE_CRAFT_GUIDANCE
    assert PROSE_CRAFT_GUIDANCE in calls[0]["system"]
    chapter = chapter_service.read_chapter(book.id, result["data"]["path"])
    # 落盘的是重写后的文本（被预览、被审批、被落盘的同一份）
    assert "突然" not in chapter["body"] and "而是麻木" not in chapter["body"]
    assert "他停在门口" in chapter["body"]
    assert result["data"]["after_content"] == chapter["content"]


def test_rewrite_still_failing_is_rejected_with_locations(book, monkeypatch):
    calls = _fake_model(monkeypatch, DIRTY_BODY)

    result = book.run_tool("new_chapter", {"title": "仍失败", "content": DIRTY_BODY})

    assert result["ok"] is False and result["code"] == "gate_rejected"
    assert len(calls) == 2                       # 默认 max_rounds = 2
    assert "禁词门" in result["gates"]["blocking_gates"]
    assert any(item["gate"] == "禁词门" for item in result["gates"]["locations"])
    assert chapter_service.list_chapters(book.id) == []


def test_auto_deslop_disabled_skips_rewrite(book, monkeypatch):
    settings_service.update_settings({"writing": {"auto_deslop": {"enabled": False}}})
    calls = _fake_model(monkeypatch, REWRITTEN_BODY)

    result = book.run_tool("new_chapter", {"title": "未去味", "content": DIRTY_BODY})

    assert result["ok"] is False and result["code"] == "gate_rejected"
    assert calls == []


def test_model_failure_skips_deslop_without_blocking(book, monkeypatch):
    def failing(**kwargs):
        return {"ok": False, "error_message": "模型超时", "text": ""}

    monkeypatch.setattr(generation_service, "run_task", failing)

    result = book.run_tool("new_chapter", {"title": "模型失败", "content": DIRTY_BODY})

    # 模型失败不阻断语义：仍按门禁拒绝（与改造前一致），而不是抛模型错误
    assert result["ok"] is False and result["code"] == "gate_rejected"
    assert chapter_service.list_chapters(book.id) == []


def test_rewrite_only_applies_to_chapters_and_produced_content(book, monkeypatch):
    calls = _fake_model(monkeypatch, REWRITTEN_BODY)

    plain = book.run_tool("create_file", {"rel_path": "备忘录/笔记.md", "content": DIRTY_BODY})
    assert plain["ok"] and calls == []

    # 章节里去 AI 味门禁通过时也不触发重写
    clean = book.run_tool("new_chapter", {"title": "干净稿", "content": CLEAN_BODY})
    assert clean["ok"] and calls == []


def test_replace_exact_deslop_preserves_operation_and_unselected_text(book, monkeypatch):
    initial = CLEAN_BODY + "\n独一无二的锚点：钉在墙上。\n"
    created = book.run_tool("new_chapter", {"title": "初稿", "content": initial})
    assert created["ok"]
    path = created["data"]["path"]
    calls = _fake_model(monkeypatch, "他把钉子留在原处")

    edited = book.run_tool("replace_text", {
        "rel_path": path, "old_text": "独一无二的锚点", "new_text": "突然他仿佛病了",
        "expected_hash": created["data"]["after_hash"]})

    assert edited["ok"], edited
    assert edited["data"]["operation"] == "replace_exact"
    content = changes.file_state(book.id, path)["content"]
    assert content == initial.replace("独一无二的锚点", "他把钉子留在原处", 1)
    recorded = changes.get_call(book.ctx["run_id"], book.ctx["tool_call_id"], book.id)
    assert recorded["request"]["old_text"] == "独一无二的锚点"
    assert recorded["request"]["new_text"] == "他把钉子留在原处"
    assert recorded["request"]["expected_hash"] == created["data"]["after_hash"]
    assert recorded["request"]["content"] is None
    message = calls[0]["messages"][0]["content"]
    assert message.split("【待修订片段】\n", 1)[1] == "突然他仿佛病了"
    assert "【只读前文：禁止输出或修改】" in message
    assert "【只读后文：禁止输出或修改】\n：钉在墙上。" in message
    assert "不要补写整章" in calls[0]["system"]
    assert calls[0]["context_snapshot"]["rewrite_scope"] == "fragment"


def test_replace_exact_retries_only_previous_fragment(book, monkeypatch):
    initial = CLEAN_BODY + "\n独一无二的锚点。\n"
    created = book.run_tool("new_chapter", {"title": "重试稿", "content": initial})
    calls = []
    outputs = iter(("他突然缩手", "他把手收回袖口"))

    def generate(**kwargs):
        calls.append(kwargs)
        return {"ok": True, "text": next(outputs)}

    monkeypatch.setattr(generation_service, "run_task", generate)
    edited = book.run_tool("replace_text", {
        "rel_path": created["data"]["path"], "old_text": "独一无二的锚点",
        "new_text": "突然他仿佛病了", "expected_hash": created["data"]["after_hash"]})

    assert edited["ok"], edited
    assert len(calls) == 2
    assert calls[1]["messages"][0]["content"].split("【待修订片段】\n", 1)[1] == "他突然缩手"
    assert changes.file_state(book.id, created["data"]["path"])["content"] == initial.replace(
        "独一无二的锚点", "他把手收回袖口", 1)


def test_replace_exact_does_not_expand_to_fix_dirty_surrounding_text(book, monkeypatch):
    initial = DIRTY_BODY + "\n独一无二的锚点。\n"
    # 存量稿或作者手工编辑的正文可能未过门禁；局部修改也必须复验候选整章。
    created = changes.apply_change(
        book.id, run_id=book.ctx["run_id"], session_id=book.ctx["session_id"],
        tool_call_id="existing-dirty", operation="create", rel_path="章节/第0001章.txt",
        content=initial, title="存量脏稿", validate_gates=False)
    calls = _fake_model(monkeypatch, "他把手收回袖口")

    edited = book.run_tool("replace_text", {
        "rel_path": created["path"], "old_text": "独一无二的锚点", "new_text": "突然缩手",
        "expected_hash": created["after_hash"]})

    assert edited["ok"] is False and edited["code"] == "gate_rejected"
    assert len(calls) == 2
    assert "禁词门" in edited["gates"]["blocking_gates"]
    assert changes.file_state(book.id, created["path"])["content"] == initial
    assert all(item["context_snapshot"]["rewrite_scope"] == "fragment" for item in calls)
    assert calls[1]["messages"][0]["content"].split("【待修订片段】\n", 1)[1] == "他把手收回袖口"
    recorded = changes.get_call(book.ctx["run_id"], book.ctx["tool_call_id"], book.id)
    assert recorded["request"]["operation"] == "replace_exact"
    assert recorded["request"]["expected_hash"] == created["after_hash"]


@pytest.mark.parametrize("raises", [False, True])
def test_replace_exact_model_failure_keeps_original_file(book, monkeypatch, raises):
    initial = CLEAN_BODY + "\n独一无二的锚点。\n"
    created = book.run_tool("new_chapter", {"title": "失败稿", "content": initial})
    calls = []

    def failing(**kwargs):
        calls.append(kwargs)
        if raises:
            raise RuntimeError("模型连接中断")
        return {"ok": False, "text": "", "error_message": "模型超时"}

    monkeypatch.setattr(generation_service, "run_task", failing)
    edited = book.run_tool("replace_text", {
        "rel_path": created["data"]["path"], "old_text": "独一无二的锚点",
        "new_text": "突然他仿佛病了", "expected_hash": created["data"]["after_hash"]})

    assert edited["ok"] is False and edited["code"] == "gate_rejected"
    assert len(calls) == 1
    assert changes.file_state(book.id, created["data"]["path"])["content"] == initial


def test_replace_exact_cancelled_during_model_call_does_not_apply(book, monkeypatch):
    initial = CLEAN_BODY + "\n独一无二的锚点。\n"
    created = book.run_tool("new_chapter", {"title": "停止稿", "content": initial})
    cancelled = False
    book.ctx["should_cancel"] = lambda: cancelled

    def generate(**kwargs):
        nonlocal cancelled
        cancelled = True
        return {"ok": True, "text": "他把手收回袖口"}

    monkeypatch.setattr(generation_service, "run_task", generate)
    edited = book.run_tool("replace_text", {
        "rel_path": created["data"]["path"], "old_text": "独一无二的锚点",
        "new_text": "突然他仿佛病了", "expected_hash": created["data"]["after_hash"]})

    assert edited["ok"] is False and "已停止" in edited["summary"]
    assert changes.file_state(book.id, created["data"]["path"])["content"] == initial


def test_deslop_includes_approved_style_and_preservation_rules(book, monkeypatch):
    calls = _fake_model(monkeypatch, REWRITTEN_BODY)
    reference = {"text": "本书认可样稿，仅供写法参考。", "reference_hash": "approved-style-hash",
                 "samples": [{"rel_path": "章节/第0001章.txt", "content_hash": "sample-hash"}]}
    inputs = []

    def style_reference(project_id, **kwargs):
        inputs.append((project_id, kwargs))
        return reference

    monkeypatch.setattr(style_service, "generation_reference", style_reference, raising=False)
    result = book.run_tool("new_chapter", {"title": "有文风参照", "content": DIRTY_BODY})

    assert result["ok"], result
    assert inputs[0][0] == book.id
    assert inputs[0][1]["chapter_rel"] == result["data"]["path"]
    assert reference["text"] in calls[0]["messages"][0]["content"]
    assert "事件顺序" in calls[0]["system"] and "视角" in calls[0]["system"]
    assert "人物知道和不知道的信息" in calls[0]["system"]
    assert calls[0]["context_snapshot"]["style_reference"]["reference_hash"] == reference["reference_hash"]
    assert calls[0]["context_snapshot"]["style_reference"]["samples"] == reference["samples"]


def test_new_chapter_deslop_replay_reuses_effective_request_without_model_or_disk_write(book, monkeypatch):
    calls = _fake_model(monkeypatch, REWRITTEN_BODY)
    args = {"title": "可重放去味稿", "content": DIRTY_BODY}
    original = book.run_tool("new_chapter", args)
    assert original["ok"], original
    path = original["data"]["path"]
    state = changes.file_state(book.id, path)
    stat = (book.root / path).stat()
    prior = changes.get_call(book.ctx["run_id"], book.ctx["tool_call_id"], book.id)
    assert prior["request"]["content"] == REWRITTEN_BODY.strip()
    assert prior["request"]["source_request"]["arguments"] == args
    assert prior["request"]["source_request"]["resolved"]["content"] == DIRTY_BODY
    # 权限档位变为严格询问也只重放已执行记录，不重复模型、审批或写盘。
    book.ctx["permission_mode"] = "ask"
    replay = tools.execute("new_chapter", args, book.ctx)

    assert replay["ok"] and replay["data"]["replayed"], replay
    assert replay["change_id"] == original["change_id"]
    assert len(calls) == 1 and len(changes.list_changes(book.ctx["run_id"], book.id)) == 1
    assert changes.file_state(book.id, path) == state
    assert (book.root / path).stat().st_mtime_ns == stat.st_mtime_ns


@pytest.mark.parametrize("field,value", [
    ("content", REWRITTEN_BODY), ("title", "另一标题"), ("rel_path", "章节/第0002章.txt"),
    ("expected_hash", "different"), ("old_text", "另一个原文"), ("new_text", "其他替换"),
    ("destination", "章节/第0002章.txt"),
])
def test_deslop_replay_different_source_request_is_rejected(book, monkeypatch, field, value):
    calls = _fake_model(monkeypatch, REWRITTEN_BODY)
    args = {"title": "来源绑定", "content": DIRTY_BODY}
    original = book.run_tool("new_chapter", args)
    assert original["ok"]
    modified = {**args, field: value}

    rejected = tools.execute("new_chapter", modified, book.ctx)

    assert rejected["ok"] is False and "来源参数不一致" in rejected["summary"]
    assert len(calls) == 1
    assert len(changes.list_changes(book.ctx["run_id"], book.id)) == 1
    assert changes.file_state(book.id, original["data"]["path"])["content"] == REWRITTEN_BODY.strip()


def test_deslop_replay_different_tool_is_rejected(book, monkeypatch):
    calls = _fake_model(monkeypatch, REWRITTEN_BODY)
    args = {"title": "工具绑定", "content": DIRTY_BODY}
    original = book.run_tool("new_chapter", args)
    assert original["ok"]
    rejected = tools.execute("create_file", {**args, "rel_path": original["data"]["path"]}, book.ctx)

    assert rejected["ok"] is False and "来源参数不一致" in rejected["summary"]
    assert len(calls) == 1


def test_replace_exact_deslop_replay_preserves_origin_and_effective_hash_checks(book, monkeypatch):
    initial = CLEAN_BODY + "\n独一无二的锚点。\n"
    created = book.run_tool("new_chapter", {"title": "片段重放", "content": initial})
    calls = _fake_model(monkeypatch, "他把手收回袖口")
    args = {"rel_path": created["data"]["path"], "old_text": "独一无二的锚点",
            "new_text": "突然他仿佛病了"}
    edited = book.run_tool("replace_text", args)
    assert edited["ok"], edited
    prior = changes.get_call(book.ctx["run_id"], book.ctx["tool_call_id"], book.id)
    replay = tools.execute("replace_text", args, book.ctx)
    assert replay["ok"] and replay["data"]["replayed"], replay
    assert len(calls) == 1
    assert prior["request"]["source_request"]["resolved"]["expected_hash"] == created["data"]["after_hash"]
    effective_args = {"run_id": book.ctx["run_id"], "session_id": book.ctx["session_id"],
                      "tool_call_id": book.ctx["tool_call_id"], "operation": "replace_exact",
                      "rel_path": created["data"]["path"], "old_text": "独一无二的锚点",
                      "new_text": "他把手收回袖口", "expected_hash": created["data"]["after_hash"]}
    with pytest.raises(InvalidOperationError, match="参数不一致"):
        changes.apply_change(book.id, **effective_args)  # 不能只凭有效稿跳过来源凭据。
    with pytest.raises(InvalidOperationError, match="参数不一致"):
        changes.apply_change(book.id, **{**effective_args, "new_text": "伪造有效稿"},
                             source_request=prior["request"]["source_request"])


def test_deslop_reverted_replay_does_not_reapply_or_claim_applied(book, monkeypatch):
    calls = _fake_model(monkeypatch, REWRITTEN_BODY)
    args = {"title": "已撤回", "content": DIRTY_BODY}
    original = book.run_tool("new_chapter", args)
    assert original["ok"]
    changes.revert(book.ctx["run_id"], [original["change_id"]], project_id=book.id)
    book.ctx["read_hashes"][original["data"]["path"]] = None
    replay = tools.execute("new_chapter", args, book.ctx)

    assert replay["ok"] and replay["applied"] is False
    assert replay["data"]["status"] == "reverted" and replay["data"]["replayed"]
    assert "重放未重新执行" in replay["summary"]
    assert not changes.file_state(book.id, original["data"]["path"])["exists"]
    assert book.ctx["read_hashes"][original["data"]["path"]] is None
    assert len(calls) == 1


def test_deslop_rejected_effective_candidate_replay_keeps_gate_rejection(book, monkeypatch):
    short_clean = CLEAN_BODY[:len(CLEAN_BODY) // 30]
    calls = _fake_model(monkeypatch, short_clean)
    args = {"title": "有效稿仍不足字数", "content": DIRTY_BODY}
    original = book.run_tool("new_chapter", args)
    assert original["ok"] is False and original["code"] == "gate_rejected"
    prior = changes.get_call(book.ctx["run_id"], book.ctx["tool_call_id"], book.id)
    assert prior["request"]["source_request"]["arguments"] == args
    replay = tools.execute("new_chapter", args, book.ctx)

    assert replay["ok"] is False and replay["code"] == "gate_rejected"
    assert replay["change_id"] == original["change_id"]
    assert len(calls) == 1 and chapter_service.list_chapters(book.id) == []


@pytest.mark.parametrize("restriction", ["read_only", "cancel", "scope"])
def test_deslop_replay_respects_current_restrictions(book, monkeypatch, restriction):
    calls = _fake_model(monkeypatch, REWRITTEN_BODY)
    args = {"title": "权限仍生效", "content": DIRTY_BODY}
    original = book.run_tool("new_chapter", args)
    assert original["ok"]
    if restriction == "read_only":
        book.ctx["read_only"] = True
    elif restriction == "cancel":
        book.ctx["should_cancel"] = lambda: True
    else:
        book.ctx["scope"] = {"label": "只改设定", "dirs": ["设定"], "files": [], "declared": True}
        monkeypatch.setattr(chat_preference_service, "get_scope_prefs", lambda: {
            "scope_strictness": "reject", "scope_limit_full": False})
    replay = tools.execute("new_chapter", args, book.ctx)

    assert replay["ok"] is False, replay
    assert len(calls) == 1
    assert changes.file_state(book.id, original["data"]["path"])["content"] == REWRITTEN_BODY.strip()


# ─────────────────────────── 对话回复体检 ───────────────────────────


def test_reply_quality_hit_is_recorded(monkeypatch):
    emitted: list[tuple] = []
    monkeypatch.setattr(runs, "emit",
                        lambda run_id, event, **payload: emitted.append((run_id, event, payload)))

    hits = runs.check_reply_quality("run-x", DIRTY_REPLY)

    assert "密度门" in hits
    assert emitted and emitted[0][1] == "context_warning"
    message = emitted[0][2]["message"]
    assert message.startswith(runs.REPLY_QUALITY_PREFIX)
    assert "写工具落盘" in message


def test_reply_quality_ignores_short_or_non_paragraph_replies():
    assert runs._reply_prose_hits("已经保存到章节文件，改动共 3 处。") == []
    assert runs._reply_prose_hits("甲" * 200) == []                  # 总汉字数不足
    assert runs._reply_prose_hits("甲乙丙丁戊。\n" * 60) == []      # 有长度但无成段正文
    assert runs._reply_prose_hits("") == []
