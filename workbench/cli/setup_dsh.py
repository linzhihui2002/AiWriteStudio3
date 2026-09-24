#!/usr/bin/env python
"""工作台内置 dsh 首次运行引导脚本。

隔离红线（务必遵守）
--------------------
* 只在 ``workbench/vendor/dsh/`` 做**本地** ``npm install``；**绝不** ``npm install -g``。
* **绝不**执行 ``dsh plugin``（会向 profile 装插件，可能污染用户环境）。
* 只初始化独立 DSH_HOME = ``<项目根>/.workbench/dsh-home``；
  **绝不**读取/创建/修改/删除用户全局 ``~/.dsh``。
* 所有 dsh 调用都走 vendor 绝对路径 + 注入 ``DSH_HOME``（见 dsh_paths）。

用法::

    python workbench/cli/setup_dsh.py
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

# workbench/cli/setup_dsh.py -> 项目根
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from workbench.backend.engine.dsh_paths import (  # noqa: E402
    VENDOR_DSH_BIN_DIR,
    VENDOR_DSH_DIR,
    build_dsh_env,
    get_dsh_binary,
    get_dsh_home,
    get_npm_binary,
    get_vendor_dsh_dir,
)

MIN_NODE_MAJOR = 20
PROFILE_NAME = "ai-novel-workbench"

# 实测（2026-09-19）：@deepseek-ai/dsh@0.1.0-rc.8 依赖树极大（约 140+ 个
# @deepseek-ai/* 子包），npm 依赖解析阶段在 Node 默认堆上限（~2GB）下会
# `FATAL ERROR: Ineffective mark-compacts near heap limit`（退出码 134）。
# 故安装时显式抬高 Node 堆上限。
NPM_NODE_OPTIONS = "--max-old-space-size=4096"

# 使用工作台自己的 npm 缓存目录（.workbench/ 下，已 gitignore）：
# 实测用户全局 npm 缓存中的过期 packument 会让解析期误报
# `ETARGET No matching version found`，隔离缓存可规避且不污染用户缓存。
NPM_CACHE_DIR = _PROJECT_ROOT / ".workbench" / "npm-cache"

SETTINGS_TEMPLATE = """\
# ============================================================
# 工作台独立 DSH_HOME 配置文件（由工作台管理，请勿手工编辑）
# ------------------------------------------------------------
# 本文件属于工作台内置 dsh 的隔离环境，路径：
#   <项目根>/.workbench/dsh-home/settings.yaml
#
# 隔离红线：
#   * 本文件与用户全局 ~/.dsh/settings.yaml 完全无关；
#     工作台绝不读取/写入用户全局 dsh 配置。
#   * 此处**不得**出现任何真实 API Key。
#
# provider 条目说明：
#   工作台的「模型配置」页维护 provider 清单，保存后**单向投影**到本文件
#   （工作台 → 本文件，反向只读）。投影时密钥一律以工作台安全存储的
#   引用形式注入，不落明文。
#
# 当前状态：占位骨架，尚未投影任何 provider。
# ============================================================

# provider 条目将由工作台的模型配置单向投影生成，此处先留空。
llm-pi-ai:
  providers: []
"""

PROFILE_README = """\
# profile: ai-novel-workbench

本 profile 属于**工作台内置 dsh 的独立 DSH_HOME**，与用户全局 dsh 的 profile
（如 `web`）完全隔离。

## 机制实测结论（2026-09-19，dsh 0.1.0-rc.8）

dsh 只对**随附模板**里的 profile 名（`web`、`headless`）做首次自动初始化；
自定义名字（如本目录）若不存在，dsh 会直接报错：

    dsh: profile "ai-novel-workbench" does not exist;
    create it with 'dsh plugin --profile ai-novel-workbench add <package>'

而 `dsh plugin` 会向 profile 装插件，**属于工作台明令禁止的操作**。
因此工作台改为**直接按 dsh 的 initProfile 格式写入 profile 工程文件**
（package.json + cordis.patch.yml + pnpm-workspace.yaml），
不借助 `dsh plugin`，效果等价且零副作用。

## 文件说明

- `package.json`      profile 清单：`dsh.profile.bundles` 声明 bundle 层顺序
- `cordis.patch.yml`  profile 用户 patch 层（默认空数组）
- `pnpm-workspace.yaml` pnpm 设置（供 out-of-tree 插件使用，当前无插件）

禁止：任何向用户全局 `~/.dsh/profiles/` 写入的操作。
"""

# 以下三个模板严格对齐 dsh-app-boot 的 initProfile 实现
# （见 workbench/vendor/dsh/node_modules/@deepseek-ai/dsh-app-boot/lib/index.js L353-368），
# 以便在不调用 `dsh plugin` 的前提下建出等价 profile。
PROFILE_BUNDLES = ["@deepseek-ai/dsh-base", "@deepseek-ai/dsh-headless"]

PROFILE_PATCH_TEMPLATE = """\
# Your patch layer for this dsh profile, applied after every bundle layer:
# a top-level YAML array of loader patch entries (id-targeted config
# overrides, disables, and insert lists; `!!js` expressions allowed).
[]
"""

PROFILE_PNPM_WORKSPACE = """\
packages:
  - .

nodeLinker: hoisted
autoInstallPeers: false
"""

# 健康检查用的 profile：先验随附模板的 headless，再验工作台专用 profile
HEALTH_CHECK_PROFILE = "headless"



def _ok(msg: str) -> None:
    print(f"  [OK]   {msg}")


def _warn(msg: str) -> None:
    print(f"  [WARN] {msg}")


def _fail(msg: str) -> None:
    print(f"  [FAIL] {msg}")


def _info(msg: str) -> None:
    print(f"  [ .. ] {msg}")


# ---------------------------------------------------------------- 步骤 1
def check_node() -> bool:
    print("[1/5] 检测 Node.js 版本 ...")
    try:
        proc = subprocess.run(
            ["node", "--version"], capture_output=True, text=True, timeout=30, check=False
        )
    except (FileNotFoundError, OSError):
        _fail("未检测到 Node.js。")
        print_node_install_guide()
        return False

    if proc.returncode != 0:
        _fail(f"`node --version` 退出码 {proc.returncode}: {proc.stderr.strip()}")
        print_node_install_guide()
        return False

    raw = proc.stdout.strip().lstrip("v")
    match = re.match(r"^(\d+)", raw)
    major = int(match.group(1)) if match else 0
    if major < MIN_NODE_MAJOR:
        _fail(f"Node.js 版本过低：v{raw}（要求 ≥ v{MIN_NODE_MAJOR}）。")
        print_node_install_guide()
        return False

    _ok(f"Node.js v{raw}（要求 ≥ v{MIN_NODE_MAJOR}）")
    return True


def print_node_install_guide() -> None:
    print(
        "\n  Node.js 安装指引（本脚本不会代装）：\n"
        "    1. 访问 https://nodejs.org/ 下载 LTS 版本（≥ 20）安装包；\n"
        "    2. 或用版本管理器：winget install OpenJS.NodeJS.LTS / nvm install 22；\n"
        "    3. 安装后重开终端，执行 `node --version` 确认，再重跑本脚本。\n"
    )


# ---------------------------------------------------------------- 步骤 2
def read_pinned_version() -> str:
    pkg = json.loads((VENDOR_DSH_DIR / "package.json").read_text(encoding="utf-8"))
    return pkg["dependencies"]["@deepseek-ai/dsh"]


def installed_dsh_version() -> str | None:
    pkg_json = VENDOR_DSH_DIR / "node_modules" / "@deepseek-ai" / "dsh" / "package.json"
    if not pkg_json.is_file():
        return None
    try:
        return json.loads(pkg_json.read_text(encoding="utf-8")).get("version")
    except (OSError, json.JSONDecodeError):
        return None


def _run_npm_install(npm: str, extra_args: list[str]) -> subprocess.CompletedProcess:
    """执行一次本地 npm install（带抬高堆上限 + 隔离缓存）。"""
    install_env = dict(os.environ)
    install_env["NODE_OPTIONS"] = NPM_NODE_OPTIONS

    cmd = [
        npm,
        "install",
        "--no-audit",
        "--no-fund",
        # 使用工作台自己的缓存目录：既规避用户 npm 缓存里可能存在的
        # 过期 packument（实测会导致 ETARGET 假报错），又不污染用户缓存。
        "--cache",
        str(NPM_CACHE_DIR),
        *extra_args,
    ]
    return subprocess.run(
        cmd,
        cwd=str(VENDOR_DSH_DIR),
        env=install_env,
        capture_output=True,
        text=True,
        check=False,
    )


def install_vendor_dsh() -> bool:
    print("[2/5] 安装 vendor 内置 dsh（本地安装，禁止 -g）...")
    pinned = read_pinned_version()
    current = installed_dsh_version()

    if current == pinned and VENDOR_DSH_BIN_DIR.is_dir():
        _ok(f"已安装 @deepseek-ai/dsh@{current}，版本匹配，跳过 npm install。")
        return True

    if current is not None:
        _warn(f"已安装版本 {current} != 锁定版本 {pinned}，重新安装。")
    else:
        _info(f"开始 npm install @deepseek-ai/dsh@{pinned}（目录：{VENDOR_DSH_DIR}）")

    npm = get_npm_binary()
    if npm is None:
        _fail("未找到 npm 可执行文件。")
        return False

    _info(f"NODE_OPTIONS={NPM_NODE_OPTIONS}（规避大依赖树解析期 OOM）")
    _info(f"npm cache={NPM_CACHE_DIR}（隔离缓存，规避过期 packument 的 ETARGET）")

    proc = _run_npm_install(npm, [])
    if proc.returncode != 0 and _is_etarget(proc):
        # 缓存里可能残留过期 packument，强制联网刷新后重试一次
        _warn("检测到 ETARGET（多为缓存中的过期 packument），清缓存后重试一次 ...")
        subprocess.run(
            [npm, "cache", "clean", "--force", "--cache", str(NPM_CACHE_DIR)],
            capture_output=True,
            text=True,
            check=False,
        )
        proc = _run_npm_install(npm, ["--prefer-online"])

    if proc.returncode != 0:
        _fail(f"npm install 退出码 {proc.returncode}")
        tail = (proc.stdout or "")[-1500:] + (proc.stderr or "")[-1500:]
        print(tail)
        return False

    current = installed_dsh_version()
    if current != pinned:
        _fail(f"安装后版本不符：期望 {pinned}，实际 {current}")
        return False

    if not VENDOR_DSH_BIN_DIR.is_dir():
        _fail(f"未生成 .bin 目录：{VENDOR_DSH_BIN_DIR}")
        return False

    _ok(f"@deepseek-ai/dsh@{current} 安装完成，bin 目录：{VENDOR_DSH_BIN_DIR}")
    return True


def _is_etarget(proc: subprocess.CompletedProcess) -> bool:
    blob = f"{proc.stdout or ''}{proc.stderr or ''}"
    return "ETARGET" in blob or "notarget" in blob


# ---------------------------------------------------------------- 步骤 3
def init_dsh_home() -> bool:
    print("[3/5] 初始化独立 DSH_HOME ...")
    home = get_dsh_home()

    for sub in ("profiles", "sessions", "storages"):
        (home / sub).mkdir(parents=True, exist_ok=True)
        _ok(f"目录就绪：{home / sub}")

    settings = home / "settings.yaml"
    if settings.exists():
        _info(f"settings.yaml 已存在，保持不动：{settings}")
    else:
        settings.write_text(SETTINGS_TEMPLATE, encoding="utf-8")
        _ok(f"写入 settings.yaml 模板（无任何真实 API Key）：{settings}")

    profile_dir = home / "profiles" / PROFILE_NAME
    profile_dir.mkdir(parents=True, exist_ok=True)
    _init_profile(profile_dir)
    _ok(f"专用 profile 就绪（不经 dsh plugin，直接写入工程文件）：{profile_dir}")

    return True


def _init_profile(profile_dir: Path) -> None:
    """按 dsh 的 initProfile 格式初始化 profile 工程文件（幂等，不覆盖已有文件）。

    对齐 workbench/vendor/dsh/node_modules/@deepseek-ai/dsh-app-boot/lib/index.js
    L353-368 的实现，从而**无需调用被禁止的 `dsh plugin`**。
    """
    manifest = profile_dir / "package.json"
    if not manifest.exists():
        payload = {
            "name": f"dsh-profile-{profile_dir.name}",
            "private": True,
            "dependencies": {},
            "dsh": {"profile": {"bundles": list(PROFILE_BUNDLES)}},
        }
        manifest.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        _ok(f"写入 profile 清单：{manifest}")

    patch = profile_dir / "cordis.patch.yml"
    if not patch.exists():
        patch.write_text(PROFILE_PATCH_TEMPLATE, encoding="utf-8")
        _ok(f"写入 profile patch 层：{patch}")

    workspace = profile_dir / "pnpm-workspace.yaml"
    if not workspace.exists():
        workspace.write_text(PROFILE_PNPM_WORKSPACE, encoding="utf-8")
        _ok(f"写入 pnpm workspace 设置：{workspace}")

    readme = profile_dir / "README.md"
    if not readme.exists():
        readme.write_text(PROFILE_README, encoding="utf-8")


# ---------------------------------------------------------------- 步骤 4
def health_check() -> bool:
    print("[4/5] 健康检查：vendor dsh --dump-config（独立 DSH_HOME）...")
    try:
        binary = get_dsh_binary()
    except Exception as exc:  # noqa: BLE001
        _fail(str(exc))
        return False

    env = build_dsh_env()
    _info(f"binary = {binary}")
    _info(f"DSH_HOME = {env['DSH_HOME']}")

    # 实测：`--dump-config` 必须搭配 `--profile <name>`，否则
    # dsh 直接报 `error: --profile <name> is required`（退出码 1）。
    # 依次验证各 profile：随附模板的 headless + 工作台专用 ai-novel-workbench。
    for profile_name in (HEALTH_CHECK_PROFILE, PROFILE_NAME):
        try:
            proc = subprocess.run(
                [str(binary), "--profile", profile_name, "--dump-config"],
                cwd=str(_PROJECT_ROOT),
                env=env,
                capture_output=True,
                text=True,
                timeout=300,
                check=False,
            )
        except subprocess.TimeoutExpired:
            _fail(f"--profile {profile_name} --dump-config 超时（300s）")
            return False

        stdout = (proc.stdout or "").strip()
        stderr = (proc.stderr or "").strip()
        print(f"  [ profile={profile_name} ] 退出码 = {proc.returncode}, 配置树 {len(stdout.splitlines())} 行")
        if proc.returncode != 0:
            if stderr:
                print("  [ stderr 前 15 行 ]")
                for line in stderr.splitlines()[:15]:
                    print(f"      {line}")
            _fail(f"--profile {profile_name} --dump-config 非零退出。")
            return False

    _ok(f"--profile {HEALTH_CHECK_PROFILE} / {PROFILE_NAME} --dump-config 均退出码 0，内置 dsh 运行正常。")
    return True


# ---------------------------------------------------------------- 步骤 5
def print_ready_report(node_ok: bool, install_ok: bool, home_ok: bool, health_ok: bool) -> None:
    print("[5/5] 就绪报告")
    print("=" * 64)
    rows = [
        ("Node.js ≥ 20", node_ok),
        ("vendor dsh 安装", install_ok),
        ("独立 DSH_HOME 初始化", home_ok),
        ("健康检查 --dump-config", health_ok),
    ]
    for name, passed in rows:
        print(f"  {'PASS' if passed else 'FAIL'}  {name}")
    print("-" * 64)
    try:
        print(f"  dsh binary : {get_dsh_binary()}")
    except Exception:  # noqa: BLE001
        print("  dsh binary : <未安装>")
    print(f"  DSH_HOME   : {get_dsh_home()}")
    print(f"  vendor dir : {get_vendor_dsh_dir()}")
    print("=" * 64)
    print("  用户全局 ~/.dsh 全程未被读取或写入。")


def main() -> int:
    print("=" * 64)
    print(" AI 小说创作工作台 · 内置 dsh 引导安装")
    print(f" 项目根: {_PROJECT_ROOT}")
    print("=" * 64)

    node_ok = check_node()
    if not node_ok:
        print("\n引导中止：Node.js 环境不满足要求。")
        return 1

    install_ok = install_vendor_dsh()
    if not install_ok:
        print("\n引导中止：vendor dsh 安装失败。")
        print_ready_report(node_ok, install_ok, False, False)
        return 1

    home_ok = init_dsh_home()
    health_ok = health_check() if home_ok else False

    print()
    print_ready_report(node_ok, install_ok, home_ok, health_ok)

    if health_ok:
        print("\n就绪：内置 dsh 可用（隔离环境）。")
        return 0

    print("\n部分完成：隔离环境已建好，但健康检查未通过（见上方真实输出）。")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
