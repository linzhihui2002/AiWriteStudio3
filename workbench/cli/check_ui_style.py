#!/usr/bin/env python
"""UI 风格审查与去 AI 感自检（Task 64）。

按 spec 的禁用清单核查前端源码，并检查 token 一致性：

禁用清单
--------
1. 紫色渐变 / 装饰性渐变（营销风）；
2. 滥用玻璃拟态（backdrop-filter + 半透明大面板）；
3. emoji 充作图标；
4. 居中大标题堆叠；
5. 无差别卡片网格（所有区块都套卡片）；
6. 散落硬编码色值（必须走 token 变量）。

附加检查
--------
- 反 AI 腔文案：正文级「赋能/一站式/极致体验」等营销词；
- 圆角/阴影 token 使用（组件里不得直接写 border-radius 像素值）。

用法::

    python workbench/cli/check_ui_style.py            # 审查（退出码 0/1）
    python workbench/cli/check_ui_style.py --verbose  # 打印全部命中
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = _PROJECT_ROOT / "workbench" / "frontend" / "src"

SCAN_SUFFIXES = {".css", ".tsx", ".ts"}
SKIP_DIR_NAMES = {"node_modules", "dist", "__pycache__"}

# 允许出现色值的文件（token 定义本身）
TOKEN_FILES = {"tokens.css", "theme.css"}

HEX_COLOR_RE = re.compile(r"#[0-9a-fA-F]{3,8}\b")
RGB_RE = re.compile(r"\b(?:rgb|rgba|hsl|hsla)\(")
GRADIENT_RE = re.compile(r"gradient\(")
GLASS_RE = re.compile(r"backdrop-filter\s*:", re.IGNORECASE)
EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF]"
)
MARKETING_WORDS = ("赋能", "一站式", "极致体验", "全链路", "生态闭环", "颠覆式", "智能赋能")
CENTERED_TITLE_RE = re.compile(r"text-align\s*:\s*center")
# 注意：负向预查必须带上 `\s*`，否则 `\s*` 会回溯到空匹配从而误判 `var(--x)` 为裸值
RAW_RADIUS_RE = re.compile(r"border-radius\s*:\s*(?!\s*var\()")
RAW_SHADOW_RE = re.compile(r"box-shadow\s*:\s*(?!\s*var\()")
RAW_ZINDEX_RE = re.compile(r"z-index\s*:\s*(?!\s*var\()")

# 允许的例外：身体文本对齐居中用于空状态/徽标（有明确语义），用行内注释标记
ALLOW_MARKER = "ui-style-ok"


class Finding:
    def __init__(self, path: Path, lineno: int, rule: str, text: str) -> None:
        self.path = path
        self.lineno = lineno
        self.rule = rule
        self.text = text.strip()

    def __str__(self) -> str:
        rel = self.path.relative_to(_PROJECT_ROOT)
        return f"{rel}:{self.lineno}  [{self.rule}]  {self.text[:90]}"


def iter_files() -> list[Path]:
    files: list[Path] = []
    for path in SRC_DIR.rglob("*"):
        if not path.is_file() or path.suffix not in SCAN_SUFFIXES:
            continue
        if any(part in SKIP_DIR_NAMES for part in path.parts):
            continue
        files.append(path)
    return sorted(files)


def check_file(path: Path) -> list[Finding]:
    findings: list[Finding] = []
    is_token_file = path.name in TOKEN_FILES
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return findings

    for lineno, raw in enumerate(lines, start=1):
        line = raw.strip()
        if not line or line.startswith(("*", "//", "/*")):
            continue
        if ALLOW_MARKER in line:
            continue
        # 上一行注释也可豁免（注释说明紧随其后的写法）
        if lineno >= 2 and ALLOW_MARKER in lines[lineno - 2]:
            continue

        if not is_token_file and (HEX_COLOR_RE.search(line) or RGB_RE.search(line)):
            findings.append(Finding(path, lineno, "硬编码色值", line))
        if GRADIENT_RE.search(line) and not is_token_file:
            findings.append(Finding(path, lineno, "渐变（禁用清单 1）", line))
        if GLASS_RE.search(line):
            findings.append(Finding(path, lineno, "玻璃拟态（禁用清单 2）", line))
        if EMOJI_RE.search(line):
            findings.append(Finding(path, lineno, "emoji 图标（禁用清单 3）", line))
        for word in MARKETING_WORDS:
            if word in line:
                findings.append(Finding(path, lineno, "营销腔文案（去 AI 感）", line))
                break
        if path.suffix == ".css":
            if RAW_RADIUS_RE.search(line):
                findings.append(Finding(path, lineno, "圆角未走 token", line))
            if RAW_SHADOW_RE.search(line):
                findings.append(Finding(path, lineno, "阴影未走 token", line))
            if RAW_ZINDEX_RE.search(line):
                findings.append(Finding(path, lineno, "层级未走 token", line))

    # 居中大标题堆叠：统计 .page-header__title 是否被 text-align:center
    if path.suffix == ".css":
        text = "\n".join(lines)
        if CENTERED_TITLE_RE.search(text) and "page-header" in text:
            findings.append(
                Finding(path, 0, "居中大标题（禁用清单 4）", "page-header 出现 text-align:center")
            )
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description="工作台 UI 风格与去 AI 感自检")
    parser.add_argument("--verbose", action="store_true", help="打印全部命中")
    args = parser.parse_args()

    print("=" * 68)
    print(" UI 风格审查（禁用清单 + token 一致性 + 去 AI 感文案）")
    print(f" 扫描目录：{SRC_DIR}")
    print("=" * 68)

    files = iter_files()
    findings: list[Finding] = []
    for path in files:
        findings.extend(check_file(path))

    by_rule: dict[str, list[Finding]] = {}
    for finding in findings:
        by_rule.setdefault(finding.rule, []).append(finding)

    print(f" 扫描文件 {len(files)} 个，命中 {len(findings)} 处")
    if findings:
        for rule, items in sorted(by_rule.items()):
            print(f"\n [{rule}] {len(items)} 处")
            limit = None if args.verbose else 5
            for item in (items if limit is None else items[:limit]):
                print(f"   {item}")
            if limit is not None and len(items) > limit:
                print(f"   … 其余 {len(items) - limit} 处（--verbose 查看全部）")

    # token 一致性：组件 CSS 必须引用变量（抽样统计）
    token_usage = 0
    raw_usage = 0
    for path in files:
        if path.suffix != ".css" or path.name in TOKEN_FILES:
            continue
        text = path.read_text(encoding="utf-8")
        token_usage += text.count("var(--")
        raw_usage += len(HEX_COLOR_RE.findall(text))
    print("\n token 使用统计：")
    print(f"   var(--…) 引用 {token_usage} 次；组件内裸色值 {raw_usage} 处")
    print("-" * 68)
    if findings:
        print(" FAIL：存在禁用清单命中或未走 token 的写法（见上）")
        return 1
    print(" PASS：未命中禁用清单；token 使用一致。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())