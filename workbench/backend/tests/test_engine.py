"""引擎层单测：Protocol 契约 / 决策表路由 / 三级覆盖 / 降级 / 错误归一化。

真实 dsh 调用依赖本机 Key 与网络，**不在单测里执行**（诚实 SKIP），
仅验证命令构造、产物收集与错误归一化等确定性逻辑。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from workbench.backend import config, db
from workbench.backend.engine import router
from workbench.backend.engine.direct_api import DirectApiEngine
from workbench.backend.engine.dsh_engine import DshEngine
from workbench.backend.engine.runtime import (
    GenerationRequest,
    HarnessRuntime,
    estimate_tokens,
    normalize_dsh_error,
    normalize_http_error,
)
from workbench.backend.services import generation_service, settings_service


@pytest.fixture()
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
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
    # 单测绝不调用真实 dsh（本机已装 vendor dsh，必须显式禁用）
    monkeypatch.setattr(router.get_engine("dsh-headless"), "available", lambda: False)
    return SimpleNamespace(projects=projects, runtime=runtime, root=tmp_path)


# ─────────────────────────── Protocol 契约 ───────────────────────────


def test_both_engines_satisfy_protocol() -> None:
    for engine in (DshEngine(), DirectApiEngine()):
        assert isinstance(engine, HarnessRuntime), engine.name
        for method in ("create_session", "run_agent", "stream_agent", "call_tool",
                       "cancel_task", "get_model_status", "available", "capabilities"):
            assert callable(getattr(engine, method))
        session = engine.create_session(task_type="测试")
        assert isinstance(session, str) and session


def test_business_layer_does_not_import_dsh_internals() -> None:
    """红线：业务层（services/api）不得 import dsh 内部对象。"""
    backend = Path(__file__).resolve().parents[1]
    offenders: list[str] = []
    for path in list((backend / "services").rglob("*.py")) + list((backend / "api").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for token in ("dsh_engine", "dsh_paths", "dsh-agent", "@deepseek-ai"):
            if f"import {token}" in text or f"from ...engine.{token}" in text:
                offenders.append(f"{path.name}: {token}")
    assert offenders == [], f"业务层出现 dsh 内部依赖：{offenders}"


# ─────────────────────────── 决策表与三级覆盖 ───────────────────────────


def test_decision_table_prefers_dsh_for_semantic_tasks(workspace: SimpleNamespace) -> None:
    assert router.resolve_route("章节正文")["engine"] == "dsh-headless"
    assert router.resolve_route("审稿")["engine"] == "dsh-headless"
    assert router.resolve_route("结构化抽取")["engine"] == "direct-api"
    assert router.resolve_route("轻量改写")["engine"] == "direct-api"
    assert router.resolve_route("章节正文")["source"] == "decision-table"


def test_three_level_override_priority(workspace: SimpleNamespace) -> None:
    project_dir = workspace.projects / "书"
    project_dir.mkdir()

    # 全局任务级覆盖
    settings_service.update_settings({"engines": {"overrides": {"章节正文": "direct-api"}}})
    assert router.resolve_route("章节正文", project_dir=project_dir)["source"] == "global-task"

    # 项目级任务覆盖 > 全局任务覆盖
    router.write_project_engine_config(project_dir, {"overrides": {"章节正文": "dsh-headless"}})
    route = router.resolve_route("章节正文", project_dir=project_dir)
    assert route["engine"] == "dsh-headless"
    assert route["source"] == "project-task"

    # 任务级显式指定 > 项目级
    route = router.resolve_route("章节正文", project_dir=project_dir,
                                 explicit_engine="direct-api")
    assert route == {"engine": "direct-api", "source": "task"}

    # 项目级默认（未命中任务映射）
    router.write_project_engine_config(project_dir, {"default": "direct-api"})
    assert router.resolve_route("未登记任务", project_dir=project_dir)["source"] == "project"


def test_fallback_when_dsh_unavailable(workspace: SimpleNamespace, monkeypatch) -> None:
    dsh = router.get_engine("dsh-headless")
    direct = router.get_engine("direct-api")
    monkeypatch.setattr(dsh, "available", lambda: False)
    monkeypatch.setattr(direct, "available", lambda: True)

    engine, route = router.pick_engine("章节正文")
    assert engine.name == "direct-api"
    assert route["fallback"] is True
    assert route["requested_engine"] == "dsh-headless"


def test_no_engine_available_marks_unavailable(workspace: SimpleNamespace, monkeypatch) -> None:
    monkeypatch.setattr(router.get_engine("dsh-headless"), "available", lambda: False)
    monkeypatch.setattr(router.get_engine("direct-api"), "available", lambda: False)
    _engine, route = router.pick_engine("章节正文")
    assert route["unavailable"] is True


# ─────────────────────────── 错误归一化 ───────────────────────────


def test_error_normalization() -> None:
    assert normalize_http_error(401)[0] == "AUTH_ERROR"
    assert normalize_http_error(429)[0] == "RATE_LIMIT"
    assert normalize_http_error(503)[0] == "SERVER_ERROR"
    assert normalize_http_error(418)[0] == "UNKNOWN"

    code, message = normalize_dsh_error(
        'dsh: UNKNOWN_MODEL: pi-ai provider "fluxlane" has no configured model "x"\n'
    )
    assert code == "MODEL_NOT_FOUND"
    assert "no configured model" in message
    assert normalize_dsh_error("")[0] == "UNKNOWN"


def test_estimate_tokens_is_deterministic() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("中文四字") == 4
    assert estimate_tokens("abcdefgh") == 2


# ─────────────────────────── DirectApiEngine ───────────────────────────


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict | str) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = payload if isinstance(payload, str) else "{}"

    def json(self) -> dict:
        assert isinstance(self._payload, dict)
        return self._payload


def test_direct_api_engine_success(workspace: SimpleNamespace, monkeypatch) -> None:
    from workbench.backend.services import provider_service

    provider_service.upsert_provider(
        "fake", display_name="Fake", base_url="https://fake.local/v1",
        models=[{"id": "fake-model"}], api_key="sk-test-1234567890", enabled=True,
    )

    captured: dict = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured.update({"url": url, "headers": headers, "json": json})
        return _FakeResponse(
            200,
            {
                "choices": [{"message": {"content": "生成结果"}}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 34},
            },
        )

    monkeypatch.setattr("workbench.backend.engine.direct_api.httpx.post", fake_post)

    engine = DirectApiEngine()
    assert engine.available() is True
    result = engine.run_agent(
        GenerationRequest(task_type="结构化抽取", system="你是助手",
                          messages=[{"role": "user", "content": "写一句话"}])
    )
    assert result.ok is True
    assert result.text == "生成结果"
    assert result.total_tokens == 46
    assert captured["url"].endswith("/chat/completions")
    assert captured["headers"]["Authorization"].startswith("Bearer sk-test")


def test_direct_api_engine_error_and_retry(workspace: SimpleNamespace, monkeypatch) -> None:
    from workbench.backend.services import provider_service

    provider_service.upsert_provider(
        "fake2", base_url="https://fake.local/v1", models=[{"id": "m"}],
        api_key="sk-test-abcdefgh", enabled=True,
    )

    calls: list[int] = []

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append(1)
        return _FakeResponse(401, {"error": "bad key"})

    monkeypatch.setattr("workbench.backend.engine.direct_api.httpx.post", fake_post)
    monkeypatch.setattr("time.sleep", lambda *_: None)

    engine = DirectApiEngine()
    result = engine.run_agent(
        GenerationRequest(task_type="结构化抽取", provider="fake2", model="m",
                          messages=[{"role": "user", "content": "x"}])
    )
    assert result.ok is False
    assert result.error_code == "AUTH_ERROR"
    assert len(calls) == 1  # 非瞬时错误不重试


def test_direct_api_writes_output_file(workspace: SimpleNamespace, monkeypatch) -> None:
    from workbench.backend.services import provider_service

    provider_service.upsert_provider("fake3", base_url="https://fake.local/v1",
                                     models=[{"id": "m"}], api_key="sk-test-abcdefgh",
                                     enabled=True)
    monkeypatch.setattr(
        "workbench.backend.engine.direct_api.httpx.post",
        lambda *a, **k: _FakeResponse(200, {"choices": [{"message": {"content": "正文"}}]}),
    )

    output = workspace.runtime / "artifact.md"
    engine = DirectApiEngine()
    result = engine.run_agent(
        GenerationRequest(task_type="章节正文", provider="fake3", model="m",
                          messages=[{"role": "user", "content": "写"}],
                          output_path=str(output))
    )
    assert result.ok is True
    assert result.output_path == str(output)
    assert output.read_text(encoding="utf-8").strip() == "正文"


# ─────────────────────────── DshEngine（不触发真实调用） ───────────────────────────


def test_dsh_engine_task_text_and_patch(workspace: SimpleNamespace) -> None:
    engine = DshEngine()
    request = GenerationRequest(
        task_type="章节正文",
        system="你是小说写作助手",
        messages=[{"role": "user", "content": "写第1章"}],
        context_files=["大纲/章纲.md"],
        output_path="章节/第0001章.md",
        provider="fluxlane",
        model="glm-5.3-flash",
    )
    text = engine._task_text(request)
    assert "产物落盘要求" in text
    assert "章节/第0001章.md" in text
    assert "【必读文件】" in text
    assert "DONE" in text

    patch = engine._patch_file(request, "sess-1")
    assert patch is not None and patch.is_file()
    import yaml

    payload = yaml.safe_load(patch.read_text(encoding="utf-8"))
    assert payload[0]["id"] == "agent-default-model"
    # B2 约束：patch 整段替换 config，必须重述 provider + model
    assert payload[0]["config"] == {"provider": "fluxlane", "model": "glm-5.3-flash"}


def test_dsh_engine_collect_output_prefers_file(workspace: SimpleNamespace) -> None:
    engine = DshEngine()
    artifact = workspace.runtime / "out.md"
    artifact.write_text("落盘产物", encoding="utf-8")
    request = GenerationRequest(task_type="章节正文", output_path=str(artifact))

    text, ok, note = engine._collect_output(request, "DONE")
    assert (text, ok, note) == ("落盘产物", True, "file")

    request2 = GenerationRequest(task_type="章节正文", output_path=str(workspace.runtime / "缺失.md"))
    text, ok, note = engine._collect_output(request2, "直接答案")
    assert ok is False
    assert note.startswith("产物文件未生成")
    assert text == "直接答案"

    text, ok, note = engine._collect_output(GenerationRequest(task_type="对话"), "答案")
    assert (text, ok, note) == ("答案", True, "stdout")


def test_dsh_engine_reports_unavailable_without_vendor(workspace: SimpleNamespace,
                                                       monkeypatch) -> None:
    import workbench.backend.engine.dsh_engine as module

    monkeypatch.setattr(module, "is_vendor_dsh_installed", lambda: False)
    engine = DshEngine()
    assert engine.available() is False
    result = engine.run_agent(GenerationRequest(task_type="章节正文"))
    assert result.ok is False
    assert result.error_code == "ENGINE_UNAVAILABLE"


# ─────────────────────────── 生成服务：任务留痕 ───────────────────────────


def test_run_task_records_task_and_usage(workspace: SimpleNamespace, monkeypatch) -> None:
    from workbench.backend.services import provider_service, project_service

    provider_service.upsert_provider("fake4", base_url="https://fake.local/v1",
                                     models=[{"id": "m"}], api_key="sk-test-abcdefgh",
                                     enabled=True)
    monkeypatch.setattr(
        "workbench.backend.engine.direct_api.httpx.post",
        lambda *a, **k: _FakeResponse(
            200,
            {"choices": [{"message": {"content": "ok"}}],
             "usage": {"prompt_tokens": 5, "completion_tokens": 7}},
        ),
    )

    project = project_service.create_project(name="任务书")
    result = generation_service.run_task(
        project_id=project["id"],
        task_type="结构化抽取",
        messages=[{"role": "user", "content": "抽取"}],
        prompt_id="extract.v1",
        prompt_version="1",
        context_snapshot={"level1": "用户材料"},
    )
    assert result["ok"] is True
    assert result["engine"] == "direct-api"

    task = generation_service.get_task(result["task_id"])
    assert task["status"] == "done"
    assert task["prompt_id"] == "extract.v1"
    assert task["context_snapshot"] == {"level1": "用户材料"}

    from workbench.backend.services import token_service

    summary = token_service.summary(project_id=project["id"])
    assert summary["totals"]["tokens"] == 12
    assert summary["totals"]["calls"] == 1