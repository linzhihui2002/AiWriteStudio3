#!/usr/bin/env python
"""金标准评测集跑测脚本（**不接入 pytest / CI**）。

本脚本供作者用**真实模型**做「路由效果评测」对照：一次运行结果只是当次观察，
不构成产品结论。用例与判定口径见 ``evals/agent-scope/README.md``。

两种模式
--------
``--dry-run``：离线字段校验。不调用路由、不调用模型、不写项目文件——
    读取 ``evals/agent-scope/cases.json``，检查 JSON 合法性、字段齐全、
    ``expected_agent`` 在 Agent 注册表内、写域元素是材料目录名或 ``xx/yy.md``
    相对路径、``expected_plan_steps`` 为非负整数；打印汇总与不合规项，
    存在不合规项时非零退出。

不带 ``--dry-run``：真实跑一轮对照。需 ``--project-id``（缺省会提示必须提供），
    逐条调用 ``routing_service.route(project_id, input, record=False)``，打印
    「期望 vs 实际」对照表与命中统计。**不写入任何文件、不改任何数据**
    （``record=False`` 关闭路由操作日志）。

用法::

    python evals/run_cases.py --dry-run
    python evals/run_cases.py --project-id 1

隔离红线：不依赖 PATH 上的全局 ``dsh``，不读写用户全局 ``~/.dsh``。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CASES_PATH = REPO_ROOT / "evals" / "agent-scope" / "cases.json"

# 令脚本可直接 `python evals/run_cases.py` 运行（与后端 `from workbench.backend...` 同源）
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

REQUIRED_FIELDS = (
    "id", "tags", "input", "expected_agent",
    "expected_write_targets", "forbidden_write_targets",
    "expected_plan_steps", "expectation",
)
OPTIONAL_FIELDS = ("context", "expected_read_only_refs", "notes")


# ─────────────────────────── 加载与字段校验 ───────────────────────────


def load_cases() -> tuple[list[dict], str]:
    """读取用例文件；返回 ``(cases, 错误说明)``，无错时错误说明为空串。"""
    try:
        raw = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return [], f"用例文件不存在：{CASES_PATH}"
    except json.JSONDecodeError as exc:
        return [], f"用例 JSON 非法：{exc}"
    if not isinstance(raw, dict) or not isinstance(raw.get("cases"), list):
        return [], "用例文件顶层必须是对象，且含数组字段 cases"
    return list(raw["cases"]), ""


def _is_material(value: object, material_dirs: tuple[str, ...]) -> bool:
    """写域元素 = 材料目录名，或形如 ``xx/yy.md`` 的本书相对路径。"""
    if not isinstance(value, str):
        return False
    item = value.strip().replace("\\", "/").strip("/")
    if not item:
        return False
    if item in material_dirs:
        return True
    parts = item.split("/")
    return (len(parts) >= 2 and parts[0] in material_dirs
            and item.endswith(".md") and ".." not in parts)


def validate_case(case: object, index: int, agent_names: list[str],
                  material_dirs: tuple[str, ...]) -> list[str]:
    """单条用例的字段校验；返回不合规项说明列表（空列表 = 合规）。"""
    if not isinstance(case, dict):
        return [f"第 {index} 条用例：不是 JSON 对象"]
    cid = case.get("id")
    where = f"用例 {cid}" if isinstance(cid, str) and cid.strip() else f"第 {index} 条用例"
    problems: list[str] = []

    for field in REQUIRED_FIELDS:
        if field not in case:
            problems.append(f"{where}：缺少必需字段 {field}")
    for field in case:
        if field not in REQUIRED_FIELDS and field not in OPTIONAL_FIELDS:
            problems.append(f"{where}：未知字段 {field}")

    if not isinstance(cid, str) or not cid.strip():
        problems.append(f"{where}：id 必须是非空字符串")

    tags = case.get("tags")
    if (not isinstance(tags, list) or not tags
            or not all(isinstance(item, str) and item.strip() for item in tags)):
        problems.append(f"{where}：tags 必须是非空字符串数组")

    text = case.get("input")
    if not isinstance(text, str) or not text.strip():
        problems.append(f"{where}：input 必须是非空字符串")

    agent = case.get("expected_agent")
    if not isinstance(agent, str) or agent not in agent_names:
        problems.append(
            f"{where}：expected_agent「{agent}」不在 Agent 注册表内"
            f"（可用：{'、'.join(agent_names)}）"
        )

    for field in ("expected_write_targets", "forbidden_write_targets",
                  "expected_read_only_refs"):
        if field not in case:
            continue
        values = case.get(field)
        if not isinstance(values, list):
            problems.append(f"{where}：{field} 必须是数组")
            continue
        for value in values:
            if not _is_material(value, material_dirs):
                problems.append(
                    f"{where}：{field} 元素「{value}」既不是材料目录名"
                    f"（{'、'.join(material_dirs)}）也不是 xx/yy.md 相对路径"
                )

    steps = case.get("expected_plan_steps")
    if isinstance(steps, bool) or not isinstance(steps, int) or steps < 0:
        problems.append(f"{where}：expected_plan_steps 必须是非负整数")

    expectation = case.get("expectation")
    if not isinstance(expectation, str) or not expectation.strip():
        problems.append(f"{where}：expectation 必须是非空字符串")

    if "notes" in case and not isinstance(case.get("notes"), str):
        problems.append(f"{where}：notes 必须是字符串")
    if "context" in case and not isinstance(case.get("context"), dict):
        problems.append(f"{where}：context 必须是对象")

    return problems


# ─────────────────────────── 离线校验（--dry-run） ───────────────────────────


def dry_run() -> int:
    from workbench.backend.services import agent_service

    agent_names = sorted(item["name"] for item in agent_service.list_all_agents())
    material_dirs = tuple(agent_service.MATERIAL_DIRS)

    cases, error = load_cases()
    if error:
        print(f"[FAIL] {error}")
        return 1

    problems: list[str] = []
    seen_ids: set[str] = set()
    for index, case in enumerate(cases, start=1):
        problems.extend(validate_case(case, index, agent_names, material_dirs))
        cid = case.get("id") if isinstance(case, dict) else None
        if isinstance(cid, str) and cid.strip():
            if cid in seen_ids:
                problems.append(f"用例 {cid}：id 重复")
            seen_ids.add(cid)

    print("=" * 68)
    print(" 金标准评测集 · 离线校验（--dry-run）")
    print(f" 用例文件：{CASES_PATH}")
    print(f" 用例条数:{len(cases)}")
    print(f" 注册表 Agent:{'、'.join(agent_names)}")
    print(f" 材料目录：{'、'.join(material_dirs)}")
    print("-" * 68)

    if problems:
        print(f" [FAIL] 发现 {len(problems)} 处不合规：")
        for problem in problems:
            print(f"   - {problem}")
        print("=" * 68)
        return 1

    print(" [PASS] 全部用例字段合规（JSON 合法 / 字段齐全 / Agent 已注册 / 写域元素合法）。")
    print(" 提示：本校验只做静态检查，不调用路由与模型；真实效果需用真实模型跑一轮。")
    print("=" * 68)
    return 0


# ─────────────────────────── 真实跑测对照 ───────────────────────────


def _judge(case: dict, actual: dict) -> dict:
    """按 README 口径逐项判定；返回各项是否命中。"""
    write_targets = list(actual.get("write_targets") or [])
    expected_write = list(case.get("expected_write_targets") or [])
    forbidden = set(case.get("forbidden_write_targets") or [])

    if expected_write:
        write_ok = set(expected_write) <= set(write_targets)
        within_scope = not (set(write_targets) & forbidden)
    else:
        # 空数组口径：本轮不应有任何写域
        write_ok = not write_targets
        within_scope = write_ok

    steps = len(actual.get("plan") or [])
    want_steps = int(case.get("expected_plan_steps") or 0)
    plan_ok = steps == 0 if want_steps == 0 else steps >= want_steps

    return {
        "agent": actual.get("agent") == case.get("expected_agent"),
        "write": write_ok,
        "scope": within_scope,
        "plan": plan_ok,
    }


def run(project_id: int) -> int:
    from workbench.backend.services import routing_service

    cases, error = load_cases()
    if error:
        print(f"[FAIL] {error}")
        return 1
    if not cases:
        print("[FAIL] 用例为空")
        return 1

    header = (f"{'id':<32} {'期望 Agent':<18} {'实际 Agent':<18} "
              f"{'实际 write_targets':<24} {'plan':<6} {'source':<9} 判定")
    print("=" * 132)
    print(f" 金标准评测集 · 真实跑测对照（project_id={project_id}）")
    print(" 说明：一次运行只是当次观察，不构成产品结论；不写入任何文件、不改任何数据。")
    print("=" * 132)
    print(header)
    print("-" * 132)

    totals = {"agent": 0, "write": 0, "scope": 0, "plan": 0}
    sources: dict[str, int] = {}
    for case in cases:
        actual = routing_service.route(project_id, case["input"], record=False)
        verdict = _judge(case, actual)
        for key, ok in verdict.items():
            totals[key] += 1 if ok else 0
        source = str(actual.get("source") or "")
        sources[source] = sources.get(source, 0) + 1
        marks = "".join(
            ("O" if verdict[key] else "X") for key in ("agent", "write", "plan")
        )
        print(f"{case['id']:<32} {case['expected_agent']:<18} "
              f"{str(actual.get('agent','')):<18} "
              f"{','.join(actual.get('write_targets') or []) or '（空）':<24} "
              f"{len(actual.get('plan') or []):<6} {source:<9} "
              f"agent/write/plan={marks}")

    total = len(cases)
    print("-" * 132)
    print(f" 命中统计（共 {total} 条）：")
    print(f"   Agent 命中   {totals['agent']}/{total}")
    print(f"   写域命中     {totals['write']}/{total}")
    print(f"   越界零命中（硬约束）{totals['scope']}/{total}")
    print(f"   计划步数命中 {totals['plan']}/{total}")
    print(f"   source 分布：{sources}")
    print(" 提示：命中率低不等于系统错——若 source 多为 keyword/fallback，说明未走 LLM 判定；")
    print("       要评测语义判定质量，需项目已配置可用模型与 API Key（见 README）。")
    print("=" * 132)
    return 0


def main(argv: list[str] | None = None) -> int:
    try:  # 避免中文/符号在部分 Windows 控制台编码下崩溃
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - 重配置失败不影响逻辑
        pass

    parser = argparse.ArgumentParser(
        description="金标准评测集跑测（不接入 pytest / CI）",
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="只做离线字段校验，不调用路由与模型")
    parser.add_argument("--project-id", type=int, default=None,
                        help="真实跑测时的项目 id（调用 routing_service.route 用）")
    args = parser.parse_args(argv)

    if args.dry_run:
        return dry_run()
    if args.project_id is None:
        print("[FAIL] 真实跑测必须提供 --project-id（如：python evals/run_cases.py --project-id 1）")
        return 2
    return run(args.project_id)


if __name__ == "__main__":
    raise SystemExit(main())
