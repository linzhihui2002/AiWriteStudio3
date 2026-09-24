"""性能基准（Task 68）：200 章项目下的首屏 / 组装 / 取消响应。

基准（spec）：
- 200 章项目编辑器首屏 < 2s（含文档树 + 章节列表 + 索引统计）
- 上下文包组装 < 1s
- headless 取消响应 < 3s（此处用可控的假引擎测量「取消调用 → 引擎确认取消」的时延）

全部在临时工作区运行；不依赖网络（假 provider）。
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from workbench.backend import config, db
from workbench.backend.engine.runtime import GenerationRequest
from workbench.backend.services import (
    chapter_service,
    context_service,
    index_service,
    project_service,
    prompt_registry_service,
    provider_service,
    tree_service,
)

CHAPTERS = 200


@pytest.fixture()
def big_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    projects = tmp_path / "projects"
    runtime = tmp_path / ".workbench"
    for directory in (projects, runtime, tmp_path / "skills", tmp_path / "agents",
                      tmp_path / "rules", tmp_path / "workflows"):
        directory.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(config, "PROJECTS_DIR", projects)
    monkeypatch.setattr(config, "RUNTIME_DIR", runtime)
    monkeypatch.setattr(config, "DB_PATH", runtime / "workbench.db")
    db.init_db()
    config.ensure_runtime_dirs()
    prompt_registry_service.ensure_registry()

    from workbench.backend.engine import router as engine_router

    monkeypatch.setattr(engine_router.get_engine("dsh-headless"), "available", lambda: False)
    provider_service.upsert_provider("bench", base_url="https://bench.local/v1",
                                     model_id="m", api_key="sk-bench-abcdefgh", enabled=True)

    project = project_service.create_project(name="基准书")
    project_id = project["id"]
    project_dir = projects / "基准书"

    body = ("沈砚把铜钱数了两遍。老周蹲在石阶上补网，线头咬在牙间。" * 30)
    for index in range(1, CHAPTERS + 1):
        rel = f"章节/第{index:04d}章.md"
        (project_dir / rel).parent.mkdir(parents=True, exist_ok=True)
        (project_dir / rel).write_text(
            chapter_service.compose_document(
                {"标题": f"第{index}章", "状态": "草稿"}, f"{body}\n本章第 {index} 个节点。\n"
            ),
            encoding="utf-8",
        )
    # 设定与状态（每章必读集的数据源）
    (project_dir / "状态" / "角色状态.md").write_text(
        chapter_service.compose_document(
            {"标题": "角色状态"},
            "## 沈砚\n- 位置：南码头\n- 心理：戒备\n\n## 老周\n- 位置：码头\n",
        ),
        encoding="utf-8",
    )
    (project_dir / "设定" / "人物设定.md").write_text(
        chapter_service.compose_document(
            {"标题": "人物设定"},
            "## 沈砚\n- 性格：沉默\n- 目标：取回货\n\n## 老周\n- 性格：油滑\n",
        ),
        encoding="utf-8",
    )
    index_service.rebuild_index(project_id)
    return {"project_id": project_id, "project_dir": project_dir}


def test_first_screen_under_2s(big_project) -> None:
    """编辑器首屏数据（文档树 + 章节列表 + 索引统计）在 200 章下 < 2s。"""
    project_id = big_project["project_id"]
    start = time.perf_counter()
    tree = tree_service.get_file_tree(project_id)
    chapters = chapter_service.list_chapters(project_id)
    stats = index_service.index_stats(project_id)
    elapsed = time.perf_counter() - start

    assert len(chapters) == CHAPTERS
    assert stats["documents"] >= CHAPTERS
    assert tree["groups"], "文档树分组缺失"
    assert elapsed < 2.0, f"首屏数据耗时 {elapsed:.3f}s（基准 < 2s）"


def test_context_assembly_under_1s(big_project) -> None:
    """上下文包组装 < 1s（含必读集 + 检索召回）。"""
    project_id = big_project["project_id"]
    chapter_rel = f"章节/第{CHAPTERS:04d}章.md"

    start = time.perf_counter()
    assembly = context_service.assemble(
        project_id, chapter_rel=chapter_rel, related_characters=["沈砚"], query="铜钱",
    )
    elapsed = time.perf_counter() - start

    assert assembly["blocks"], "上下文为空"
    assert assembly["total_tokens"] > 0
    assert elapsed < 1.0, f"上下文组装耗时 {elapsed:.3f}s（基准 < 1s）"


def test_search_under_1s(big_project) -> None:
    project_id = big_project["project_id"]
    start = time.perf_counter()
    hits = index_service.search(project_id, "铜钱", limit=20)
    elapsed = time.perf_counter() - start
    assert hits, "检索无命中（索引应包含章节正文）"
    assert elapsed < 1.0, f"检索耗时 {elapsed:.3f}s"


def test_cancel_response_under_3s(big_project) -> None:
    """取消响应 < 3s：取消调用到引擎标记取消（含会话登记查询）。"""
    from workbench.backend.engine import router

    engine = router.get_engine("direct-api")
    session = engine.create_session(project_id=big_project["project_id"], task_type="章节正文")

    start = time.perf_counter()
    engine.cancel_task(session)
    cancelled = engine.is_cancelled(session)
    elapsed = time.perf_counter() - start

    assert cancelled is True
    assert elapsed < 3.0, f"取消耗时 {elapsed:.3f}s（基准 < 3s）"


def test_cancel_flag_stops_run(big_project, monkeypatch) -> None:
    """取消后引擎立即停止（不再发起请求）。"""
    from workbench.backend.engine.direct_api import DirectApiEngine

    calls: list[int] = []

    def fake_post(*args, **kwargs):
        calls.append(1)
        raise AssertionError("已取消的任务不应发起请求")

    monkeypatch.setattr("workbench.backend.engine.direct_api.httpx.post", fake_post)
    engine = DirectApiEngine()
    session = engine.create_session(task_type="章节正文")
    engine.cancel_task(session)

    result = engine.run_agent(GenerationRequest(task_type="章节正文"), session_id=session)
    assert result.cancelled is True
    assert calls == []