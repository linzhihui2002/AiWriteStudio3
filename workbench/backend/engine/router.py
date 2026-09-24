"""双引擎路由：决策表 + 三级配置覆盖 + dsh 不可用降级。

决策表（spec §4.3）
-------------------
| 任务 | 引擎 |
|---|---|
| 大纲 / 正文 / 审稿 / 润色 / 拆书 / 对话 等语义任务 | ``dsh-headless``（技能自动生效） |
| 结构化抽取 / 轻量改写（<500 字）/ embedding | ``direct-api`` |

三级覆盖（优先级由高到低）
--------------------------
1. **任务级**：本次请求显式指定（Agent 配置 / 对话内切换模型）；
2. **项目级**：``projects/{书名}/.meta/engine.json``；
3. **全局级**：``.workbench/settings.json`` 的 ``engines`` 节。
任务类型映射（overrides）先于各自的默认值生效。

降级：``dsh-headless`` 不可用（vendor 缺失 / profile 未初始化 / 未配 Key）时，
自动改用 ``direct-api``，并把降级事件写进结果 ``raw``（核心功能不瘫痪）。
"""

from __future__ import annotations

import json
from pathlib import Path

from .. import config
from ..services.errors import InvalidOperationError
from ..services.settings_service import read_settings
from .direct_api import DirectApiEngine
from .dsh_engine import DshEngine
from .runtime import GenerationRequest, GenerationResult, HarnessRuntime

# 决策表：任务类型 → 首选引擎
TASK_ROUTING: dict[str, str] = {
    "大纲生成": "dsh-headless",
    "大纲完善": "dsh-headless",
    "章节正文": "dsh-headless",
    "续写": "dsh-headless",
    "审稿": "dsh-headless",
    "一致性检查": "direct-api",
    "润色": "dsh-headless",
    "改写": "dsh-headless",
    "扩写": "dsh-headless",
    "缩写": "dsh-headless",
    "拆书": "dsh-headless",
    "对话": "dsh-headless",
    "灵感追问": "dsh-headless",
    "会话标题": "direct-api",
    "结构化抽取": "direct-api",
    "卡片补全": "direct-api",
    "摄取": "direct-api",
    "轻量改写": "direct-api",
    "embedding": "direct-api",
    "梗概摘要": "direct-api",
}

DEFAULT_ENGINE = "dsh-headless"
FALLBACK_ENGINE = "direct-api"

_ENGINES: dict[str, HarnessRuntime] = {}


def get_engine(name: str) -> HarnessRuntime:
    """按名称取引擎单例。"""
    key = (name or DEFAULT_ENGINE).strip()
    if key in _ENGINES:
        return _ENGINES[key]
    if key == "dsh-headless":
        engine: HarnessRuntime = DshEngine()
    elif key == "direct-api":
        engine = DirectApiEngine()
    else:
        raise InvalidOperationError(f"未知引擎：{name}（可选：dsh-headless / direct-api）")
    _ENGINES[key] = engine
    return engine


def engine_status() -> dict:
    """两个引擎的可用性总览（供设置页与健康检查）。"""
    result: dict = {}
    for name in ("dsh-headless", "direct-api"):
        engine = get_engine(name)
        try:
            available = engine.available()
            status = engine.get_model_status()
        except Exception as exc:  # noqa: BLE001 - 探测失败按不可用处理
            available = False
            status = {"error": str(exc)}
        result[name] = {**status, "available": bool(available)}
    return result


# ─────────────────────────── 项目级配置 ───────────────────────────


def project_engine_file(project_dir: Path) -> Path:
    return Path(project_dir) / ".meta" / "engine.json"


def read_project_engine_config(project_dir: Path | None) -> dict:
    if not project_dir:
        return {}
    path = project_engine_file(Path(project_dir))
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def write_project_engine_config(project_dir: Path, patch: dict) -> dict:
    """写项目级引擎配置（与现有配置深合并）。"""
    current = read_project_engine_config(project_dir)
    merged = {**current, **(patch or {})}
    if "overrides" in patch and isinstance(patch["overrides"], dict):
        merged["overrides"] = {**current.get("overrides", {}), **patch["overrides"]}
    path = project_engine_file(Path(project_dir))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return merged


# ─────────────────────────── 路由解析 ───────────────────────────


def resolve_route(
    task_type: str,
    *,
    project_dir: Path | None = None,
    explicit_engine: str = "",
) -> dict:
    """解析任务应走的引擎与来源（三级覆盖 + 决策表）。"""
    settings = read_settings().get("engines", {})
    global_default = str(settings.get("default") or DEFAULT_ENGINE)
    global_overrides = settings.get("overrides") or {}
    project_config = read_project_engine_config(project_dir)
    project_default = str(project_config.get("default") or "")
    project_overrides = project_config.get("overrides") or {}

    if explicit_engine:
        return {"engine": explicit_engine, "source": "task"}
    if task_type in project_overrides:
        return {"engine": str(project_overrides[task_type]), "source": "project-task"}
    if task_type in global_overrides:
        return {"engine": str(global_overrides[task_type]), "source": "global-task"}
    if task_type in TASK_ROUTING:
        return {"engine": TASK_ROUTING[task_type], "source": "decision-table"}
    if project_default:
        return {"engine": project_default, "source": "project"}
    return {"engine": global_default, "source": "global"}


def resolve_model(
    *,
    provider: str = "",
    model: str = "",
    project_dir: Path | None = None,
) -> dict:
    """解析 provider/model（任务级 → 项目级 → 全局默认模型）。"""
    project_config = read_project_engine_config(project_dir)
    resolved_provider = provider or str(project_config.get("provider") or "")
    resolved_model = model or str(project_config.get("model") or "")

    if not resolved_provider or not resolved_model:
        from ..services.provider_service import default_target

        target = default_target()
        resolved_provider = resolved_provider or target["provider_id"]
        resolved_model = resolved_model or target["model_id"]
    return {"provider": resolved_provider, "model": resolved_model}


def pick_engine(
    task_type: str,
    *,
    project_dir: Path | None = None,
    explicit_engine: str = "",
) -> tuple[HarnessRuntime, dict]:
    """取实际可用的引擎：首选不可用时降级到 direct-api。"""
    route = resolve_route(task_type, project_dir=project_dir, explicit_engine=explicit_engine)
    engine = get_engine(route["engine"])
    if engine.available():
        route["fallback"] = False
        return engine, route

    fallback = get_engine(FALLBACK_ENGINE)
    if route["engine"] != FALLBACK_ENGINE and fallback.available():
        route = {
            **route,
            "engine": FALLBACK_ENGINE,
            "requested_engine": route["engine"],
            "fallback": True,
            "reason": f"{route['engine']} 不可用，已降级到 {FALLBACK_ENGINE}",
        }
        return fallback, route

    route["fallback"] = False
    route["unavailable"] = True
    return engine, route


def annotate(request: GenerationRequest, *, project_dir: Path | None = None) -> dict:
    """把路由结果补进请求（供调用方按需）。"""
    route = resolve_route(request.task_type, project_dir=project_dir)
    model = resolve_model(provider=request.provider, model=request.model,
                          project_dir=project_dir)
    request.provider = request.provider or model["provider"]
    request.model = request.model or model["model"]
    return {**route, **model}


def describe_routing() -> dict:
    """决策表 + 当前覆盖状态（设置页展示用）。"""
    settings = read_settings().get("engines", {})
    return {
        "decision_table": TASK_ROUTING,
        "global_default": settings.get("default", DEFAULT_ENGINE),
        "global_overrides": settings.get("overrides", {}),
        "fallback_engine": FALLBACK_ENGINE,
        "engines": engine_status(),
    }


def is_streaming(engine_name: str) -> bool:
    return bool(get_engine(engine_name).capabilities().get("stream"))


def unexpected_result(engine: str, code: str, message: str) -> GenerationResult:
    """构造「未执行」结果（例如前置条件缺失）。"""
    return GenerationResult(ok=False, engine=engine, error_code=code, error_message=message)


__all__ = [
    "FALLBACK_ENGINE",
    "TASK_ROUTING",
    "annotate",
    "describe_routing",
    "engine_status",
    "get_engine",
    "is_streaming",
    "pick_engine",
    "read_project_engine_config",
    "resolve_model",
    "resolve_route",
    "unexpected_result",
    "write_project_engine_config",
]