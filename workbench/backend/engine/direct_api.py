"""DirectApiEngine：直连 OpenAI 兼容 API（轻量任务 / 降级路径 / 结构化输出 / 向量）。

用途（决策表见 :mod:`workbench.backend.engine.router`）：
- 结构化抽取（摄取、卡片字段补全）、<500 字轻量改写、embedding；
- dsh 不可用时的**降级路径**（核心功能不瘫痪）。

实现要点：错误归一化（:func:`normalize_http_error`）、指数退避重试 ≤2、
流式可中断、取消即时生效。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Iterator

import httpx

from .runtime import (
    BaseEngine,
    CancelledError,
    GenerationRequest,
    GenerationResult,
    estimate_tokens,
    normalize_http_error,
)

TRANSIENT_CODES = ("RATE_LIMIT", "TIMEOUT", "NETWORK_ERROR", "SERVER_ERROR")


class DirectApiEngine(BaseEngine):
    """OpenAI 兼容直连引擎。"""

    name = "direct-api"
    supports_stream = True

    def __init__(self, *, default_timeout: int = 180) -> None:
        super().__init__()
        self.default_timeout = default_timeout

    # ── 能力 ──

    def available(self) -> bool:
        from ..services.provider_service import default_provider

        try:
            provider = default_provider()
        except Exception:  # noqa: BLE001
            return False
        return bool(provider and provider["has_secret"])

    def capabilities(self) -> dict:
        return {"stream": True, "skills": False, "embed": True, "local": False}

    def get_model_status(self) -> dict:
        from ..services.provider_service import default_target, get_provider, list_providers

        try:
            target = default_target()
            provider = get_provider(target["provider_id"]) if target["provider_id"] else None
        except Exception as exc:  # noqa: BLE001
            return {"engine": self.name, "available": False, "error": str(exc)}
        return {
            "engine": self.name,
            "available": bool(provider),
            "provider": target["provider_id"],
            "model": target["model_id"],
            "health": provider["health"] if provider else "",
            "providers": [
                {
                    "provider_id": item["provider_id"],
                    "models": item["models"],
                    "enabled": item["enabled"],
                    "has_secret": item["has_secret"],
                }
                for item in list_providers()
            ],
        }

    # ── 目标解析 ──

    def _resolve_target(self, request: GenerationRequest) -> dict:
        from ..services.provider_service import default_target, resolve

        provider_id = request.provider.strip()
        model = request.model.strip()
        if not provider_id or not model:
            target = default_target()
            provider_id = provider_id or target["provider_id"]
            model = model or target["model_id"]
        if not provider_id:
            raise _EngineError("ENGINE_UNAVAILABLE", "尚未启用任何模型供应商")
        resolved = resolve(provider_id, model)
        if not resolved["model_id"]:
            raise _EngineError("MODEL_NOT_FOUND", f"供应商 {provider_id} 未指定模型")
        return resolved

    @staticmethod
    def _build_messages(request: GenerationRequest) -> list[dict]:
        messages: list[dict] = []
        if request.system.strip():
            messages.append({"role": "system", "content": request.system.strip()})
        for message in request.messages:
            role = str(message.get("role") or "user")
            content = str(message.get("content") or "")
            if role not in ("system", "user", "assistant"):
                role = "user"
            if content.strip():
                messages.append({"role": role, "content": content})
        if not messages:
            messages.append({"role": "user", "content": request.to_task_text()})
        return messages

    # ── 执行 ──

    def run_agent(
        self,
        request: GenerationRequest,
        *,
        session_id: str | None = None,
        should_cancel=None,
    ) -> GenerationResult:
        started = time.time()
        attempts = 0
        max_retries = int((request.extra or {}).get("max_retries", 2))
        last: GenerationResult | None = None

        while attempts <= max_retries:
            attempts += 1
            if self.is_cancelled(session_id, should_cancel):
                return self._cancelled(started, attempts, session_id)

            result = self._attempt(request, session_id, should_cancel, attempts, started)
            if result.ok:
                return result
            last = result
            if result.cancelled:
                return result
            if result.error_code not in TRANSIENT_CODES or attempts > max_retries:
                return result
            time.sleep(min(2.0 * attempts, 8.0))  # 指数退避

        return last or self._cancelled(started, attempts, session_id)

    def _attempt(
        self,
        request: GenerationRequest,
        session_id: str | None,
        should_cancel,
        attempts: int,
        started: float,
    ) -> GenerationResult:
        try:
            target = self._resolve_target(request)
        except _EngineError as exc:
            return GenerationResult(
                ok=False, engine=self.name, error_code=exc.code, error_message=exc.message,
                duration_ms=int((time.time() - started) * 1000), attempts=attempts,
                session_id=session_id or "",
            )

        payload: dict = {
            "model": target["model_id"],
            "messages": self._build_messages(request),
            "stream": False,
        }
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens

        timeout = request.timeout_seconds or target["timeout_seconds"] or self.default_timeout
        url = f"{target['base_url']}/chat/completions"
        try:
            response = httpx.post(
                url,
                headers={
                    "Authorization": f"Bearer {target['api_key']}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=timeout,
            )
        except httpx.TimeoutException:
            return _fail(self.name, "TIMEOUT", f"请求超时（{timeout}s）", started, attempts,
                         session_id)
        except httpx.HTTPError as exc:
            return _fail(self.name, "NETWORK_ERROR", str(exc), started, attempts, session_id)

        if response.status_code != 200:
            code, message = normalize_http_error(response.status_code, response.text[:300])
            return _fail(self.name, code, message, started, attempts, session_id)

        try:
            data = response.json()
        except ValueError:
            return _fail(self.name, "UNKNOWN", "响应不是 JSON", started, attempts, session_id)

        text = _extract_text(data)
        usage = data.get("usage") or {}
        prompt_tokens = int(usage.get("prompt_tokens") or estimate_tokens(
            "\n".join(str(m.get("content", "")) for m in payload["messages"])
        ))
        completion_tokens = int(usage.get("completion_tokens") or estimate_tokens(text))

        if request.output_path:
            _write_output(request.output_path, text)

        return GenerationResult(
            ok=True,
            text=text,
            engine=self.name,
            model=target["model_id"],
            provider=target["provider_id"],
            output_path=request.output_path,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            duration_ms=int((time.time() - started) * 1000),
            attempts=attempts,
            session_id=session_id or "",
        )

    def stream_agent(
        self,
        request: GenerationRequest,
        *,
        session_id: str | None = None,
        should_cancel=None,
    ) -> Iterator[str]:
        """流式生成（SSE）：逐段产出增量；中断时停止产出（已产出部分由调用方接管）。"""
        try:
            target = self._resolve_target(request)
        except _EngineError as exc:
            self._last_error = exc
            return

        payload: dict = {
            "model": target["model_id"],
            "messages": self._build_messages(request),
            "stream": True,
        }
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens

        timeout = request.timeout_seconds or target["timeout_seconds"] or self.default_timeout
        url = f"{target['base_url']}/chat/completions"
        buffer = ""
        try:
            with httpx.stream(
                "POST",
                url,
                headers={
                    "Authorization": f"Bearer {target['api_key']}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=timeout,
            ) as response:
                if response.status_code != 200:
                    response.read()
                    raise _EngineError(
                        *normalize_http_error(response.status_code, response.text[:200])
                    )
                for line in response.iter_lines():
                    if self.is_cancelled(session_id, should_cancel):
                        raise CancelledError()
                    if not line:
                        continue
                    if line.startswith("data:"):
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        delta = _extract_delta(chunk)
                        if delta:
                            buffer += delta
                            yield delta
        except CancelledError:
            if request.output_path and buffer:
                _write_output(request.output_path, buffer)
            return
        except (_EngineError, httpx.HTTPError) as exc:
            self._last_error = exc
            return

        if request.output_path and buffer:
            _write_output(request.output_path, buffer)

    def embed(self, texts: list[str], *, provider: str = "", model: str = "") -> list[list[float]]:
        """文本嵌入（OpenAI 兼容 ``POST /embeddings``）。"""
        target = self._resolve_target(
            GenerationRequest(provider=provider, model=model, task_type="embedding")
        )
        model_id = model or "text-embedding-3-small"
        url = f"{target['base_url']}/embeddings"
        response = httpx.post(
            url,
            headers={
                "Authorization": f"Bearer {target['api_key']}",
                "Content-Type": "application/json",
            },
            json={"model": model_id, "input": texts},
            timeout=target["timeout_seconds"],
        )
        if response.status_code != 200:
            code, message = normalize_http_error(response.status_code, response.text[:200])
            raise _EngineError(code, message)
        data = response.json()
        items = sorted(data.get("data") or [], key=lambda item: item.get("index", 0))
        return [list(item.get("embedding") or []) for item in items]

    # ── 取消 ──

    def cancel_task(self, session_id: str) -> bool:
        return self.request_cancel(session_id)

    def _cancelled(self, started: float, attempts: int, session_id: str | None) -> GenerationResult:
        return GenerationResult(
            ok=False,
            engine=self.name,
            error_code="CANCELLED",
            error_message="任务已取消",
            cancelled=True,
            duration_ms=int((time.time() - started) * 1000),
            attempts=attempts,
            session_id=session_id or "",
        )


class _EngineError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _fail(engine: str, code: str, message: str, started: float, attempts: int,
          session_id: str | None) -> GenerationResult:
    return GenerationResult(
        ok=False,
        engine=engine,
        error_code=code,
        error_message=message,
        duration_ms=int((time.time() - started) * 1000),
        attempts=attempts,
        session_id=session_id or "",
    )


def _extract_text(data: dict) -> str:
    choices = data.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, list):  # 部分网关返回分段内容
        return "".join(
            str(item.get("text") or "") for item in content if isinstance(item, dict)
        )
    return str(content or "")


def _extract_delta(chunk: dict) -> str:
    choices = chunk.get("choices") or []
    if not choices:
        return ""
    delta = choices[0].get("delta") or {}
    return str(delta.get("content") or "")


def _write_output(path: str | Path, text: str) -> None:
    from ..services.fs_utils import atomic_write_text

    atomic_write_text(Path(path), text if text.endswith("\n") else text + "\n")


__all__ = ["DirectApiEngine"]