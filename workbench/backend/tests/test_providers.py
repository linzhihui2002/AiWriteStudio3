"""模型供应商：多模型列表 / 全局默认模型 / dsh 投影。

覆盖 ``provider_service`` 的模型列表存储、默认模型三级解析与投影契约
（投影**绝不**写 ``agent-default-model``）。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from workbench.backend import config, db
from workbench.backend.engine import dsh_paths, router
from workbench.backend.services import provider_service
from workbench.backend.services.errors import InvalidOperationError


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
    # 投影落点沙箱化，绝不写到真实 .workbench/dsh-home
    monkeypatch.setattr(dsh_paths, "DSH_HOME_DIR", runtime / "dsh-home")
    db.init_db()
    config.ensure_runtime_dirs()
    monkeypatch.setattr(router.get_engine("dsh-headless"), "available", lambda: False)
    return SimpleNamespace(projects=projects, runtime=runtime, root=tmp_path)


# ─────────────────────────── 多模型 ───────────────────────────


def test_provider_multiple_models_round_trip(workspace: SimpleNamespace) -> None:
    provider_service.upsert_provider(
        "multi",
        display_name="多家模型",
        base_url="https://api.example.com/v1",
        models=[
            {"id": "big-model", "name": "大模型", "context_window": 262144, "max_tokens": 32768},
            {"id": "small-model"},
        ],
        api_key="sk-test-abcdefgh",
        enabled=True,
    )

    provider = provider_service.get_provider("multi")
    assert provider["model_count"] == 2
    assert [model["id"] for model in provider["models"]] == ["big-model", "small-model"]
    assert provider["models"][0]["name"] == "大模型"
    assert provider["models"][0]["context_window"] == 262144
    assert provider["models"][0]["max_tokens"] == 32768
    # 未填参数回落默认值
    assert provider["models"][1]["context_window"] == provider_service.DEFAULT_CONTEXT_WINDOW
    assert provider["models"][1]["max_tokens"] == provider_service.DEFAULT_MAX_TOKENS
    assert provider_service.primary_model_id(provider) == "big-model"


def test_legacy_model_id_column_falls_back(workspace: SimpleNamespace) -> None:
    """旧库残留的 model_id 列在 models 为空时合成单模型。"""
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO providers (provider_id, display_name, source, base_url,"
            " model_id, models, capabilities, secret_ref, timeout_seconds, retry_policy,"
            " enabled) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("legacy", "旧库供应商", "workbench_custom", "https://legacy.local/v1",
             "legacy-model", None, "[]", "", 60, "{}", 1),
        )

    provider = provider_service.get_provider("legacy")
    assert provider["model_count"] == 1
    assert provider["models"][0]["id"] == "legacy-model"


# ─────────────────────────── 全局默认模型 ───────────────────────────


def test_set_default_target_validates_and_clears(workspace: SimpleNamespace) -> None:
    provider_service.upsert_provider(
        "p1", base_url="https://p1.local/v1",
        models=[{"id": "m1"}, {"id": "m2"}], api_key="sk-test-abcdefgh", enabled=True,
    )

    assert provider_service.get_default_target() == {"provider_id": "", "model_id": ""}

    # model_id 留空 → 取该供应商首个模型
    assert provider_service.set_default_target("p1", "") == {"provider_id": "p1", "model_id": "m1"}
    assert provider_service.get_default_target() == {"provider_id": "p1", "model_id": "m1"}

    assert provider_service.set_default_target("p1", "m2") == {"provider_id": "p1", "model_id": "m2"}

    with pytest.raises(InvalidOperationError):
        provider_service.set_default_target("p1", "不存在")

    # 空 provider_id → 清空
    assert provider_service.set_default_target("", "") == {"provider_id": "", "model_id": ""}
    assert provider_service.get_default_target() == {"provider_id": "", "model_id": ""}


def test_default_target_falls_back_when_default_provider_disabled(
    workspace: SimpleNamespace,
) -> None:
    provider_service.upsert_provider(
        "p1", base_url="https://p1.local/v1",
        models=[{"id": "m1"}], api_key="sk-test-abcdefgh", enabled=False,
    )
    provider_service.upsert_provider(
        "p2", base_url="https://p2.local/v1",
        models=[{"id": "m9"}], api_key="sk-test-abcdefgh", enabled=True,
    )

    provider_service.set_default_target("p1", "m1")
    # 停用默认供应商 → 默认被清空 → 回退到第一个启用的供应商
    provider_service.set_enabled("p1", False)
    assert provider_service.get_default_target() == {"provider_id": "", "model_id": ""}
    assert provider_service.default_target() == {"provider_id": "p2", "model_id": "m9"}

    # 删除默认供应商同样自动清空
    provider_service.set_default_target("p2", "m9")
    provider_service.delete_provider("p2")
    assert provider_service.default_target() == {"provider_id": "", "model_id": ""}


# ─────────────────────────── dsh 投影 ───────────────────────────


def test_project_writes_all_models_without_agent_default_model(
    workspace: SimpleNamespace,
) -> None:
    provider_service.upsert_provider(
        "alpha", display_name="Alpha", base_url="https://alpha.local/v1",
        models=[
            {"id": "a-1", "name": "A1", "context_window": 262144, "max_tokens": 32768},
            {"id": "a-2"},
        ],
        api_key="sk-alpha-12345678", enabled=True,
    )
    provider_service.upsert_provider(
        "beta", display_name="Beta", base_url="https://beta.local/v1",
        models=[{"id": "b-1"}], api_key="sk-beta-12345678", enabled=True,
    )
    provider_service.upsert_provider(
        "off", display_name="Off", base_url="https://off.local/v1",
        models=[{"id": "o-1"}], api_key="sk-off-12345678", enabled=False,
    )

    result = provider_service.project_to_dsh_home()
    assert result["projected"] == 2
    assert result["agent_default_model_written"] is False

    settings_path = Path(result["settings_path"])
    settings = yaml.safe_load(settings_path.read_text(encoding="utf-8"))
    providers = settings["llm-pi-ai"]["providers"]

    assert set(providers) == {"alpha", "beta"}
    assert [model["id"] for model in providers["alpha"]["models"]] == ["a-1", "a-2"]
    assert providers["alpha"]["models"][0]["contextWindow"] == 262144
    assert providers["alpha"]["models"][0]["maxTokens"] == 32768
    assert providers["alpha"]["models"][1]["contextWindow"] == (
        provider_service.DEFAULT_CONTEXT_WINDOW
    )
    assert providers["alpha"]["apiKeyEnv"] == "WB_ALPHA_API_KEY"
    # 红线：投影绝不写 agent-default-model
    assert "agent-default-model" not in settings

    credentials = yaml.safe_load(
        Path(result["credentials_path"]).read_text(encoding="utf-8")
    )
    assert credentials["WB_ALPHA_API_KEY"] == "sk-alpha-12345678"


def test_editing_enabled_provider_updates_native_projection_immediately(
    workspace: SimpleNamespace,
) -> None:
    provider_service.upsert_provider(
        "Fluxl", base_url="https://first.example/v1", models=[{"id": "glm-old"}],
        api_key="sk-first-12345678", enabled=True,
    )
    settings_path, credentials_path = provider_service.dsh_home_paths()
    first = yaml.safe_load(settings_path.read_text(encoding="utf-8"))
    assert first["llm-pi-ai"]["providers"]["Fluxl"]["models"][0]["id"] == "glm-old"
    first["unrelated-settings"] = {"keep": True}
    first["agent-default-model"] = {"provider": "wrong"}
    settings_path.write_text(yaml.safe_dump(first), encoding="utf-8")
    credentials_path.write_text(
        credentials_path.read_text(encoding="utf-8") + "OTHER_API_KEY: preserve-me\n",
        encoding="utf-8",
    )

    # Saving an already-enabled provider is the settings-page path; it never
    # calls set_enabled again and previously left native dsh without the route.
    provider_service.upsert_provider(
        "Fluxl", base_url="https://second.example/v1",
        models=[{"id": "glm-5.3-flash"}], api_key="sk-second-12345678",
        enabled=True,
    )
    second = yaml.safe_load(settings_path.read_text(encoding="utf-8"))
    route = second["llm-pi-ai"]["providers"]["Fluxl"]
    assert route["baseURL"] == "https://second.example/v1"
    assert [model["id"] for model in route["models"]] == ["glm-5.3-flash"]
    assert second["unrelated-settings"] == {"keep": True}
    assert "agent-default-model" not in second
    assert yaml.safe_load(credentials_path.read_text(encoding="utf-8"))[
        "WB_FLUXL_API_KEY"
    ] == "sk-second-12345678"
    assert yaml.safe_load(credentials_path.read_text(encoding="utf-8"))[
        "OTHER_API_KEY"
    ] == "preserve-me"

    provider_service.set_enabled("Fluxl", False)
    disabled = yaml.safe_load(settings_path.read_text(encoding="utf-8"))
    assert "Fluxl" not in disabled["llm-pi-ai"]["providers"]
    assert "WB_FLUXL_API_KEY" not in yaml.safe_load(
        credentials_path.read_text(encoding="utf-8")
    )


def test_auth_denial_recovers_provider_reason_without_echoing_key(
    workspace: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider_service.upsert_provider(
        "Fluxl", base_url="https://gateway.example/v1", models=[{"id": "glm"}],
        api_key="sk-private-12345678", enabled=True,
    )

    class Response:
        status_code = 403

        def json(self):
            return {"code": "GROUP_DELETED", "message": "API Key 所属分组已删除"}

    observed = {}

    def fake_get(url, *, headers, timeout):
        observed.update(url=url, timeout=timeout, authorized=headers["Authorization"].startswith("Bearer "))
        return Response()

    monkeypatch.setattr(provider_service.httpx, "get", fake_get)
    message = provider_service.explain_auth_denial("Fluxl")
    assert "GROUP_DELETED" in message and "API Key 所属分组已删除" in message
    assert "sk-private-12345678" not in message
    assert observed == {"url": "https://gateway.example/v1/models", "timeout": 5, "authorized": True}
