#!/usr/bin/env python
"""中文小说短场景人工盲评；仅标准库，不接模型、密钥、项目扫描或 pytest / CI。"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import sys
from collections import Counter
from pathlib import Path

DEFAULT_CASES = Path(__file__).with_name("cases.json")
PREFERENCES = {"A", "B", "tie", "both_bad"}
USAGE_FIELDS = {"cost_amount", "currency", "prompt_tokens", "completion_tokens", "duration_ms"}


class SchemaError(ValueError):
    """可定位的输入格式错误。"""


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def load_json(path: str | Path):
    def unique_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise SchemaError(f"JSON对象包含重复字段：{key}")
            result[key] = value
        return result

    with Path(path).open(encoding="utf-8-sig") as handle:
        return json.load(handle, object_pairs_hook=unique_keys)


def write_json(path: str | Path, value) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def fields(value, required: set[str], optional: set[str], where: str) -> dict:
    if not isinstance(value, dict):
        raise SchemaError(f"{where} 必须是对象")
    missing, unknown = required - value.keys(), value.keys() - required - optional
    if missing or unknown:
        raise SchemaError(f"{where} 字段错误：缺少 {sorted(missing)}；未知 {sorted(unknown)}")
    return value


def text(value, where: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise SchemaError(f"{where} 必须是{'可空' if empty else '非空'}字符串")
    return value


def strings(value, where: str, *, empty: bool = False) -> list:
    if not isinstance(value, list) or (not empty and not value):
        raise SchemaError(f"{where} 必须是{'可空' if empty else '非空'}数组")
    for index, item in enumerate(value):
        text(item, f"{where}[{index}]")
    if len(value) != len(set(value)):
        raise SchemaError(f"{where} 有重复值")
    return value


def number(value, where: str, *, integer: bool = False, nullable: bool = False,
           minimum: float = 0, maximum: float | None = None):
    if nullable and value is None:
        return None
    valid = isinstance(value, int if integer else (int, float)) and not isinstance(value, bool)
    if not valid or not math.isfinite(value) or value < minimum or (maximum is not None and value > maximum):
        raise SchemaError(f"{where} 必须是范围内的有限{'整数' if integer else '数值'}")
    return value


def validate_usage(usage, currency: str, where: str) -> dict:
    fields(usage, set(), USAGE_FIELDS, where)
    for key in USAGE_FIELDS - {"currency"}:
        if key in usage:
            number(usage[key], f"{where}.{key}", nullable=True,
                   integer=key in {"prompt_tokens", "completion_tokens"})
    if usage.get("cost_amount") is not None and usage.get("currency") != currency:
        raise SchemaError(f"{where} 已知成本的币种必须与 budget.currency 一致")
    if "currency" in usage and usage["currency"] is not None:
        text(usage["currency"], f"{where}.currency")
    return usage


def validate_cases(raw) -> dict:
    fields(raw, {"schema_version", "origin", "dimensions", "cases"}, set(), "场景集")
    if raw["schema_version"] != 1 or isinstance(raw["schema_version"], bool):
        raise SchemaError("场景集 schema_version 必须为 1")
    text(raw["origin"], "origin")
    if not isinstance(raw["dimensions"], dict) or not raw["dimensions"]:
        raise SchemaError("dimensions 必须是非空对象")
    for key, label in raw["dimensions"].items():
        text(key, "维度 ID")
        text(label, "维度说明")
    if not isinstance(raw["cases"], list) or not raw["cases"]:
        raise SchemaError("cases 必须是非空数组")
    ids = set()
    required = {"id", "category", "prompt", "word_budget", "facts", "character_goals", "knowledge_boundaries", "dimensions"}
    for case in raw["cases"]:
        fields(case, required, set(), "场景")
        cid = text(case["id"], "场景 ID")
        if cid in ids:
            raise SchemaError(f"重复场景 ID：{cid}")
        ids.add(cid)
        text(case["category"], f"{cid}.category")
        text(case["prompt"], f"{cid}.prompt")
        budget = fields(case["word_budget"], {"min_han", "max_han"}, set(), f"{cid}.word_budget")
        number(budget["min_han"], "min_han", integer=True, minimum=1)
        number(budget["max_han"], "max_han", integer=True, minimum=budget["min_han"])
        if not isinstance(case["facts"], list) or not case["facts"]:
            raise SchemaError(f"{cid}.facts 必须是非空数组")
        fact_ids = set()
        for fact in case["facts"]:
            fields(fact, {"id", "statement"}, set(), f"{cid}.fact")
            fid = text(fact["id"], "事实 ID")
            if fid in fact_ids:
                raise SchemaError(f"{cid} 有重复事实 ID：{fid}")
            fact_ids.add(fid)
            text(fact["statement"], "事实约束")
        if not isinstance(case["character_goals"], list) or not case["character_goals"]:
            raise SchemaError(f"{cid}.character_goals 必须是非空数组")
        characters = set()
        for goal in case["character_goals"]:
            fields(goal, {"character", "goal", "hidden_goal"}, set(), f"{cid}.goal")
            name = text(goal["character"], "人物")
            if name in characters:
                raise SchemaError(f"{cid} 重复人物目标：{name}")
            characters.add(name)
            text(goal["goal"], "人物目标")
            text(goal["hidden_goal"], "隐含目标", empty=True)
        boundaries = case["knowledge_boundaries"]
        if not isinstance(boundaries, list) or len(boundaries) != len(characters):
            raise SchemaError(f"{cid} 必须为每个人物提供知识边界")
        seen = set()
        for boundary in boundaries:
            fields(boundary, {"character", "knows", "does_not_know", "believes"}, set(), f"{cid}.knowledge")
            name = text(boundary["character"], "人物")
            if name not in characters or name in seen:
                raise SchemaError(f"{cid} 知识边界人物无效或重复：{name}")
            seen.add(name)
            for key in ("knows", "does_not_know", "believes"):
                strings(boundary[key], f"{cid}.{key}", empty=True)
        dimensions = strings(case["dimensions"], f"{cid}.dimensions")
        if set(dimensions) - raw["dimensions"].keys():
            raise SchemaError(f"{cid} 使用未知评价维度")
    return raw


def validate_outputs(raw, corpus: dict) -> dict:
    fields(raw, {"schema_version", "experiment", "outputs"}, set(), "方案输出")
    if raw["schema_version"] != 1 or isinstance(raw["schema_version"], bool):
        raise SchemaError("方案输出 schema_version 必须为 1")
    experiment = fields(raw["experiment"], {"id", "conditions", "budget"}, {"created_at", "notes"}, "experiment")
    text(experiment["id"], "experiment.id")
    for key in ("created_at", "notes"):
        if key in experiment:
            text(experiment[key], f"experiment.{key}", empty=True)
    conditions = experiment["conditions"]
    if not isinstance(conditions, list) or len(conditions) != 2:
        raise SchemaError("必须提供恰好两个方案 conditions")
    ids = set()
    for item in conditions:
        fields(item, {"id", "label", "provider", "model", "model_revision", "prompt_version", "parameters"},
               {"prompt_hash", "settings_hash", "style_reference_hash"}, "condition")
        for key in ("id", "label", "provider", "model", "model_revision", "prompt_version"):
            text(item[key], f"condition.{key}")
        if item["id"] in {"tie", "both_bad"}:
            raise SchemaError("condition.id 不能使用保留值 tie/both_bad")
        for key in ("prompt_hash", "settings_hash", "style_reference_hash"):
            if key in item:
                value = text(item[key], f"condition.{key}")
                if value != "unknown" and (len(value) != 64 or any(char not in "0123456789abcdef" for char in value)):
                    raise SchemaError(f"condition.{key} 必须是小写SHA256或unknown")
        if item["id"] in ids:
            raise SchemaError("condition.id 不得重复")
        ids.add(item["id"])
        parameters = fields(item["parameters"], {"temperature", "max_output_tokens"}, set(), "parameters")
        number(parameters["temperature"], "temperature", nullable=True, maximum=2)
        number(parameters["max_output_tokens"], "max_output_tokens", integer=True, minimum=1)
    budget = fields(experiment["budget"], {"currency", "max_cost_per_case", "max_tokens_per_candidate"}, set(), "budget")
    text(budget["currency"], "budget.currency")
    number(budget["max_cost_per_case"], "max_cost_per_case")
    number(budget["max_tokens_per_candidate"], "max_tokens_per_candidate", integer=True, minimum=1)
    if not isinstance(raw["outputs"], list):
        raise SchemaError("outputs 必须是数组")
    valid_cases = {case["id"] for case in corpus["cases"]}
    seen = set()
    for output in raw["outputs"]:
        fields(output, {"case_id", "candidates"}, set(), "output")
        cid = output["case_id"]
        if not isinstance(cid, str) or cid not in valid_cases or cid in seen:
            raise SchemaError(f"未知或重复 output.case_id：{cid}")
        seen.add(cid)
        candidates = output["candidates"]
        if not isinstance(candidates, list) or len(candidates) > 2:
            raise SchemaError(f"{cid}.candidates 必须是至多两项的数组")
        candidate_ids = set()
        for candidate in candidates:
            fields(candidate, {"condition_id", "text"}, {"usage"}, f"{cid}.candidate")
            condition = candidate["condition_id"]
            if not isinstance(condition, str) or condition not in ids or condition in candidate_ids:
                raise SchemaError(f"{cid} 有未知或重复 condition_id")
            candidate_ids.add(condition)
            text(candidate["text"], f"{cid}.text")
            if "usage" in candidate:
                validate_usage(candidate["usage"], budget["currency"], f"{cid}.usage")
    return raw


def prepare(corpus: dict, outputs: dict, seed: int) -> tuple[dict, dict, dict]:
    """只使用显式传入的数据；公开 packet 不含任何方案配置、名称或来源映射。"""
    validate_cases(corpus)
    validate_outputs(outputs, corpus)
    number(seed, "seed", integer=True)
    rng = random.Random(seed)
    conditions = [item["id"] for item in outputs["experiment"]["conditions"]]
    by_case = {item["case_id"]: {c["condition_id"]: c for c in item["candidates"]}
               for item in outputs["outputs"]}
    cases = list(corpus["cases"])
    rng.shuffle(cases)
    packet = {"schema_version": 1, "cases_hash": digest(corpus), "dimensions": corpus["dimensions"],
              "instructions": "每场景只提交一条最终裁决；A/B为随机展示顺序。允许平局与两者都不好。未知维度、事实核查或修改量留null，不算通过。",
              "cases": [], "unpaired_case_ids": []}
    labels = {"schema_version": 1, "seed": seed, "cases_hash": digest(corpus),
              "outputs_hash": digest(outputs), "experiment": outputs["experiment"],
              "configuration_hashes": {item["id"]: digest(item) for item in outputs["experiment"]["conditions"]},
              "cases": []}
    for case in cases:
        order = list(conditions)
        rng.shuffle(order)
        available = by_case.get(case["id"], {})
        private = {"case_id": case["id"], "dimensions": case["dimensions"],
                   "sides": dict(zip(("A", "B"), order)),
                   "candidates": {key: {"text_hash": digest(item["text"]), "usage": item.get("usage", {})}
                                  for key, item in available.items()}}
        labels["cases"].append(private)
        if len(available) != 2:
            packet["unpaired_case_ids"].append(case["id"])
            continue
        packet["cases"].append({**case, "candidates": {
            side: {"text": available[condition]["text"], "text_hash": digest(available[condition]["text"])}
            for side, condition in private["sides"].items()}})
    packet["packet_id"] = "prose-" + digest(packet)[:20]
    labels["packet_id"] = packet["packet_id"]
    labels["packet_hash"] = digest(packet)
    template = {"schema_version": 1, "packet_id": packet["packet_id"], "reviews": [
        {"case_id": case["id"], "reviewer_id": "", "preference": None,
         "dimensions": {side: {key: None for key in case["dimensions"]} for side in ("A", "B")},
         "fact_errors": {"A": None, "B": None},
         "edits": {side: {"characters": None, "minutes": None} for side in ("A", "B")}, "notes": ""}
        for case in packet["cases"]]}
    return packet, labels, template


def validate_reviews(raw, packet: dict) -> dict:
    fields(raw, {"schema_version", "packet_id", "reviews"}, set(), "人工评分")
    if raw["schema_version"] != 1 or isinstance(raw["schema_version"], bool) or raw["packet_id"] != packet["packet_id"]:
        raise SchemaError("人工评分版本或 packet_id 不匹配")
    if not isinstance(raw["reviews"], list):
        raise SchemaError("reviews 必须是数组")
    cases = {case["id"]: case for case in packet["cases"]}
    seen = set()
    for review in raw["reviews"]:
        fields(review, {"case_id", "reviewer_id", "preference"}, {"dimensions", "fact_errors", "edits", "notes"}, "review")
        cid = review["case_id"]
        if not isinstance(cid, str) or cid not in cases or cid in seen:
            raise SchemaError(f"未知、未配对或重复 review.case_id：{cid}；每场景只允许一条最终裁决")
        seen.add(cid)
        text(review["reviewer_id"], "reviewer_id", empty=review["preference"] is None)
        preference = review["preference"]
        if preference is not None and (not isinstance(preference, str) or preference not in PREFERENCES):
            raise SchemaError(f"{cid}.preference 只能为 A/B/tie/both_bad/null")
        if "notes" in review:
            text(review["notes"], "notes", empty=True)
        if "dimensions" in review:
            dimensions = fields(review["dimensions"], set(), {"A", "B"}, "dimensions")
            for side, values in dimensions.items():
                fields(values, set(), set(cases[cid]["dimensions"]), f"dimensions.{side}")
                for key, value in values.items():
                    number(value, f"{cid}.{side}.{key}", nullable=True, integer=True, minimum=1, maximum=5)
        if "fact_errors" in review:
            errors = fields(review["fact_errors"], set(), {"A", "B"}, "fact_errors")
            valid_facts = {item["id"] for item in cases[cid]["facts"]}
            for side, findings in errors.items():
                if findings is None:
                    continue
                if not isinstance(findings, list):
                    raise SchemaError(f"{cid}.fact_errors.{side} 必须是数组或null")
                fact_ids = set()
                for finding in findings:
                    fields(finding, {"fact_id", "detail"}, set(), "fact_error")
                    fid = finding["fact_id"]
                    if not isinstance(fid, str) or fid not in valid_facts or fid in fact_ids:
                        raise SchemaError(f"{cid} 事实错误必须引用本场景不重复的 fact_id")
                    fact_ids.add(fid)
                    text(finding["detail"], "fact_error.detail")
        if "edits" in review:
            for side, edit in fields(review["edits"], set(), {"A", "B"}, "edits").items():
                fields(edit, set(), {"characters", "minutes"}, f"edits.{side}")
                for key, value in edit.items():
                    number(value, f"{cid}.edits.{side}.{key}", nullable=True, integer=key == "characters")
    return raw


def wilson(wins: int, total: int) -> list[float] | None:
    """95% Wilson区间；没有明确偏好的场景时不生成概率。"""
    if total == 0:
        return None
    z = 1.959963984540054
    p, divisor = wins / total, 1 + z * z / total
    center = (p + z * z / (2 * total)) / divisor
    radius = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / divisor
    return [round(max(0, center - radius), 6), round(min(1, center + radius), 6)]


def observed(values: list, expected: int) -> dict:
    return {"observed": len(values), "missing": expected - len(values),
            "mean": round(sum(values) / len(values), 4) if values else None,
            "sum": round(sum(values), 4) if values else None}


def score(packet: dict, labels: dict, reviews: dict) -> dict:
    fields(packet, {"schema_version", "cases_hash", "dimensions", "instructions", "cases", "unpaired_case_ids", "packet_id"}, set(), "packet")
    fields(labels, {"schema_version", "seed", "cases_hash", "outputs_hash", "experiment", "configuration_hashes", "cases", "packet_id", "packet_hash"}, set(), "labels")
    if labels.get("schema_version") != 1 or labels.get("packet_id") != packet.get("packet_id"):
        raise SchemaError("私有映射与 packet 不匹配")
    if labels.get("packet_hash") != digest(packet) or labels.get("cases_hash") != packet.get("cases_hash"):
        raise SchemaError("评审 packet 已变化，hash校验失败")
    if packet["schema_version"] != 1 or isinstance(packet["schema_version"], bool):
        raise SchemaError("packet.schema_version 必须为1")
    number(labels["seed"], "labels.seed", integer=True)
    case_fields = {"id", "category", "prompt", "word_budget", "facts", "character_goals", "knowledge_boundaries", "dimensions"}
    if not isinstance(packet["cases"], list):
        raise SchemaError("packet.cases 必须是数组")
    paired_corpus = {"schema_version": 1, "origin": "评审packet字段校验", "dimensions": packet["dimensions"], "cases": []}
    for case in packet["cases"]:
        fields(case, case_fields | {"candidates"}, set(), "packet.case")
        paired_corpus["cases"].append({key: case[key] for key in case_fields})
        candidates = fields(case["candidates"], {"A", "B"}, set(), "packet.candidates")
        for candidate in candidates.values():
            fields(candidate, {"text", "text_hash"}, set(), "packet.candidate")
            text(candidate["text"], "packet.candidate.text")
    # 无配对输出也可汇总为缺失；复用场景schema时用已验证源场景的空集合例外。
    if paired_corpus["cases"]:
        validate_cases(paired_corpus)
    strings(packet["unpaired_case_ids"], "unpaired_case_ids", empty=True)
    validate_outputs({"schema_version": 1, "experiment": labels["experiment"], "outputs": []}, paired_corpus)
    validate_reviews(reviews, packet)
    conditions = [item["id"] for item in labels["experiment"]["conditions"]]
    if len(conditions) != 2 or len(set(conditions)) != 2:
        raise SchemaError("私有映射必须包含两个不同方案")
    if not isinstance(labels["cases"], list):
        raise SchemaError("labels.cases 必须是数组")
    for item in labels["cases"]:
        fields(item, {"case_id", "dimensions", "sides", "candidates"}, set(), "labels.case")
        text(item["case_id"], "labels.case_id")
        strings(item["dimensions"], "labels.dimensions")
        if set(item["dimensions"]) - packet["dimensions"].keys():
            raise SchemaError("私有映射有未知评价维度")
        sides = fields(item["sides"], {"A", "B"}, set(), "labels.sides")
        for value in sides.values():
            text(value, "labels.side")
        if set(sides.values()) != set(conditions):
            raise SchemaError("私有A/B映射异常")
        fields(item["candidates"], set(), set(conditions), "labels.candidates")
        for candidate in item["candidates"].values():
            fields(candidate, {"text_hash", "usage"}, set(), "labels.candidate")
            text(candidate["text_hash"], "labels.candidate.text_hash")
            validate_usage(candidate["usage"], labels["experiment"]["budget"]["currency"], "labels.usage")
    private = {item["case_id"]: item for item in labels["cases"]}
    if len(private) != len(labels["cases"]):
        raise SchemaError("私有映射有重复场景")
    expected_ids = {case["id"] for case in packet["cases"]}
    if expected_ids & set(packet["unpaired_case_ids"]) or expected_ids | set(packet["unpaired_case_ids"]) != set(private):
        raise SchemaError("私有映射与配对/未配对场景清单不一致")
    if labels["configuration_hashes"] != {item["id"]: digest(item) for item in labels["experiment"]["conditions"]}:
        raise SchemaError("方案配置hash不一致")
    for case in packet["cases"]:
        item = private.get(case["id"], {})
        if item.get("dimensions") != case["dimensions"]:
            raise SchemaError("场景与私有映射的评价维度不一致")
        if set(item.get("sides", {})) != {"A", "B"} or set(item["sides"].values()) != set(conditions):
            raise SchemaError("私有A/B映射异常")
        for side, candidate in case["candidates"].items():
            actual = digest(candidate["text"])
            if candidate["text_hash"] != actual or item["candidates"][item["sides"][side]]["text_hash"] != actual:
                raise SchemaError("候选文本与私有映射hash不匹配")
    reviewed = {review["case_id"]: review for review in reviews["reviews"] if review["preference"] is not None}
    counts = Counter({"tie": 0, "both_bad": 0, **{key: 0 for key in conditions}})
    dimensions = {key: {} for key in conditions}
    facts = {key: [] for key in conditions}
    edits = {key: {"characters": [], "minutes": []} for key in conditions}
    dimension_expected = Counter(key for item in private.values() for key in item["dimensions"])
    case_results = []
    for case in packet["cases"]:
        cid, review = case["id"], reviewed.get(case["id"])
        if review is None:
            case_results.append({"case_id": cid, "status": "unreviewed"})
            continue
        mapping = private[cid]["sides"]
        preference = review["preference"]
        winner = mapping[preference] if preference in {"A", "B"} else preference
        counts[winner] += 1
        case_results.append({"case_id": cid, "status": "reviewed", "preference": winner,
                             "reviewer_id": review["reviewer_id"]})
        for side, condition in mapping.items():
            for key, value in review.get("dimensions", {}).get(side, {}).items():
                if value is not None:
                    dimensions[condition].setdefault(key, []).append(value)
            errors = review.get("fact_errors", {}).get(side)
            if errors is not None:
                facts[condition].append(len(errors))
            for key, value in review.get("edits", {}).get(side, {}).items():
                if value is not None:
                    edits[condition][key].append(value)
    total, paired, rated = len(private), len(packet["cases"]), len(reviewed)
    decisive = sum(counts[key] for key in conditions)
    metrics = {}
    budget = labels["experiment"]["budget"]
    for condition in conditions:
        usage = {key: [] for key in USAGE_FIELDS - {"currency"}}
        generated = 0
        for item in private.values():
            candidate = item["candidates"].get(condition)
            if candidate is None:
                continue
            generated += 1
            for key in usage:
                value = candidate.get("usage", {}).get(key)
                if value is not None:
                    usage[key].append(value)
        metrics[condition] = {
            "dimensions": {key: observed(dimensions[condition].get(key, []), expected)
                           for key, expected in dimension_expected.items()},
            "fact_errors": {**observed(facts[condition], total),
                            "checked_without_known_error": sum(value == 0 for value in facts[condition]),
                            "note": "空数组表示审阅者核查未发现列出的事实错误；不等于全部语义正确。null/缺字段为未核查。"},
            "edits": {key: observed(values, total) for key, values in edits[condition].items()},
            "usage": {"generated_candidates": generated, "currency": budget["currency"],
                      **{key: observed(values, total) for key, values in usage.items()}}}
    violations = []
    for cid, item in private.items():
        costs = []
        for condition, candidate in item["candidates"].items():
            usage = candidate.get("usage", {})
            if usage.get("completion_tokens") is not None and usage["completion_tokens"] > budget["max_tokens_per_candidate"]:
                violations.append({"case_id": cid, "condition_id": condition, "type": "token_budget"})
            if usage.get("cost_amount") is not None:
                costs.append(usage["cost_amount"])
        if costs and sum(costs) > budget["max_cost_per_case"]:
            violations.append({"case_id": cid, "type": "cost_budget", "known_cost": sum(costs),
                               "missing_costs": 2 - len(costs)})
    return {
        "schema_version": 1, "packet_id": packet["packet_id"],
        "trace": {"cases_hash": packet["cases_hash"], "packet_hash": digest(packet),
                  "ratings_hash": digest(reviews), "outputs_hash": labels["outputs_hash"],
                  "seed": labels["seed"], "configuration_hashes": labels["configuration_hashes"],
                  "experiment": labels["experiment"]},
        "coverage": {"total_cases": total, "paired_cases": paired, "reviewed_cases": rated,
                     "unreviewed_paired_cases": paired - rated, "unpaired_cases": total - paired,
                     "missing_or_unreviewed_cases": total - rated},
        "preferences": {"wins": {key: counts[key] for key in conditions}, "tie": counts["tie"],
                        "both_bad": counts["both_bad"], "decisive_cases": decisive,
                        "decisive_win_rate": {key: counts[key] / decisive if decisive else None for key in conditions},
                        "wilson_95": {key: wilson(counts[key], decisive) for key in conditions},
                        "denominator_note": "仅A/B明确偏好进入胜率分母；平局、两者都不好、未评和未配对均单列。"},
        "metrics": metrics, "budget_violations_observed": violations,
        "case_results": case_results,
        "limitations": ["这是少量原创短场景的当次人工判断，不代表中文长篇小说的总体质量。",
                        f"Wilson区间以每场景一条最终裁决为单位；本轮总场景{total}个，明确偏好{decisive}个，样本少时区间更宽。",
                        "共享评审者与题材会造成相关性；区间只是描述性不确定性，不证明优化有效。",
                        "没有核查或成本记录的项目不算通过，也不按零错误或零成本填补。",
                        "短场景练习不替代正式章节硬门禁、连续章节评测和作者修订验收。"]}


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--dry-run", action="store_true", help="仅离线校验场景集，无模型、密钥或写入")
    sub = parser.add_subparsers(dest="command")
    prepare_parser = sub.add_parser("prepare", help="读取显式提供的方案输出，随机盲化")
    prepare_parser.add_argument("--outputs", type=Path, required=True)
    prepare_parser.add_argument("--out-dir", type=Path, required=True)
    prepare_parser.add_argument("--seed", type=int, default=20261003)
    score_parser = sub.add_parser("score", help="校验hash并汇总人工裁决")
    score_parser.add_argument("--packet", type=Path, required=True)
    score_parser.add_argument("--labels", type=Path, required=True)
    score_parser.add_argument("--ratings", type=Path, required=True)
    score_parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.dry_run:
            corpus = validate_cases(load_json(args.cases))
            print(f"离线字段校验通过：{len(corpus['cases'])} 个原创场景；{len(corpus['dimensions'])} 个维度；cases_hash={digest(corpus)}")
            print("此结果只确认输入合规，没有生成正文或评估小说质量。")
            return 0
        if args.command == "prepare":
            corpus = validate_cases(load_json(args.cases))
            packet, labels, template = prepare(corpus, load_json(args.outputs), args.seed)
            paths = [args.out_dir / "review" / "packet.json", args.out_dir / "private" / "labels.json",
                     args.out_dir / "review" / "ratings.json"]
            if any(path.exists() for path in paths):
                raise SchemaError("输出文件已存在，请使用新的 out-dir，避免覆盖已有盲评")
            for path, value in zip(paths, (packet, labels, template)):
                write_json(path, value)
            print(f"已准备 {len(packet['cases'])} 个配对场景；未配对 {len(packet['unpaired_case_ids'])} 个；packet_id={packet['packet_id']}")
            print(f"仅分享 {args.out_dir / 'review'}；private/labels.json 含方案来源与成本，不交给盲评者。")
            return 0
        if args.command == "score":
            report = score(load_json(args.packet), load_json(args.labels), load_json(args.ratings))
            write_json(args.out, report)
            print(json.dumps({"coverage": report["coverage"], "preferences": report["preferences"]}, ensure_ascii=False, indent=2))
            print(f"完整报告已保存：{args.out}")
            return 0
        parser.error("请选择 --dry-run、prepare 或 score；脚本没有自动生成模式")
    except (SchemaError, OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        print(f"输入/输出失败：{exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
