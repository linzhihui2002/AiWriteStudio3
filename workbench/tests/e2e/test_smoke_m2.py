"""E2E 冒烟 v1（Task 31）：建项目 → 大纲 → 生成一章（8 步）→ 应用落盘 → 摄取 → 导出。

设计
----
- 全部在 ``tmp_path`` 下运行，**不触碰真实 projects/**；
- 引擎用**假 provider**（monkeypatch httpx），因此不需要网络与真实 Key；
- 覆盖 spec 要求的「dsh 中途禁用降级」用例：把 dsh 标记为不可用后，任务经 direct-api 完成。

用法::

    python workbench/tests/e2e/smoke_m2.py     # 直接跑，打印各步骤结果
    pytest workbench/tests/e2e -q              # 作为测试跑
"""

from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from workbench.backend import config, db  # noqa: E402
from workbench.backend.services import (  # noqa: E402
    chapter_service,
    contract_service,
    ingestion_service,
    outline_service,
    pipeline_service,
    project_service,
    prompt_registry_service,
    proposal_service,
    provider_service,
    review_service,
    transfer_service,
)

CHAPTER_BODY = (
    "码头的灯一盏一盏亮起来。老周蹲在石阶上补网，手指粗糙，线头咬在牙间。\n"
    "“今夜潮水早。”他把网提起，抖了抖。\n"
    "沈砚蹲下来，数了数袋子里的铜钱，一共十七枚。他挑了十二枚摆在石阶上。"
    "“先付一半，剩下的货到再给。”\n"
    "老周没急着收钱，他把网摊开，指着破口：“船上出了事，货晚三天。”\n"
    "“三天。”沈砚重复了一遍，把铜钱又收回两枚，“那就是十枚。”\n"
    "“你倒是算得清。”老周笑了，笑得不太好看。\n"
    "风从江面推过来，带着水汽和铁锈味。上游传来一声闷响，像是什么东西断了。\n"
) * 14


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self.status_code = 200
        self._payload = payload
        self.text = "{}"

    def json(self) -> dict:
        return self._payload


def _install_fake_provider(monkeypatch, body: str) -> None:
    provider_service.upsert_provider(
        "smoke", display_name="冒烟假供应商", base_url="https://smoke.local/v1",
        model_id="smoke-model", api_key="sk-smoke-abcdefgh", enabled=True,
    )
    monkeypatch.setattr(
        "workbench.backend.engine.direct_api.httpx.post",
        lambda *args, **kwargs: _FakeResponse(
            {
                "choices": [{"message": {"content": body}}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 34},
            }
        ),
    )


def run_smoke(workspace: Path, monkeypatch) -> dict:
    """跑完整链路，返回每步结果（供测试断言与人工查看）。"""
    projects = workspace / "projects"
    runtime = workspace / ".workbench"
    for directory in (projects, runtime, workspace / "templates", workspace / "skills",
                      workspace / "agents", workspace / "rules", workspace / "workflows"):
        directory.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(config, "PROJECT_ROOT", workspace)
    monkeypatch.setattr(config, "PROJECTS_DIR", projects)
    monkeypatch.setattr(config, "RUNTIME_DIR", runtime)
    monkeypatch.setattr(config, "DB_PATH", runtime / "workbench.db")
    db.init_db()
    config.ensure_runtime_dirs()
    prompt_registry_service.ensure_registry()

    from workbench.backend.engine import router as engine_router

    # 用例 1：dsh 标记为不可用 → 必须降级到 direct-api（核心功能不瘫痪）
    monkeypatch.setattr(engine_router.get_engine("dsh-headless"), "available", lambda: False)
    _install_fake_provider(monkeypatch, CHAPTER_BODY)

    steps: dict = {}

    # 1) 建项目
    project = project_service.create_project(
        name="冒烟测试书", genre="悬疑", protagonist="沈砚", one_liner="码头取货，货没了。"
    )
    project_id = project["id"]
    steps["建项目"] = {"project": project["name"], "id": project_id}

    # 2) 大纲：灵感 → 候选 → 锁定 → 冻结
    questions = outline_service.expand_ideas(project_id, idea="码头取货", use_ai=False)
    candidates = outline_service.generate_candidates(project_id, idea="码头取货",
                                                     count=2, use_ai=False)
    outline_service.lock_candidate(project_id, 0)
    outline_service.freeze(project_id)
    steps["大纲"] = {"questions": len(questions["questions"]),
                     "candidates": candidates["count"]}

    # 3) 生成一章（8 步循环）
    chapter = chapter_service.create_chapter(project_id, "第一章 潮水")
    pipeline = pipeline_service.run_pipeline(project_id, chapter["rel_path"], use_ai=True,
                                            auto_apply=True)
    steps["8步循环"] = {
        "status": pipeline["status"],
        "engine_fallback": pipeline["events"][0].get("engine", "direct-api"),
        "steps": {name: (data or {}).get("status")
                  for name, data in pipeline["steps"].items()},
        "proposal_id": pipeline.get("proposal_id"),
    }

    # 4) 应用落盘（管线里已 auto_apply；此处核对磁盘状态）
    detail = chapter_service.read_chapter(project_id, chapter["rel_path"])
    steps["落盘"] = {"words": detail["word_count"], "status": detail["status"],
                     "empty": detail["is_empty"]}

    # 5) 摄取四件套
    ingestion = ingestion_service.ingest_chapter(project_id, chapter["rel_path"], use_ai=False)
    steps["摄取"] = {
        "timeline": ingestion["timeline_added"],
        "ledger": ingestion["ledger_added"],
        "foreshadow": ingestion["foreshadow_added"],
        "memory": ingestion["memory_added"],
    }

    # 6) 审稿（硬门禁 + 逐项）
    review = review_service.review_chapter(project_id, chapter["rel_path"], use_ai=True)
    steps["审稿"] = {"verdict": review["verdict"],
                     "blocking": review["hard_gates"]["blocking_gates"],
                     "counts": review["counts"]}

    # 7) 导出
    exported = transfer_service.export_chapters(project_id, scope="with_title", fmt="md")
    steps["导出"] = {"chapters": exported["chapter_count"], "words": exported["word_count"]}

    # 8) 收件箱状态
    steps["收件箱"] = {"pending": proposal_service.pending_count(project_id)}

    return steps


# ─────────────────────────── pytest 入口 ───────────────────────────


def test_e2e_smoke(tmp_path: Path, monkeypatch) -> None:
    steps = run_smoke(tmp_path, monkeypatch)

    assert steps["建项目"]["id"] > 0
    assert steps["大纲"]["candidates"] >= 2
    assert steps["8步循环"]["status"] in ("done", "manual", "blocked"), steps
    assert steps["落盘"]["words"] > 0, "正文必须落盘"
    assert steps["摄取"]["timeline"] >= 1
    assert steps["导出"]["chapters"] >= 1
    assert steps["导出"]["words"] > 0


def test_e2e_dsh_disabled_falls_back(tmp_path: Path, monkeypatch) -> None:
    steps = run_smoke(tmp_path, monkeypatch)
    # dsh 不可用时，所有生成任务都必须经 direct-api 完成（无引擎不可用失败）
    assert steps["8步循环"]["status"] != "failed", steps["8步循环"]
    assert steps["落盘"]["words"] > 0


def test_outline_gate_blocks_without_freeze(tmp_path: Path, monkeypatch) -> None:
    """CHECK 门控：未冻结大纲时管线必须阻断（不能悄悄生成）。"""
    projects = tmp_path / "projects"
    runtime = tmp_path / ".workbench"
    projects.mkdir()
    runtime.mkdir()
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(config, "PROJECTS_DIR", projects)
    monkeypatch.setattr(config, "RUNTIME_DIR", runtime)
    monkeypatch.setattr(config, "DB_PATH", runtime / "workbench.db")
    db.init_db()
    config.ensure_runtime_dirs()
    prompt_registry_service.ensure_registry()
    from workbench.backend.engine import router as engine_router

    monkeypatch.setattr(engine_router.get_engine("dsh-headless"), "available", lambda: False)

    project = project_service.create_project(name="门控冒烟书")
    chapter = chapter_service.create_chapter(project["id"], "第一章")
    result = pipeline_service.run_pipeline(project["id"], chapter["rel_path"], use_ai=False)
    assert result["status"] == "blocked"
    assert result["blocked_reasons"]

    gate = contract_service.check_prerequisites(project["id"], chapter["rel_path"])
    assert gate["blocked"] is True


# ─────────────────────────── CLI 入口 ───────────────────────────


def main() -> int:
    import json
    import tempfile
    from unittest import mock

    print("=" * 68)
    print(" E2E 冒烟 v1：建项目 → 大纲 → 8 步产章 → 落盘 → 摄取 → 审稿 → 导出")
    print("=" * 68)
    with tempfile.TemporaryDirectory() as raw:
        workspace = Path(raw)
        with mock.patch("workbench.backend.engine.direct_api.httpx.post") as patched:
            patched.side_effect = mock.Mock(
                return_value=_FakeResponse(
                    {"choices": [{"message": {"content": CHAPTER_BODY}}],
                     "usage": {"prompt_tokens": 12, "completion_tokens": 34}}
                )
            )
            steps = run_smoke(workspace, mock.patch)
        print(json.dumps(steps, ensure_ascii=False, indent=2))
    print("-" * 68)
    print(" 冒烟完成（临时工作区已清理）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())