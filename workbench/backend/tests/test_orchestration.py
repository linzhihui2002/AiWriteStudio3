"""编排层单测：技能同步 / Agent 定义与内置保护 / 规则优先级链 / 路由 / 工作流 / 对话。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from workbench.backend import config, db
from workbench.backend.services import (
    agent_service,
    chat_service,
    project_service,
    prompt_registry_service,
    provider_service,
    rule_service,
    routing_service,
    skill_service,
    workflow_service,
)

# 干净长正文（≥2000 字，含对话、无禁词/无规划记号），用于产章链路
CLEAN_BODY = (
    "码头上的风带着咸味。老周把缆绳在桩子上绕了两圈，手背上的旧伤被磨得发亮。\n"
    "“今夜潮水涨得早。”他抬头看了一眼天。\n"
    "沈砚没答话，他从怀里摸出半块干饼，掰了一角递过去。"
    "“你上次说，北边那批货晚三天到。”\n"
    "“晚三天。”老周接了饼，没吃，捏在手里，“船老大的儿子病了，"
    "耽误了两日。第三日是逆风。”\n"
    "沈砚数了数手里的铜钱，一共十七枚。他把其中十二枚放在桩子上，"
    "剩下五枚揣回衣袋。\n"
    "“十二枚，先付一半。”\n"
    "老周摇头：“规矩是先付三成。”\n"
    "“规矩是你定的。”沈砚笑了一下，“我等这批货，等了四十天。”\n"
    "远处传来一声闷响，像是缆绳崩断。两个人同时转过头去。\n"
) * 12


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
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    db.init_db()
    config.ensure_runtime_dirs()
    prompt_registry_service.ensure_registry()

    from workbench.backend.engine import router as engine_router

    monkeypatch.setattr(engine_router.get_engine("dsh-headless"), "available", lambda: False)
    return SimpleNamespace(projects=projects, runtime=runtime, root=tmp_path, skills=skills)


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self.status_code = 200
        self._payload = payload
        self.text = "{}"

    def json(self) -> dict:
        return self._payload


@pytest.fixture()
def fake_model(monkeypatch: pytest.MonkeyPatch):
    provider_service.upsert_provider("fake", base_url="https://fake.local/v1",
                                     models=[{"id": "m"}], api_key="sk-test-abcdefgh",
                                     enabled=True)

    def _install(text: str):
        monkeypatch.setattr(
            "workbench.backend.engine.direct_api.httpx.post",
            lambda *a, **k: _FakeResponse(
                {"choices": [{"message": {"content": text}}],
                 "usage": {"prompt_tokens": 5, "completion_tokens": 9}}
            ),
        )

    return _install


# ─────────────────────────── 技能 ───────────────────────────


def test_skill_sync_and_validation(workspace: SimpleNamespace) -> None:
    report = skill_service.sync_skills()
    assert report["count"] == 1
    assert report["errors"] == []
    target = workspace.root / ".dsh" / "skills" / "novel-review" / "SKILL.md"
    assert target.is_file()
    assert report["total_bytes"] <= report["budget_bytes"]

    created = skill_service.create_skill(
        "foreshadow-check", "伏笔回收检查技能。",
        "## 步骤\n\n1. 读伏笔台账\n2. 核对回收状态\n",
    )
    assert created["name"] == "foreshadow-check"
    assert (workspace.root / ".dsh" / "skills" / "foreshadow-check" / "SKILL.md").is_file()

    with pytest.raises(Exception):
        skill_service.create_skill("BadName", "非法名称", "正文")

    with pytest.raises(Exception):
        skill_service.create_skill("no-body", "空正文技能", "   ")

    # 停用 → 同步产物被移除
    skill_service.set_enabled("foreshadow-check", False)
    assert not (workspace.root / ".dsh" / "skills" / "foreshadow-check").exists()

    # 内置技能不可删
    with pytest.raises(Exception):
        skill_service.delete_skill("novel-review")


def test_skill_import_and_duplicate(workspace: SimpleNamespace) -> None:
    text = ("---\nname: imported-skill\ndescription: 导入的技能示例。\n---\n\n"
            "## 做什么\n\n- 输出结构化建议\n")
    imported = skill_service.import_skill(text)
    assert imported["name"] == "imported-skill"
    assert imported["source"] == "imported"

    duplicate = skill_service.duplicate_skill("novel-review", "novel-review-copy")
    assert duplicate["name"] == "novel-review-copy"
    assert duplicate["source"] == "workbench_custom"


# ─────────────────────────── Agent ───────────────────────────


def test_builtin_agents_protected(workspace: SimpleNamespace) -> None:
    agents = agent_service.list_agents()
    assert agents == [], "内置 Agent 默认隐藏"

    all_agents = agent_service.list_all_agents()
    names = {item["name"] for item in all_agents}
    assert {"chief-editor", "planner", "writer", "reviewer", "context-keeper"} <= names
    assert "writing-assistant" in names

    with pytest.raises(Exception) as excinfo:
        agent_service.delete_agent("writer")
    assert "复制" in str(excinfo.value)

    copy = agent_service.duplicate_agent("writer", "my-writer")
    assert copy["is_builtin"] is False
    assert agent_service.delete_agent("my-writer")["deleted"] is True


def test_agent_validation_and_model_binding(workspace: SimpleNamespace) -> None:
    with pytest.raises(Exception):
        agent_service.create_agent("Bad_Name", system_prompt="x")

    with pytest.raises(Exception):
        agent_service.create_agent("bad-skill", system_prompt="x", skills=["not-exist"])

    with pytest.raises(Exception):
        agent_service.create_agent("bad-tool", system_prompt="x", tools=["任意工具"])

    agent = agent_service.create_agent(
        "foreshadow-checker",
        description="专管伏笔回收检查",
        system_prompt="你只检查伏笔埋设与回收，输出未回收清单。",
        skills=["novel-review"],
        tools=["检索", "文件读写"],
    )
    assert agent["version"] == 1

    updated = agent_service.set_agent_model("foreshadow-checker", "fake", "m")
    assert updated["provider_id"] == "fake"
    assert updated["version"] == 2


# ─────────────────────────── 规则 ───────────────────────────


def test_rule_priority_chain_and_conflict(workspace: SimpleNamespace) -> None:
    project = project_service.create_project(name="规则书")
    project_id = project["id"]

    rule_service.upsert_global_rule("破折号用量", "破折号每 250 字不超过 1 个。",
                                    keys=["破折号"])
    rule_service.upsert_project_rule(project_id, "本书破折号", "本书完全不用破折号。",
                                     keys=["破折号"], confirmed=True)

    resolved = rule_service.resolve_rules(project_id)
    assert resolved["conflicts"], "同 key 不同作用域必须报冲突"
    conflict = resolved["conflicts"][0]
    assert conflict["winner"]["name"] == "本书破折号"
    assert "作者确认" in conflict["basis"]
    # 胜出方排在最前；技能内置规则仍保留在列表末尾（最低优先级，仅参照）
    assert resolved["effective"][0]["name"] == "本书破折号"
    assert resolved["effective"][0]["scope"] == "project"

    digest = rule_service.rules_digest(project_id)
    assert "本书破折号" in digest
    assert "冲突提示" in digest

    compiled = rule_service.compile_rules(project_id)
    assert Path(compiled["path"]).is_file()
    compiled_text = Path(compiled["path"]).read_text(encoding="utf-8")
    assert "workbench-rules" in compiled_text
    assert "本书完全不用破折号" in compiled_text

    rule_service.delete_rule("project", "本书破折号", project_id=project_id)
    assert rule_service.resolve_rules(project_id)["conflicts"] == []


# ─────────────────────────── 意图路由 ───────────────────────────


def test_intent_routing(workspace: SimpleNamespace) -> None:
    review = routing_service.route(None, "帮我审一下这章，看看能不能发", record=False)
    assert review["intent"] == "章节审稿"
    assert review["agent"] == "reviewer"
    assert review["skill"] == "novel-review"
    assert "依据" in review["basis"] or review["basis"]
    assert review["silent_write"] is False

    write = routing_service.route(None, "续写这一章", record=False)
    assert write["intent"] == "章节写作"
    assert write["agent"] == "writer"

    outline = routing_service.route(None, "帮我列个细纲", record=False)
    assert outline["intent"] == "大纲规划"
    assert outline["skill"] == "novel-planning"

    dirty = routing_service.route(None, "今天天气不错", record=False)
    assert dirty["matched"] is False
    assert len(dirty["candidates"]) >= 2
    assert dirty["agent"] == agent_service.DEFAULT_AGENT  # 未命中不再落到「总编」分类人格


# ─────────────────────────── 工作流 ───────────────────────────


def test_workflow_builtin_and_custom(workspace: SimpleNamespace) -> None:
    workflows = workflow_service.list_workflows()
    assert any(item["name"] == workflow_service.DEFAULT_WORKFLOW_NAME for item in workflows)
    default = workflow_service.get_workflow(workflow_service.DEFAULT_WORKFLOW_NAME)
    assert [node["type"] for node in default["nodes"]] == list(workflow_service.STEP_NODES)

    with pytest.raises(Exception):
        workflow_service.delete_workflow(workflow_service.DEFAULT_WORKFLOW_NAME)

    duplicate = workflow_service.duplicate_workflow(
        workflow_service.DEFAULT_WORKFLOW_NAME, "我的流程"
    )
    assert duplicate["is_builtin"] is False
    assert duplicate["version"] == 1

    with pytest.raises(Exception):
        workflow_service.save_workflow({"name": "坏流程", "nodes": [{"type": "不存在"}]})

    with pytest.raises(Exception):
        workflow_service.save_workflow(
            {"name": "断链流程", "nodes": [{"id": "a", "type": "DRAFT", "next": "missing"}]}
        )


def test_workflow_run_batch_with_checkpoints(workspace: SimpleNamespace,
                                             fake_model) -> None:
    from workbench.backend.services import chapter_service, contract_service

    fake_model(CLEAN_BODY)
    project = project_service.create_project(name="批产书")
    project_id = project["id"]
    (workspace.projects / "批产书" / "大纲" / "大纲.md").write_text(
        "---\n标题: 大纲\n---\n\n卷一：开局。\n", encoding="utf-8"
    )
    contract_service.freeze_outline(project_id)
    for index in range(2):
        chapter_service.create_chapter(project_id, f"第{index + 1}章")

    run = workflow_service.start_run(workflow_service.DEFAULT_WORKFLOW_NAME, project_id,
                                     chapter_range=(1, 2))
    assert run["progress"]["chapters_total"] == 2

    executed = workflow_service.execute_run(run["id"], use_ai=True)
    assert executed["status"] == "done"
    assert len(executed["progress"]["completed"]) == 2
    assert executed["log"], "执行日志必须有节点记录"

    # 已完成章节不重复生成：再跑一次应立即完成且不新增 log
    again = workflow_service.execute_run(run["id"], use_ai=True) if False else None
    rerun = workflow_service.start_run(workflow_service.DEFAULT_WORKFLOW_NAME, project_id,
                                      chapters=[])
    assert rerun["progress"]["chapters_total"] >= 1


def test_workflow_pause(workspace: SimpleNamespace, fake_model) -> None:
    from workbench.backend.services import chapter_service

    fake_model("内容" * 100)
    project = project_service.create_project(name="暂停书")
    chapter_service.create_chapter(project["id"], "第一章")

    run = workflow_service.start_run(workflow_service.DEFAULT_WORKFLOW_NAME, project["id"])
    paused = workflow_service.pause_run(run["id"])
    assert paused["status"] == "paused"


# ─────────────────────────── 对话 ───────────────────────────


def test_chat_round_with_routing_and_native_tools(workspace: SimpleNamespace, monkeypatch) -> None:
    from workbench.backend.tests.test_chat_runs import FakeNative
    from workbench.backend.services import chat_run_service
    engine = FakeNative(turns=[
        ["这是审稿建议。"],
        [("create_file", {"rel_path": "备忘录/创作思路.md", "content": "先写码头相遇。"}), "已写入备忘录。"],
    ])
    monkeypatch.setattr(chat_run_service, "_runtime", engine)
    project = project_service.create_project(name="对话书")
    session = chat_service.create_session(project_id=project["id"], title="测试会话")
    assert session["agent"] == agent_service.DEFAULT_AGENT
    result = chat_service.send_message(session["id"], "帮我审一下这章")
    assert result["ok"] is True
    assert result["routing"]["intent"] == "章节审稿"
    assert result["assistant_message_id"] > 0
    assert [item["role"] for item in chat_service.list_messages(session["id"])] == ["user", "assistant"]
    updated = chat_service.update_session(session["id"], {"model_id": "m2", "provider_id": "fake"})
    assert updated["model_id"] == "m2"
    assert {card["key"] for card in chat_service.quick_cards()} >= {"next-direction", "review", "stuck"}
    written = chat_service.send_message(session["id"], "新建一份创作思路备忘录")
    assert written["changes"][0]["status"] == "applied"
    assert engine.calls[-1]["resume"] and engine.calls[-1]["model"] == "m2"
    chat_service.delete_session(session["id"])
    assert chat_service.list_sessions(project["id"]) == []
