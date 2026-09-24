"""旧提案工具和技能资产兼容性；原生对话验收见 test_chat_runs / test_dsh_chat。"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from workbench.backend import config, db
from workbench.backend.services import (
    chapter_service,
    chat_service,
    chat_tools,
    generation_service,
    project_service,
    prompt_registry_service,
    proposal_service,
    provider_service,
)
from workbench.backend.services.chat_tools import ToolContext

# 干净长正文（含对话、无禁词），用于章节与门禁用例
CLEAN_BODY = (
    "码头上的风带着咸味。老周把缆绳在桩子上绕了两圈，手背上的旧伤被磨得发亮。\n"
    "“今夜潮水涨得早。”他抬头看了一眼天。\n"
    "沈砚没答话，他从怀里摸出半块干饼，掰了一角递过去。"
    "“你上次说，北边那批货晚三天到。”\n"
    "“晚三天。”老周接了饼，没吃，捏在手里，“船老大的儿子病了，"
    "耽误了两日。第三日是逆风。”\n"
) * 10

# 命中禁词（density_gate.DEFAULT_FORBIDDEN 含「缓缓」）→ 门禁必然不通过
BAD_BODY = "他缓缓抬头。缓缓放下杯子。缓缓转身。"


@pytest.fixture()
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    projects = tmp_path / "projects"
    runtime = tmp_path / ".workbench"
    skills = tmp_path / "skills"
    agents = tmp_path / "agents"
    rules = tmp_path / "rules"
    workflows = tmp_path / "workflows"
    for directory in (projects, runtime, skills, agents, rules, workflows):
        directory.mkdir()
    (skills / "novel-review").mkdir()
    (skills / "novel-review" / "SKILL.md").write_text(
        "---\nname: novel-review\ndescription: 章节审稿技能，逐项验收。\n---\n\n"
        "## 硬约束\n\n- 禁止在审稿报告里改写正文\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(config, "PROJECTS_DIR", projects)
    monkeypatch.setattr(config, "RUNTIME_DIR", runtime)
    monkeypatch.setattr(config, "DB_PATH", runtime / "workbench.db")
    db.init_db()
    config.ensure_runtime_dirs()
    prompt_registry_service.ensure_registry()

    from workbench.backend.engine import router as engine_router

    monkeypatch.setattr(engine_router.get_engine("dsh-headless"), "available", lambda: False)
    provider_service.upsert_provider("fake", base_url="https://fake.local/v1",
                                     models=[{"id": "m"}], api_key="sk-test-abcdefgh",
                                     enabled=True)
    return SimpleNamespace(projects=projects, runtime=runtime, root=tmp_path, skills=skills)


class _FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = "{}"

    def json(self) -> dict:
        return self._payload


def install_sequence(monkeypatch: pytest.MonkeyPatch, texts: list[str]) -> list[str]:
    """假模型：按顺序返回 ``texts``；队列用尽后返回空回复（不触发重试）。"""
    queue = list(texts)

    def _post(*_args, **_kwargs) -> _FakeResponse:
        text = queue.pop(0) if queue else ""
        return _FakeResponse(
            {"choices": [{"message": {"content": text}}],
             "usage": {"prompt_tokens": 5, "completion_tokens": 9}}
        )

    monkeypatch.setattr("workbench.backend.engine.direct_api.httpx.post", _post)
    return queue


def tool_call(name: str, args: dict) -> str:
    return f"<tool_call>{json.dumps({'tool': name, 'args': args}, ensure_ascii=False)}</tool_call>"


def make_project(name: str) -> tuple[dict, str, Path]:
    """建项目 + 一章正文 + 一份设定文件，返回 (project, 章节 rel, 项目目录)。"""
    project = project_service.create_project(name=name)
    _row, project_dir = project_service.get_project_dir(project["id"])
    chapter = chapter_service.create_chapter(project["id"], title="开端")
    rel = chapter["rel_path"]
    chapter_service.save_chapter(project["id"], rel, CLEAN_BODY, status="草稿")
    setting = project_dir / "设定" / "人物设定.md"
    setting.parent.mkdir(parents=True, exist_ok=True)
    setting.write_text("# 人物\n\n- 名字：沈砚\n- 身份：跑货的\n", encoding="utf-8")
    return project, rel, project_dir


# ─────────────────────────── 工具表 ───────────────────────────


def test_read_tools_and_path_guard(workspace: SimpleNamespace) -> None:
    project, rel, _project_dir = make_project("工具书")
    ctx = ToolContext(project_id=project["id"], chapter_rel=rel, session_id=1)

    listing = chat_tools.execute("list_chapters", {}, ctx)
    assert listing["ok"] is True and rel in listing["text"]

    detail = chat_tools.execute("read_chapter", {"rel_path": rel}, ctx)
    assert detail["ok"] is True and "字数" in detail["text"]

    setting = chat_tools.execute("read_setting", {"category": "人物"}, ctx)
    assert setting["ok"] is True and "沈砚" in setting["text"]

    gates = chat_tools.execute("run_gates", {"text": BAD_BODY}, ctx)
    assert gates["ok"] is True and "未通过" in gates["summary"]
    clean_gates = chat_tools.execute("run_gates", {"rel_path": rel}, ctx)
    assert clean_gates["ok"] is True

    # 读工具越界
    escaped = chat_tools.execute("read_chapter", {"rel_path": "../../secret.md"}, ctx)
    assert escaped["ok"] is False and "失败" in escaped["summary"]

    # 未知工具 / 坏 JSON
    assert chat_tools.execute("no_such_tool", {}, ctx)["ok"] is False
    from workbench.backend.services import agent_loop

    assert agent_loop.parse_tool_call("<tool_call>{oops}</tool_call>") == (
        agent_loop.INVALID_TOOL_NAME, {})


def test_write_tools_create_proposals_only(workspace: SimpleNamespace) -> None:
    project, rel, _project_dir = make_project("入箱书")
    ctx = ToolContext(project_id=project["id"], chapter_rel=rel, session_id=2,
                      auto_apply=False)

    created = chat_tools.execute(
        "propose_chapter_edit",
        {"rel_path": rel, "content": CLEAN_BODY + "\n他数了数手里的铜钱。\n"},
        ctx,
    )
    assert created["ok"] is True and created["applied"] is False
    assert ctx.proposals, "写工具必须登记提案"

    proposal = proposal_service.get_proposal(ctx.proposals[0])
    assert proposal["status"] == "pending"
    assert proposal["diff"], "应用前必须有 diff"
    assert proposal["target_path"] == rel

    # 越界写路径被拒
    denied = chat_tools.execute("propose_chapter_edit", {"rel_path": "../../x.md",
                                                        "content": CLEAN_BODY},
                                ToolContext(project_id=project["id"], session_id=2))
    assert denied["ok"] is False
    denied_dir = chat_tools.execute("propose_outline_edit",
                                    {"which": "大纲", "content": ""},
                                    ToolContext(project_id=project["id"], session_id=2))
    assert denied_dir["ok"] is False  # content 为空


def test_skill_digest_skips_missing_and_disabled(workspace: SimpleNamespace) -> None:
    from workbench.backend.services import skill_service

    digest = skill_service.skill_digest(["novel-review", "not-exist"])
    assert "novel-review" in digest and "not-exist" not in digest

    skill_service.sync_skills()          # 先注册到 skills 表，set_enabled 才有行可更新
    skill_service.set_enabled("novel-review", False)
    assert skill_service.skill_digest(["novel-review"]) == ""


