#!/usr/bin/env python
"""第三方许可证合规自检 —— 扫描禁用项目的特征字符串。

背景
----
参考项目位于 ``projects/``（只读，见 ``docs/third-party-notices.md``）。
其中 GPL / AGPL / CC BY-NC-SA / 无 LICENSE 项目**只能借鉴思想，不得复制任何
文件内容**。本脚本防止「无意把禁用项目的代码或文本搬进本仓库」。

扫描范围
--------
默认扫描根为项目根，实际扫描 ``workbench/``、``skills/``、``.dsh/`` 三处
（技能文本移植是高风险路径：CC BY-NC-SA 的禁令明确点名 ``skills/**`` 文本）。
跳过依赖与构建产物目录（``node_modules`` / ``vendor`` / ``__pycache__`` /
``dist`` / ``build`` / ``.venv`` / ``.pytest_cache`` …）。

白名单设计（为什么这样设计）
---------------------------
直接按字符串判定必然误报：讨论许可证、说明端口用途、记录引入来源时都会合法
点名禁用项目。因此本脚本采用「分层豁免 + 显式痕迹」策略，而不是简单忽略关键词：

1. **路径豁免 ``docs/**``**：合规登记与调研文档必须能点名禁用项目，
   否则无法说明「为什么不引入」。文档里的提及不构成引入。
2. **路径豁免 ``projects/**``**：第三方只读参考本身就在那里，不参与扫描。
3. **路径豁免 ``workbench/backend/gates/**``**：引入的 MIT 文件必须在文件头
   写明「原项目名 + 原文件路径」作为来源声明；要求来源声明不许点名原项目，
   会与合规要求自相矛盾。
4. **整行注释豁免**（行首 ``#`` / ``//``）：注释是讨论区，不是引入路径。
   纯注释里的提及只作提示级，不判违规。
5. **行内显式豁免标记** ``compliance-ok``：用于字符串/文案里**确有必要的
   说明性提及**（如错误信息里解释某端口属于哪个外部工具）。它必须写在同一行，
   从而在代码评审时可见、可追溯；不给「整文件免检」的口子。
6. **自豁免**：本脚本自身持有禁用标记清单，不扫描自己。

未命中以上任何一条豁免的命中即判违规，按 ``文件:行号`` 报告并非零退出。

用法::

    python workbench/cli/check_license_compliance.py
    python workbench/cli/check_license_compliance.py --root . --verbose

退出码：0 = 通过；1 = 发现违规。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]

# 默认扫描根（相对项目根）
DEFAULT_SCAN_TARGETS: tuple[str, ...] = ("workbench", "skills", ".dsh")

# 扫描的文本/源码后缀
SCAN_SUFFIXES: frozenset[str] = frozenset(
    {
        ".py", ".pyi", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs",
        ".json", ".md", ".txt", ".yml", ".yaml", ".html", ".css",
        ".sh", ".toml", ".cfg", ".ini",
    }
)

# 跳过目录（依赖、构建产物、缓存）
SKIP_DIR_NAMES: frozenset[str] = frozenset(
    {
        "node_modules", "vendor", "__pycache__", "dist", "build",
        ".venv", "venv", ".pytest_cache", ".mypy_cache", ".ruff_cache",
        ".git", "projects",
    }
)

# 路径豁免前缀（相对项目根，POSIX 分隔）
EXEMPT_PATH_PREFIXES: tuple[str, ...] = (
    "docs",                       # 合规登记 / 调研文档必须能点名禁用项目
    "projects",                   # 第三方只读参考，不参与扫描
    "workbench/backend/gates",    # MIT 引入文件的来源声明必须点名原项目
)

# 行内显式豁免标记（必须与命中同行，评审可见）
INLINE_EXEMPT_MARKERS: tuple[str, ...] = ("compliance-ok", "noqa: compliance")

# 整行注释前缀
COMMENT_PREFIXES: tuple[str, ...] = ("#", "//", "--", ";")

# 禁用项目特征字符串 → 原因
BANNED_MARKERS: tuple[tuple[str, str], ...] = (
    # ── CC BY-NC-SA 4.0：禁止移植其 .harness/**、skills/**、memory/**、rag/** 文本 ──
    ("novel-harness", "manhai934__novel-harness 为 CC BY-NC-SA 4.0，禁用"),
    ("manhai934", "manhai934__novel-harness 为 CC BY-NC-SA 4.0，禁用"),
    ("tianming-skill", "zy-zmc__tianming-skill 为 CC BY-NC-SA 4.0，禁用"),
    ("zy-zmc", "zy-zmc__tianming-skill 为 CC BY-NC-SA 4.0，禁用"),
    # ── AGPL：禁用 ──
    ("ExplosiveCoderflome", "AI-Novel-Writing-Assistant 为 AGPL，禁用"),
    ("AI-Novel-Writing-Assistant", "AI-Novel-Writing-Assistant 为 AGPL，禁用"),
    # ── GPL：仅思想，不得复制 ──
    ("MuMuAINovel", "xiamuceer-j__MuMuAINovel 为 GPL，仅思想"),
    ("xiamuceer-j", "xiamuceer-j__MuMuAINovel 为 GPL，仅思想"),
    ("Mochocyang", "Mochocyang__QMAI 为 GPL，仅思想"),
    ("QMAI", "Mochocyang__QMAI 为 GPL，仅思想"),
    # ── 无 LICENSE：保留全部权利，禁用 ──
    ("analyze-hit-novel", "ciel-oliver__analyze-hit-novel 无 LICENSE，禁用"),
    ("ciel-oliver", "ciel-oliver__analyze-hit-novel 无 LICENSE，禁用"),
    ("inliver233", "inliver233__Ai-Novel 无 LICENSE，禁用"),
    ("Jieice__", "Jieice__ai-novel-writer 无 LICENSE，禁用"),
    ("luciferlihaoyu", "luciferlihaoyu__novel-skill-cards 无 LICENSE，禁用"),
    ("novel-skill-cards", "luciferlihaoyu__novel-skill-cards 无 LICENSE，禁用"),
    ("qi531416", "qi531416__novel-humanize 无 LICENSE，禁用"),
    ("novel-humanize", "qi531416__novel-humanize 无 LICENSE，禁用"),
)

SELF_PATH = Path(__file__).resolve()


class Violation:
    def __init__(self, path: Path, lineno: int, marker: str, reason: str, text: str) -> None:
        self.path = path
        self.lineno = lineno
        self.marker = marker
        self.reason = reason
        self.text = text.strip()

    def __str__(self) -> str:
        try:
            rel = self.path.relative_to(_PROJECT_ROOT)
        except ValueError:
            rel = self.path
        return f"{rel}:{self.lineno}  [{self.marker}]  {self.reason}\n      {self.text}"


def _rel_posix(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def is_exempt_path(rel_posix: str) -> bool:
    """路径级豁免：见模块 docstring 的白名单设计 1-3。"""
    return any(
        rel_posix == prefix or rel_posix.startswith(prefix + "/")
        for prefix in EXEMPT_PATH_PREFIXES
    )


def iter_scan_files(root: Path, targets: tuple[str, ...]) -> list[Path]:
    files: list[Path] = []
    for target in targets:
        base = root / target
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in SCAN_SUFFIXES:
                continue
            if any(part in SKIP_DIR_NAMES for part in path.parts):
                continue
            if path.resolve() == SELF_PATH:
                continue
            if is_exempt_path(_rel_posix(path, root)):
                continue
            files.append(path)
    return sorted(files)


def check_file(path: Path) -> list[Violation]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []

    violations: list[Violation] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        hits = [(marker, reason) for marker, reason in BANNED_MARKERS if marker in line]
        if not hits:
            continue
        stripped = line.strip()
        # 白名单设计 4：整行注释豁免（讨论区，不是引入路径）
        if stripped.startswith(COMMENT_PREFIXES):
            continue
        # 白名单设计 5：行内显式豁免标记（可见、可追溯）
        if any(marker in line for marker in INLINE_EXEMPT_MARKERS):
            continue
        for marker, reason in hits:
            violations.append(Violation(path, lineno, marker, reason, line))
    return violations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="第三方许可证合规自检（禁用项目特征扫描）")
    parser.add_argument("--root", default=str(_PROJECT_ROOT), help="项目根（默认：本仓库根）")
    parser.add_argument(
        "--targets",
        nargs="*",
        default=list(DEFAULT_SCAN_TARGETS),
        help=f"扫描目标目录（默认：{' '.join(DEFAULT_SCAN_TARGETS)}）",
    )
    parser.add_argument("--verbose", action="store_true", help="列出每个被扫描的文件")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    targets = tuple(args.targets)

    print("=" * 72)
    print(" 第三方许可证合规自检（禁用项目特征扫描）")
    print(f" 项目根: {root}")
    print(f" 扫描目标: {', '.join(targets)}")
    print(f" 禁用特征: {len(BANNED_MARKERS)} 条")
    print("=" * 72)

    files = iter_scan_files(root, targets)
    print(f" 待扫描文件数: {len(files)}")
    if args.verbose:
        for path in files:
            print(f"   - {_rel_posix(path, root)}")

    violations: list[Violation] = []
    for path in files:
        violations.extend(check_file(path))

    print("-" * 72)
    if not violations:
        print(" PASS：未发现禁用项目的特征字符串。")
        print(" 豁免范围：docs/**、projects/**、workbench/backend/gates/**、整行注释、")
        print("          行内 compliance-ok 标记、本脚本自身。")
        return 0

    print(f" FAIL：发现 {len(violations)} 处违规：")
    for violation in violations:
        print(f"   {violation}")
    print("-" * 72)
    print(" 修复指引：删除引入内容，或若确为说明性提及，")
    print(" 在同行加 `# compliance-ok: <用途>` 显式登记。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
