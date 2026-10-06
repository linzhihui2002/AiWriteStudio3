"""写域守卫单测：build_scope / evaluate / grant 的边界，以及写域设置项。

原生对话路径（批准卡、档位）的验收见 ``test_chat_interactions.py``，
旧提案路径（chat_tools）见 ``test_chat_agent.py``。
"""
from __future__ import annotations

import json

import pytest

from workbench.backend import config, db
from workbench.backend.services import (
    chat_preference_service, project_service, scope_guard, settings_service,
)
from workbench.backend.services.errors import InvalidOperationError


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "books")
    monkeypatch.setattr(config, "RUNTIME_DIR", tmp_path / ".workbench")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / ".workbench/workbench.db")
    db.init_db()
    config.ensure_runtime_dirs()
    return tmp_path


# ─────────────────────────── build_scope ───────────────────────────


def test_undeclared_scope_rejects_any_material() -> None:
    scope = scope_guard.build_scope(project_id=None, write_targets=[], target_chapter="")
    assert scope == {"dirs": [], "files": [], "declared": False, "label": ""}
    decision = scope_guard.evaluate(scope, "设定/世界设定.md")
    assert decision["allowed"] is False
    assert decision["material"] == "设定"
    assert decision["reason"] == "本轮未声明产出材料"
    assert scope_guard.evaluate(None, "设定/世界设定.md")["allowed"] is False
    assert scope_guard.describe(None) == "本轮未声明可写材料"


def test_directory_and_exact_file_hits() -> None:
    scope = scope_guard.build_scope(project_id=None, write_targets=["设定", "大纲/大纲.md"])
    assert scope["dirs"] == ["设定"]
    assert scope["files"] == ["大纲/大纲.md"]
    assert scope["declared"] is True
    assert scope["label"] == "设定/、大纲/大纲.md"
    assert scope_guard.describe(scope) == "本轮范围：设定/、大纲/大纲.md"

    # 目录命中：该目录下任意文件都在写域内
    assert scope_guard.evaluate(scope, "设定/人物设定.md")["allowed"] is True
    # 精确文件命中：只允许该文件，不放开整个顶层目录
    assert scope_guard.evaluate(scope, "大纲/大纲.md")["allowed"] is True
    denied = scope_guard.evaluate(scope, "大纲/章纲.md")
    assert denied["allowed"] is False and denied["material"] == "大纲"
    assert denied["reason"] == "本轮范围是 设定/、大纲/大纲.md，不含 大纲/"


def test_illegal_items_are_dropped(tmp_path, monkeypatch) -> None:
    root = tmp_path / "book"
    (root / "设定").mkdir(parents=True)
    monkeypatch.setattr(project_service, "get_project_dir", lambda project_id: ({}, root))
    scope = scope_guard.build_scope(
        project_id=1,
        write_targets=["设定/../秘密.md", "../外部.md", "/etc/passwd", "C:/x.md",
                       ".meta/私记.md", "设定/人物设定.md", "大纲", "设定/人物设定.md"],
        target_chapter="章节/第0001章.txt",
    )
    assert scope["dirs"] == ["大纲"]
    assert scope["files"] == ["设定/人物设定.md", "章节/第0001章.txt"]
    assert scope["declared"] is True

    # 全部非法 → 未声明
    empty = scope_guard.build_scope(project_id=1, write_targets=["..", "/绝对.md", "无斜杠.md"])
    assert empty == {"dirs": [], "files": [], "declared": False, "label": ""}


def test_scope_key_scopes_grants_per_run() -> None:
    scope = scope_guard.build_scope(project_id=None, write_targets=["设定"])
    assert scope_guard.evaluate(scope, "大纲/大纲.md")["allowed"] is False
    scope_guard.grant("run-a", "大纲")
    assert scope_guard.grants("run-a") == {"大纲"}
    # 授权按 scope_key 生效：未授权的 run 仍被拒
    assert scope_guard.evaluate(scope, "大纲/大纲.md")["allowed"] is False
    keyed = {**scope, "key": "run-a"}
    granted = scope_guard.evaluate(keyed, "大纲/大纲.md")
    assert granted["allowed"] is True and granted["granted"] is True
    assert "已获本任务越界授权" in granted["reason"]
    assert scope_guard.grants("run-b") == set()


def test_memo_is_always_writable_free_note_zone() -> None:
    """备忘录是自由便签区：写域只含 设定 时它照写；其它材料仍然受限。"""
    scope = scope_guard.build_scope(project_id=None, write_targets=["设定"])
    memo = scope_guard.evaluate(scope, "备忘录/临时想法.md")
    assert memo["allowed"] is True
    assert memo["material"] == "备忘录"
    assert memo["reason"] == "备忘录是自由便签区，不受本轮写域限制"
    denied = scope_guard.evaluate(scope, "大纲/大纲.md")
    assert denied["allowed"] is False and denied["material"] == "大纲"


# ─────────────────────────── 设置项 ───────────────────────────


def test_scope_prefs_defaults_roundtrip_and_bad_values(workspace) -> None:
    assert chat_preference_service.get_scope_prefs() == {
        "scope_strictness": "ask", "scope_limit_full": False}
    updated = settings_service.update_settings(
        {"chat": {"scope_strictness": "reject", "scope_limit_full": True}})
    assert updated["chat"]["scope_strictness"] == "reject"
    assert settings_service.read_settings()["chat"]["scope_limit_full"] is True
    assert chat_preference_service.get_scope_prefs() == {
        "scope_strictness": "reject", "scope_limit_full": True}
    # 其余对话权限项不受影响
    assert updated["chat"]["permission_mode"] == "auto"

    before = settings_service.read_settings()
    with pytest.raises(InvalidOperationError):
        settings_service.update_settings({"chat": {"scope_strictness": "loose"}})
    with pytest.raises(InvalidOperationError):
        settings_service.update_settings({"chat": {"scope_limit_full": "yes"}})
    assert settings_service.read_settings() == before

    # 手工改坏 → 读取时回落默认值，绝不抛错
    settings_service.settings_file().write_text(json.dumps({
        "chat": {"permission_mode": "auto", "discussion_only": False,
                 "scope_strictness": 7, "scope_limit_full": "yes"}}), encoding="utf-8")
    assert chat_preference_service.get_scope_prefs() == {
        "scope_strictness": "ask", "scope_limit_full": False}


def test_settings_api_carries_scope_prefs(workspace) -> None:
    from fastapi.testclient import TestClient

    from workbench.backend.app import create_app

    with TestClient(create_app()) as client:
        initial = client.get("/api/settings")
        assert initial.status_code == 200
        assert initial.json()["chat"]["scope_strictness"] == "ask"
        assert initial.json()["chat"]["scope_limit_full"] is False
        put = client.put("/api/settings", json={"patch": {"chat": {
            "scope_strictness": "reject", "scope_limit_full": True}}})
        assert put.status_code == 200, put.text
        assert put.json()["chat"]["scope_strictness"] == "reject"
        assert client.get("/api/settings").json()["chat"]["scope_limit_full"] is True
        invalid = client.put("/api/settings", json={"patch": {"chat": {"scope_strictness": "loose"}}})
        assert invalid.status_code >= 400
