"""工作台后端配置：路径定位与端口策略。

隔离红线（与用户本机 DeepSeekHarness 零冲突共存）：
- 绝不读写 `C:\\Users\\<user>\\.dsh` 下的任何内容；
- 绝不使用 3080（dsh web）、8765、3456（用户本地代理）等端口。
"""

from __future__ import annotations

import os
from pathlib import Path

VERSION = "0.1.0"

# 绑定地址固定本机回环，不对外暴露
HOST = "127.0.0.1"

DEFAULT_PORT = 8790
PORT_ENV_VAR = "WORKBENCH_PORT"

# 与 dsh web（3080）及既有本地服务严格隔离的禁用端口集合
FORBIDDEN_PORTS: frozenset[int] = frozenset({3080, 8765, 3456})


def find_project_root(start: Path | None = None) -> Path:
    """从本文件向上查找含 `workbench/` 子目录的项目根。"""
    current = (start or Path(__file__)).resolve()
    if current.is_file():
        current = current.parent
    for candidate in (current, *current.parents):
        if (candidate / "workbench").is_dir():
            return candidate
    raise RuntimeError(
        f"无法定位项目根：从 {current} 向上未找到包含 workbench/ 的目录"
    )


PROJECT_ROOT = find_project_root()

# 代码与构建产物
WORKBENCH_DIR = PROJECT_ROOT / "workbench"
BACKEND_DIR = WORKBENCH_DIR / "backend"
FRONTEND_DIST_DIR = WORKBENCH_DIR / "frontend" / "dist"

# 运行时目录（已加入 .gitignore，可随时删除重建）
RUNTIME_DIR = PROJECT_ROOT / ".workbench"
DB_PATH = RUNTIME_DIR / "workbench.db"

# 小说项目事实源根目录（Markdown 落盘于此）
PROJECTS_DIR = PROJECT_ROOT / "projects"

# ── 受管资产目录（版本化落盘，属仓库内容） ──
# 一律用函数形式取路径：便于测试用 monkeypatch 覆盖基目录，且始终读到最新值。


def agents_dir() -> Path:
    return Path(PROJECT_ROOT) / "agents"


def rules_dir() -> Path:
    return Path(PROJECT_ROOT) / "rules"


def workflows_dir() -> Path:
    return Path(PROJECT_ROOT) / "workflows"


def templates_dir() -> Path:
    return Path(PROJECT_ROOT) / "templates"


def skills_dir() -> Path:
    return Path(PROJECT_ROOT) / "skills"


def dsh_skills_dir() -> Path:
    """dsh 项目级技能根（同步产物，禁止手工编辑）。"""
    return Path(PROJECT_ROOT) / ".dsh" / "skills"


# ── 运行时子目录（可随时删除重建） ──


def runtime_dir() -> Path:
    return Path(RUNTIME_DIR)


def snapshots_dir() -> Path:
    return runtime_dir() / "snapshots"


def exports_dir() -> Path:
    return runtime_dir() / "exports"


def logs_dir() -> Path:
    return runtime_dir() / "logs"


def models_dir() -> Path:
    return runtime_dir() / "models"


def vector_dir() -> Path:
    return runtime_dir() / "vector"


def images_dir() -> Path:
    """生图工坊产物根目录（图片 + sidecar JSON + 参考图暂存）。"""
    return runtime_dir() / "images"


def secrets_path() -> Path:
    return runtime_dir() / "secrets.json"


def settings_path() -> Path:
    return runtime_dir() / "settings.json"


# 兼容别名（多数调用方只想读常量）
SNAPSHOTS_DIR = RUNTIME_DIR / "snapshots"
EXPORTS_DIR = RUNTIME_DIR / "exports"
LOGS_DIR = RUNTIME_DIR / "logs"
MODELS_DIR = RUNTIME_DIR / "models"
VECTOR_DIR = RUNTIME_DIR / "vector"
SECRETS_PATH = RUNTIME_DIR / "secrets.json"
SETTINGS_PATH = RUNTIME_DIR / "settings.json"

# ── 生成任务参数（唯一事实源，供引擎与管线共用） ──
DEFAULT_WORD_BUDGET = 3000          # 单章默认字数预算
MIN_CHAPTER_WORDS = 2000            # 字数门下限
DSH_TIMEOUT_SECONDS = 1800          # 对话活跃执行总时长；等待作者回应不计入
DSH_IDLE_TIMEOUT_SECONDS = 600      # 连续无模型文本或工具进展的活跃时长
DIRECT_API_TIMEOUT_SECONDS = 180    # 直连 API 超时
MAX_CONCURRENT_DSH = 2              # 同项目 dsh 并发上限
MILESTONE_STEP_DEFAULT = 500        # 字数里程碑阈值（每 N 字）
SMALL_EDIT_THRESHOLD = 500          # 「修改模式优先」的字数阈值
CHAT_MAX_TOOL_STEPS = 6             # 对话单轮允许的工具调用步数上限
CHAT_NATIVE_MAX_TOOL_CALLS = 40     # 原生持续对话每轮结构化工具调用预算


def resolve_port() -> int:
    """解析端口：默认 8790，环境变量 WORKBENCH_PORT 可覆盖；禁用端口直接拒绝。"""
    raw = os.environ.get(PORT_ENV_VAR)
    if raw is None or raw.strip() == "":
        port = DEFAULT_PORT
    else:
        try:
            port = int(raw.strip())
        except ValueError as exc:
            raise ValueError(
                f"{PORT_ENV_VAR} 必须是整数，当前值：{raw!r}"
            ) from exc

    if not 1 <= port <= 65535:
        raise ValueError(f"端口必须在 1-65535 之间，当前值：{port}")

    if port in FORBIDDEN_PORTS:
        raise RuntimeError(
            f"端口 {port} 属于禁用端口 {sorted(FORBIDDEN_PORTS)}"
            f"（3080=dsh web，8765=novel-harness Studio，3456=用户本地代理）。"  # compliance-ok: 端口用途说明，非引入
            f"工作台固定使用 {DEFAULT_PORT}，拒绝启动。"
        )

    return port


def ensure_runtime_dirs() -> None:
    """创建运行时目录（幂等）。"""
    for directory in (
        runtime_dir(),
        snapshots_dir(),
        exports_dir(),
        logs_dir(),
        models_dir(),
        vector_dir(),
        images_dir(),
    ):
        Path(directory).mkdir(parents=True, exist_ok=True)
