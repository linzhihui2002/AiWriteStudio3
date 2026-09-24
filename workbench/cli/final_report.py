#!/usr/bin/env python
"""终验报告（Task 68）：跑完整套测试与专项自检，把结果落盘为可追溯报告。

运行：``python workbench/cli/final_report.py``

产出：``.workbench/reports/final_report.md`` 与 ``final_report.json``
（含各套件通过/失败/跳过数、专项自检结论、性能基准、环境信息）。
命令失败会被记录并如实反映（不美化、不隐藏 SKIP）。
"""

from __future__ import annotations

import json
import platform
import subprocess
import sys
import time
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from workbench.backend.engine.dsh_paths import (  # noqa: E402
    get_dsh_home,
    is_vendor_dsh_installed,
)

REPORT_DIR = _PROJECT_ROOT / ".workbench" / "reports"

SUITES: tuple[tuple[str, str, int], ...] = (
    ("后端单测（数据层/引擎/管线/内容/编排/向量）", "workbench/backend/tests", 1200),
    ("契约测试 contract ①-⑤", "workbench/tests/contract", 900),
    ("隔离测试 I1-I6", "workbench/tests/isolation", 900),
    ("E2E 冒烟（含 dsh 禁用降级）", "workbench/tests/e2e", 600),
    ("性能基准（200 章）", "workbench/tests/perf", 900),
)

CHECKS: tuple[tuple[str, list[str]], ...] = (
    ("裸调 dsh 静态扫描", ["workbench/cli/check_no_bare_dsh.py"]),
    ("UI 风格与 token 一致性", ["workbench/cli/check_ui_style.py"]),
    ("第三方许可证合规扫描", ["workbench/cli/check_license_compliance.py"]),
    ("用户 ~/.dsh 基线比对（只读）", ["workbench/cli/selfcheck.py", "--verify"]),
    ("卸载清单（dry-run）", ["workbench/cli/uninstall.py", "--dry-run"]),
)


def _run(command: list[str], timeout: int) -> dict:
    started = time.perf_counter()
    try:
        result = subprocess.run(
            command,
            cwd=str(_PROJECT_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        code = result.returncode
        output = (result.stdout or "") + (result.stderr or "")
    except subprocess.TimeoutExpired:
        code = 124
        output = f"超时（>{timeout}s）"
    except OSError as exc:
        code = 127
        output = str(exc)
    return {
        "exit_code": code,
        "duration_s": round(time.perf_counter() - started, 2),
        "tail": "\n".join(output.strip().splitlines()[-12:]),
    }


def _parse_pytest_tail(tail: str) -> dict:
    """从 pytest 汇总行提取 passed/failed/skipped。"""
    summary = {"passed": 0, "failed": 0, "skipped": 0}
    for line in reversed(tail.splitlines()):
        if "passed" in line or "failed" in line or "error" in line:
            for token in line.replace(",", " ").split():
                pass
            import re

            for key, pattern in (("passed", r"(\d+) passed"),
                                 ("failed", r"(\d+) failed"),
                                 ("skipped", r"(\d+) skipped")):
                match = re.search(pattern, line)
                if match:
                    summary[key] = int(match.group(1))
            if summary["passed"] or summary["failed"]:
                break
    return summary


def main() -> int:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    python = sys.executable

    print("=" * 70)
    print(" 终验：全量测试 + 专项自检（结果写入 .workbench/reports/）")
    print("=" * 70)

    suites: list[dict] = []
    for label, path, timeout in SUITES:
        print(f"\n[套件] {label}")
        outcome = _run([python, "-m", "pytest", path, "-q", "--no-header",
                        "-p", "no:cacheprovider"], timeout)
        outcome["label"] = label
        outcome["suite"] = path
        outcome.update(_parse_pytest_tail(outcome["tail"]))
        suites.append(outcome)
        print(f"  退出码 {outcome['exit_code']} · {outcome['duration_s']}s · "
              f"通过 {outcome.get('passed', 0)} / 失败 {outcome.get('failed', 0)} / "
              f"跳过 {outcome.get('skipped', 0)}")

    checks: list[dict] = []
    for label, command in CHECKS:
        print(f"\n[自检] {label}")
        outcome = _run([python, *command], 600)
        outcome["label"] = label
        outcome["command"] = " ".join(command)
        checks.append(outcome)
        print(f"  退出码 {outcome['exit_code']} · {outcome['duration_s']}s")

    environment = {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "node": _run(["node", "--version"], 60).get("tail", "").strip(),
        "dsh_vendor_installed": is_vendor_dsh_installed(),
        "dsh_home": str(get_dsh_home()),
    }

    total_passed = sum(item.get("passed", 0) for item in suites)
    total_failed = sum(item.get("failed", 0) for item in suites)
    total_skipped = sum(item.get("skipped", 0) for item in suites)
    all_green = all(item["exit_code"] == 0 for item in suites + checks)

    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "environment": environment,
        "suites": suites,
        "checks": checks,
        "totals": {"passed": total_passed, "failed": total_failed,
                   "skipped": total_skipped},
        "all_green": all_green,
    }

    json_path = REPORT_DIR / "final_report.json"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                         encoding="utf-8")

    lines = [
        "# 终验报告（Task 68）",
        "",
        f"- 生成时间：{report['generated_at']}",
        f"- Python {environment['python']} · Node {environment['node']} · {environment['platform']}",
        f"- 内置 dsh 就绪：{'是' if environment['dsh_vendor_installed'] else '否'}",
        f"- 独立 DSH_HOME：`{environment['dsh_home']}`",
        "",
        f"**汇总**：通过 {total_passed} · 失败 {total_failed} · 跳过 {total_skipped} · "
        f"总体结论 {'PASS' if all_green else 'FAIL'}",
        "",
        "## 测试套件",
        "",
        "| 套件 | 退出码 | 通过 | 失败 | 跳过 | 耗时 |",
        "|---|---|---|---|---|---|",
    ]
    for item in suites:
        lines.append(
            f"| {item['label']} | {item['exit_code']} | {item.get('passed', 0)} |"
            f" {item.get('failed', 0)} | {item.get('skipped', 0)} | {item['duration_s']}s |"
        )
    lines += ["", "## 专项自检", "", "| 检查 | 退出码 | 耗时 |", "|---|---|---|"]
    for item in checks:
        lines.append(f"| {item['label']} | {item['exit_code']} | {item['duration_s']}s |")

    lines += ["", "## 各套件输出（末尾 12 行）", ""]
    for item in suites + checks:
        lines += [f"### {item['label']}", "", "```", item["tail"], "```", ""]

    lines += [
        "## 如实说明",
        "",
        "- 依赖真实模型 Key 的契约用例（headless 最小任务）在未配置 Key 时**显式 SKIP**，不伪通过；",
        "- 隔离测试（I1-I6）为只读校验，不会修改用户环境；",
        "- 性能基准在 200 章临时项目上运行（首屏 < 2s / 上下文组装 < 1s / 取消 < 3s）。",
        "",
    ]
    md_path = REPORT_DIR / "final_report.md"
    md_path.write_text("\n".join(lines), encoding="utf-8")

    print("\n" + "=" * 70)
    print(f" 汇总：通过 {total_passed} · 失败 {total_failed} · 跳过 {total_skipped}"
          f" · 结论 {'PASS' if all_green else 'FAIL'}")
    print(f" 报告：{md_path.relative_to(_PROJECT_ROOT)}")
    print(f"       {json_path.relative_to(_PROJECT_ROOT)}")
    print("=" * 70)
    return 0 if all_green else 1


if __name__ == "__main__":
    raise SystemExit(main())