#!/usr/bin/env python
"""隔离验证脚本 —— 比对执行前后「全局 npm 列表」与「用户 ~/.dsh 顶层条目」。

隔离红线的自动化断言：

* 全局 npm 包列表在运行前后**必须一致**（证明未 ``npm install -g`` 污染环境）；
* 用户全局 dsh 主目录（本机 ``C:\\Users\\30332\\.dsh``）的顶层条目
  （**仅目录名/文件名清单，不读取任何文件内容**）在运行前后**必须一致**
  （证明未触碰用户 dsh 环境）。

本脚本**只列目录名，不读内容**，因此不会泄露用户配置与 API Key。

用法::

    python workbench/cli/verify_isolation.py
        # 默认流程：抓 before → 运行 setup_dsh.py → 抓 after → 比对

    python workbench/cli/verify_isolation.py --cmd "python workbench/cli/check_no_bare_dsh.py"
        # 自定义中间执行的命令

    python workbench/cli/verify_isolation.py --snapshot-only
        # 只输出当前快照，不做比对
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]

# 用户全局 dsh 主目录：一律用 ~ 展开，禁止硬编码用户名
USER_DSH_HOME = Path(os.path.expanduser("~")) / ".dsh"

# 用户侧 sessions 为 dsh 运行时产物，比对时忽略（顶层名清单仍会展示）
IGNORE_TOP_NAMES = {"sessions"}

DEFAULT_COMMAND = [sys.executable, str(_PROJECT_ROOT / "workbench" / "cli" / "setup_dsh.py")]


# ----------------------------------------------------------------- 快照
def capture_global_npm() -> dict[str, str]:
    """``npm ls -g --depth=0`` 实时快照，返回 {包名: 版本}。

    Windows 上 npm 是 ``npm.cmd`` shim，subprocess 不带 shell 时找不到，
    故先用 shutil.which 解析真实路径。
    """
    npm = shutil.which("npm")
    if npm is None:
        return {"__error__": "未找到 npm 可执行文件"}

    try:
        proc = subprocess.run(
            [npm, "ls", "-g", "--depth=0", "--json"],
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
    except (FileNotFoundError, OSError) as exc:
        return {"__error__": f"无法执行 npm ls -g: {exc}"}

    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return {"__error__": "npm ls -g 输出无法解析为 JSON"}

    deps = data.get("dependencies", {})
    result: dict[str, str] = {}
    for name, info in deps.items():
        result[name] = info.get("version", "?") if isinstance(info, dict) else str(info)
    return result


def snapshot_user_dsh_top() -> dict:
    """用户 ~/.dsh 顶层条目名清单（仅名称与类型，绝不读内容）。"""
    if not USER_DSH_HOME.is_dir():
        return {"path": str(USER_DSH_HOME), "present": False, "entries": []}
    entries = []
    for child in sorted(USER_DSH_HOME.iterdir(), key=lambda p: p.name):
        entries.append({"name": child.name, "kind": "dir" if child.is_dir() else "file"})
    return {"path": str(USER_DSH_HOME), "present": True, "entries": entries}


def take_snapshot() -> dict:
    return {
        "project_root": str(_PROJECT_ROOT),
        "dsh_home": str(_PROJECT_ROOT / ".workbench" / "dsh-home"),
        "npm_global": capture_global_npm(),
        "user_dsh_top": snapshot_user_dsh_top(),
    }


# ----------------------------------------------------------------- 比对
def _npm_diff(before: dict[str, str], after: dict[str, str]) -> list[str]:
    diffs: list[str] = []
    for name in sorted(set(before) | set(after)):
        b, a = before.get(name), after.get(name)
        if b != a:
            diffs.append(f"{name}: {b} -> {a}")
    return diffs


def _top_diff(before: dict, after: dict) -> list[str]:
    if before.get("present") != after.get("present"):
        return [f"present 状态变化: {before.get('present')} -> {after.get('present')}"]
    if not before.get("present"):
        return []
    before_names = [e["name"] for e in before.get("entries", []) if e["name"] not in IGNORE_TOP_NAMES]
    after_names = [e["name"] for e in after.get("entries", []) if e["name"] not in IGNORE_TOP_NAMES]
    if before_names == after_names:
        return []
    added = sorted(set(after_names) - set(before_names))
    removed = sorted(set(before_names) - set(after_names))
    return [f"新增={added or '无'} 移除={removed or '无'}"]


def render_snapshot(snap: dict, title: str) -> None:
    print("-" * 64)
    print(f" {title}")
    print(f"   project_root : {snap['project_root']}")
    print(f"   dsh_home     : {snap['dsh_home']}")
    npm = snap["npm_global"]
    print(f"   全局 npm ({len([k for k in npm if not k.startswith('__')])} 个):")
    for name, ver in npm.items():
        print(f"     - {name} {ver}")
    top = snap["user_dsh_top"]
    print(f"   用户 ~/.dsh ({top['path']}) present={top['present']}:")
    for entry in top.get("entries", []):
        print(f"     - [{entry['kind']}] {entry['name']}")


def compare_and_report(before: dict, after: dict) -> int:
    print()
    print("=" * 64)
    print(" 隔离验证比对报告")
    print("=" * 64)
    issues = 0

    # [1] 全局 npm
    npm_before = {k: v for k, v in before.get("npm_global", {}).items() if not k.startswith("__")}
    npm_after = {k: v for k, v in after.get("npm_global", {}).items() if not k.startswith("__")}
    npm_diffs = _npm_diff(npm_before, npm_after)
    print("\n[1] 全局 npm 包列表（npm ls -g --depth=0）")
    print(f"    before: {len(npm_before)} 个  |  after: {len(npm_after)} 个")
    if npm_diffs:
        print("    变动明细:")
        for line in npm_diffs:
            print(f"      ! {line}")
        issues += len(npm_diffs)
    else:
        print("    PASS：全局 npm 列表完全一致（未污染全局环境）。")

    # [2] 用户 ~/.dsh 顶层条目
    top_diffs = _top_diff(before.get("user_dsh_top", {}), after.get("user_dsh_top", {}))
    print("\n[2] 用户 ~/.dsh 顶层条目名清单（只列名称，不读内容）")
    top_before = before.get("user_dsh_top", {})
    top_after = after.get("user_dsh_top", {})
    print(f"    before: present={top_before.get('present')}, {len(top_before.get('entries', []))} 项")
    print(f"    after : present={top_after.get('present')}, {len(top_after.get('entries', []))} 项")
    if top_diffs:
        print("    变动明细:")
        for line in top_diffs:
            print(f"      ! {line}")
        issues += len(top_diffs)
    else:
        print("    PASS：用户 ~/.dsh 顶层条目未变动（未触碰用户 dsh 环境）。")

    print("\n" + "=" * 64)
    if issues:
        print(f" 结论：检测到 {issues} 处隔离违规！")
        return 1
    print(" 结论：隔离通过 —— 全局 npm 与用户 ~/.dsh 均无变化。")
    return 0


# ----------------------------------------------------------------- main
def main() -> int:
    parser = argparse.ArgumentParser(description="dsh 隔离验证：全局 npm 与用户 ~/.dsh 前后比对")
    parser.add_argument("--cmd", help="在 before/after 快照之间执行的命令（默认运行 setup_dsh.py）")
    parser.add_argument("--snapshot-only", action="store_true", help="只输出当前快照，不做比对")
    parser.add_argument("--out", help="将 before 快照写入指定 JSON 文件")
    args = parser.parse_args()

    print("=" * 64)
    print(" DeepSeekHarness 隔离验证")
    print("=" * 64)

    before = take_snapshot()
    render_snapshot(before, "BEFORE 快照")
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(before, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"   before 快照已写入: {out}")

    if args.snapshot_only:
        return 0

    cmdline = shlex.split(args.cmd) if args.cmd else DEFAULT_COMMAND
    print()
    print("-" * 64)
    print(f" 执行中间步骤: {' '.join(cmdline)}")
    print("-" * 64)
    proc = subprocess.run(cmdline, cwd=str(_PROJECT_ROOT), check=False)
    print(f" 中间步骤退出码: {proc.returncode}")

    after = take_snapshot()
    render_snapshot(after, "AFTER 快照")

    return compare_and_report(before, after)


if __name__ == "__main__":
    raise SystemExit(main())