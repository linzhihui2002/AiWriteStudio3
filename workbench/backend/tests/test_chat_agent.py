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


def _write_skill(workspace: SimpleNamespace, name: str, body: str) -> str:
    directory = workspace.skills / name
    directory.mkdir()
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {name} 说明\n---\n\n{body}\n", encoding="utf-8")
    return body


def test_skill_digest_prioritizes_always_inject(workspace: SimpleNamespace) -> None:
    """优先技能完整加载；旧预算参数不再裁剪其余技能或技能尾部。"""
    from workbench.backend.services import skill_service
    from workbench.backend.engine.runtime import estimate_tokens

    body = _write_skill(workspace, "human-linguistics", "去AI味正文" * 20)
    _write_skill(workspace, "novel-setting", "设定正文" * 40)
    must_block = f"### 技能 human-linguistics（human-linguistics 说明）\n{body}"

    digest = skill_service.skill_digest(["novel-setting", "human-linguistics"],
                                        budget_chars=len(must_block) + 5,
                                        priority_names=["human-linguistics"])
    assert must_block in digest                       # 必注入技能正文完整出现
    assert "超预算" not in digest
    assert "设定正文" * 40 in digest
    assert digest.index("### 技能 human-linguistics") < digest.index("### 技能 novel-setting")

    # 不做优先级时保持原顺序，所有正文仍完整保留。
    plain = skill_service.skill_digest(["novel-setting", "human-linguistics"],
                                       budget_chars=len(must_block) + 5)
    assert body in plain
    assert plain.index("### 技能 novel-setting") < plain.index("### 技能 human-linguistics")

    # 即使兼容调用显式传入极小预算，也完整保留。
    cramped = skill_service.skill_digest(["human-linguistics"], budget_chars=20,
                                         priority_names=["human-linguistics"])
    assert cramped.startswith("### 技能")
    assert body in cramped and "截断" not in cramped
    compiled = skill_service.compile_skills(["novel-setting", "human-linguistics", "novel-setting"],
                                            priority_names=["human-linguistics"])
    assert [item["name"] for item in compiled["skills"]] == ["human-linguistics", "novel-setting"]
    assert compiled["skills"][0]["estimated_tokens"] == estimate_tokens(must_block)
    assert compiled["estimated_tokens"] == estimate_tokens(digest)


def test_skill_reference_remains_complete_in_both_tool_paths(workspace: SimpleNamespace) -> None:
    from workbench.backend.engine.runtime import estimate_tokens
    from workbench.backend.services import skill_service, chat_workspace_tools

    refs = workspace.skills / "novel-review" / "references"
    refs.mkdir()
    content = "正文条目。\n" * 6000 + "引用尾部验证。"
    (refs / "long.md").write_text(content, encoding="utf-8")
    reference = skill_service.read_reference("novel-review", "long.md")
    assert reference["content"] == content
    assert reference["truncated"] is False
    assert reference["estimated_tokens"] == estimate_tokens(content)
    args = {"skill": "novel-review", "path": "references/long.md"}
    legacy = chat_tools.tool_read_skill_reference(args, ToolContext(project_id=1, session_id=1))
    assert legacy["text"] == content and legacy["estimated_tokens"] == estimate_tokens(content)
    native = chat_workspace_tools._read_skill_reference(args)
    assert native["data"]["content"] == content
    assert "截断" not in native["summary"]
    assert json.dumps(native, ensure_ascii=False).count("引用尾部验证。") == 1


def test_skill_compilation_reports_unavailable_without_false_loading(workspace: SimpleNamespace) -> None:
    from workbench.backend.services import skill_service

    invalid = workspace.skills / "invalid-skill"
    invalid.mkdir()
    (invalid / "SKILL.md").write_text("---\nname: invalid-skill\n---\n正文", encoding="utf-8")
    skill_service.sync_skills()
    skill_service.set_enabled("novel-review", False)
    compiled = skill_service.compile_skills(["invalid-skill", "missing-skill", "novel-review"])
    assert compiled["text"] == "" and compiled["skills"] == []
    assert compiled["estimated_tokens"] == 0
    assert {item["name"] for item in compiled["unavailable"]} == {
        "invalid-skill", "missing-skill", "novel-review"}


def test_skill_compilation_only_reads_requested_bodies(workspace: SimpleNamespace, monkeypatch) -> None:
    from workbench.backend.services import skill_service

    read_paths = []
    original = skill_service.read_text

    def read_body(path):
        read_paths.append(Path(path))
        assert Path(path).name == "SKILL.md", "引用应在模型按需读取时加载"
        return original(path)

    monkeypatch.setattr(skill_service, "read_text", read_body)
    compiled = skill_service.compile_skills(["novel-review", "novel-review"])
    assert compiled["skills"][0]["name"] == "novel-review"
    assert read_paths == [workspace.skills / "novel-review/SKILL.md"]


def test_workspace_list_files_shows_chapter_txt(workspace: SimpleNamespace) -> None:
    """章节改为 .txt 后仍能被对话工具列出与搜索，且不越界。"""
    from workbench.backend.services import chat_workspace_tools

    project, rel, project_dir = make_project("txt 可见书")
    assert rel == "章节/第0001章.txt"
    # 越界 txt（非章节目录）与隐藏文件不得出现在结果里
    (project_dir / "设定").mkdir(parents=True, exist_ok=True)
    (project_dir / "设定" / "杂记.txt").write_text("不应出现。", encoding="utf-8")
    (project_dir / "章节" / ".隐藏.txt").write_text("不应出现。", encoding="utf-8")
    ctx = {"project_id": project["id"], "session_id": 1, "run_id": "run-one",
           "tool_call_id": "call-one", "read_hashes": {}}

    listing = chat_workspace_tools.execute("list_files", {}, ctx)
    assert listing["ok"] and rel in listing["data"]["files"]
    assert "设定/杂记.txt" not in listing["data"]["files"]
    assert "章节/.隐藏.txt" not in listing["data"]["files"]

    found = chat_workspace_tools.execute("search_files", {"query": "码头上的风"}, ctx)
    assert found["ok"] and any(hit["path"] == rel for hit in found["data"]["matches"])


# ───────────────────── 工具可见性：只按 Agent 白名单与只读过滤 ─────────────────────


def test_list_specs_filters_by_agent_tools_whitelist(workspace: SimpleNamespace) -> None:
    from workbench.backend.services import chat_workspace_tools

    reviewer = chat_workspace_tools.list_specs(agent_tools=["检索", "审稿门禁"])
    names = {spec["name"] for spec in reviewer}
    assert not any(spec["write"] for spec in reviewer)          # 审稿类 Agent 没有写工具
    assert {"list_files", "read_file", "search_files", "run_gates", "check_tracking"} <= names
    assert "ask_user_question" in names                          # 提问工具始终保留
    assert not {"create_file", "write_file", "replace_text", "delete_file"} & names

    writer = {spec["name"] for spec in
              chat_workspace_tools.list_specs(agent_tools=["检索", "文件读写", "收件箱"])}
    assert {"create_file", "new_chapter", "replace_text", "write_file",
            "move_file", "delete_file"} <= writer
    assert "ask_user_question" in writer
    # 写工具不因「不在本轮写域内」而消失（否则模型无法发起越界申请）
    assert "create_file" in {spec["name"] for spec in
                             chat_workspace_tools.list_specs(agent_tools=["检索", "文件读写"])}
    # 只读轮次仍然过滤写工具；无白名单（旧调用）时保持全量
    assert all(not spec["write"] for spec in
               chat_workspace_tools.list_specs(read_only=True, agent_tools=["检索", "文件读写"]))
    assert len(chat_workspace_tools.list_specs()) == len(chat_workspace_tools.TOOLS)
    assert [spec["name"] for spec in chat_workspace_tools.list_specs(agent_tools=["无"])] == [
        "ask_user_question", "read_skill_reference"]


def test_list_specs_exposes_readonly_teardown_recall(workspace: SimpleNamespace) -> None:
    """拆书召回工具只读、归「检索」分组：检索类与审稿类 Agent 可见且都不带写工具。"""
    from workbench.backend.services import chat_workspace_tools

    retrieval = chat_workspace_tools.list_specs(agent_tools=["检索"])
    assert "list_teardown" in {spec["name"] for spec in retrieval}
    assert not any(spec["write"] for spec in retrieval)

    reviewer = chat_workspace_tools.list_specs(agent_tools=["检索", "审稿门禁"])
    assert "list_teardown" in {spec["name"] for spec in reviewer}
    assert not any(spec["write"] for spec in reviewer)


# ───────────────────── 旧提案路径：写域判定与提案标注 ─────────────────────


def test_chat_tools_scope_gates_direct_apply_and_annotates_proposals(workspace: SimpleNamespace) -> None:
    from workbench.backend.services import scope_guard, settings_service

    project, _rel, project_dir = make_project("写域旧路径书")
    scope = scope_guard.build_scope(project_id=project["id"], write_targets=["设定"])
    out_scope = {"which": "大纲", "content": "新大纲。"}

    # 范围外 + 直接落盘档：不走自动落盘，降级为收件箱提案并标注越界
    ctx = ToolContext(project_id=project["id"], session_id=3, auto_apply=True,
                      scope=scope, scope_key="run-out")
    result = chat_tools.execute("propose_outline_edit", out_scope, ctx)
    assert result["ok"] is True and result["applied"] is False
    assert result["scope"]["out_of_scope"] is True
    proposal = proposal_service.get_proposal(ctx.proposals[0])
    assert proposal["meta"]["scope"]["out_of_scope"] is True
    assert proposal["status"] == "pending"        # 越界时不自动落盘，等作者在收件箱确认

    # 范围内 + 直接落盘档：保持既有语义（直接落盘）
    ctx_in = ToolContext(project_id=project["id"], session_id=4, auto_apply=True,
                         scope=scope, scope_key="run-in")
    applied = chat_tools.execute("propose_setting_edit",
                                 {"category": "人物", "content": "新的设定内容。"}, ctx_in)
    assert applied["ok"] is True and applied["applied"] is True

    # 默认（只产提案）时提案 meta 也带 scope 说明
    ctx_prop = ToolContext(project_id=project["id"], session_id=6, auto_apply=False, scope=scope)
    drafted = chat_tools.execute("propose_outline_edit", out_scope, ctx_prop)
    assert drafted["ok"] is True and drafted["applied"] is False
    assert proposal_service.get_proposal(ctx_prop.proposals[0])["meta"]["scope"]["material"] == "大纲"

    # 严格模式：范围外直接拒绝，连提案都不生成
    settings_service.update_settings({"chat": {"scope_strictness": "reject"}})
    ctx_strict = ToolContext(project_id=project["id"], session_id=5, auto_apply=True,
                             scope=scope, scope_key="run-strict")
    denied = chat_tools.execute("propose_outline_edit", out_scope, ctx_strict)
    assert denied["ok"] is False and denied["code"] == "out_of_scope"
    assert "越界" in denied["summary"]
    assert ctx_strict.proposals == []


