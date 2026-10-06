"""M2 管线单测：上下文组装 / 提示词注册 / 合同 / 收件箱 / 摄取 / 8 步循环。

AI 调用一律用假 provider（monkeypatch httpx），不依赖网络与 Key。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from workbench.backend import config, db
from workbench.backend.services import (
    character_service,
    context_service,
    contract_service,
    generation_service,
    ingestion_service,
    pipeline_service,
    project_service,
    prompt_registry_service,
    proposal_service,
    provider_service,
    review_service,
)

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
    templates = tmp_path / "templates"
    for directory in (projects, runtime, templates):
        directory.mkdir()
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(config, "PROJECTS_DIR", projects)
    monkeypatch.setattr(config, "RUNTIME_DIR", runtime)
    monkeypatch.setattr(config, "DB_PATH", runtime / "workbench.db")
    db.init_db()
    config.ensure_runtime_dirs()
    prompt_registry_service.ensure_registry()

    # 单测绝不调用真实 dsh（本机已装 vendor dsh，必须显式禁用，避免真跑 600s 超时）
    from workbench.backend.engine import router

    monkeypatch.setattr(router.get_engine("dsh-headless"), "available", lambda: False)
    return SimpleNamespace(projects=projects, runtime=runtime, root=tmp_path)


class _FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = "{}"

    def json(self) -> dict:
        return self._payload


def _install_fake_model(monkeypatch, text: str) -> None:
    provider_service.upsert_provider("fake", base_url="https://fake.local/v1",
                                     models=[{"id": "m"}], api_key="sk-test-abcdefgh",
                                     enabled=True)
    monkeypatch.setattr(
        "workbench.backend.engine.direct_api.httpx.post",
        lambda *a, **k: _FakeResponse(
            {"choices": [{"message": {"content": text}}],
             "usage": {"prompt_tokens": 10, "completion_tokens": 20}}
        ),
    )


# ─────────────────────────── 提示词注册 ───────────────────────────


def test_prompt_registry_seeded(workspace: SimpleNamespace) -> None:
    prompts = prompt_registry_service.list_prompts()
    ids = {item["prompt_id"] for item in prompts}
    assert "novel.chapter.draft" in ids
    assert "novel.review.contract" in ids
    assert all(item["task_type"] for item in prompts)

    rendered = prompt_registry_service.render("novel.chapter.draft", {"字数预算": 3000})
    assert "3000" in rendered["body"]
    assert "{字数预算}" not in rendered["body"]


# ─────────────────────────── 上下文组装 ───────────────────────────


def test_context_assembly_levels_and_trimming(workspace: SimpleNamespace) -> None:
    project = project_service.create_project(name="上下文书")
    project_id = project["id"]
    project_dir = workspace.projects / "上下文书"

    (project_dir / "设定" / "人物设定.md").write_text(
        "---\n标题: 人物设定\n---\n\n## 沈砚\n- 性格：沉默，算计清楚\n- 目标：把货取回来\n",
        encoding="utf-8",
    )
    (project_dir / "状态" / "角色状态.md").write_text(
        "---\n标题: 角色状态\n---\n\n## 沈砚\n- 位置：南码头\n- 伤势：左臂旧伤\n",
        encoding="utf-8",
    )
    (project_dir / "设定" / "伏笔管理.md").write_text(
        "---\n标题: 伏笔管理\n---\n\n- [待回收] 崩断的缆绳｜埋设：第0001章\n",
        encoding="utf-8",
    )

    from workbench.backend.services import chapter_service

    first = chapter_service.create_chapter(project_id, "第一章")
    chapter_service.save_chapter(project_id, first["rel_path"], "前章结尾：缆绳断了。")
    chapter_service.set_chapter_meta(project_id, first["rel_path"], status="完成")
    second = chapter_service.create_chapter(project_id, "第二章")

    assembly = context_service.assemble(
        project_id, chapter_rel=second["rel_path"], related_characters=["沈砚"],
        query="缆绳",
    )
    levels = {block["level"] for block in assembly["blocks"]}
    assert 3 in levels, "前章末必须进上下文（必读）"
    assert 5 in levels, "角色状态卡必须进上下文（必读）"
    assert 6 in levels
    assert assembly["mandatory_sources"], "必读集不得为空"
    assert assembly["total_tokens"] > 0
    assert assembly["duration_ms"] < 1000

    # 预算裁剪：把预算压到很低 → 非必读级别被裁，必读保留
    tight = context_service.assemble(project_id, chapter_rel=second["rel_path"],
                                     related_characters=["沈砚"], budget_tokens=15)
    assert tight["degradation"], "超预算必须产生降级事件"
    remaining_levels = {block["level"] for block in tight["blocks"]}
    assert 3 in remaining_levels and 5 in remaining_levels

    # 预览内容
    preview = context_service.build_preview(assembly)
    assert preview["items"] and preview["budget_tokens"] == context_service.DEFAULT_INPUT_BUDGET


# ─────────────────────────── 章节合同 ───────────────────────────


def test_contract_fallback_and_freeze(workspace: SimpleNamespace) -> None:
    project = project_service.create_project(name="合同书")
    project_id = project["id"]
    from workbench.backend.services import chapter_service

    chapter = chapter_service.create_chapter(project_id, "第一章")
    contract = contract_service.generate_contract(project_id, chapter["rel_path"], use_ai=False)
    assert contract["status"] == "draft"
    assert contract["plot_points"]

    frozen = contract_service.freeze_contract(project_id, chapter["rel_path"])
    assert frozen["status"] == "frozen"

    with pytest.raises(Exception) as excinfo:
        contract_service.update_contract(project_id, chapter["rel_path"],
                                         {"word_budget": 5000})
    assert "差异确认" in str(excinfo.value)

    updated = contract_service.update_contract(
        project_id, chapter["rel_path"], {"word_budget": 5000}, confirm=True
    )
    assert updated["word_budget"] == 5000
    assert updated["history"][-1]["confirmed"] is True
    assert contract_service.contract_budget(project_id, chapter["rel_path"]) == 5000


def test_check_prerequisites_blocks_without_outline(workspace: SimpleNamespace) -> None:
    project = project_service.create_project(name="门控书")
    project_id = project["id"]
    from workbench.backend.services import chapter_service

    chapter = chapter_service.create_chapter(project_id, "第一章")
    gate = contract_service.check_prerequisites(project_id, chapter["rel_path"])
    assert gate["blocked"] is True
    assert any("大纲" in reason for reason in gate["reasons"])

    # 写入大纲并冻结 → CHECK 通过（合同由 CONTRACT 步生成冻结）
    (workspace.projects / "门控书" / "大纲" / "大纲.md").write_text(
        "---\n标题: 大纲\n---\n\n卷一：主角回到码头取货。\n", encoding="utf-8"
    )
    contract_service.freeze_outline(project_id)
    assert contract_service.check_prerequisites(project_id, chapter["rel_path"])["blocked"] is False

    # DRAFT 前置：合同未冻结时被拦
    ready = contract_service.check_contract_ready(project_id, chapter["rel_path"])
    assert ready["ok"] is False
    contract_service.generate_contract(project_id, chapter["rel_path"], use_ai=False)
    assert contract_service.check_contract_ready(project_id, chapter["rel_path"])["ok"] is False
    contract_service.freeze_contract(project_id, chapter["rel_path"])
    assert contract_service.check_contract_ready(project_id, chapter["rel_path"])["ok"] is True


# ─────────────────────────── 收件箱 ───────────────────────────


def test_proposal_inbox_apply_and_discard(workspace: SimpleNamespace) -> None:
    project = project_service.create_project(name="收件箱书")
    project_id = project["id"]
    target = workspace.projects / "收件箱书" / "设定" / "世界设定.md"
    target.write_text("---\n标题: 世界设定\n---\n\n原有内容。\n", encoding="utf-8")

    proposal = proposal_service.create_proposal(
        project_id=project_id, kind="setting", title="补全世界设定",
        target_path="设定/世界设定.md", content="新增：北境三年一雪。",
    )
    assert proposal["status"] == "pending"
    assert proposal["diff"], "应用前必须有 diff"
    assert proposal["is_new_file"] is False

    item = proposal_service.get_proposal(proposal["id"])
    assert any(entry["type"] == "add" for entry in item["diff"])

    applied = proposal_service.apply_proposal(proposal["id"])
    assert applied["written"] == "设定/世界设定.md"
    text = target.read_text(encoding="utf-8")
    assert "北境三年一雪" in text and "原有内容" not in text

    with pytest.raises(Exception):
        proposal_service.apply_proposal(proposal["id"])  # 不能重复应用

    second = proposal_service.create_proposal(
        project_id=project_id, kind="setting", title="第二条",
        target_path="设定/世界设定.md", content="不该落盘的内容",
    )
    proposal_service.discard_proposal(second["id"])
    assert "不该落盘" not in target.read_text(encoding="utf-8")
    assert proposal_service.pending_count(project_id) == 0


# ─────────────────────────── 摄取 ───────────────────────────


def test_ingestion_four_trackers(workspace: SimpleNamespace) -> None:
    project = project_service.create_project(name="摄取书")
    project_id = project["id"]
    project_dir = workspace.projects / "摄取书"
    (project_dir / "设定" / "人物设定.md").write_text(
        "---\n标题: 人物设定\n---\n\n## 沈砚\n- 性格：沉默\n", encoding="utf-8"
    )

    from workbench.backend.services import chapter_service

    chapter = chapter_service.create_chapter(project_id, "第一章")
    chapter_service.save_chapter(
        project_id, chapter["rel_path"],
        "次日清晨，沈砚在南码头等货。获得 3 袋盐。伏笔：崩断的缆绳另有蹊跷。\n"
        "沈砚数了数铜钱，消耗 5 枚铜钱。",
        status="完成",
    )

    result = ingestion_service.ingest_chapter(project_id, chapter["rel_path"], use_ai=False)
    assert result["timeline_added"] >= 1
    assert result["ledger_added"] >= 1
    assert result["foreshadow_added"] >= 1
    # 出场不代表知晓整章事件；自动摄取不能制造全知记忆。
    assert result["memory_added"] == 0
    assert "沈砚" in result["characters_present"]
    assert not character_service.list_memory(project_id)

    timeline = ingestion_service.list_timeline(project_id)
    assert timeline and "依据" in timeline[0] or timeline[0].get("evidence") is not None
    ledger = ingestion_service.list_ledger(project_id)
    assert any("盐" in entry["item"] for entry in ledger)

    foreshadows = ingestion_service.list_foreshadows(project_id)
    assert foreshadows and foreshadows[0]["status"] == "待回收"
    confirmed = ingestion_service.confirm_payoff(project_id, foreshadows[0]["line"])
    assert "[已回收]" in confirmed["content"]

    # 摘要进入 .meta/summaries.json
    summaries = (project_dir / ".meta" / "summaries.json").read_text(encoding="utf-8")
    assert chapter["rel_path"] in summaries


# ─────────────────────────── 审稿 ───────────────────────────


def test_hard_gates_detect_forbidden_words(workspace: SimpleNamespace) -> None:
    clean = review_service.run_hard_gates(CLEAN_BODY)
    assert clean["gates"][0]["key"] == "字数门"
    assert clean["words"] >= 2000

    dirty = CLEAN_BODY + "\n突然，他仿佛看见了什么。不是恐惧，不是惊讶，而是麻木。\n"
    dirty_result = review_service.run_hard_gates(dirty)
    assert dirty_result["passed"] is False
    assert "禁词门" in dirty_result["blocking_gates"]
    reasons = " ".join(item["reason"] for item in dirty_result["locations"])
    assert "禁用词" in reasons or "推测词" in reasons or "三连排比" in reasons

    leak = review_service.run_hard_gates(CLEAN_BODY + "\n情节点1：他到了码头。\n")
    assert "记号泄漏" in leak["blocking_gates"]

    short = review_service.run_hard_gates("太短了。")
    assert "字数门" in short["blocking_gates"]


def test_review_ledger_arithmetic(workspace: SimpleNamespace) -> None:
    project = project_service.create_project(name="算术书")
    project_dir = workspace.projects / "算术书"
    (project_dir / "状态" / "资源账本.md").write_text(
        "---\n标题: 资源账本\n---\n"
        "| 物品 | 章节 | 变动 | 结余 |\n"
        "| --- | --- | --- | --- |\n"
        "| 灵石 | 第0001章 | +10 | 10 |\n"
        "| 灵石 | 第0002章 | -3 | 9 |\n",
        encoding="utf-8",
    )
    problems = review_service.check_ledger_arithmetic(project_dir)
    assert problems and problems[0]["类型"] == "数值算术"


def test_quality_matrix(workspace: SimpleNamespace) -> None:
    project = project_service.create_project(name="矩阵书")
    project_id = project["id"]
    from workbench.backend.services import chapter_service

    chapter = chapter_service.create_chapter(project_id, "第一章")
    chapter_service.save_chapter(project_id, chapter["rel_path"], CLEAN_BODY)

    matrix = review_service.quality_matrix(project_id)
    assert matrix["columns"] == ["字数门", "语言门", "禁词门", "去AI味", "格式门", "硬门禁总状态", "一致性", "逐项审稿"]
    row = matrix["chapters"][0]
    assert row["cells"]["字数门"] == "通过"
    assert row["cells"]["逐项审稿"] == "未跑"


# ─────────────────────────── 角色 ───────────────────────────


def test_character_state_and_memory(workspace: SimpleNamespace) -> None:
    project = project_service.create_project(name="角色书")
    project_id = project["id"]
    project_dir = workspace.projects / "角色书"
    (project_dir / "设定" / "人物设定.md").write_text(
        "---\n标题: 人物设定\n---\n\n## 沈砚\n- 性格：沉默\n- 弱点：轻信旧友\n",
        encoding="utf-8",
    )
    characters = character_service.list_characters(project_id)
    assert characters[0]["name"] == "沈砚"
    assert "说话风格" in characters[0]["missing"]

    character_service.apply_state_change(project_id, character="沈砚", field="位置",
                                         value="北码头", chapter="章节/第0002章.md")
    assert character_service.get_state_value(project_id, "沈砚", "位置") == "北码头"

    memory = character_service.add_memory(project_id, character="沈砚",
                                          know_what="老周的儿子生病", when_known="第0001章")
    assert memory["id"] > 0
    assert character_service.memory_digest(project_id, ["沈砚"])["沈砚"]

    curve = character_service.growth_curve(project_id, "沈砚")
    assert "位置" in curve["series"]


# ─────────────────────────── 8 步循环 ───────────────────────────


def test_pipeline_end_to_end(workspace: SimpleNamespace, monkeypatch) -> None:
    _install_fake_model(monkeypatch, CLEAN_BODY)

    def fake_post(*args, **kwargs):
        system = str(kwargs["json"]["messages"][0]["content"])
        if "逐项" in system and "判定" in system:
            text = json.dumps({"逐项": [{"项": "码头交付定金", "判定": "已完成",
                                         "证据": "十二枚，先付一半。"}],
                               "一致性": [], "修改指令": []}, ensure_ascii=False)
        elif "情节点" in system and "必须承上" in system:
            text = json.dumps({"情节点": ["码头交付定金"], "必须承上": [],
                               "字数预算": 2000, "钩子类型": "悬念"}, ensure_ascii=False)
        else:
            text = CLEAN_BODY
        return _FakeResponse({"choices": [{"message": {"content": text}}],
                              "usage": {"prompt_tokens": 10, "completion_tokens": 20}})

    monkeypatch.setattr("workbench.backend.engine.direct_api.httpx.post", fake_post)

    project = project_service.create_project(name="循环书")
    project_id = project["id"]
    project_dir = workspace.projects / "循环书"
    (project_dir / "大纲" / "大纲.md").write_text(
        "---\n标题: 大纲\n---\n\n卷一：码头取货。\n", encoding="utf-8"
    )
    contract_service.freeze_outline(project_id)

    from workbench.backend.services import chapter_service

    chapter = chapter_service.create_chapter(project_id, "第一章")
    result = pipeline_service.run_pipeline(project_id, chapter["rel_path"], use_ai=True,
                                           auto_apply=True)

    assert result["status"] == "done", result
    steps = result["steps"]
    assert all(steps[step]["status"] == "done" for step in pipeline_service.STEPS), steps

    # 正文已应用落盘（章节目录下内容非空）
    detail = chapter_service.read_chapter(project_id, chapter["rel_path"])
    assert detail["word_count"] > 1000

    # 摄取产物
    assert ingestion_service.list_timeline(project_id)

    # checkpoint 落盘
    checkpoint = pipeline_service.load_checkpoint(project_id, chapter["rel_path"])
    assert checkpoint["status"] == "done"
    assert checkpoint["steps"]["DECIDE"]["status"] == "done"

    # 断点恢复：重跑同一章（resume）应直接复用检查点，不重复生成
    again = pipeline_service.run_pipeline(project_id, chapter["rel_path"], use_ai=True,
                                          auto_apply=False)
    assert again["status"] == "done"
    assert again["resumed_from"] == "DECIDE"


def test_pipeline_blocks_without_outline(workspace: SimpleNamespace) -> None:
    project = project_service.create_project(name="阻断书")
    from workbench.backend.services import chapter_service

    chapter = chapter_service.create_chapter(project["id"], "第一章")
    result = pipeline_service.run_pipeline(project["id"], chapter["rel_path"], use_ai=False)
    assert result["status"] == "blocked"
    assert result["blocked_reasons"]
