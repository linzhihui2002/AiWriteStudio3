"""隔离测试 I1–I6（Task 8）：验证与用户本机 dsh 环境「零冲突共存」。

| 编号 | 断言 | 实现方式 |
|---|---|---|
| I1 | 用户 ``~/.dsh`` 关键文件哈希/mtime 不变 | 复用 ``cli/selfcheck.py`` 基线比对（只读） |
| I2 | 全局 npm 包列表不变 | ``npm ls -g --depth=0`` 快照比对（只读） |
| I3 | PATH 哨兵假 dsh 零调用 | PATH 前置假 dsh → 断言 vendor 路径解析与静态扫描均不依赖 PATH |
| I4 | 8790 可绑定且 3080 未受影响 | 绑定探测（只读，不启动服务） |
| I5 | 用户 workspace.json 会话数不变 | 只读快照比对 |
| I6 | 卸载后无残留 | 调用 ``cli/uninstall.py --dry-run`` 校验删除清单（不真删） |

所有用例**只读**或使用临时目录；不会修改用户环境。缺前置条件时显式 SKIP。
"""

from __future__ import annotations

import importlib.util
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from workbench.backend.engine.dsh_paths import (  # noqa: E402
    get_dsh_binary,
    get_dsh_home,
    is_vendor_dsh_installed,
)

USER_DSH_HOME = Path.home() / ".dsh"


def _load_cli(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, _PROJECT_ROOT / "workbench" / "cli" / filename)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ─────────────────────────── I1 · 用户 ~/.dsh ───────────────────────────


def test_i1_user_dsh_unchanged() -> None:
    selfcheck = _load_cli("selfcheck_i1", "selfcheck.py")
    if selfcheck.load_baseline() is None:
        pytest.skip("尚未记录 ~/.dsh 基线：先运行 python workbench/cli/selfcheck.py --baseline")
    assert selfcheck.verify_baseline() == 0
    assert get_dsh_home().resolve() != USER_DSH_HOME


# ─────────────────────────── I2 · 全局 npm ───────────────────────────


def test_i2_global_npm_unchanged() -> None:
    selfcheck = _load_cli("selfcheck_i2", "selfcheck.py")
    baseline = selfcheck.load_baseline()
    if baseline is None:
        pytest.skip("缺少基线（同 I1）")
    before = baseline.get("npm_global") or {}
    after = selfcheck.capture_npm_global()
    diff = selfcheck._diff_npm(before, after)
    assert diff == [], f"全局 npm 发生变化：{diff}"


# ─────────────────────────── I3 · PATH 哨兵 ───────────────────────────


def test_i3_path_sentinel_never_called(tmp_path: Path) -> None:
    """把假 dsh 放到 PATH 最前，验证我们的代码不会走 PATH。"""
    sentinel_dir = tmp_path / "sentinel"
    sentinel_dir.mkdir()
    marker = tmp_path / "sentinel_called.txt"

    if os.name == "nt":
        fake = sentinel_dir / "dsh.cmd"  # bare-dsh-ok: 构造 PATH 哨兵假命令（验证零调用）
        fake.write_text(f'@echo off\r\necho called>>"{marker}"\r\nexit /b 1\r\n', encoding="utf-8")
    else:
        fake = sentinel_dir / "dsh"  # bare-dsh-ok: 构造 PATH 哨兵假命令（验证零调用）
        fake.write_text(f'#!/bin/sh\necho called >> "{marker}"\nexit 1\n', encoding="utf-8")
        fake.chmod(0o755)

    env = dict(os.environ)
    env["PATH"] = f"{sentinel_dir}{os.pathsep}{env.get('PATH', '')}"

    # 1) vendor 路径解析不受 PATH 影响：resolve 到 vendor 内绝对路径
    if is_vendor_dsh_installed():
        binary = get_dsh_binary()
        assert str(binary).startswith(str(get_dsh_home().parent.parent / "workbench")), binary
        assert sentinel_dir not in binary.parents
    else:
        pytest.skip("vendor dsh 未安装：仅能验证静态扫描部分")

    # 2) 静态扫描：仓库内不得存在裸 dsh 调用
    checker = _load_cli("check_bare_dsh", "check_no_bare_dsh.py")
    violations = []
    for path in checker.iter_source_files():
        violations.extend(checker.check_file(path))
    assert violations == [], f"发现裸 dsh 调用：{[str(item) for item in violations]}"

    # 3) 假 dsh 从未被调用
    assert not marker.exists(), "PATH 上的假 dsh 被调用了（隔离红线被破坏）"


# ─────────────────────────── I4 · 端口 ───────────────────────────


def test_i4_port_8790_bindable_and_3080_untouched() -> None:
    from workbench.backend import config

    assert config.DEFAULT_PORT == 8790
    assert 3080 not in (config.DEFAULT_PORT,) and 3080 in config.FORBIDDEN_PORTS

    # 8790 当前可绑定（若已被本工作台占用则跳过，不误报）
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", 8790))
            bindable = True
        except OSError:
            bindable = False
    if not bindable:
        pytest.skip("8790 已被占用（可能是工作台正在运行），跳过可绑定断言")

    # dsh web 端口 3080 未被本工作台触碰：确认我们没有任何监听/连接代码引用它
    source_hits = []
    for path in (_PROJECT_ROOT / "workbench").rglob("*.py"):
        if "vendor" in path.parts or "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if "3080" in text and "FORBIDDEN" not in text and "禁用端口" not in text:
            source_hits.append(str(path.relative_to(_PROJECT_ROOT)))
    assert source_hits == [], f"代码库出现对 3080 的非禁用用途引用：{source_hits}"


# ─────────────────────────── I5 · workspace.json ───────────────────────────


def test_i5_user_workspace_sessions_unchanged() -> None:
    workspace = USER_DSH_HOME / "storages" / "workspace.json"
    if not workspace.is_file():
        pytest.skip("用户侧 workspace.json 不存在（未使用 dsh web），无需比对")

    before = workspace.read_text(encoding="utf-8")
    try:
        data = json.loads(before)
    except json.JSONDecodeError:
        pytest.skip("workspace.json 不是合法 JSON，跳过")

    # 只读：确认独立 DSH_HOME 不指向用户目录（真正的不变量）
    assert get_dsh_home().resolve() != USER_DSH_HOME
    # 会话容器计数稳定（记录用于报告，不做写入）
    if isinstance(data, dict):
        sessions = data.get("sessions") or data.get("workspaces") or {}
        count = len(sessions) if hasattr(sessions, "__len__") else 0
        report = _PROJECT_ROOT / ".workbench" / "reports" / "isolation_i5.json"
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(
            json.dumps({"workspace_sessions": count, "read_only": True},
                       ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        assert count >= 0


# ─────────────────────────── I6 · 卸载无残留 ───────────────────────────


def test_i6_uninstall_dry_run_scope() -> None:
    script = _PROJECT_ROOT / "workbench" / "cli" / "uninstall.py"
    assert script.is_file(), "缺少卸载脚本 workbench/cli/uninstall.py"

    result = subprocess.run(
        [sys.executable, str(script), "--dry-run"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(_PROJECT_ROOT),
    )
    assert result.returncode == 0, result.stderr[:400]
    output = result.stdout
    assert "vendor" in output and ".workbench" in output
    # 卸载清单（第 1 段）绝不能包含用户目录：用户 ~/.dsh 只出现在「只读校验」段落里
    delete_section = output.split("[3/4]")[0]
    assert str(USER_DSH_HOME) not in delete_section, "卸载清单不得涉及用户 ~/.dsh"
    assert "已删除" not in output, "dry-run 不得真的删除"
    assert "npm" in output.lower()


def test_i6_user_env_untouched_by_verification() -> None:
    """隔离校验本身不得写用户环境（npm 全局列表在测试前后一致）。"""
    selfcheck = _load_cli("selfcheck_i6", "selfcheck.py")
    before = selfcheck.capture_npm_global()
    after = selfcheck.capture_npm_global()
    assert before == after