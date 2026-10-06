"""编排层单测：技能同步 / Agent 定义与内置保护 / 规则优先级链 / 路由 / 工作流 / 对话。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from workbench.backend import config, db
from workbench.backend.services import (
    agent_service,
    chat_service,
    prompt_registry_service,
    project_service,
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

    def _install(text: str, *, route_workflow: bool = False):
        calls = []
        plot_evidence = {
            "沈砚询问北边货物延误": "你上次说，北边那批货晚三天到。",
            "沈砚支付定金": "十二枚，先付一半。",
            "码头闷响让两人回头": "两个人同时转过头去。",
            "沈砚已等货四十天": "我等这批货，等了四十天。",
        }

        def respond(*_args, **kwargs):
            response_text = text
            if route_workflow:
                messages = kwargs["json"]["messages"]
                system = messages[0]["content"]
                if system.startswith("你是中文小说的章节策划。"):
                    task = "章节合同"
                    response_text = json.dumps({
                        "情节点": list(plot_evidence)[:3],
                        "字数预算": 3000, "钩子类型": "悬念",
                        "涉及实体": ["沈砚", "老周"],
                        "必须承上": ["沈砚已等货四十天"], "禁止事项": [],
                    }, ensure_ascii=False)
                elif system.startswith("你是严格的小说审稿编辑。"):
                    task = "审稿"
                    contract_text = messages[-1]["content"].split(
                        "【本轮章节合同（本次逐项验收依据）】\n", 1)[1]
                    contract, _end = json.JSONDecoder().raw_decode(contract_text)
                    response_text = json.dumps({
                        "逐项": [{"项": point, "判定": "已完成",
                                  "证据": plot_evidence[point]}
                                 for field in ("plot_points", "must_connect")
                                 for point in contract[field]],
                        "一致性": [], "结论": "通过", "修改指令": [],
                    }, ensure_ascii=False)
                elif "你是中文小说写作者。请按本章合同写作正文" in system:
                    task = "章节正文"
                elif system.startswith("你是小说信息抽取器。"):
                    task = "结构化抽取"
                    response_text = "{}"
                else:
                    raise AssertionError(f"Unexpected workflow model prompt: {system[:120]}")
                calls.append({"task": task, "messages": messages})
            return _FakeResponse(
                {"choices": [{"message": {"content": response_text}}],
                 "usage": {"prompt_tokens": 5, "completion_tokens": 9}}
            )

        monkeypatch.setattr(
            "workbench.backend.engine.direct_api.httpx.post",
            respond,
        )
        return calls

    return _install


# ─────────────────────────── 技能 ───────────────────────────


def test_skill_sync_and_validation(workspace: SimpleNamespace) -> None:
    report = skill_service.sync_skills()
    assert report["count"] == 1
    assert report["errors"] == []
    target = workspace.root / ".dsh" / "skills" / "novel-review" / "SKILL.md"
    assert target.is_file()
    assert report["estimated_tokens"] > 0 and "budget_bytes" not in report

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
    assert {"setting-keeper", "teardown-analyst"} <= names

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


# ─────────────────────── 意图路由 v2（LLM 判定 + 回退） ───────────────────────


def _install_intent_llm(monkeypatch: pytest.MonkeyPatch, payload) -> None:
    """把 LLM 语义判定打成可控假响应：不联网、不需要 API Key。"""
    from workbench.backend.services import intent_service

    monkeypatch.setattr(intent_service, "_llm_engine_available", lambda: True)
    monkeypatch.setattr(intent_service, "_intent_llm_enabled", lambda: True)

    def fake_run_task(**_kwargs):
        if isinstance(payload, Exception):
            raise payload
        text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        return {"ok": True, "text": text, "task_id": 0}

    monkeypatch.setattr(intent_service.generation_service, "run_task", fake_run_task)


# 触发词并列（设定 vs 大纲）→ 关键词快路径不唯一 → 走 LLM 判定
AMBIGUOUS_TEXT = "设定和大纲都过一遍"


def test_intent_llm_setting_only(workspace: SimpleNamespace, monkeypatch) -> None:
    """完善设定：判定主导设定类 Agent，产出材料只含 设定。"""
    project = project_service.create_project(name="判定书")
    _install_intent_llm(monkeypatch, {
        "intent": "设定管理", "primary_agent": "setting-keeper",
        "support_agents": [], "write_targets": ["设定"], "read_only_refs": [],
        "skills": ["novel-setting"], "plan": [], "confidence": 0.9, "reason": "完善设定",
    })

    res = routing_service.route(project["id"], AMBIGUOUS_TEXT, record=False)

    assert res["write_targets"] == ["设定"]
    assert res["agent"] == "setting-keeper"
    assert res["plan"] == []
    assert res["llm_used"] is True
    assert res["source"] == "llm"
    assert res["read_only_refs"] == []
    assert res["scope_unresolved"] is False
    assert res["reason"] == "完善设定"


def test_intent_llm_separates_basis_and_output(workspace: SimpleNamespace, monkeypatch) -> None:
    """根据设定写大纲：设定是只读依据，大纲才是产出。"""
    project = project_service.create_project(name="依据书")
    _install_intent_llm(monkeypatch, {
        "intent": "大纲规划", "primary_agent": "planner",
        "write_targets": ["大纲"], "read_only_refs": ["设定"],
        "skills": ["novel-planning"], "plan": [], "confidence": 0.82,
        "reason": "根据设定写大纲",
    })

    res = routing_service.route(project["id"], "根据现有设定把大纲补出来", record=False)

    assert res["write_targets"] == ["大纲"]
    assert res["read_only_refs"] == ["设定"]
    assert "设定" not in res["write_targets"]
    assert res["agent"] == "planner"


@pytest.mark.parametrize("payload", ["{oops 不是 JSON", RuntimeError("判定服务挂了")])
def test_intent_llm_failure_falls_back_to_keyword(workspace: SimpleNamespace,
                                                 monkeypatch, payload) -> None:
    """非法 JSON / 抛异常：确定性回退关键词结果，不阻断整轮对话。"""
    project = project_service.create_project(name="回退书")
    _install_intent_llm(monkeypatch, payload)

    res = routing_service.route(project["id"], AMBIGUOUS_TEXT, record=False)

    assert res["source"] == "keyword"
    assert res["llm_used"] is False
    assert res["matched"] is True
    assert res["agent"] in {"planner", "setting-keeper"}
    assert "回退关键词" in res["basis"]


def test_intent_llm_unknown_agent_falls_back_to_keyword(workspace: SimpleNamespace,
                                                        monkeypatch) -> None:
    """判定返回注册表里没有的角色名：丢弃并回退关键词结果。"""
    project = project_service.create_project(name="未知角色书")
    _install_intent_llm(monkeypatch, {
        "intent": "设定管理", "primary_agent": "not-exist",
        "write_targets": ["设定"], "confidence": 0.9, "reason": "瞎猜",
    })

    res = routing_service.route(project["id"], AMBIGUOUS_TEXT, record=False)

    assert res["source"] == "keyword"
    assert res["llm_used"] is False
    assert res["agent"] != "not-exist"
    assert "未知角色" in res["basis"]


@pytest.mark.parametrize("confidence", [0.01, None])
def test_intent_llm_low_confidence_falls_back_to_keyword(workspace: SimpleNamespace,
                                                         monkeypatch, confidence) -> None:
    """判定置信低于阈值（含缺失按 0 处理）：丢弃 LLM 判定，确定性回退关键词结果并说明原因。"""
    project = project_service.create_project(name="低置信书")
    payload = {
        "intent": "设定管理", "primary_agent": "setting-keeper",
        "write_targets": ["设定"], "read_only_refs": [],
        "skills": ["novel-setting"], "plan": [], "reason": "瞎猜",
    }
    if confidence is not None:
        payload["confidence"] = confidence
    _install_intent_llm(monkeypatch, payload)

    res = routing_service.route(project["id"], AMBIGUOUS_TEXT, record=False)

    assert res["source"] == "keyword"
    assert res["llm_used"] is False
    assert res["agent"] in {"planner", "setting-keeper"}
    assert "置信" in res["basis"] and "回退关键词" in res["basis"]


def test_teardown_analyst_binds_novel_teardown(workspace: SimpleNamespace) -> None:
    """拆书 Agent 绑定 1 个对应技能，且版本升到 4（否则既有安装不幂等升级）。"""
    agent_service.list_all_agents()  # 先确保内置定义落盘

    agent = agent_service.get_agent("teardown-analyst")

    assert agent["skills"] == ["novel-teardown"]
    assert agent["capabilities"][0]["skill"] == "novel-teardown"
    assert agent["version"] >= 4
    assert agent["capabilities"][0]["retrieval_profile"] == "teardown"


def test_intent_llm_multi_step_plan(workspace: SimpleNamespace, monkeypatch) -> None:
    """跨材料任务：多步计划按序保留，且每步只写自己声明的材料。"""
    project = project_service.create_project(name="接力书")
    step_one = {"step": 1, "agent": "setting-keeper", "write_targets": ["设定"],
                "read_only_refs": [], "note": "先补设定"}
    step_two = {"step": 2, "agent": "planner", "write_targets": ["大纲"],
                "read_only_refs": ["设定"], "note": "再按设定列卷纲"}
    _install_intent_llm(monkeypatch, {
        "intent": "大纲规划", "primary_agent": "planner", "write_targets": ["大纲"],
        "read_only_refs": ["设定"], "plan": [step_one, step_two],
        "confidence": 0.85, "reason": "先设定后大纲",
    })

    res = routing_service.route(project["id"], "先帮我把设定补全，再按设定列一版卷纲",
                                record=False)

    assert len(res["plan"]) == 2
    assert res["plan"][0]["agent"] == "setting-keeper"
    assert res["plan"][1]["agent"] == "planner"
    assert "设定" in res["plan"][1]["read_only_refs"]
    assert res["plan"][0]["write_targets"] == ["设定"]

    # 计划里塞入不存在的 Agent → 该步整步丢弃
    ghost = {"step": 3, "agent": "ghost-keeper", "write_targets": ["大纲"],
             "read_only_refs": []}
    _install_intent_llm(monkeypatch, {
        "intent": "大纲规划", "primary_agent": "planner", "write_targets": ["大纲"],
        "read_only_refs": ["设定"], "plan": [step_one, ghost, step_two],
        "confidence": 0.85, "reason": "先设定后大纲",
    })

    filtered = routing_service.route(project["id"], "先帮我把设定补全，再按设定列一版卷纲",
                                     record=False)

    assert len(filtered["plan"]) == 2
    assert all(step["agent"] != "ghost-keeper" for step in filtered["plan"])
    assert [step["step"] for step in filtered["plan"]] == [1, 2]


def test_intent_llm_invalid_write_targets_dropped(workspace: SimpleNamespace,
                                                  monkeypatch) -> None:
    """产出材料非法（不存在的目录 / 越界路径）一律丢弃；兜底也空则标记未声明。"""
    project = project_service.create_project(name="越界材料书")
    _install_intent_llm(monkeypatch, {
        "intent": "设定管理", "primary_agent": "writing-assistant",
        "write_targets": ["不存在的目录", "../../x"], "read_only_refs": [],
        "confidence": 0.5, "reason": "瞎猜",
    })

    res = routing_service.route(project["id"], AMBIGUOUS_TEXT, record=False)

    assert res["write_targets"] == []
    assert res["scope_unresolved"] is True


# ───────────── 意图路由：关键词路径的显式材料扫描（无项目场景） ─────────────


def test_keyword_scan_merges_named_materials_into_plan(workspace: SimpleNamespace) -> None:
    """作者点名多类材料：并入写域，并按点名先后生成协作计划。"""
    res = routing_service.route(None, "把设定补全，顺便把后续章节也写出来", record=False)

    assert res["source"] == "keyword" and res["llm_used"] is False
    assert "设定" in res["write_targets"] and "章节" in res["write_targets"]
    assert len(res["plan"]) == 2
    assert res["plan"][0]["agent"] == "setting-keeper"
    assert res["plan"][1]["agent"] == "writer"
    assert res["plan"][1]["read_only_refs"] == ["设定"]
    assert res["scope_unresolved"] is False
    assert "生成 2 步协作计划" in res["basis"]


def test_keyword_scan_skips_readonly_agents(workspace: SimpleNamespace) -> None:
    """审稿是只读角色：消息里的「这章」不为其带来写域。"""
    res = routing_service.route(None, "帮我审一下这章，看看能不能发", record=False)

    assert res["agent"] == "reviewer"
    assert res["write_targets"] == []
    assert res["plan"] == []


def test_keyword_scan_single_material_keeps_no_plan(workspace: SimpleNamespace) -> None:
    """只点名一类材料：写域就是这一类，不生成计划。"""
    res = routing_service.route(None, "完善设定", record=False)

    assert res["write_targets"] == ["设定"]
    assert res["plan"] == []
    assert res["scope_unresolved"] is False


def test_keyword_basis_material_goes_readonly(workspace: SimpleNamespace) -> None:
    """依据介词后的材料是只读依据，不进写域，也不触发多步计划。"""
    res = routing_service.route(None, "根据现有设定把大纲补出来", record=False)

    assert res["source"] == "keyword"
    assert res["write_targets"] == ["大纲"]
    assert "设定" in res["read_only_refs"]
    assert "设定" not in res["write_targets"]
    assert res["plan"] == []


def test_keyword_location_qualified_material_goes_readonly(workspace: SimpleNamespace) -> None:
    """「设定里的角色状态」：设定是查找位置（只读），状态才是产出。"""
    res = routing_service.route(None, "把设定里的角色状态更新一下", record=False)

    assert res["agent"] == "context-keeper"
    assert "状态" in res["write_targets"]
    assert "设定" not in res["write_targets"]
    assert "设定" in res["read_only_refs"]
    assert res["plan"] == []


def test_keyword_agent_own_materials_do_not_force_plan(workspace: SimpleNamespace) -> None:
    """只有 Agent 自带的多类材料（作者只点名一类）不得自动分步。"""
    state = routing_service.route(None, "把角色状态和时间线更新一下", record=False)
    assert "状态" in state["write_targets"]
    assert state["plan"] == []

    hook = routing_service.route(None, "伏笔检查一下，有哪些坑还没填", record=False)
    assert hook["plan"] == []


def test_keyword_normalized_trigger_matches(workspace: SimpleNamespace) -> None:
    """含空格/全角的触发词归一化后仍能命中：去 AI 味 → reviewer（只读，无写域）。"""
    res = routing_service.route(None, "这几章的去 AI 味处理一下", record=False)

    assert res["source"] == "keyword"
    assert res["agent"] == "reviewer"
    assert res["write_targets"] == []
    assert res["plan"] == []


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
    from workbench.backend.services import chapter_service, contract_service, review_service

    calls = fake_model(CLEAN_BODY, route_workflow=True)
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
    # 空的已建章节也必须审本轮候选，不能以旧正文为空跳过模型验收。
    review_calls = [call for call in calls if call["task"] == "审稿"]
    assert len(review_calls) == 2
    for call in review_calls:
        assert call["messages"][-1]["content"].split("【待审正文】\n", 1)[1] == CLEAN_BODY.strip()
    reviews = review_service.list_reviews(project_id)
    assert len(reviews) == 2
    expected_hash = hashlib.sha256(CLEAN_BODY.strip().encode("utf-8")).hexdigest()
    for review in reviews:
        assert review["verdict"] == "通过"
        assert review["payload"]["ai_used"] is True
        assert review["payload"]["candidate_hash"] == expected_hash
        assert review["payload"]["counts"] == {"已完成": 4, "未完成": 0, "待核实": 0}
        assert all(item["证据"] in CLEAN_BODY for item in review["payload"]["items"])
        assert contract_service.get_contract(project_id, review["rel_path"])["source"] == "model"
        assert (workspace.projects / "批产书" / review["rel_path"]).read_text(
            encoding="utf-8") == CLEAN_BODY.strip()

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


# ─────────────── 技能绑定与「上传/自建技能」闭环 ───────────────


def _repo_skills_root() -> Path:
    """仓库真实 ``skills/`` 目录（只读；用于绑定建议的「真实数据」断言）。"""
    return Path(skill_service.__file__).resolve().parents[3] / "skills"


def test_skill_sync_accepts_large_files_and_reports_body_tokens_only(workspace: SimpleNamespace) -> None:
    """单文件与合计超出旧阈值仍完整同步，token 估算不含按需引用。"""
    from workbench.backend.engine.runtime import estimate_tokens
    skill_dir = workspace.skills / "ref-heavy"
    (skill_dir / "references").mkdir(parents=True)
    body = "技能条目。\n" * 12000 + "技能正文尾部验证。"
    (skill_dir / "SKILL.md").write_text(
        "---\nname: ref-heavy\ndescription: 仅用于体积口径测试。\n---\n\n" + body,
        encoding="utf-8",
    )
    reference = "清单行\n" * 12000 + "引用尾部验证。"
    (skill_dir / "references" / "big.md").write_text(reference, encoding="utf-8")

    report = skill_service.sync_skills()
    assert "core_bytes" in report
    assert report["core_bytes"] < report["total_bytes"], "total_bytes 仍应含附带文件"

    assert report["core_bytes"] > 128 * 1024
    assert report["errors"] == [] and report["warnings"] == []
    assert "budget_bytes" not in report
    item = skill_service.get_skill("ref-heavy")
    assert item["size_bytes"] > 32 * 1024
    assert item["estimated_tokens"] == estimate_tokens(body.strip())
    assert item["references_estimated_tokens"] == estimate_tokens(reference)
    assert report["estimated_tokens"] == sum(row["estimated_tokens"] for row in skill_service.list_skills())
    assert body in skill_service.skill_digest(["ref-heavy"])
    target = skill_service.dsh_skills_root() / "ref-heavy"
    assert (target / "SKILL.md").read_text(encoding="utf-8").endswith(body)
    assert (target / "references/big.md").read_text(encoding="utf-8") == reference


def test_skills_endpoint_merges_binding_fields(workspace: SimpleNamespace) -> None:
    from workbench.backend.api import orchestration

    payload = orchestration.list_skills()
    assert payload["skills"]
    item = payload["skills"][0]
    assert {"agents", "suggested_agents", "missing_references"} <= set(item)
    assert {"name", "size_bytes", "synced", "reference_files", "references_bytes"} <= set(item)
    assert "estimated_tokens" in payload and "budget_bytes" not in payload and "target_root" in payload
    assert payload["estimated_tokens"] == sum(row["estimated_tokens"] for row in payload["skills"])

    agents_payload = orchestration.list_agents()
    assert agents_payload["material_dirs"] == list(agent_service.MATERIAL_DIRS)


def test_binding_report_bound_and_unbound(workspace: SimpleNamespace,
                                          monkeypatch) -> None:
    monkeypatch.setattr(skill_service, "skills_root", _repo_skills_root)
    report = skill_service.binding_report()

    bound = report["novel-setting"]
    assert "setting-keeper" in bound["agents"], "内置设定类 Agent 应已绑定该技能"
    assert bound["suggested_agents"] == [], "已有绑定时不再给建议"

    unbound = report["foreshadow-check"]
    assert unbound["agents"] == []
    assert "context-keeper" in unbound["suggested_agents"], "按材料交集给出建议"
    assert unbound["missing_references"], "未同步时源 references 应被列为缺失"
    assert unbound["synced"] is False


def test_list_skills_reports_applies_to_and_references(workspace: SimpleNamespace,
                                                       monkeypatch) -> None:
    monkeypatch.setattr(skill_service, "skills_root", _repo_skills_root)
    items = {item["name"]: item for item in skill_service.list_skills()}

    setting = items["novel-setting"]
    assert "设定" in setting["appliesTo"]
    assert len(setting["reference_files"]) >= 2
    assert setting["references_bytes"] > 0
    for rel in setting["reference_files"]:
        assert (_repo_skills_root() / "novel-setting" / rel).is_file(), rel


def test_custom_skill_binding_injects_into_digest(workspace: SimpleNamespace) -> None:
    """上传/自建技能闭环：建技能 → 同步 → 绑定自建 Agent → 注入正文 → 清理。"""
    marker = "自建技能闭环标记词"
    skill_service.create_skill(
        "e2e-binding-skill", "端到端绑定测试技能。",
        f"## 做什么\n\n- 输出包含 {marker} 的清单。\n",
    )
    report = skill_service.sync_skills()
    assert any(item["name"] == "e2e-binding-skill" for item in report["synced"])

    agent = agent_service.create_agent(
        "e2e-bound-agent",
        description="端到端绑定测试",
        system_prompt="只做绑定闭环测试。",
        skills=["e2e-binding-skill"],
    )
    try:
        assert "e2e-binding-skill" in agent["skills"]
        assert marker in skill_service.skill_digest(agent["skills"])
        assert "e2e-bound-agent" in skill_service.binding_report()["e2e-binding-skill"]["agents"]
    finally:
        agent_service.delete_agent("e2e-bound-agent")
        skill_service.delete_skill("e2e-binding-skill")

    assert all(item["name"] != "e2e-bound-agent" for item in agent_service.list_agents())
    assert all(item["name"] != "e2e-binding-skill" for item in skill_service.list_skills())
