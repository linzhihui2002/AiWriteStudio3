"""dsh 路径解析与调用环境构建 —— 工作台唯一允许的 dsh 调用入口。

隔离红线（务必遵守）
--------------------
1. 本模块是工作台内**唯一允许**解析并调用 dsh 的位置。业务代码禁止裸调
   ``dsh``（即禁止依赖 PATH 上的全局命令，禁止写死 ``"dsh"`` 作为可执行文件）。
   所有调用方必须：

   .. code-block:: python

       from workbench.backend.engine.dsh_paths import get_dsh_binary, build_dsh_env

       subprocess.run([str(get_dsh_binary()), "--dump-config"], env=build_dsh_env())

   裸调痕迹由 ``workbench/cli/check_no_bare_dsh.py`` 静态扫描拦截。

2. 工作台只调用 **vendor 内置、版本锁定**的 dsh
   （``workbench/vendor/dsh/node_modules/.bin/dsh``），绝不调用 PATH 上的全局 dsh。

3. 工作台只使用 **独立 DSH_HOME** = ``<项目根>/.workbench/dsh-home``。
   **绝对禁止**读取、创建、修改或删除用户全局的 ``~/.dsh``
   （本机为 ``C:\\Users\\30332\\.dsh``）下的任何内容。
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

# workbench/backend/engine/dsh_paths.py -> 项目根
# parents[0]=engine  parents[1]=backend  parents[2]=workbench  parents[3]=项目根
_PROJECT_ROOT = Path(__file__).resolve().parents[3]

VENDOR_DSH_DIR = _PROJECT_ROOT / "workbench" / "vendor" / "dsh"
VENDOR_DSH_NODE_MODULES = VENDOR_DSH_DIR / "node_modules"
VENDOR_DSH_BIN_DIR = VENDOR_DSH_NODE_MODULES / ".bin"

DSH_HOME_DIR = _PROJECT_ROOT / ".workbench" / "dsh-home"

# Windows 上是 dsh.cmd / dsh.ps1 / dsh（shim）；POSIX 上是 dsh
_BIN_CANDIDATES_WINDOWS = ("dsh.cmd", "dsh.ps1", "dsh.exe", "dsh")
_BIN_CANDIDATES_POSIX = ("dsh",)


class DshNotInstalledError(RuntimeError):
    """vendor 内置 dsh 尚未安装时抛出，异常信息内含修复指引。"""


def get_project_root() -> Path:
    """返回工作台项目根目录（绝对路径）。"""
    return _PROJECT_ROOT


def get_dsh_home() -> Path:
    """返回工作台独立 DSH_HOME：``<项目根>/.workbench/dsh-home``（绝对路径）。

    注意：该目录与用户全局 ``~/.dsh`` 完全无关，工作台永不触碰后者。
    """
    return DSH_HOME_DIR


def get_vendor_dsh_dir() -> Path:
    """返回 vendor dsh 安装目录（绝对路径）。"""
    return VENDOR_DSH_DIR


def _bin_candidates() -> tuple[Path, ...]:
    names = _BIN_CANDIDATES_WINDOWS if sys.platform == "win32" else _BIN_CANDIDATES_POSIX
    return tuple(VENDOR_DSH_BIN_DIR / name for name in names)


def get_dsh_binary() -> Path:
    """返回 vendor 内置 dsh 可执行文件的绝对路径。

    Windows 下优先 ``dsh.cmd`` / ``dsh.ps1``；POSIX 下为 ``dsh``。
    若 vendor 未安装，抛出 :class:`DshNotInstalledError`（含修复指引）。
    """
    for candidate in _bin_candidates():
        if candidate.is_file():
            return candidate

    raise DshNotInstalledError(
        "未找到 vendor 内置 dsh 可执行文件。\n"
        f"  期望位置: {VENDOR_DSH_BIN_DIR}\n"
        f"  期望文件: {', '.join(p.name for p in _bin_candidates())}\n"
        "  修复指引: 先运行 `python workbench/cli/setup_dsh.py` 完成内置 dsh 的本地安装。\n"
        "  （禁止使用 `npm install -g`，禁止调用 PATH 上的全局 dsh。）"
    )


def is_vendor_dsh_installed() -> bool:
    """vendor 内置 dsh 是否已安装（不抛异常的安全探测）。"""
    return any(candidate.is_file() for candidate in _bin_candidates())


def build_dsh_env(base_env: dict[str, str] | None = None) -> dict[str, str]:
    """构建注入了独立 ``DSH_HOME`` 的环境变量 dict。

    :param base_env: 基础环境变量，默认取当前进程环境。
    :return: 新 dict，其中 ``DSH_HOME`` 指向 :func:`get_dsh_home`。
    """
    env = dict(os.environ if base_env is None else base_env)
    home = get_dsh_home()
    home.mkdir(parents=True, exist_ok=True)
    env["DSH_HOME"] = str(home)
    return env


def get_node_binary() -> str | None:
    """返回 Node.js 可执行文件路径（供 npm 调用与健康检查使用）。"""
    return shutil.which("node")


def get_dsh_cli_command() -> list[str]:
    """Run the locked vendor CLI without a Windows command-shell shim.

    The CLI remains the vendor executable; Node is only its interpreter. Keeping
    this resolution here prevents chat runners from resolving a global package.
    """
    get_dsh_binary()  # Preserve the normal installation check and repair hint.
    node = get_node_binary()
    entry = VENDOR_DSH_NODE_MODULES / "@deepseek-ai" / "dsh" / "lib" / "bin.js"
    if node is None or not entry.is_file():
        raise DshNotInstalledError("内置 dsh 或 Node.js 不完整，请运行 setup_dsh.py 修复。")
    return [node, str(entry)]


def get_npm_binary() -> str | None:
    """返回 npm 可执行文件路径（仅用于 vendor 目录本地安装，禁止 -g）。"""
    return shutil.which("npm")
