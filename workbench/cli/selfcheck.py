#!/usr/bin/env python
"""dsh 完整性自检 —— 用户全局 dsh 环境（``~/.dsh``）的**只读**基线与比对。

用途
----
工作台与用户本机已装的 DeepSeekHarness 必须零冲突共存。本脚本为「未被
触碰」提供可复核证据：

1. **记录基线**（``--baseline``）：对用户全局 dsh 主目录下的关键项记录
   SHA-256 + mtime，写入 ``.workbench/dsh_baseline.json``。
2. **比对模式**（``--verify``）：再次采集当前状态并与基线逐项比对，不一致
   则高亮告警并列出差异项；**绝不自动修复用户文件**。
3. **全局 npm 快照**：记录 ``npm ls -g --depth=0`` 输出并比对，证明未发生
   ``npm install -g`` 污染。

隔离红线（务必遵守）
--------------------
* 本脚本对 ``~/.dsh`` **只读**：只读取关键文件内容做哈希，**绝不写入、
  绝不删除、绝不修改**该目录下任何内容。
* 所有产出（基线文件）写入 ``<项目根>/.workbench/``，该目录已被 .gitignore 忽略。

关键项（不存在则记 null，不报错）
--------------------------------
* ``settings.yaml``
* ``profiles/`` 目录下的文件清单与各自哈希（跳过 ``node_modules`` 依赖树）
* ``.agent-presets/`` 下的文件
* ``storages/workspace.json``

用法::

    python workbench/cli/selfcheck.py --baseline   # 记录基线
    python workbench/cli/selfcheck.py --verify     # 比对当前状态与基线
    python workbench/cli/selfcheck.py              # 基线不存在则记录，存在则比对

退出码：0 = 一致；1 = 不一致（或基线缺失导致无法比对）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]

# 用户全局 dsh 主目录：一律用 ~ 展开，禁止硬编码用户名
USER_DSH_HOME = Path(os.path.expanduser("~")) / ".dsh"

# 基线落盘位置（工作台运行时目录，已 gitignore）
BASELINE_PATH = _PROJECT_ROOT / ".workbench" / "dsh_baseline.json"

# 关键项路径（相对用户 dsh 主目录）
KEY_SETTINGS_YAML = "settings.yaml"
KEY_PROFILES_DIR = "profiles"
KEY_AGENT_PRESETS_DIR = ".agent-presets"
KEY_WORKSPACE_JSON = Path("storages") / "workspace.json"

# 不参与哈希的目录名：pnpm / npm 依赖树体量巨大且非用户配置
SKIP_DIR_NAMES = {"node_modules"}

SCHEMA_VERSION = 1


def _ok(msg: str) -> None:
    print(f"  [OK]   {msg}")


def _warn(msg: str) -> None:
    print(f"  [WARN] {msg}")


def _fail(msg: str) -> None:
    print(f"  [FAIL] {msg}")


def _info(msg: str) -> None:
    print(f"  [ .. ] {msg}")


# ------------------------------------------------------------ 采集
def _file_record(path: Path) -> dict | None:
    """返回单文件的 {sha256, size, mtime}；不存在或不可读返回 None。"""
    try:
        if not path.is_file():
            return None
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        stat = path.stat()
        return {
            "sha256": digest.hexdigest(),
            "size": stat.st_size,
            "mtime": round(stat.st_mtime, 6),
        }
    except OSError:
        return None


def _dir_records(root: Path) -> dict[str, dict]:
    """递归采集目录下所有文件的 {相对路径: 文件记录}（跳过依赖树目录）。

    目录不存在返回空 dict（不报错）。
    """
    records: dict[str, dict] = {}
    if not root.is_dir():
        return records
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIR_NAMES)
        for filename in sorted(filenames):
            path = Path(dirpath) / filename
            record = _file_record(path)
            if record is None:
                continue
            records[path.relative_to(root).as_posix()] = record
    return records


def collect_key_items() -> dict:
    """采集用户 dsh 主目录下全部关键项的当前状态。"""
    home = USER_DSH_HOME
    return {
        "user_dsh_home": str(home),
        "user_dsh_home_present": home.is_dir(),
        "settings_yaml": _file_record(home / KEY_SETTINGS_YAML),
        "profiles": _dir_records(home / KEY_PROFILES_DIR),
        "agent_presets": _dir_records(home / KEY_AGENT_PRESETS_DIR),
        "workspace_json": _file_record(home / KEY_WORKSPACE_JSON),
    }


def capture_npm_global() -> dict[str, str]:
    """``npm ls -g --depth=0 --json`` 快照，返回 {包名: 版本}。"""
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

    result: dict[str, str] = {}
    for name, info in (data.get("dependencies") or {}).items():
        result[name] = info.get("version", "?") if isinstance(info, dict) else str(info)
    return result


def collect_state() -> dict:
    """采集一次完整状态（关键项 + 全局 npm）。"""
    return {
        "key_items": collect_key_items(),
        "npm_global": capture_npm_global(),
    }


# ------------------------------------------------------------ 基线读写
def record_baseline() -> int:
    """采集当前状态并写入基线文件。"""
    state = collect_state()
    payload = {
        "schema_version": SCHEMA_VERSION,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "project_root": str(_PROJECT_ROOT),
        "note": "用户全局 dsh 环境的只读基线；由 workbench/cli/selfcheck.py 生成。",
        **state,
    }
    BASELINE_PATH.parent.mkdir(parents=True, exist_ok=True)
    BASELINE_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    key_items = state["key_items"]
    profiles_count = len(key_items["profiles"])
    presets_count = len(key_items["agent_presets"])
    npm_count = len([k for k in state["npm_global"] if not k.startswith("__")])

    print()
    print("=" * 64)
    print(" 基线已记录")
    print("=" * 64)
    _info(f"用户 dsh 主目录: {key_items['user_dsh_home']}（存在={key_items['user_dsh_home_present']}）")
    _ok(f"settings.yaml     : {'已记录' if key_items['settings_yaml'] else '不存在（记 null）'}")
    _ok(f"profiles/ 文件    : {profiles_count} 个（已跳过 node_modules）")
    _ok(f".agent-presets/   : {presets_count} 个文件")
    _ok(f"storages/workspace.json: {'已记录' if key_items['workspace_json'] else '不存在（记 null）'}")
    _ok(f"全局 npm 包       : {npm_count} 个")
    print(f"  基线文件: {BASELINE_PATH}")
    print("=" * 64)
    return 0


def load_baseline() -> dict | None:
    if not BASELINE_PATH.is_file():
        return None
    try:
        # utf-8-sig：容忍外部工具（如 PowerShell Set-Content）写入的 BOM
        return json.loads(BASELINE_PATH.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None


# ------------------------------------------------------------ 比对
def _diff_file(label: str, before: dict | None, after: dict | None) -> list[str]:
    """比对单个文件记录，返回差异描述列表。"""
    if before is None and after is None:
        return []
    if before is None:
        return [f"{label}: 基线为 null，当前已存在（新增文件）"]
    if after is None:
        return [f"{label}: 基线存在，当前缺失（被删除或不可读）"]

    diffs: list[str] = []
    if before.get("sha256") != after.get("sha256"):
        diffs.append(
            f"{label}: 内容已变更  {before.get('sha256', '?')[:12]} -> {after.get('sha256', '?')[:12]}"
        )
    if before.get("mtime") != after.get("mtime"):
        diffs.append(f"{label}: mtime 变更  {before.get('mtime')} -> {after.get('mtime')}")
    if before.get("size") != after.get("size"):
        diffs.append(f"{label}: 大小变更  {before.get('size')} -> {after.get('size')}")
    return diffs


def _diff_dir(label: str, before: dict, after: dict) -> list[str]:
    """比对目录文件清单，返回差异描述列表。"""
    diffs: list[str] = []
    for name in sorted(set(before) - set(after)):
        diffs.append(f"{label}/{name}: 基线存在，当前缺失（被删除）")
    for name in sorted(set(after) - set(before)):
        diffs.append(f"{label}/{name}: 当前新增（基线中不存在）")
    for name in sorted(set(before) & set(after)):
        diffs.extend(_diff_file(f"{label}/{name}", before[name], after[name]))
    return diffs


def _diff_npm(before: dict[str, str], after: dict[str, str]) -> list[str]:
    if "__error__" in before or "__error__" in after:
        if before.get("__error__") != after.get("__error__"):
            return [f"npm 快照不可用: {before.get('__error__')} -> {after.get('__error__')}"]
        return []
    diffs: list[str] = []
    for name in sorted(set(before) | set(after)):
        b, a = before.get(name), after.get(name)
        if b != a:
            diffs.append(f"全局包 {name}: {b} -> {a}")
    return diffs


def print_hints() -> None:
    """不一致时给出「最近可能原因」提示（仅供排查，不做任何自动修复）。"""
    print()
    print("  最近可能原因（排查提示，本脚本不会自动修复任何用户文件）：")
    print("    * 可能由其他 dsh 会话或第三方工具写入（用户可能同时在用全局 dsh）；")
    print("    * 用户手工编辑了 ~/.dsh 下的配置；")
    print("    * dsh 自身升级或运行期写入了缓存 / 会话 / workspace 注册表；")
    print("    * 若差异出现在全局 npm 快照：可能有人执行了 npm install -g。")
    print("  处置建议：确认差异是否为预期变更；若是工作台导致，请立即排查。")
    print("  重新记录基线：python workbench/cli/selfcheck.py --baseline")


def verify_baseline() -> int:
    """比对当前状态与基线，输出差异报告。"""
    baseline = load_baseline()
    if baseline is None:
        _fail(f"未找到可用基线：{BASELINE_PATH}")
        print("  请先运行：python workbench/cli/selfcheck.py --baseline")
        return 1

    current = collect_state()
    before_items = baseline.get("key_items", {})
    after_items = current["key_items"]

    print()
    print("=" * 64)
    print(" dsh 完整性比对报告（用户全局 dsh 环境，只读）")
    print("=" * 64)
    _info(f"基线记录时间: {baseline.get('recorded_at', '未知')}")
    _info(f"基线 schema  : {baseline.get('schema_version', '?')}（当前 {SCHEMA_VERSION}）")
    _info(f"用户 dsh 主目录: {after_items['user_dsh_home']}")

    sections: list[tuple[str, list[str]]] = []

    # [1] settings.yaml
    sections.append(
        (
            "settings.yaml",
            _diff_file(
                "settings.yaml",
                before_items.get("settings_yaml"),
                after_items.get("settings_yaml"),
            ),
        )
    )
    # [2] profiles/
    sections.append(
        ("profiles/", _diff_dir("profiles", before_items.get("profiles", {}), after_items.get("profiles", {})))
    )
    # [3] .agent-presets/
    sections.append(
        (
            ".agent-presets/",
            _diff_dir(
                ".agent-presets",
                before_items.get("agent_presets", {}),
                after_items.get("agent_presets", {}),
            ),
        )
    )
    # [4] storages/workspace.json
    sections.append(
        (
            "storages/workspace.json",
            _diff_file(
                "storages/workspace.json",
                before_items.get("workspace_json"),
                after_items.get("workspace_json"),
            ),
        )
    )
    # [5] 全局 npm
    sections.append(
        ("全局 npm 快照", _diff_npm(baseline.get("npm_global", {}), current["npm_global"]))
    )

    issues = 0
    for index, (title, diffs) in enumerate(sections, start=1):
        print()
        print(f"[{index}] {title}")
        if not diffs:
            print("    PASS：与基线一致。")
            continue
        issues += len(diffs)
        print("    !! 不一致：")
        for line in diffs:
            print(f"      ! {line}")

    print()
    print("=" * 64)
    if issues:
        print(f" 结论：检测到 {issues} 处与基线不一致！")
        print_hints()
        print("=" * 64)
        return 1
    print(" 结论：全部关键项与基线一致 —— 用户全局 dsh 环境未被触碰。")
    print("=" * 64)
    return 0


# ------------------------------------------------------------ main
def main() -> int:
    parser = argparse.ArgumentParser(
        description="dsh 完整性自检：用户全局 dsh 环境的只读基线与比对"
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--baseline", action="store_true", help="记录当前状态为基线")
    group.add_argument("--verify", action="store_true", help="比对当前状态与已有基线")
    args = parser.parse_args()

    print("=" * 64)
    print(" dsh 完整性自检（只读用户 ~/.dsh，绝不写入）")
    print(f" 用户 dsh 主目录: {USER_DSH_HOME}")
    print(f" 基线文件       : {BASELINE_PATH}")
    print("=" * 64)

    if args.baseline:
        return record_baseline()
    if args.verify:
        return verify_baseline()

    # 默认：基线不存在则记录，存在则比对
    if load_baseline() is None:
        _info("未发现基线，先记录基线。")
        return record_baseline()
    _info("已发现基线，执行比对。")
    return verify_baseline()


if __name__ == "__main__":
    raise SystemExit(main())
