"""每章字数区间：书级覆盖 / 全局默认 / 字数门 / 合同预算 / 导出对齐。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from workbench.backend import config, db
from workbench.backend.services import (
    agent_service, chapter_service, chat_run_service as runs, chat_service,
    contract_service, project_service, publish_service, review_service,
    settings_service, writing_preference_service,
)
from workbench.backend.services.errors import InvalidOperationError

# 干净长正文（含对话、无禁词、无 Markdown 泄漏），汉字数 ≥ 2000
CLEAN_BODY = (
    "码头上的风带着咸味。老周把缆绳在桩子上绕了两圈，手背上的旧伤被磨得发亮。\n"
    "“今夜潮水涨得早。”他抬头看了一眼天。\n"
    "沈砚没答话，他从怀里摸出半块干饼，掰了一角递过去。"
    "“你上次说，北边那批货晚三天到。”\n"
    "“晚三天。”老周接了饼，没吃，捏在手里，“船老大的儿子病了，"
    "耽误了两日。第三日是逆风。”\n"
) * 30


@pytest.fixture()
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    for name in ("books", "agents", "skills", "rules", "workflows"):
        (tmp_path / name).mkdir()
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "books")
    monkeypatch.setattr(config, "RUNTIME_DIR", tmp_path / ".workbench")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / ".workbench" / "workbench.db")
    db.init_db()
    config.ensure_runtime_dirs()
    agent_service.ensure_builtin_agents()
    project = project_service.create_project(name="字数区间书")
    yield project
    runs.shutdown()


# ─────────────────────────── 书级区间读写 ───────────────────────────


def test_book_range_roundtrip_and_global_default(workspace):
    pid = workspace["id"]
    assert writing_preference_service.get_writing_prefs(pid) == {
        "chapter_min_words": 2000, "chapter_max_words": 4000, "source": "global"}
    saved = writing_preference_service.update_writing_prefs(
        pid, chapter_min_words=2000, chapter_max_words=3500)
    assert saved == {"chapter_min_words": 2000, "chapter_max_words": 3500, "source": "book"}
    path = project_service.get_project_dir(pid)[1] / ".meta/writing-prefs.json"
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "chapter_min_words": 2000, "chapter_max_words": 3500}
    assert writing_preference_service.chapter_word_range(pid) == (2000, 3500)

    settings_service.update_settings({"writing": {"chapter_min_words": 1000, "chapter_max_words": 9000}})
    other = project_service.create_project(name="另一本")
    assert writing_preference_service.chapter_word_range(pid) == (2000, 3500)
    assert writing_preference_service.chapter_word_range(other["id"]) == (1000, 9000)


@pytest.mark.parametrize("minimum,maximum", [
    (0, 100), (3000, 2000), (2000, 2000), (-5, 100), (100, 50001), (100, 50000.5),
])
def test_invalid_range_is_rejected(workspace, minimum, maximum):
    with pytest.raises(InvalidOperationError):
        writing_preference_service.update_writing_prefs(
            workspace["id"], chapter_min_words=minimum, chapter_max_words=maximum)
    # 非法写入不落盘，仍回落全局默认
    assert writing_preference_service.get_writing_prefs(workspace["id"])["source"] == "global"


@pytest.mark.parametrize("bad", [
    "{",
    "[]",
    '{"chapter_min_words":3000}',
    '{"chapter_min_words":3000,"chapter_max_words":2000}',
    '{"chapter_min_words":"2000","chapter_max_words":3000}',
    '{"chapter_min_words":true,"chapter_max_words":3000}',
])
def test_malformed_book_range_never_falls_back_to_looser_global(workspace, bad):
    pid = workspace["id"]
    settings_service.update_settings({"writing": {"chapter_min_words": 1000, "chapter_max_words": 9000}})
    path = project_service.get_project_dir(pid)[1] / ".meta/writing-prefs.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(bad, encoding="utf-8")

    assert writing_preference_service.get_writing_prefs(pid) == {
        "chapter_min_words": 2000, "chapter_max_words": 4000, "source": "book"}
    assert path.read_text(encoding="utf-8") == bad


@pytest.mark.parametrize("kind", ["is_symlink", "is_junction"])
def test_link_book_range_is_refused(workspace, monkeypatch, kind):
    pid = workspace["id"]
    _, root = project_service.get_project_dir(pid)
    path_type = type(root)
    original = getattr(path_type, kind, lambda _path: False)
    monkeypatch.setattr(path_type, kind, lambda path: path == root or original(path), raising=False)

    with pytest.raises(InvalidOperationError):
        writing_preference_service.get_writing_prefs(pid)
    with pytest.raises(InvalidOperationError):
        writing_preference_service.update_writing_prefs(
            pid, chapter_min_words=2000, chapter_max_words=3000)


def test_writing_prefs_api(workspace):
    from fastapi.testclient import TestClient
    from workbench.backend.app import create_app

    pid = workspace["id"]
    path = f"/api/projects/{pid}/writing-prefs"
    with TestClient(create_app()) as client:
        initial = client.get(path)
        assert initial.status_code == 200
        assert initial.json() == {"chapter_min_words": 2000, "chapter_max_words": 4000,
                                  "source": "global"}
        saved = client.put(path, json={"chapter_min_words": 2200, "chapter_max_words": 3600})
        assert saved.status_code == 200
        assert saved.json() == {"chapter_min_words": 2200, "chapter_max_words": 3600,
                                "source": "book"}
        assert client.get(path).json()["source"] == "book"
        assert client.put(path, json={"chapter_min_words": 4000, "chapter_max_words": 3000}).status_code == 400
        assert client.put(path, json={"chapter_min_words": 100, "chapter_max_words": 50001}).status_code == 400


# ─────────────────────────── 字数门区间行为 ───────────────────────────


def test_word_gate_lower_bound_blocks_and_upper_bound_only_warns():
    below = review_service.run_hard_gates(CLEAN_BODY, min_words=99999, max_words=100000)
    assert below["passed"] is False
    assert "字数门" in below["blocking_gates"]

    over = review_service.run_hard_gates(CLEAN_BODY, min_words=1, max_words=1)
    assert over["passed"] is True                       # 上限只告警，不阻断
    assert "字数上限" not in over["blocking_gates"]
    upper = next(gate for gate in over["gates"] if gate["key"] == "字数上限")
    assert upper["passed"] is False and upper["blocking"] is False
    assert upper["detail"] == f"正文 {over['words']} 汉字（上限 1 汉字）"

    within = review_service.run_hard_gates(CLEAN_BODY, min_words=2000, max_words=4000)
    assert within["passed"] is True
    assert next(g for g in within["gates"] if g["key"] == "字数上限")["passed"] is True


def test_gate_structure_and_other_gates_unaffected_by_range():
    result = review_service.run_hard_gates(CLEAN_BODY + "\n# 第12章 起风\n",
                                           min_words=1, max_words=99999)
    assert result["passed"] is False
    assert "格式门" in result["blocking_gates"]
    assert result["gates"][0]["key"] == "字数门"
    assert [gate["key"] for gate in result["gates"]] == [
        "字数门", "语言门", "禁词门", "去AI味门", "记号泄漏", "格式门", "字数上限"]
    assert result["locations"] and result["blocking_gates"]

    # 不传上限时（既有调用方）不新增告警项，passed 语义不变
    no_upper = review_service.run_hard_gates(CLEAN_BODY)
    assert "字数上限" not in [gate["key"] for gate in no_upper["gates"]]


def test_chapter_write_uses_book_range(workspace):
    """调用侧传入本书区间：下限提高后同一正文被判失败。"""
    from workbench.backend.services import file_change_service as changes

    pid = workspace["id"]
    writing_preference_service.update_writing_prefs(pid, chapter_min_words=2000, chapter_max_words=4000)
    ok = changes.preview_change(pid, operation="create", rel_path="章节/第0001章.txt",
                                content=CLEAN_BODY, title="开端")
    assert ok["gates"]["passed"] is True

    writing_preference_service.update_writing_prefs(pid, chapter_min_words=49999, chapter_max_words=50000)
    strict = changes.preview_change(pid, operation="create", rel_path="章节/第0002章.txt",
                                    content=CLEAN_BODY, title="开端")
    assert strict["gates"]["passed"] is False
    assert "字数门" in strict["gates"]["blocking_gates"]


# ─────────────────────────── 生成注入 ───────────────────────────


def test_prepare_system_injects_book_word_target(workspace):
    session = chat_service.create_session(project_id=workspace["id"])
    run = runs.create_run(session["id"], text="写下一章正文", start=False)
    try:
        _session, system, _context, _info, _tools = runs._prepare(runs._row(run["id"]))
        assert "本书每章正文目标：2000–4000 汉字" in system
    finally:
        runs.cancel(run["id"])

    writing_preference_service.update_writing_prefs(
        workspace["id"], chapter_min_words=2500, chapter_max_words=3200)
    run2 = runs.create_run(session["id"], text="写下一章正文", start=False)
    try:
        _session, system, _context, _info, _tools = runs._prepare(runs._row(run2["id"]))
        assert "本书每章正文目标：2500–3200 汉字" in system
    finally:
        runs.cancel(run2["id"])

    other = project_service.create_project(name="另一本书")
    other_session = chat_service.create_session(project_id=other["id"])
    run3 = runs.create_run(other_session["id"], text="写下一章正文", start=False)
    try:
        _session, system, _context, _info, _tools = runs._prepare(runs._row(run3["id"]))
        assert "本书每章正文目标：2000–4000 汉字" in system
    finally:
        runs.cancel(run3["id"])


# ─────────────────────────── 合同预算中值 / 导出对齐 ───────────────────────────


def test_contract_word_budget_defaults_to_range_midpoint(workspace):
    pid = workspace["id"]
    assert contract_service.default_word_budget(pid) == 3000                  # 默认 2000–4000
    writing_preference_service.update_writing_prefs(pid, chapter_min_words=2001, chapter_max_words=3500)
    assert contract_service.default_word_budget(pid) == 2751                  # 四舍五入取整

    chapter = chapter_service.create_chapter(pid, "开端")
    contract = contract_service.generate_contract(pid, chapter["rel_path"], use_ai=False)
    assert contract["word_budget"] == 2751


def test_publish_export_prefers_book_range(workspace):
    pid = workspace["id"]
    writing_preference_service.update_writing_prefs(pid, chapter_min_words=2000, chapter_max_words=3500)
    chapter = chapter_service.create_chapter(pid, "开端")
    chapter_service.save_chapter(pid, chapter["rel_path"], CLEAN_BODY, status="完成")

    result = publish_service.build_publish_export(pid, platform="番茄小说")
    assert result["stats"]["word_range"] == [2000, 3500]

    other = project_service.create_project(name="无覆盖书")
    other_chapter = chapter_service.create_chapter(other["id"], "开端")
    chapter_service.save_chapter(other["id"], other_chapter["rel_path"], CLEAN_BODY, status="完成")
    fallback = publish_service.build_publish_export(other["id"], platform="番茄小说")
    assert fallback["stats"]["word_range"] == [1500, 3000]   # 回落档案默认值