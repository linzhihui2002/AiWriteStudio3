#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""密度门禁（去 AI 味量化红线）—— 由 MIT 项目引入并改造。

来源声明（第三方引入登记见 docs/third-party-notices.md）
---------------------------------------------------------------------------
原项目   : huahuakandaima__human-novel-writer
许可证   : MIT License (Copyright (c) 2026 huahuakandaima)
原文件   : projects/huahuakandaima__human-novel-writer/scripts/density_check.py
引入日期 : 2026-09-19
修改说明 :
  1. 文件头补充本来源声明（原文件为 `density_check.py — 朱雀密度红线检查器`）；
  2. **检测逻辑与全部阈值保持不变**（阈值来自人工段 vs AI 段的同文对照实测）；
  3. 硬编码阈值 / 词表抽为模块级常量，并支持 `DensityConfig` 覆盖（配置化）；
  4. 新增 `run(text, config=None, **overrides) -> DensityResult` 的 import 接口，
     同时保留原 CLI 入口与退出码语义（0 = 通过，1 = 存在 FAIL，2 = 用法/文件错误）。
许可全文 : projects/huahuakandaima__human-novel-writer/LICENSE
---------------------------------------------------------------------------

对照红线（来自《读条》第 1 章同文实测：人工段 vs AI 段差异量化）：
    破折号  人工每 ~268 字 1 个 / AI 每 ~136 字 1 个 → 每 250 字 ≤1 个
    对话    人工每 ~67 字 1 个引号 → 每 120 字 ≥1 个引号（低于此即叙述堆砌）
    推测词  叙述层 0 容忍（似乎/显然/仿佛/大概/略显）——口语吐槽语境人工判断
    三连排比 全禁（不是X不是Y不是Z / 没有A没有B没有C / 记得X记得Y记得Z）
    省略号  留白利器，鼓励 ≥2 个（悬停/拖音，替代破折号）
    数字    具体化鼓励：数字出现次数是人工特征（AI 段 5 个 vs 人工段 26 个）

CLI 用法::

    python workbench/backend/gates/density_gate.py 稿件.md
    python workbench/backend/gates/density_gate.py 稿件.md --forbidden 突然 忽然 只见

import 用法::

    from workbench.backend.gates import density_gate

    result = density_gate.run(text)
    if not result.passed:
        print(result.report())
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

# ── 红线参数（唯一事实源，改这里即改门禁） ─────────────────────
DASH_LIMIT = 250          # 每 N 字允许 1 个破折号
DIALOG_MIN = 120          # 每 N 字至少 1 个对话引号

DEFAULT_FORBIDDEN: tuple[str, ...] = (
    "突然", "忽然", "只见", "紧接着", "话音刚落", "就在这时", "下一秒", "不由得",
    "霎时间", "良久", "微微一笑", "淡淡地说", "沉声道",
    # 2026-08-08 合并 story-deslop 一级禁用词（oh-story-claudecode）
    "仿佛", "犹如", "宛若", "一丝", "一抹", "些许", "隐约", "深吸一口气", "缓缓",
    "不禁", "微微", "轻轻", "淡淡",
    "眼中闪过", "嘴角勾起", "眉头微皱", "瞳孔微缩", "心中一动", "心头一震",
    "心下了然", "心中暗道", "心底泛起",
    "不容置疑", "不易察觉", "显而易见", "毫无疑问", "不可否认", "闪烁着光芒", "凛冽",
    "不由自主", "情不自禁", "自然而然",
)

# 二级：升华/总结句式（出现即提示，人工判断；命中即计入升华腔）
UPGRADE_PATTERNS: tuple[str, ...] = (
    r"这一刻", r"他终于明白", r"她终于明白", r"他终于意识到", r"她终于意识到",
    r"这才意识到", r"这就是[^。！？]{2,10}的意义", r"一切[^。！？]{2,10}都(?:变得|化为|归于)",
)

GUESS_WORDS: tuple[str, ...] = ("似乎", "显然", "仿佛", "大概", "略显")

TRIPLE_PATTERNS: tuple[str, ...] = (
    r"不是[^，。]{2,12}，不是[^，。]{2,12}，不是",   # 不是X，不是Y，不是Z
    r"没有[^，。]{2,12}，没有[^，。]{2,12}，没有",   # 没有A，没有B，没有C
    r"记得[^，。]{2,12}，记得[^，。]{2,12}，记得",   # 记得X，记得Y，记得Z
)

ELLIPSIS_MIN = 2          # 省略号鼓励下限（仅建议，不判 FAIL）

# 建议项标记（不判 FAIL）
ISSUE_DIALOG = "DIALOG"
ISSUE_FAIL = "FAIL"


@dataclass(frozen=True)
class DensityConfig:
    """密度门禁可配置参数。默认值即原脚本硬编码阈值（保持不变）。"""

    dash_limit: int = DASH_LIMIT
    dialog_min: int = DIALOG_MIN
    forbidden: tuple[str, ...] = DEFAULT_FORBIDDEN
    guess_words: tuple[str, ...] = GUESS_WORDS
    triple_patterns: tuple[str, ...] = TRIPLE_PATTERNS
    upgrade_patterns: tuple[str, ...] = UPGRADE_PATTERNS
    ellipsis_min: int = ELLIPSIS_MIN


@dataclass
class DensityResult:
    """密度门禁结果对象。`passed` 为 False 时 `issues` 非空。"""

    total: int
    dash_count: int
    dash_density: float | None
    dash_ok: bool
    quote_count: int
    quote_density: float | None
    dialog_ok: bool
    guess_hits: dict[str, int] = field(default_factory=dict)
    triple_hits: list[str] = field(default_factory=list)
    ellipsis: int = 0
    ellipsis_ok: bool = False
    number_count: int = 0
    forbidden_hits: dict[str, int] = field(default_factory=dict)
    upgrade_hits: dict[str, str] = field(default_factory=dict)
    issues: list[str] = field(default_factory=list)
    config: DensityConfig = field(default_factory=DensityConfig)

    @property
    def passed(self) -> bool:
        return not self.issues

    def report(self, name: str = "文本") -> str:
        """渲染与原 CLI 一致的报告文本（不含退出码）。"""
        cfg = self.config
        lines: list[str] = [f"=== 朱雀密度红线检查: {name} ({self.total} 字) ==="]

        if self.dash_count:
            mark = "✓" if self.dash_ok else "✗ FAIL"
            lines.append(
                f"破折号: {self.dash_count} 个 "
                f"(每 {self.dash_density:.0f} 字 1 个, 红线每 {cfg.dash_limit} 字) {mark}"
            )
            if not self.dash_ok:
                lines.append(
                    f"  提示: 需砍到 ≤{self.total // cfg.dash_limit} 个，"
                    "停顿用句号，悬停用省略号"
                )
        else:
            lines.append("破折号: 0 个 ✓")

        if self.quote_count:
            mark = "✓" if self.dialog_ok else "! 建议"
            lines.append(
                f"对话引号: {self.quote_count} 个 "
                f"(每 {self.quote_density:.0f} 字 1 次, 人工段约每 67 字) {mark}"
            )
            if not self.dialog_ok:
                lines.append("  提示: 连续叙述过长，把信息塞进对话")
        else:
            lines.append("对话引号: 0 个 ✗ 本章无对话？")

        if self.guess_hits:
            lines.append(
                f"推测词: {self.guess_hits} ! 叙述层应删除（吐槽口语可留，人工判断）"
            )
        else:
            lines.append("推测词: 0 个 ✓")

        if self.triple_hits:
            lines.append(f"三连排比: {len(self.triple_hits)} 处 ✗ FAIL: {self.triple_hits}")
        else:
            lines.append("三连排比: 0 处 ✓")

        if self.ellipsis >= cfg.ellipsis_min:
            lines.append(f"省略号: {self.ellipsis} 个 ✓ (留白利器)")
        else:
            lines.append(
                f"省略号: {self.ellipsis} 个 ! 建议 ≥{cfg.ellipsis_min}"
                "（台词拖音/悬停用省略号，别用破折号顶替）"
            )

        lines.append(
            f"数字: {self.number_count} 个 (人工段约 26 个/1600 字，具体数字是人工特征)"
        )

        if self.forbidden_hits:
            lines.append(f"禁词: {self.forbidden_hits} ✗ FAIL")
        else:
            lines.append("禁词: 全清零 ✓")

        if self.upgrade_hits:
            lines.append(
                f"升华腔: {self.upgrade_hits} ! 总结/点题句式"
                "（'这一刻/他终于明白/这就是…的意义'），人工判断是否删除"
            )

        lines.append("")
        if self.passed:
            lines.append("结论: 全部通过 ✓")
        else:
            lines.append(
                f"结论: {len(self.issues)} 项需处理 (FAIL=必须修, !=建议)"
            )
        return "\n".join(lines)


def run(
    text: str,
    config: DensityConfig | None = None,
    **overrides: object,
) -> DensityResult:
    """对正文文本执行密度红线检查。

    :param text: 正文文本（Markdown 或纯文本）。
    :param config: 完整配置；缺省使用模块级默认常量。
    :param overrides: 覆盖 `DensityConfig` 单个字段（如 ``dash_limit=200``）。
    :return: :class:`DensityResult`
    """
    cfg = config or DensityConfig()
    if overrides:
        cfg = DensityConfig(**{**cfg.__dict__, **overrides})

    body = re.sub(r"\s", "", text)
    total = len(body)
    issues: list[str] = []

    # 1. 破折号密度
    dashes = body.count("——")
    dash_density: float | None = None
    dash_ok = True
    if dashes:
        dash_density = total / dashes
        dash_ok = dash_density >= cfg.dash_limit
        if not dash_ok:
            issues.append(ISSUE_FAIL)

    # 2. 对话密度（兼容直角引号「」与弯引号“”，取配对对数近似）
    quotes = min(body.count("“"), body.count("”")) + body.count("「")
    quote_density: float | None = None
    dialog_ok = True
    if quotes:
        quote_density = total / quotes
        dialog_ok = quote_density <= cfg.dialog_min
        if not dialog_ok:
            issues.append(ISSUE_DIALOG)
    else:
        dialog_ok = False
        issues.append(ISSUE_FAIL)

    # 3. 推测词
    guess_hits = {w: body.count(w) for w in cfg.guess_words if body.count(w) > 0}

    # 4. 三连排比
    triple_hits: list[str] = []
    for pat in cfg.triple_patterns:
        for m in re.finditer(pat, body):
            triple_hits.append(m.group(0)[:30])
    if triple_hits:
        issues.append(ISSUE_FAIL)

    # 5. 省略号（留白鼓励）
    ellipsis = body.count("……")

    # 6. 数字具体化
    number_count = len(re.findall(r"[0-9０-９]", body))

    # 7. 禁词
    forbidden_hits = {w: body.count(w) for w in cfg.forbidden if body.count(w) > 0}
    if forbidden_hits:
        issues.append(ISSUE_FAIL)

    # 7.5 升华/总结句式（二级，出现即提示人工判断）
    upgrade_hits: dict[str, str] = {}
    for pat in cfg.upgrade_patterns:
        m = re.search(pat, body)
        if m:
            upgrade_hits[pat] = m.group(0)

    return DensityResult(
        total=total,
        dash_count=dashes,
        dash_density=dash_density,
        dash_ok=dash_ok,
        quote_count=quotes,
        quote_density=quote_density,
        dialog_ok=dialog_ok,
        guess_hits=guess_hits,
        triple_hits=triple_hits,
        ellipsis=ellipsis,
        ellipsis_ok=ellipsis >= cfg.ellipsis_min,
        number_count=number_count,
        forbidden_hits=forbidden_hits,
        upgrade_hits=upgrade_hits,
        issues=issues,
        config=cfg,
    )


def read_text(path: str) -> str:
    """读取稿件；``-`` 表示标准输入。"""
    if path == "-":
        return sys.stdin.read()
    return Path(path).read_text(encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="朱雀密度红线检查器（去 AI 味密度门禁）")
    parser.add_argument("path", help="Markdown 或文本文件路径。使用 - 从标准输入读取")
    parser.add_argument(
        "--forbidden",
        nargs="*",
        default=None,
        help="覆盖禁词表（缺省使用模块级 DEFAULT_FORBIDDEN）",
    )
    args = parser.parse_args(argv)

    try:
        text = read_text(args.path)
    except (OSError, UnicodeError) as error:
        print(f"文件不存在: {error}", file=sys.stderr)
        return 2

    overrides: dict[str, object] = {}
    if args.forbidden is not None:
        overrides["forbidden"] = tuple(args.forbidden)

    name = Path(args.path).name if args.path != "-" else "<stdin>"
    result = run(text, **overrides)
    print(result.report(name))
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
