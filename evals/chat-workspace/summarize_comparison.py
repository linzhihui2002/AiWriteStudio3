"""Summarize recorded model runs without another model call or credentials."""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing
import json
from pathlib import Path
import sqlite3
from comparison_support import routing_usage


def events_for(work: Path, turn: dict) -> list[dict]:
    """Read the isolated authoritative journal, including pages beyond 500 events."""
    database = work / ".workbench/workbench.db"
    if not database.exists():
        return turn.get("events", [])
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
        rows = connection.execute("SELECT payload FROM chat_run_events WHERE run_id=? ORDER BY seq",
                                  (turn["run"]["id"],)).fetchall()
    return [json.loads(row[0]) for row in rows]


def known_outputs(case: int, result: dict) -> dict:
    """Narrow deterministic observations; prose quality still needs author review."""
    turns = result["turns"]
    final = turns[-1]["files_after"]
    content = lambda path: final.get(path, {}).get("content", "")
    unchanged = lambda path: final.get(path) == result["initial_files"].get(path)
    observations = {}
    if case == 1:
        observations["renamed_character_and_retained_age_goal"] = all(word in content("设定/评测人物.md") for word in ["沈砚", "十八", "失散家人"])
    elif case == 3:
        observations["both_ordered_artifacts_exist"] = all(content(path) for path in ["设定/评测规则.md", "大纲/评测任务.md"])
    elif case == 4:
        observations["negated_material_absent_from_each_write_scope"] = all(
            prohibited not in turn["run"].get("routing", {}).get("write_targets", [])
            for turn, prohibited in zip(turns, ["大纲", "设定"]))
    elif case == 5:
        observations["all_original_reference_materials_unchanged"] = all(unchanged(path) for path in ["设定/世界设定.md", "设定/人物设定.md", "设定/场景设定.md"])
    elif case == 6:
        observations["report_role_and_scope_readonly"] = all(turn["run"].get("routing", {}).get("agent") == "reviewer"
            and not turn["run"].get("routing", {}).get("write_targets") and turn["run"].get("read_only") for turn in turns)
        observations["no_manuscript_change"] = not any(turn["changed_paths"] for turn in turns)
    elif case == 8:
        observations["confirmed_name_currency_source_and_balance_recalled"] = all(word in turns[-1]["run"].get("text", "") for word in ["李长歌", "委托报酬", "三"])
    elif case == 10:
        observations["selected_character_age_changed_to_nineteen"] = "十九" in content("设定/评测初始人物.md")
        observations["unrelated_character_source_unchanged"] = unchanged("设定/人物设定.md")
    elif case == 11:
        observations["duplicate_request_kept_same_run_id"] = turns[0].get("duplicate_same_id") is True
        observations["memo_retains_original_and_followup"] = all(word in content("备忘录/评测幂等.md") for word in ["茶杯", "缺", "账本"])
    elif case == 12:
        observations["injected_transport_fault_actually_reached"] = result.get("fault_injection", {}).get("applied", False)
        observations["both_resume_artifacts_exist"] = all(content(path) for path in ["设定/评测续接规则.md", "大纲/评测续接任务.md"])
        evidence = result.get("fault_injection", {}).get("evidence") or {}
        observations["fault_after_verified_first_step"] = bool(evidence.get("first_step_verified"))
        observations["verified_first_step_reused"] = any(
            step.get("step") == 1 and step.get("reused") for turn in turns[1:]
            for step in turn["run"].get("plan_state", {}).get("steps", []))
        first_writes = [change for turn in turns for change in turn["run"].get("changes", [])
                        if change.get("status") == "applied" and change.get("path") == "设定/评测续接规则.md"]
        observations["first_artifact_applied_once"] = len(first_writes) == 1 if evidence else None
    return observations


def summarize(directory: Path) -> dict:
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    output = {"parameters": manifest, "arms": {}, "cases": [],
              "interpretation": "One fixed-parameter observation; status, routing, receipts and deterministic facts do not establish general literary quality."}
    for arm in ["before", "after"]:
        rows, statuses, usage, intent_usage = [], Counter(), Counter(), Counter()
        for identifier in manifest["cases"]:
            work = directory / arm / f"case-{identifier:02d}"
            source = work / "result.json"
            if not source.exists():
                rows.append({"case": identifier, "missing": True})
                continue
            result = json.loads(source.read_text(encoding="utf-8"))
            case_intent = routing_usage(work / ".workbench/workbench.db")
            intent_usage.update(case_intent.get("totals", {}))
            turns = []
            for turn in result["turns"]:
                run = turn["run"]
                events = events_for(work, turn)
                tokens = Counter()
                for event in events:
                    if event.get("event") == "usage":
                        tokens.update({key: int(value) for key, value in event.get("usage", {}).items() if isinstance(value, (int, float))})
                usage.update(tokens)
                statuses[run["status"]] += 1
                routing = run.get("routing", {})
                error_message = str(run.get("error_message") or "")
                external_quota = "insufficient_user_quota" in error_message or "额度不足" in error_message or "预扣费额度失败" in error_message
                turns.append({"status": run["status"], "error_code": run.get("error_code"), "external_quota_failure": external_quota, "agent": routing.get("agent"),
                    "write_targets": routing.get("write_targets", []), "read_only": run.get("read_only"),
                    "completion": run.get("completion", {}).get("status"), "changed_paths": turn["changed_paths"],
                    "applied_changes": len([change for change in run.get("changes", []) if change.get("status") == "applied"]),
                    "reused_steps": [step.get("step") for step in run.get("plan_state", {}).get("steps", []) if step.get("reused")],
                    "duplicate_same_id": turn.get("duplicate_same_id"), "elapsed_ms": turn["elapsed_ms"],
                    "first_text_ms": turn.get("first_text_ms"), "metrics": run.get("metrics", {}), "reported_usage": dict(tokens),
                    "native_execution_usage": dict(tokens), "routing_usage": turn.get("routing_usage", {"available": False, "reason": "legacy turn has no usage boundaries; see case aggregate"}),
                    "event_journal_complete": bool(events) and events[-1].get("seq") == run.get("last_seq")})
            row = {"case": identifier, "name": result["case"]["name"], "arm": arm, "turns": turns,
                   "planned_turns": len(result["case"]["turns"]), "recorded_turns": len(turns),
                   "all_planned_turns_recorded": len(turns) == len(result["case"]["turns"]),
                   "routing_usage": case_intent, "observations": known_outputs(identifier, result), "fault_injection": result.get("fault_injection", {})}
            rows.append(row)
            output["cases"].append(row)
        output["arms"][arm] = {"recorded_cases": len([row for row in rows if not row.get("missing")]),
                               "complete_multiturn_records": sum(bool(row.get("all_planned_turns_recorded")) for row in rows),
                               "external_quota_failures": sum(turn["external_quota_failure"] for row in rows for turn in row.get("turns", [])),
                               "turn_statuses": dict(statuses), "reported_usage": dict(usage),
                               "native_execution_usage": dict(usage), "routing_usage": dict(intent_usage),
                               "missing_cases": [row["case"] for row in rows if row.get("missing")]}
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    directory = args.directory.resolve()
    result = summarize(directory)
    (directory / "observations.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# 固定模型多轮对照记录", "", f"模型：{result['parameters']['model']}；temperature=0.2；每次输出上限 {result['parameters']['max_output_tokens']}。", "",
             "一次观察，保留超时、输出上限与主动故障。native执行usage与SQLite意图判定usage分开汇总，不将缓存字段擅自折算为计费tokens。旧记录无逐轮路由用量边界时只提供case汇总。", "",
             "| 组 | 用例 | 改造前逐轮状态 | 改造后逐轮状态 |", "|---|---|---|---|"]
    for identifier in result["parameters"]["cases"]:
        arms = {row["arm"]: row for row in result["cases"] if row["case"] == identifier}
        def status(arm):
            row = arms.get(arm, {})
            values = [turn["status"] + (" (供应商额度不足)" if turn["external_quota_failure"] else f" ({turn['error_code']})" if turn["error_code"] else "") for turn in row.get("turns", [])]
            if row and not row["all_planned_turns_recorded"]:
                values.append(f"后续未执行（已记录 {row['recorded_turns']}/{row['planned_turns']} 轮）")
            return "；".join(values) or "尚未记录"
        name = next(iter(arms.values()))["name"] if arms else str(identifier)
        lines.append(f"| {identifier} | {name} | {status('before')} | {status('after')} |")
    lines.extend(["", "完整路由、写域、落盘凭据、续接、耗时与token字段见 observations.json；各case的result.json包含合成文件与原始事件。", ""])
    (directory / "observations.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps(result["arms"], ensure_ascii=False))


if __name__ == "__main__":
    main()
