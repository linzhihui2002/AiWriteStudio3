"""DshEngine：以 subprocess 调用 vendor 内置 dsh headless（语义任务主引擎）。

实现方式**完全依据** ``docs/compat-matrix.md`` 的三项实测结论：

- **B1** headless 无会话续接 → 每次调用都是新会话；多轮对话由工作台「每轮完整重放上下文」实现；
- **B2** 模型注入走 ``--patch``（并在 patch 中重述 provider+model，因为 patch 是整段替换）；
  ``settings.yaml`` 由工作台投影时**不写** ``agent-default-model``（否则静默压过 patch）；
- **B3** 结果判定用**退出码**（失败时 stdout 可能是空行）；产物要靠任务文本里**显式要求落盘**。

其他工程约束：cwd=项目根、注入独立 DSH_HOME、600s 默认超时、取消=进程树终止、
失败指数退避重试 ≤2、每任务 checkpoint、同项目并发 ≤2、会话文件定期清理。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Iterator

import yaml

from .. import config
from .dsh_paths import (
    DshNotInstalledError,
    build_dsh_env,
    get_dsh_binary,
    get_dsh_home,
    is_vendor_dsh_installed,
)
from .runtime import (
    BaseEngine,
    GenerationRequest,
    GenerationResult,
    normalize_dsh_error,
)

PROFILE_NAME = "ai-novel-workbench"
FALLBACK_PROFILE = "headless"

TRANSIENT_CODES = ("TIMEOUT", "NETWORK_ERROR", "SERVER_ERROR", "RATE_LIMIT")

# 产物落盘指令模板（B3：必须显式要求模型写文件）
OUTPUT_HINT = (
    "【产物落盘要求】\n"
    "请把你的最终产物（且只有产物本身，不含任何解释）写入这个文件：{path}\n"
    "使用你的文件写入工具完成写入；写完后，只回复一行：DONE\n"
    "重要：不要在回复里重复产物内容，产物必须在文件里。"
)

# 无产物文件时的答复要求（B3：答案在 stdout）
STDOUT_HINT = "【输出要求】只输出最终产物本身，不要任何寒暄、解释或 Markdown 代码围栏。"

MAX_SESSION_FILES = 200


class DshEngine(BaseEngine):
    """内置 dsh headless 引擎。"""

    name = "dsh-headless"
    supports_stream = False

    def __init__(self, *, timeout_seconds: int | None = None,
                 max_concurrency: int | None = None) -> None:
        super().__init__()
        self.timeout_seconds = int(timeout_seconds or config.DSH_TIMEOUT_SECONDS)
        self._semaphore = threading.Semaphore(max_concurrency or config.MAX_CONCURRENT_DSH)
        self._procs: dict[str, subprocess.Popen] = {}
        self._lock = threading.Lock()

    # ── 能力 ──

    def available(self) -> bool:
        if not is_vendor_dsh_installed():
            return False
        return self._profile_exists()

    def _profile_exists(self) -> bool:
        profiles = get_dsh_home() / "profiles"
        if not profiles.is_dir():
            return False
        if (profiles / PROFILE_NAME).is_dir():
            return True
        return (profiles / FALLBACK_PROFILE).is_dir()

    def _profile(self) -> str:
        profiles = get_dsh_home() / "profiles"
        if (profiles / PROFILE_NAME).is_dir():
            return PROFILE_NAME
        return FALLBACK_PROFILE

    def capabilities(self) -> dict:
        return {"stream": False, "skills": True, "embed": False, "local": True}

    def get_model_status(self) -> dict:
        from ..services.provider_service import default_target

        try:
            target = default_target()
        except Exception as exc:  # noqa: BLE001
            return {"engine": self.name, "available": False, "error": str(exc)}
        return {
            "engine": self.name,
            "available": self.available(),
            "profile": self._profile() if self.available() else "",
            "dsh_home": str(get_dsh_home()),
            "default_provider": target["provider_id"],
            "default_model": target["model_id"],
            "timeout_seconds": self.timeout_seconds,
        }

    # ── 任务文本与 patch ──

    def _task_text(self, request: GenerationRequest) -> str:
        if request.output_path:
            hint = OUTPUT_HINT.format(path=request.output_path)
        else:
            hint = STDOUT_HINT
        return request.to_task_text(output_hint=hint)

    def _patch_file(self, request: GenerationRequest, session_id: str) -> Path | None:
        """写 model patch（B2：整段替换 config，必须重述 provider + model）。"""
        provider = request.provider.strip()
        model = request.model.strip()
        if not provider or not model:
            from ..services.provider_service import default_target

            target = default_target()
            provider = provider or target["provider_id"]
            model = model or target["model_id"]
        if not provider or not model:
            return None

        patch_dir = config.logs_dir() / "patches"
        patch_dir.mkdir(parents=True, exist_ok=True)
        patch_path = patch_dir / f"{session_id}.yml"
        payload = [{"id": "agent-default-model", "config": {"provider": provider, "model": model}}]
        patch_path.write_text(
            yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
        return patch_path

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

        if not is_vendor_dsh_installed():
            return GenerationResult(
                ok=False, engine=self.name, error_code="ENGINE_UNAVAILABLE",
                error_message=str(_not_installed_error()), attempts=0,
                duration_ms=0, session_id=session_id or "",
            )
        if not self._profile_exists():
            return GenerationResult(
                ok=False, engine=self.name, error_code="ENGINE_UNAVAILABLE",
                error_message="dsh 独立 DSH_HOME 缺少 profile，请运行 setup_dsh.py 初始化。",
                attempts=0, duration_ms=0, session_id=session_id or "",
            )

        while attempts <= max_retries:
            attempts += 1
            if self.is_cancelled(session_id, should_cancel):
                return self._cancelled(started, attempts, session_id)

            result = self._attempt(request, session_id, should_cancel, attempts, started)
            if result.ok or result.cancelled:
                return result
            last = result
            if result.error_code not in TRANSIENT_CODES or attempts > max_retries:
                return result
            time.sleep(min(3.0 * attempts, 12.0))

        return last or GenerationResult(ok=False, engine=self.name,
                                        error_code="UNKNOWN", error_message="未知失败")

    def _attempt(
        self,
        request: GenerationRequest,
        session_id: str | None,
        should_cancel,
        attempts: int,
        started: float,
    ) -> GenerationResult:
        session = session_id or self.create_session(
            project_id=request.project_id, task_type=request.task_type,
            model=request.model, provider=request.provider,
        )

        cwd = Path(request.project_root) if request.project_root else config.PROJECT_ROOT
        if not cwd.is_dir():
            return GenerationResult(
                ok=False, engine=self.name, error_code="BAD_REQUEST",
                error_message=f"工作目录不存在：{cwd}", attempts=attempts,
                duration_ms=int((time.time() - started) * 1000), session_id=session,
            )

        patch_path = self._patch_file(request, session)
        command = [str(get_dsh_binary()), "--profile", self._profile()]
        if patch_path is not None:
            command += ["--patch", str(patch_path)]
        command.append(self._task_text(request))

        env = build_dsh_env()
        env["NO_COLOR"] = "1"
        timeout = int(request.timeout_seconds or self.timeout_seconds)
        checkpoint = self._write_checkpoint(session, request, command)

        with self._semaphore:  # 同项目并发 ≤2
            if self.is_cancelled(session, should_cancel):
                return self._cancelled(started, attempts, session)
            try:
                proc = subprocess.Popen(
                    command,
                    cwd=str(cwd),
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    creationflags=_creation_flags(),
                )
            except OSError as exc:
                return GenerationResult(
                    ok=False, engine=self.name, error_code="ENGINE_UNAVAILABLE",
                    error_message=f"无法启动 dsh：{exc}", attempts=attempts,
                    duration_ms=int((time.time() - started) * 1000), session_id=session,
                )

            with self._lock:
                self._procs[session] = proc
            try:
                stdout, stderr = proc.communicate(timeout=timeout)
                exit_code = proc.returncode
            except subprocess.TimeoutExpired:
                self._kill_tree(proc)
                stdout, stderr = proc.communicate()
                self._finish_checkpoint(checkpoint, "timeout")
                return GenerationResult(
                    ok=False, engine=self.name, error_code="TIMEOUT",
                    error_message=f"dsh 执行超时（{timeout}s），已终止进程树",
                    exit_code=proc.returncode, stderr=(stderr or "")[:2000],
                    attempts=attempts, session_id=session,
                    duration_ms=int((time.time() - started) * 1000),
                )
            finally:
                with self._lock:
                    self._procs.pop(session, None)

        if self.is_cancelled(session, should_cancel):
            self._finish_checkpoint(checkpoint, "cancelled")
            return self._cancelled(started, attempts, session)

        # B3：必须用退出码判定成功（失败时 stdout 可能是空行）
        if exit_code != 0:
            code, message = normalize_dsh_error(stderr or stdout or "")
            self._finish_checkpoint(checkpoint, f"failed:{code}")
            return GenerationResult(
                ok=False, engine=self.name, error_code=code, error_message=message,
                exit_code=exit_code, stderr=(stderr or "")[:2000], attempts=attempts,
                session_id=session, duration_ms=int((time.time() - started) * 1000),
            )

        text, artifact_ok, artifact_note = self._collect_output(request, stdout or "")
        self._finish_checkpoint(checkpoint, "ok" if artifact_ok else "ok:artifact-missing")

        from ..services.secret_store import redact

        from .runtime import estimate_tokens

        return GenerationResult(
            ok=True,
            text=text,
            engine=self.name,
            model=request.model,
            provider=request.provider,
            output_path=request.output_path if artifact_ok else None,
            exit_code=0,
            stderr=redact((stderr or "")[:500]),
            prompt_tokens=estimate_tokens(self._task_text(request)),
            completion_tokens=estimate_tokens(text),
            duration_ms=int((time.time() - started) * 1000),
            attempts=attempts,
            session_id=session,
            raw={"artifact_ok": artifact_ok, "artifact_note": artifact_note,
                 "stdout": redact((stdout or "")[:500])},
        )

    def _collect_output(self, request: GenerationRequest, stdout: str) -> tuple[str, bool, str]:
        """取产物：优先读落盘文件（B3 推荐路径），缺失时回退 stdout。"""
        if not request.output_path:
            return stdout.strip(), True, "stdout"

        path = Path(request.output_path)
        if path.is_file():
            try:
                content = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                return stdout.strip(), False, f"产物读取失败：{exc}"
            if content.strip():
                return content, True, "file"
            return stdout.strip(), False, "产物文件为空"

        return stdout.strip(), False, "产物文件未生成（模型未按指令落盘）"

    # ── 取消与进程树 ──

    def cancel_task(self, session_id: str) -> bool:
        self.request_cancel(session_id)
        with self._lock:
            proc = self._procs.get(session_id)
        if proc is None:
            return False
        self._kill_tree(proc)
        return True

    @staticmethod
    def _kill_tree(proc: subprocess.Popen) -> None:
        """终止整个进程树（Windows: taskkill /T /F；POSIX: killpg）。"""
        if proc.poll() is not None:
            return
        try:
            if sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    capture_output=True, check=False,
                )
            else:
                os.killpg(os.getpgid(proc.pid), 9)
        except Exception:  # noqa: BLE001 - 兜底强杀
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass

    # ── checkpoint ──

    def _checkpoint_dir(self) -> Path:
        directory = config.logs_dir() / "tasks"
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def _write_checkpoint(self, session_id: str, request: GenerationRequest,
                          command: list[str]) -> Path:
        path = self._checkpoint_dir() / f"{session_id}.json"
        payload = {
            "session_id": session_id,
            "task_type": request.task_type,
            "prompt_id": request.prompt_id,
            "prompt_version": request.prompt_version,
            "project_root": request.project_root,
            "output_path": request.output_path,
            "engine": self.name,
            "profile": self._profile(),
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "status": "running",
            "task_chars": len(self._task_text(request)),
            "argv_tail": [command[0], *command[1:-1]],  # 不落 task 全文（可能含上下文）
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    @staticmethod
    def _finish_checkpoint(path: Path, status: str) -> None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        data["status"] = status
        data["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        try:
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass

    def list_checkpoints(self, limit: int = 50) -> list[dict]:
        """最近的任务 checkpoint（供中断恢复与审计）。"""
        directory = self._checkpoint_dir()
        entries: list[dict] = []
        for path in sorted(directory.glob("*.json"), reverse=True)[:limit]:
            try:
                entries.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                continue
        return entries

    def prune_sessions(self, keep: int = MAX_SESSION_FILES,
                       protected_session_ids: set[str] | None = None) -> int:
        """清理 dsh 会话文件（B1：每次调用落一个会话，需配套清理策略）。"""
        sessions_root = get_dsh_home() / "sessions"
        if not sessions_root.is_dir():
            return 0
        # Native chat uses the separate dsh-home/chat-sessions root. An explicit
        # exclusion also protects linked sessions created by older integrations.
        from urllib.parse import quote

        protected = {quote(value, safe="-_.~") for value in (protected_session_ids or set())}
        files = sorted((path for path in sessions_root.rglob("session.jsonl*")
                        if path.parent.name not in protected), key=lambda p: p.stat().st_mtime)
        removed = 0
        stale_files = files if keep <= 0 else files[:-keep]
        for stale in stale_files:
            try:
                stale.unlink()
                removed += 1
            except OSError:
                continue
        return removed

    def _cancelled(self, started: float, attempts: int,
                   session_id: str | None) -> GenerationResult:
        return GenerationResult(
            ok=False, engine=self.name, error_code="CANCELLED",
            error_message="任务已取消（进程树已终止）", cancelled=True,
            attempts=attempts, session_id=session_id or "",
            duration_ms=int((time.time() - started) * 1000),
        )


def _creation_flags() -> int:
    """Windows 下新建进程组，便于整树终止。"""
    if sys.platform == "win32":
        return getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    return 0


def _not_installed_error() -> Exception:
    try:
        get_dsh_binary()
    except DshNotInstalledError as exc:
        return exc
    return RuntimeError("vendor dsh 不可用")


def list_recent_checkpoints(limit: int = 50) -> list[dict]:
    """模块级便捷函数（供 API 调用）。"""
    return DshEngine().list_checkpoints(limit=limit)


__all__ = ["DshEngine", "list_recent_checkpoints"]
