#!/usr/bin/env python
"""裸调 dsh 静态自检 —— 拦截绕过 vendor 内置 dsh 的调用痕迹。

扫描 ``workbench/`` 下所有 ``.py`` / ``.ts`` / ``.tsx`` 源码，检测两类违规：

1. **命令字面量**：出现裸 ``dsh`` 命令字面量（如 ``"dsh"``、``'dsh.cmd'``、
   ``"dsh --profile headless"``），即把 dsh 当作 PATH 上的全局命令使用；
2. **子进程调用**：出现子进程 API（subprocess / Popen / spawn / os.system …）
   的同一行同时出现裸 ``dsh`` 标记，且**不含** ``vendor`` 路径。

合法写法：通过 ``workbench.backend.engine.dsh_paths.get_dsh_binary()`` 取
vendor 内绝对路径后再调用（例如 ``subprocess.run([str(get_dsh_binary()), ...])``）。

白名单：``dsh_paths.py`` 自身、``check_no_bare_dsh.py`` 自身、注释行、docstring。

用法::

    python workbench/cli/check_no_bare_dsh.py

退出码：0 = 通过；1 = 发现违规。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
WORKBENCH_DIR = _PROJECT_ROOT / "workbench"

SCAN_SUFFIXES = {".py", ".ts", ".tsx"}
SKIP_DIR_NAMES = {"node_modules", "vendor", "__pycache__", ".venv", "venv", "dist", "build"}

# 白名单：允许出现裸 dsh 字面量的文件（dsh 解析入口、自检脚本、卸载脚本自身）
WHITELIST_FILES = {
    (WORKBENCH_DIR / "backend" / "engine" / "dsh_paths.py").resolve(),
    (WORKBENCH_DIR / "cli" / "check_no_bare_dsh.py").resolve(),
    # 卸载脚本要按名字移除 vendor/dsh 目录（是路径而非命令调用），
    # 且必须显式打印「不会触碰用户 ~/.dsh」的说明。
    (WORKBENCH_DIR / "cli" / "uninstall.py").resolve(),
}

# 行内豁免标记：确有必要书写 bare dsh 字面量（如构造 PATH 哨兵）时，
# 在同一行加 `# bare-dsh-ok: <理由>`，并在代码评审中可追溯。
INLINE_ALLOW_MARKER = "bare-dsh-ok"

# 裸 dsh 命令标记：dsh 前后不能是标识符/路径分隔字符
#   命中:  "dsh"   'dsh.cmd'   dsh --profile headless
#   排除:  dsh_paths  dsh-home  .dsh  setup_dsh.py  DSH_HOME
BARE_DSH_RE = re.compile(r"(?<![A-Za-z0-9_.\-/\\])dsh(?:\.(?:cmd|ps1|exe|js|mjs))?(?![A-Za-z0-9_.\-])")

# 引号字符串内容：整体就是一条裸 dsh 命令名（如 "dsh" / 'dsh.cmd'）
BARE_CMD_LITERAL_RE = re.compile(r"""["']dsh(?:\.(?:cmd|ps1|exe|js|mjs))?["']""")

# 引号字符串内容：以 dsh + CLI 参数开头（如 "dsh --profile headless"、"dsh plugin ..."）
BARE_CMD_WITH_ARGS_RE = re.compile(r"""["']dsh\s+(?:plugin\b|-{1,2}\w)""")

# 子进程 API 提示词（小写比对）
SUBPROCESS_HINTS = (
    "subprocess",
    "popen",
    "check_output",
    "check_call",
    "os.system",
    "os.exec",
    "os.spawn",
    "execsync",
    "spawnsync",
    "child_process",
    "execfile",
    "exec(",
    "run(",
)

# 判定"已正确走 vendor"的豁免标记
VENDOR_MARKERS = ("vendor", "get_dsh_binary", "dsh_paths")

# 非命令上下文豁免：npm 包路径（node_modules/@deepseek-ai/dsh/...）与日志/文案输出
PATH_CONTEXT_MARKERS = ("@deepseek-ai", "node_modules", "npm view", "npm ls")
# profile 清单里的 `"dsh": { "profile": { "bundles": [...] } }` 是 dsh 的数据字段
# （见 dsh-app-boot initProfile），不是命令调用，属合法用法。
MANIFEST_CONTEXT_MARKERS = ('"dsh":', "'dsh':")
LOG_CONTEXT_MARKERS = (
    "print(",
    "console.log",
    "console.error",
    "console.warn",
    "_ok(",
    "_info(",
    "_warn(",
    "_fail(",
    "logger.",
    "logging.",
)


class Violation:
    def __init__(self, path: Path, lineno: int, reason: str, text: str) -> None:
        self.path = path
        self.lineno = lineno
        self.reason = reason
        self.text = text.strip()

    def __str__(self) -> str:
        rel = self.path.relative_to(_PROJECT_ROOT)
        return f"{rel}:{self.lineno}  [{self.reason}]  {self.text}"


def iter_source_files() -> list[Path]:
    files: list[Path] = []
    for path in WORKBENCH_DIR.rglob("*"):
        if not path.is_file() or path.suffix not in SCAN_SUFFIXES:
            continue
        if any(part in SKIP_DIR_NAMES for part in path.parts):
            continue
        files.append(path)
    return sorted(files)


def strip_comment(line: str) -> str:
    """去掉行内 ``#`` / ``//`` 注释（粗略处理，够用即可）。"""
    for marker in ("#", "//"):
        idx = line.find(marker)
        if idx != -1:
            line = line[:idx]
    return line


def iter_code_lines(path: Path):
    """逐行产出 (行号, 代码内容)，跳过 docstring 内部与注释行。"""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return

    in_docstring = False
    quote_token = ""
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw
        stripped = line.strip()

        # 三引号 docstring 状态机
        if in_docstring:
            if quote_token in line:
                in_docstring = False
                line = line.split(quote_token, 1)[1]
            else:
                continue

        for token in ('"""', "'''"):
            if line.count(token) == 1 and not in_docstring:
                # 开启一个跨行 docstring
                in_docstring = True
                quote_token = token
                line = line.split(token, 1)[0]
                break
            if line.count(token) >= 2:
                # 单行 docstring，整体剔除
                first = line.find(token)
                second = line.find(token, first + 3)
                line = line[:first] + line[second + 3 :]

        if stripped.startswith("#") or stripped.startswith("//"):
            continue

        yield lineno, strip_comment(line)


def check_file(path: Path) -> list[Violation]:
    if path.resolve() in WHITELIST_FILES:
        return []

    try:
        raw_lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        raw_lines = []

    violations: list[Violation] = []
    for lineno, code in iter_code_lines(path):
        lowered = code.lower()

        # 行内豁免：作者显式标注理由（如构造 PATH 哨兵假命令，用于验证「零调用」）
        if 0 < lineno <= len(raw_lines) and INLINE_ALLOW_MARKER in raw_lines[lineno - 1]:
            continue

        # 豁免：npm 包路径上下文（卸载/版本判断里引用 @deepseek-ai/dsh 目录）
        if any(marker in code for marker in PATH_CONTEXT_MARKERS):
            continue

        # 豁免：profile 清单里的 "dsh": { "profile": ... } 数据字段
        if any(marker in code for marker in MANIFEST_CONTEXT_MARKERS):
            continue

        matches = list(BARE_DSH_RE.finditer(code))

        # 显式子进程调用是最高优先级风险
        has_subprocess = any(hint in lowered for hint in SUBPROCESS_HINTS)
        bare_cmd_literal = bool(BARE_CMD_LITERAL_RE.search(code))
        bare_cmd_with_args = bool(BARE_CMD_WITH_ARGS_RE.search(code))

        if has_subprocess and matches and not any(marker in code for marker in VENDOR_MARKERS):
            if bare_cmd_literal or bare_cmd_with_args or any(
                code[m.start() - 1] in ('"', "'") if m.start() > 0 else True for m in matches
            ):
                violations.append(Violation(path, lineno, "子进程裸调 dsh", code))
                continue

        # 纯日志/文案输出不予追究（信息性文字，非执行路径）
        if any(marker in lowered for marker in LOG_CONTEXT_MARKERS):
            continue

        # 裸 dsh 命令字面量 / 带参数调用字面量
        if bare_cmd_literal or bare_cmd_with_args:
            violations.append(Violation(path, lineno, "裸 dsh 命令字面量", code))

    return violations


def main() -> int:
    print("=" * 64)
    print(" 裸调 dsh 自检（workbench/ 源码静态扫描）")
    print(f" 扫描根: {WORKBENCH_DIR}")
    print("=" * 64)

    files = iter_source_files()
    print(f" 待扫描文件数: {len(files)}")
    for path in files:
        print(f"   - {path.relative_to(_PROJECT_ROOT)}")

    all_violations: list[Violation] = []
    for path in files:
        all_violations.extend(check_file(path))

    print("-" * 64)
    if not all_violations:
        print(" PASS：未发现裸调 dsh 痕迹。")
        print(" 所有 dsh 调用均须经由 dsh_paths.get_dsh_binary() 获取 vendor 绝对路径。")
        return 0

    print(f" FAIL：发现 {len(all_violations)} 处违规：")
    for violation in all_violations:
        print(f"   {violation}")
    print("-" * 64)
    print(" 修复指引：改用 workbench.backend.engine.dsh_paths.get_dsh_binary()")
    print(" 获取 vendor 内 dsh 绝对路径，并配合 build_dsh_env() 注入独立 DSH_HOME。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
