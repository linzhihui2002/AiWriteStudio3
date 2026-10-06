"""Persistent, streaming dsh chat using the workbench's own stdio surface.

The vendor owns model calls, native session history, and tool orchestration.
Python owns the only file-changing tools. There is no network listener here.
"""

from __future__ import annotations

import atexit
import hashlib
import json
import queue
import re
import subprocess
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Callable, Iterator

from .. import config
from .dsh_chat_profile import PROFILE_NAME, ensure_chat_profile
from .dsh_engine import DshEngine, _creation_flags
from .dsh_paths import build_dsh_env, get_dsh_cli_command, get_dsh_home, is_vendor_dsh_installed

ToolExecutor = Callable[[str, dict, str], dict]
MAX_PROTOCOL_BYTES = 8 * 1024 * 1024
TERMINAL_EVENTS = {"done", "error", "cancelled"}


class RunControl:
    """Exclude human interaction time from the model's execution budget."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._waiting_since: float | None = None
        self._paused = 0.0
        self._started = time.monotonic()
        self._last_progress = self._started
        self._timeout = float(config.DSH_TIMEOUT_SECONDS)
        self._idle_timeout = float(config.DSH_IDLE_TIMEOUT_SECONDS)
        self._stop_reason: str | None = None

    def configure(self, timeout: float, idle_timeout: float) -> None:
        self._timeout, self._idle_timeout = timeout, idle_timeout

    def progress(self) -> None:
        self._last_progress = time.monotonic() - self.paused_seconds()

    def expiration_reason(self) -> str | None:
        active_now = time.monotonic() - self.paused_seconds()
        if active_now - self._started >= self._timeout:
            return "total"
        if active_now - self._last_progress >= self._idle_timeout:
            return "idle"
        return None

    def stop(self, reason: str = "cancelled") -> None:
        # A detached tool must retain cancellation after its run has ended.
        with self._lock:
            if self._stop_reason is None:
                self._stop_reason = reason

    def should_cancel(self) -> bool:
        return self._stop_reason is not None or self.expiration_reason() is not None

    def remaining_seconds(self) -> float:
        return max(0.0, self._timeout - self.active_seconds())

    def active_seconds(self) -> float:
        return max(0.0, time.monotonic() - self._started - self.paused_seconds())

    def begin_wait(self) -> None:
        with self._lock:
            if self._waiting_since is None:
                self._waiting_since = time.monotonic()

    def end_wait(self) -> None:
        with self._lock:
            if self._waiting_since is not None:
                self._paused += time.monotonic() - self._waiting_since
                self._waiting_since = None

    def paused_seconds(self) -> float:
        with self._lock:
            return self._paused + (time.monotonic() - self._waiting_since
                                   if self._waiting_since is not None else 0.0)


def _safe_error(message: str) -> str:
    from ..services.secret_store import redact
    return redact(str(message))[:3000]


def _safe_provider_error(event: dict) -> dict:
    """Keep diagnostics, distinguishing explicit exhausted quota from auth."""
    safe = {key: value for key, value in event.items() if key != "protocol"}
    message = _safe_error(event.get("message", "对话失败。"))
    code = str(event.get("code") or "DSH_CHAT_ERROR")
    # A throttled request remains a rate-limit error even if its body mentions
    # quota; only explicit provider balance failures receive QUOTA.
    if code == "RATE_LIMIT" or event.get("status") == 429 or event.get("status_code") == 429:
        code = "RATE_LIMIT"
    elif re.search(r"insufficient_(?:user_)?quota|(?:余额|额度)不足|预扣费额度失败", message, re.I):
        code = "QUOTA"
    return {**safe, "code": code, "message": message}


def _provider_config_revision() -> tuple[bytes | None, bytes | None]:
    """Identify the exact isolated provider config seen by a native worker.

    Hashes are kept in memory only. They let an idle native session cold-resume
    after a provider or credential change without exposing either credential.
    """
    home = get_dsh_home()
    result = []
    for name in ("settings.yaml", ".credentials.yaml"):
        try:
            result.append(hashlib.sha256((home / name).read_bytes()).digest())
        except FileNotFoundError:
            result.append(None)
    return tuple(result)


class DshChatError(RuntimeError):
    def __init__(self, message: str, code: str = "DSH_CHAT_ERROR") -> None:
        super().__init__(message)
        self.code = code


class _Worker:
    def __init__(self, command: list[str], project_root: str, env: dict[str, str],
                 provider_revision: tuple[bytes | None, bytes | None]) -> None:
        self.project_root = str(Path(project_root).resolve())
        self.provider_revision = provider_revision
        self.lock = threading.Lock()
        self.write_lock = threading.Lock()
        self.events: queue.Queue[dict] = queue.Queue()
        self.stderr: deque[str] = deque(maxlen=20)
        self.initialized = False
        self.last_used = time.monotonic()
        self.proc = subprocess.Popen(
            command, cwd=self.project_root, env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            creationflags=_creation_flags(),
        )
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def _read_stdout(self) -> None:
        assert self.proc.stdout is not None
        try:
            while line := self.proc.stdout.readline(MAX_PROTOCOL_BYTES + 1):
                if len(line.encode("utf-8")) > MAX_PROTOCOL_BYTES:
                    self.events.put({"type": "error", "code": "DSH_PROTOCOL_ERROR", "message": "对话事件过大。"})
                    break
                try:
                    value = json.loads(line)
                    if not isinstance(value, dict) or value.get("protocol") != 1:
                        raise ValueError("invalid event envelope")
                except (ValueError, TypeError):
                    self.events.put({"type": "error", "code": "DSH_PROTOCOL_ERROR", "message": "内置对话引擎返回了无效事件。"})
                    break
                self.events.put(value)
        except (OSError, ValueError):
            pass  # Owner teardown may close a pipe while its reader is draining.
        finally:
            self.events.put({"type": "_eof"})

    def _read_stderr(self) -> None:
        assert self.proc.stderr is not None
        try:
            for line in self.proc.stderr:
                self.stderr.append(_safe_error(line))
        except (OSError, ValueError):
            pass

    def send(self, payload: dict) -> None:
        raw = json.dumps(payload, ensure_ascii=False, allow_nan=False) + "\n"
        if len(raw.encode("utf-8")) > MAX_PROTOCOL_BYTES:
            raise DshChatError("对话请求过大，请缩小本轮上下文。", "DSH_REQUEST_TOO_LARGE")
        with self.write_lock:
            if self.proc.poll() is not None or self.proc.stdin is None:
                raise DshChatError("内置对话进程已退出，可重试以恢复会话。", "DSH_PROCESS_EXITED")
            self.proc.stdin.write(raw)
            self.proc.stdin.flush()

    def receive(self, timeout: float = 0.1) -> dict | None:
        try:
            event = self.events.get(timeout=timeout)
        except queue.Empty:
            return None
        if event.get("type") == "_eof":
            detail = "".join(self.stderr).strip()
            raise DshChatError(detail or "内置对话进程意外退出，可重试以恢复会话。", "DSH_PROCESS_EXITED")
        return event

    def close(self, *, force: bool = False) -> None:
        if self.proc.poll() is None and not force:
            try:
                self.send({"type": "shutdown"})
                self.proc.wait(timeout=5)
            except (OSError, ValueError, DshChatError, subprocess.TimeoutExpired):
                pass
        if self.proc.poll() is None:
            DshEngine._kill_tree(self.proc)
        try:
            self.proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass
        for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            if stream is not None:
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass


class DshChatRuntime:
    """One live worker per native conversation; cold workers resume from disk.

    Events: session_ready, delta, tool_call, tool_result, usage, and exactly one
    done/error/cancelled. execute_tool receives (name, arguments, call_id).
    Callers store session_ready.session_id before considering the turn durable.
    """

    def __init__(self, *, timeout_seconds: float | None = None,
                 idle_timeout_seconds: float | None = None, max_workers: int = 4) -> None:
        self.timeout_seconds = float(timeout_seconds or config.DSH_TIMEOUT_SECONDS)
        self.idle_timeout_seconds = float(idle_timeout_seconds or config.DSH_IDLE_TIMEOUT_SECONDS)
        self.max_workers = max_workers
        self._workers: dict[str, _Worker] = {}
        self._lock = threading.Lock()

    def available(self) -> bool:
        return is_vendor_dsh_installed()

    def _command(self) -> list[str]:
        overlay = ensure_chat_profile()
        return [*get_dsh_cli_command(), "--profile", PROFILE_NAME, "--patch", str(overlay)]

    def _environment(self) -> dict[str, str]:
        env = build_dsh_env()
        env.update(NO_COLOR="1", DSH_PERMISSION_MODE="read-only", DSH_TOOLS_MODE="native", DSH_TELEMETRY_DISABLED="1")
        return env

    def _worker(self, session_id: str, project_root: str) -> _Worker:
        root = str(Path(project_root).resolve())
        if not Path(root).is_dir():
            raise DshChatError("当前小说目录不存在。", "BAD_PROJECT_ROOT")
        with self._lock:
            worker = self._workers.get(session_id)
            if worker is not None and worker.project_root != root:
                raise DshChatError("原生会话不能跨小说使用。", "SESSION_PROJECT_MISMATCH")
            revision = _provider_config_revision()
            if worker is not None and worker.provider_revision != revision:
                if worker.lock.locked():
                    raise DshChatError("同一会话正在处理上一条消息。", "DSH_SESSION_BUSY")
                self._workers.pop(session_id)
                worker.close()
                worker = None
            if worker is not None and worker.proc.poll() is not None:
                self._workers.pop(session_id)
                worker.close(force=True)
                worker = None
            if worker is None:
                # Idle eviction flushes the agent; a later turn resumes it.
                if len(self._workers) >= self.max_workers:
                    idle = sorted(((key, candidate) for key, candidate in self._workers.items()
                                   if not candidate.lock.locked()), key=lambda item: item[1].last_used)
                    if idle:
                        key, candidate = idle[0]
                        self._workers.pop(key)
                        candidate.close()
                    else:
                        raise DshChatError("当前对话任务较多，请等待一个任务结束后重试。", "DSH_BUSY")
                worker = _Worker(self._command(), root, self._environment(), revision)
                self._workers[session_id] = worker
            # Reserve while holding the registry lock so another conversation
            # cannot evict this worker between lookup and starting its turn.
            if not worker.lock.acquire(blocking=False):
                raise DshChatError("同一会话正在处理上一条消息。", "DSH_SESSION_BUSY")
            return worker

    def stream(self, *, session_id: str, resume: bool, project_root: str,
               user_text: str, provider: str, model: str, tool_specs: list[dict],
               execute_tool: ToolExecutor, system: str = "", context: str = "",
               should_cancel: Callable[[], bool] | None = None,
               message_id: int | None = None, history: list[dict] | None = None,
               wait_control: RunControl | None = None) -> Iterator[dict]:
        worker = None
        acquired = False
        terminal = False
        turn_sent = False
        run_id = uuid.uuid4().hex
        cancelled_at = None
        timeout_reason: str | None = None
        control = wait_control or RunControl()
        control.configure(self.timeout_seconds, self.idle_timeout_seconds)
        deadline = control._started + self.timeout_seconds
        cancelled = should_cancel or (lambda: False)
        try:
            if not session_id or not provider or not model or not user_text.strip():
                raise DshChatError("会话、模型和消息不能为空。", "BAD_REQUEST")
            if cancelled():
                terminal = True
                yield {"type": "cancelled", "session_id": session_id, "text": ""}
                return
            worker = self._worker(session_id, project_root)
            acquired = True
            fields = {"provider": provider, "model": model, "system": system,
                      "context": context, "tool_specs": tool_specs}
            if not worker.initialized:
                init_sent = False
                while not worker.initialized:
                    if cancelled():
                        terminal = True
                        self._discard(session_id, worker)
                        yield {"type": "cancelled", "session_id": session_id, "text": ""}
                        return
                    if time.monotonic() - control.paused_seconds() >= deadline or time.monotonic() >= worker.last_used + 45:
                        raise DshChatError("内置对话引擎启动超时。", "DSH_STARTUP_TIMEOUT")
                    event = worker.receive()
                    if event is None:
                        continue
                    kind = event.get("type")
                    if kind == "ready" and not init_sent:
                        worker.send({"type": "init", "session_id": session_id, "resume": resume,
                                     "project_root": worker.project_root, **fields})
                        init_sent = True
                    elif kind == "session_ready":
                        worker.initialized = True
                        yield {key: value for key, value in event.items() if key != "protocol"}
                    elif kind == "error":
                        error = _safe_provider_error(event)
                        raise DshChatError(error["message"], error["code"])
            else:
                yield {"type": "session_ready", "session_id": session_id, "resumed": True}

            worker.send({"type": "turn", "run_id": run_id, "user_text": user_text,
                         "message_id": message_id, "history": history or [], **fields})
            turn_sent = True
            last_progress_active = time.monotonic() - control.paused_seconds()
            control.progress()
            seen_activity: set[tuple[str, str]] = set()

            def timeout_event() -> dict:
                seconds = self.timeout_seconds if timeout_reason == "total" else self.idle_timeout_seconds
                duration = f"{seconds / 60:g} 分钟" if seconds >= 60 else f"{seconds:g} 秒"
                message = (f"对话活跃执行已达 {duration}上限，已停止；已完成的文件改动仍保留。"
                           if timeout_reason == "total" else
                           f"对话连续 {duration}没有模型输出或工具进展，已停止；已完成的文件改动仍保留。")
                return {"type": "error", "code": "TIMEOUT", "reason": timeout_reason, "message": message}

            while True:
                now = time.monotonic()
                active_now = now - control.paused_seconds()
                if cancelled_at is None and (cancelled() or active_now >= deadline or
                                             active_now - last_progress_active >= self.idle_timeout_seconds):
                    if not cancelled():
                        timeout_reason = "total" if active_now >= deadline else "idle"
                    cancelled_at = now
                    control.stop(timeout_reason or "cancelled")
                    worker.send({"type": "cancel"})
                if cancelled_at is not None and now - cancelled_at > 5:
                    self._discard(session_id, worker)
                    terminal = True
                    yield (timeout_event() if timeout_reason else
                           {"type": "cancelled", "session_id": session_id, "text": ""})
                    return
                event = worker.receive()
                if event is None:
                    continue
                if event.get("run_id") not in (None, run_id):
                    continue
                kind = event.get("type")
                if kind == "tool_request":
                    last_progress_active = time.monotonic() - control.paused_seconds()
                    control.progress()
                    if cancelled_at is not None or cancelled():
                        result = {"ok": False, "summary": "任务已取消，未执行工具。", "error": "CANCELLED"}
                    else:
                        # Tools may wait for a provider or filesystem operation.
                        # Keep the run watchdog alive while they execute; the
                        # shared control also guards the final file commit.
                        results: queue.Queue[dict] = queue.Queue(maxsize=1)
                        request_event = dict(event)
                        def run_tool() -> None:
                            try:
                                value = execute_tool(str(request_event["name"]), request_event.get("arguments") or {}, str(request_event["call_id"]))
                                json.dumps(value, ensure_ascii=False, allow_nan=False)
                            except Exception as exc:
                                value = {"ok": False, "summary": _safe_error(str(exc)), "error": "TOOL_ERROR"}
                            results.put(value)
                        threading.Thread(target=run_tool, daemon=True, name="chat-tool-" + str(event["call_id"])[:16]).start()
                        while True:
                            reason = control.expiration_reason()
                            if reason or cancelled():
                                timeout_reason = reason
                                control.stop(reason or "cancelled")
                                worker.send({"type": "cancel"})
                                self._discard(session_id, worker, force=True)
                                terminal = True
                                yield (timeout_event() if reason else
                                       {"type": "cancelled", "session_id": session_id, "text": ""})
                                return
                            try:
                                result = results.get(timeout=0.05)
                                break
                            except queue.Empty:
                                continue
                    worker.send({"type": "tool_response", "request_id": event["request_id"], "result": result})
                    last_progress_active = time.monotonic() - control.paused_seconds()
                    control.progress()
                    continue
                if kind in TERMINAL_EVENTS:
                    terminal = True
                    worker.last_used = time.monotonic()
                    if timeout_reason:
                        yield timeout_event()
                    elif kind == "error":
                        yield _safe_provider_error(event)
                    else:
                        yield {key: value for key, value in event.items() if key != "protocol"}
                    return
                if kind in {"delta", "tool_call", "tool_result", "usage", "delivery_ack", "activity", "compaction"}:
                    if ((kind == "delta" and str(event.get("text") or "").strip()) or
                            kind in {"tool_call", "tool_result"}):
                        last_progress_active = time.monotonic() - control.paused_seconds()
                        control.progress()
                    elif kind in {"activity", "compaction"}:
                        marker = (str(event.get("activity_id") or ""), str(event.get("status") or ""))
                        if marker not in seen_activity:
                            seen_activity.add(marker)
                            last_progress_active = time.monotonic() - control.paused_seconds()
                            control.progress()
                    yield {key: value for key, value in event.items() if key != "protocol"}
        except (DshChatError, OSError, ValueError) as exc:
            if worker is not None and acquired:
                self._discard(session_id, worker)
            terminal = True
            yield {"type": "error", "code": getattr(exc, "code", "DSH_UNAVAILABLE"), "message": _safe_error(str(exc))}
        finally:
            # A disconnected SSE consumer must not leave an unseen turn editing.
            if worker is not None and acquired:
                if turn_sent and not terminal:
                    self._discard(session_id, worker)
                worker.lock.release()

    def _discard(self, session_id: str, worker: _Worker, *, force: bool = False) -> None:
        with self._lock:
            if self._workers.get(session_id) is worker:
                self._workers.pop(session_id, None)
        worker.close(force=force)

    def close(self, session_id: str) -> None:
        with self._lock:
            worker = self._workers.pop(session_id, None)
        if worker is not None:
            worker.close()

    def close_all(self) -> None:
        with self._lock:
            workers = list(self._workers.values())
            self._workers.clear()
        for worker in workers:
            worker.close()


_runtime = DshChatRuntime()
atexit.register(_runtime.close_all)


def get_dsh_chat_runtime() -> DshChatRuntime:
    return _runtime
