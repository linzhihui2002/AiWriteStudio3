"""Workbench-owned, no-port chat profile over the locked vendor runtime."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import yaml

from .dsh_paths import get_dsh_home, get_project_root

PROFILE_NAME = "ai-novel-chat"
_WRITE_LOCK = threading.Lock()

# Remove every bundled model-facing tool except the read-only skill loader.
# The runner also installs a monotonic allowlist guard, including scoped tools.
DISABLED_ROWS = (
    "hmr", "session-title-llm", "session-telemetry-otel", "agent-instructions",
    "tool-bash", "tool-pwsh", "tool-jobs", "tool-fs", "tool-fs-search",
    "tool-subagent-control", "tool-subagent-list-agents", "tool-subagent",
    "tool-subagent-fork", "tool-subagent-report", "tool-workflow", "tool-todo",
    "tool-goal", "tool-ralph", "tool-str-replace-editor", "tool-web", "plan-mode",
    "goal-round-driver", "command-goal", "command-feedback", "web-search-deepseek",
    "subagent-spawn-in-process", "subagent-fork-in-process", "workflow-worker-thread",
    "bash-sandbox", "pwsh-sandbox", "shell-env", "permission", "repeat-tool-reminder",
)


def profile_patch() -> list[dict]:
    home = get_dsh_home()
    return [
        *({"id": row, "disabled": True} for row in DISABLED_ROWS),
        {"id": "tools", "config": {"mode": "native"}},
        {"id": "approval", "config": {"policy": "never"}},
        {"id": "sandbox-policy", "config": {
            "mode": "read-only", "workspaceRoot": str(get_project_root()),
        }},
        {"id": "system-prompt", "config": {"persona": (
            "你是本地中文小说创作助手。使用工作台提供的工具读取和修改当前小说，"
            "以工具实际返回的结果为准。需要作者选择时使用 ask_user_question 提供具体选项，"
            "工具会等待作者回答后继续；其他输入由界面提供。不得声称尚未执行的修改已经完成。"
        )}},
        {"id": "skill-filesystem", "config": {
            "includeDefaultRoots": False,
            "dshHome": str(home),
            "agentsHome": str(home / "isolated-agents"),
            "customSkillDirs": [str(get_project_root() / ".dsh" / "skills")],
            "watchFollowSymlinks": False,
        }},
        {"id": "session-persistence-jsonl", "config": {
            "root": str(home / "chat-sessions"),
        }},
        {"insert": [{
            "id": "workbench-chat-runner",
            "name": Path(__file__).with_name("dsh_chat_runner.mjs").resolve().as_uri(),
        }]},
    ]


def ensure_chat_profile() -> Path:
    """Project the owned profile; return the final security overlay path.

    Applying the same file with --patch places the safety policy after any home
    patch. Existing one-shot profiles and vendor packages are never edited.
    """
    directory = get_dsh_home() / "profiles" / PROFILE_NAME
    manifest = {"name": "workbench-native-chat", "private": True,
                "dependencies": {}, "dsh": {"profile": {"bundles": ["@deepseek-ai/dsh-base"]}}}
    files = {
        "package.json": json.dumps(manifest, indent=2) + "\n",
        "cordis.patch.yml": "[]\n",
        "workbench-chat.patch.yml": yaml.safe_dump(profile_patch(), allow_unicode=True, sort_keys=False),
        "pnpm-workspace.yaml": "packages:\n  - .\nnodeLinker: hoisted\nautoInstallPeers: false\n",
    }
    with _WRITE_LOCK:
        directory.mkdir(parents=True, exist_ok=True)
        for filename, content in files.items():
            path = directory / filename
            if not path.exists() or path.read_text(encoding="utf-8") != content:
                temporary = path.with_suffix(path.suffix + ".tmp")
                temporary.write_text(content, encoding="utf-8")
                temporary.replace(path)
    return directory / "workbench-chat.patch.yml"
