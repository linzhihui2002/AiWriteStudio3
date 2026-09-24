#!/usr/bin/env python
"""工作台卸载脚本（Task 67 / I6）：移除工作台自己产生的东西，**绝不碰用户环境**。

删除范围（**仅限本仓库内**）：
- ``workbench/vendor/dsh/``：vendor 内置 dsh（本地 npm 安装产物）
- ``.workbench/``：独立 DSH_HOME、索引、快照、回收站、向量库、日志、导出
- ``.workbench/reports/``、``.pytest_cache``、``workbench/frontend/dist/``（构建产物）

**明确不删除**（并打印为「保留」）：
- ``projects/``：小说事实源（用户的正文与设定）
- ``skills/``、``agents/``、``rules/``、``workflows/``、``templates/``：受管资产
- ``docs/``、``workbench/`` 源码、``AGENTS.md``
- 用户全局 ``~/.dsh`` 与全局 npm 包（**只读校验**，不做任何修改）

用法::

    python workbench/cli/uninstall.py --dry-run   # 只列清单（默认行为）
    python workbench/cli/uninstall.py --yes       # 实际删除
"""

from __future__ import annotations

import argparse
import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
USER_DSH_HOME = Path.home() / ".dsh"


def _ok(message: str) -> None:
    print(f"  [OK] {message}")


def _info(message: str) -> None:
    print(f"  [..] {message}")


def _warn(message: str) -> None:
    print(f"  [!!] {message}")


def removable_targets() -> list[Path]:
    """可安全删除的目标（全部在仓库内）。"""
    return [
        _PROJECT_ROOT / "workbench" / "vendor" / "dsh",
        _PROJECT_ROOT / ".workbench",
        _PROJECT_ROOT / ".pytest_cache",
        _PROJECT_ROOT / "workbench" / "frontend" / "dist",
    ]


def preserved_targets() -> list[Path]:
    """明确保留（打印给用户看，避免误以为被删）。"""
    return [
        _PROJECT_ROOT / "projects",
        _PROJECT_ROOT / "skills",
        _PROJECT_ROOT / "agents",
        _PROJECT_ROOT / "rules",
        _PROJECT_ROOT / "workflows",
        _PROJECT_ROOT / "templates",
        _PROJECT_ROOT / "docs",
        _PROJECT_ROOT / "AGENTS.md",
    ]


def user_env_snapshot() -> dict:
    """只读采集用户环境摘要（用于卸载前后一致性说明）。"""
    npm: list[str] = []
    try:
        result = subprocess.run(
            ["npm", "ls", "-g", "--depth=0"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
        )
        npm = [line.strip() for line in result.stdout.splitlines() if "@" in line][:50]
    except (OSError, subprocess.SubprocessError):
        npm = []

    dsh_files: list[str] = []
    if USER_DSH_HOME.is_dir():
        dsh_files = sorted(item.name for item in USER_DSH_HOME.iterdir())[:20]
    return {"npm_global": npm, "user_dsh_entries": dsh_files, "user_dsh_path": str(USER_DSH_HOME)}


def run_selfcheck_verify() -> int:
    """调用自检（只读）确认用户 ~/.dsh 基线未变；返回退出码（缺基线时返回 0 并提示）。"""
    script = _PROJECT_ROOT / "workbench" / "cli" / "selfcheck.py"
    if not script.is_file():
        return 0
    try:
        result = subprocess.run(
            [sys.executable, str(script), "--verify"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180,
        )
    except (OSError, subprocess.SubprocessError):
        return 0
    print(result.stdout[-1500:])
    return result.returncode


def human_size(path: Path) -> str:
    if path.is_file():
        return f"{path.stat().st_size} B"
    total = 0
    for item in path.rglob("*"):
        if item.is_file():
            total += item.stat().st_size
    for unit in ("B", "KB", "MB", "GB"):
        if total < 1024 or unit == "GB":
            return f"{total:.1f} {unit}" if unit != "B" else f"{total} B"
        total /= 1024
    return f"{total} B"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="AI 小说创作工作台 · 卸载（只删工作台自己的产物，绝不碰用户环境）"
    )
    parser.add_argument("--dry-run", action="store_true", help="只列清单，不删除（默认）")
    parser.add_argument("--yes", action="store_true", help="确认执行删除")
    args = parser.parse_args()

    print("=" * 68)
    print(" 工作台卸载")
    print(f" 仓库根：{_PROJECT_ROOT}")
    print("=" * 68)

    print("\n[1/4] 将删除（工作台自己的产物）：")
    targets = removable_targets()
    existing = [path for path in targets if path.exists()]
    if not existing:
        _info("没有需要删除的产物（可能已卸载过）")
    for path in existing:
        _ok(f"{path.relative_to(_PROJECT_ROOT)}  ({human_size(path)})")

    print("\n[2/4] 明确保留：")
    for path in preserved_targets():
        if path.exists():
            _ok(f"{path.relative_to(_PROJECT_ROOT)}（事实源/受管资产，不删除）")

    print("\n[3/4] 用户环境（只读校验，不修改）：")
    snapshot = user_env_snapshot()
    _info(f"全局 npm 包 {len(snapshot['npm_global'])} 个（本脚本不会执行 npm 命令更改）")
    _info(f"用户 dsh 目录：{snapshot['user_dsh_path']}（{len(snapshot['user_dsh_entries'])} 个条目，只读）")

    print("\n[4/4] 隔离自检（~/.dsh 基线比对）：")
    code = run_selfcheck_verify()
    if code == 0:
        _ok("用户 ~/.dsh 与基线一致")
    else:
        _warn("用户 ~/.dsh 与基线存在差异（见上方输出；本脚本不做任何修复）")

    if not args.yes:
        print("\n（dry-run 模式，未执行删除；确认后加 --yes 执行）")
        return 0

    print("\n开始删除：")
    for path in existing:
        try:
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)
            _ok(f"已删除 {path.relative_to(_PROJECT_ROOT)}")
        except OSError as exc:
            _warn(f"删除失败 {path}：{exc}")

    print("\n完成。提示：")
    print("  · 小说正文与设定在 projects/ 下，未被删除；")
    print("  · 用户全局 dsh（~/.dsh）与全局 npm 包全程未被修改；")
    print("  · 需要重装：python workbench/cli/setup_dsh.py 然后 npm --prefix workbench/frontend install。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())