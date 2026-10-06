"""HarnessRuntime 接口层 —— 业务代码与具体引擎之间的唯一契约。

红线（架构要求）
----------------
业务层（services / api）**只允许** import 本模块的 :class:`HarnessRuntime` 与
:class:`GenerationRequest` / :class:`GenerationResult`，
**禁止** import dsh 内部对象（``dsh_*``）或直连 HTTP 细节。
引擎实现（DshEngine / DirectApiEngine）放在本包内，通过
:func:`workbench.backend.engine.router.get_engine` 获取。

为什么用 Protocol 而不是抽象基类：两个引擎能力不同（dsh 无 embedding、
direct-api 无技能注入），Protocol 只约束**共同子集**，差异能力用能力探测
（``capabilities``）表达，避免为了统一而虚构接口。
"""

from __future__ import annotations

import time
import uuid
import re
from dataclasses import dataclass, field
from typing import Iterable, Iterator, Protocol, runtime_checkable

# ─────────────────────────── 错误归一化 ───────────────────────────

#: 引擎统一错误码（前端与重试策略只认这一套）
ERROR_CODES = (
    "AUTH_ERROR",          # 401/403 或凭据缺失
    "RATE_LIMIT",          # 429
    "TIMEOUT",             # 本地超时
    "NETWORK_ERROR",       # 连接失败/DNS
    "MODEL_NOT_FOUND",     # 模型或 provider 未配置
    "BAD_REQUEST",         # 400 参数错误
    "SERVER_ERROR",        # 5xx
    "ENGINE_UNAVAILABLE",  # vendor dsh 未安装 / 供应商未启用
    "CANCELLED",           # 用户取消
    "UNKNOWN",
)


def normalize_http_error(status: int, message: str = "") -> tuple[str, str]:
    """HTTP 状态码 → (错误码, 可读说明)。"""
    mapping = {
        400: "BAD_REQUEST",
        401: "AUTH_ERROR",
        403: "AUTH_ERROR",
        404: "MODEL_NOT_FOUND",
        408: "TIMEOUT",
        429: "RATE_LIMIT",
    }
    code = mapping.get(status)
    if code is None:
        code = "SERVER_ERROR" if status >= 500 else "UNKNOWN"
    return code, message or f"HTTP {status}"


def normalize_dsh_error(stderr: str) -> tuple[str, str]:
    """dsh stderr 首行 → (错误码, 说明)。格式为 ``dsh: <CODE>: <message>``。"""
    line = (stderr or "").strip().splitlines()[0] if (stderr or "").strip() else ""
    if not line:
        return "UNKNOWN", "dsh 执行失败（无 stderr 输出）"
    # Some vendor providers emit an HTTP status directly instead of a DSH code.
    # Match only the error prefix; incidental numbers in prose are not statuses.
    http = re.match(r"^(?:dsh:\s*)?(?:HTTP(?:\s+status)?\s*)?([45]\d{2})\s*[: -]", line, re.I)
    if http:
        return normalize_http_error(int(http[1]), line)
    if line.startswith("dsh: "):
        parts = line[len("dsh: "):].split(":", 1)
        raw_code = parts[0].strip()
        message = parts[1].strip() if len(parts) > 1 else line
        mapping = {
            "UNKNOWN_MODEL": "MODEL_NOT_FOUND",
            "NO_MODEL": "MODEL_NOT_FOUND",
            "AUTH": "AUTH_ERROR",
            "UNAUTHORIZED": "AUTH_ERROR",
            "RATE_LIMIT": "RATE_LIMIT",
            "TIMEOUT": "TIMEOUT",
            "SERVER_ERROR": "SERVER_ERROR",
            "NETWORK_ERROR": "NETWORK_ERROR",
            "BAD_REQUEST": "BAD_REQUEST",
        }
        return mapping.get(raw_code.upper(), "UNKNOWN"), message
    return "UNKNOWN", line


# ─────────────────────────── 请求 / 结果 ───────────────────────────


@dataclass
class GenerationRequest:
    """一次生成任务的完整描述（引擎无关）。

    两种产出模式：
    - **落盘模式**（推荐，dsh 走这条）：给 ``output_path``，模型需把产物写入该文件，
      stdout 只作为日志/简要确认；
    - **直返模式**：不给 ``output_path``，引擎把答案放在 :attr:`GenerationResult.text`。
    """

    task_type: str = "通用"
    system: str = ""
    messages: list[dict] = field(default_factory=list)
    project_root: str | None = None
    project_id: int | None = None
    prompt_id: str = ""
    prompt_version: str = ""
    context_files: list[str] = field(default_factory=list)
    output_path: str | None = None
    provider: str = ""
    model: str = ""
    temperature: float | None = None
    max_tokens: int | None = None
    timeout_seconds: int | None = None
    agent: str = ""
    extra: dict = field(default_factory=dict)

    def to_task_text(self, *, output_hint: str = "") -> str:
        """把请求拼成单段任务文本（dsh headless 只接受单个位置参数）。"""
        parts: list[str] = []
        if self.system.strip():
            parts.append(self.system.strip())
        for message in self.messages:
            role = str(message.get("role") or "user")
            content = str(message.get("content") or "").strip()
            if not content:
                continue
            label = {"system": "系统", "user": "用户", "assistant": "助手"}.get(role, role)
            parts.append(f"【{label}】\n{content}")
        if self.context_files:
            listing = "\n".join(f"- {path}" for path in self.context_files)
            parts.append(f"【必读文件】请先读取以下文件再作答：\n{listing}")
        if output_hint:
            parts.append(output_hint)
        return "\n\n".join(parts)


@dataclass
class GenerationResult:
    """引擎返回的统一结果（成功与失败同构，靠 ``ok`` 区分）。"""

    ok: bool
    text: str = ""
    engine: str = ""
    model: str = ""
    provider: str = ""
    output_path: str | None = None
    exit_code: int | None = None
    stderr: str = ""
    error_code: str | None = None
    error_message: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    duration_ms: int = 0
    attempts: int = 1
    session_id: str = ""
    cancelled: bool = False
    raw: dict = field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        return int(self.prompt_tokens) + int(self.completion_tokens)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "engine": self.engine,
            "model": self.model,
            "provider": self.provider,
            "output_path": self.output_path,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "duration_ms": self.duration_ms,
            "attempts": self.attempts,
            "tokens": {
                "prompt": self.prompt_tokens,
                "completion": self.completion_tokens,
                "total": self.total_tokens,
            },
            "cancelled": self.cancelled,
            "session_id": self.session_id,
        }


def new_session_id(prefix: str = "wb") -> str:
    """工作台侧会话 id（dsh headless 自己生成随机 id，此处仅作工作台留痕）。"""
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class CancelledError(RuntimeError):
    """任务被取消时抛出（引擎内部使用）。"""


@runtime_checkable
class HarnessRuntime(Protocol):
    """引擎契约：业务层只依赖这里的五个方法 + 能力探测。"""

    name: str

    def available(self) -> bool:
        """引擎当前是否可用（vendor 就绪 / 供应商已启用）。"""
        ...

    def capabilities(self) -> dict:
        """能力探测：``{"skills": bool, "stream": bool, "embed": bool, ...}``。"""
        ...

    def create_session(
        self,
        *,
        project_id: int | None = None,
        task_type: str = "",
        model: str = "",
        provider: str = "",
    ) -> str:
        """创建一个工作台侧会话标识（用于任务留痕与取消）。"""
        ...

    def run_agent(
        self,
        request: GenerationRequest,
        *,
        session_id: str | None = None,
        should_cancel=None,
    ) -> GenerationResult:
        """执行一次生成任务（阻塞直到完成/失败/取消）。"""
        ...

    def stream_agent(
        self,
        request: GenerationRequest,
        *,
        session_id: str | None = None,
        should_cancel=None,
    ) -> Iterator[str]:
        """流式生成：逐段产出文本增量（不支持流式的引擎一次性产出）。"""
        ...

    def call_tool(self, tool: str, payload: dict) -> dict:
        """调用工作台内置工具（如本地检索），不涉及 dsh 内部。"""
        ...

    def cancel_task(self, session_id: str) -> bool:
        """取消指定会话正在执行的任务。"""
        ...

    def get_model_status(self) -> dict:
        """当前引擎的模型状态（provider/model/健康情况）。"""
        ...


class BaseEngine:
    """引擎公共实现：会话登记、取消标记、计时。"""

    name = "base"
    supports_stream = False

    def __init__(self) -> None:
        self._sessions: dict[str, dict] = {}
        self._last_error: Exception | None = None

    # -- 能力 --
    def available(self) -> bool:  # pragma: no cover - 子类实现
        raise NotImplementedError

    def capabilities(self) -> dict:
        return {"stream": self.supports_stream, "skills": False, "embed": False}

    # -- 会话 --
    def create_session(
        self,
        *,
        project_id: int | None = None,
        task_type: str = "",
        model: str = "",
        provider: str = "",
    ) -> str:
        session_id = new_session_id(self.name)
        self._sessions[session_id] = {
            "project_id": project_id,
            "task_type": task_type,
            "model": model,
            "provider": provider,
            "cancel": False,
            "started_at": time.time(),
        }
        return session_id

    def _session(self, session_id: str | None) -> dict:
        if session_id and session_id in self._sessions:
            return self._sessions[session_id]
        return {}

    def request_cancel(self, session_id: str) -> bool:
        info = self._sessions.get(session_id)
        if info is None:
            return False
        info["cancel"] = True
        return True

    def cancel_task(self, session_id: str) -> bool:
        return self.request_cancel(session_id)

    def is_cancelled(self, session_id: str | None, should_cancel=None) -> bool:
        if should_cancel is not None and callable(should_cancel) and should_cancel():
            return True
        info = self._session(session_id)
        return bool(info.get("cancel"))

    def call_tool(self, tool: str, payload: dict) -> dict:
        """默认无内置工具；具体引擎可覆盖。"""
        return {"ok": False, "error": f"引擎 {self.name} 未实现工具 {tool}"}

    def get_model_status(self) -> dict:  # pragma: no cover - 子类实现
        raise NotImplementedError

    def stream_agent(
        self,
        request: GenerationRequest,
        *,
        session_id: str | None = None,
        should_cancel=None,
    ) -> Iterator[str]:
        """默认实现：非流式引擎一次性产出全文。"""
        result = self.run_agent(request, session_id=session_id, should_cancel=should_cancel)
        if result.ok and result.text:
            yield result.text

    def run_agent(  # pragma: no cover - 子类实现
        self,
        request: GenerationRequest,
        *,
        session_id: str | None = None,
        should_cancel=None,
    ) -> GenerationResult:
        raise NotImplementedError


def estimate_tokens(text: str) -> int:
    """粗略 token 估算（中文按 1 字 ≈ 1 token，英文按 4 字符 ≈ 1 token）。

    仅用于上下文预算与成本预估（真实用量以引擎返回为准）。
    """
    if not text:
        return 0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    other = len(text) - cjk
    return cjk + max(1, other // 4) if other else cjk


def collect_text(chunks: Iterable[str]) -> str:
    return "".join(chunk for chunk in chunks if chunk)


__all__ = [
    "BaseEngine",
    "CancelledError",
    "ERROR_CODES",
    "GenerationRequest",
    "GenerationResult",
    "HarnessRuntime",
    "collect_text",
    "estimate_tokens",
    "new_session_id",
    "normalize_dsh_error",
    "normalize_http_error",
]
