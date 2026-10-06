#!/usr/bin/env python
"""标准库离线自检；只检查评测工具行为，不评判模型效果，不接 pytest / CI。"""
from __future__ import annotations

import copy
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

import run_eval as runner


class EvalToolChecks(unittest.TestCase):
    def setUp(self):
        self.corpus = runner.validate_cases(runner.load_json(runner.DEFAULT_CASES))
        self.outputs = runner.load_json(Path(__file__).with_name("outputs.example.json"))
        self.outputs["experiment"]["conditions"][0]["label"] = "SECRET-BASELINE-LABEL"
        self.outputs["experiment"]["conditions"][1]["label"] = "SECRET-OPTIMIZED-LABEL"
        self.outputs["experiment"]["conditions"][0]["provider"] = "SECRET-PROVIDER"
        self.outputs["experiment"]["conditions"][0]["model"] = "SECRET-MODEL"
        self.outputs["outputs"][0]["candidates"][0]["text"] = "甲方案的合成正文。"
        self.outputs["outputs"][0]["candidates"][1]["text"] = "乙方案的合成正文。"
        self.packet, self.labels, self.reviews = runner.prepare(self.corpus, self.outputs, 20261003)

    def rate(self, preference):
        self.reviews["reviews"][0]["reviewer_id"] = "synthetic-reviewer"
        self.reviews["reviews"][0]["preference"] = preference
        return runner.score(self.packet, self.labels, self.reviews)

    def test_twenty_original_cases_validate(self):
        self.assertEqual(len(self.corpus["cases"]), 20)
        self.assertEqual(len({case["id"] for case in self.corpus["cases"]}), 20)

    def test_public_packet_has_no_experiment_labels_or_model_metadata(self):
        content = json.dumps(self.packet, ensure_ascii=False)
        for marker in ("SECRET-BASELINE-LABEL", "SECRET-OPTIMIZED-LABEL", "SECRET-PROVIDER", "SECRET-MODEL", "condition_id", "configuration_hashes", "seed"):
            self.assertNotIn(marker, content)
        self.assertIn("SECRET-BASELINE-LABEL", json.dumps(self.labels))

    def test_same_seed_same_packet_and_mapping(self):
        packet, labels, _ = runner.prepare(self.corpus, self.outputs, 20261003)
        self.assertEqual(packet, self.packet)
        self.assertEqual(labels, self.labels)

    def test_mapping_restores_both_texts_and_preference(self):
        by_condition = {item["condition_id"]: item["text"] for item in self.outputs["outputs"][0]["candidates"]}
        private = next(item for item in self.labels["cases"] if item["case_id"] == "pq-01")
        for side in ("A", "B"):
            self.assertEqual(self.packet["cases"][0]["candidates"][side]["text"], by_condition[private["sides"][side]])
        report = self.rate("A")
        self.assertEqual(report["preferences"]["wins"][private["sides"]["A"]], 1)
        self.assertEqual(report["preferences"]["decisive_cases"], 1)

    def test_unreviewed_has_no_fabricated_win_rate_or_fact_success(self):
        report = runner.score(self.packet, self.labels, self.reviews)
        self.assertEqual(report["coverage"]["missing_or_unreviewed_cases"], 20)
        self.assertEqual(report["preferences"]["decisive_cases"], 0)
        self.assertIsNone(report["preferences"]["decisive_win_rate"]["baseline"])
        self.assertIsNone(report["preferences"]["wilson_95"]["baseline"])
        self.assertEqual(report["metrics"]["baseline"]["fact_errors"]["observed"], 0)
        self.assertIsNone(report["metrics"]["baseline"]["usage"]["cost_amount"]["sum"])

    def test_both_bad_and_tie_do_not_enter_win_rate_denominator(self):
        for preference in ("tie", "both_bad"):
            report = self.rate(preference)
            self.assertEqual(report["preferences"][preference], 1)
            self.assertEqual(report["preferences"]["decisive_cases"], 0)
            self.assertIsNone(report["preferences"]["decisive_win_rate"]["optimized"])
            self.assertEqual(report["coverage"]["reviewed_cases"], 1)

    def test_observed_dimensions_facts_edits_and_cost_map_to_original_condition(self):
        review = self.reviews["reviews"][0]
        review["dimensions"]["A"]["subtext"] = 4
        review["fact_errors"]["A"] = [{"fact_id": "F1", "detail": "合成错误示例"}]
        review["fact_errors"]["B"] = []
        review["edits"]["A"] = {"characters": 23, "minutes": 2.5}
        report = self.rate("both_bad")
        private = next(item for item in self.labels["cases"] if item["case_id"] == "pq-01")
        condition = private["sides"]["A"]
        self.assertEqual(report["metrics"][condition]["dimensions"]["subtext"]["mean"], 4)
        self.assertEqual(report["metrics"][condition]["fact_errors"]["sum"], 1)
        self.assertEqual(report["metrics"][condition]["edits"]["characters"]["sum"], 23)
        self.assertEqual(report["metrics"][condition]["edits"]["minutes"]["sum"], 2.5)
        self.assertEqual(report["metrics"][condition]["dimensions"]["restraint"]["missing"], 19)

    def test_no_outputs_still_reports_all_missing_and_no_winner(self):
        self.outputs["outputs"] = []
        packet, labels, reviews = runner.prepare(self.corpus, self.outputs, 1)
        report = runner.score(packet, labels, reviews)
        self.assertEqual(report["coverage"]["unpaired_cases"], 20)
        self.assertEqual(report["coverage"]["reviewed_cases"], 0)
        self.assertIsNone(report["preferences"]["decisive_win_rate"]["baseline"])
        self.assertEqual(report["metrics"]["baseline"]["dimensions"]["restraint"]["missing"], 19)
        self.assertEqual(report["metrics"]["baseline"]["fact_errors"]["missing"], 20)

    def test_known_cost_and_exceeded_budget_are_reported(self):
        for candidate in self.outputs["outputs"][0]["candidates"]:
            candidate["usage"] = {"cost_amount": 0.8, "currency": "CNY", "completion_tokens": 1700}
        packet, labels, reviews = runner.prepare(self.corpus, self.outputs, 1)
        report = runner.score(packet, labels, reviews)
        self.assertEqual(len(report["budget_violations_observed"]), 3)
        self.assertEqual(report["metrics"]["baseline"]["usage"]["cost_amount"]["sum"], 0.8)
        self.assertEqual(report["metrics"]["baseline"]["usage"]["cost_amount"]["missing"], 19)

    def test_duplicate_review_and_unknown_or_bool_score_are_rejected(self):
        variants = []
        duplicate = copy.deepcopy(self.reviews)
        duplicate["reviews"].append(copy.deepcopy(duplicate["reviews"][0]))
        variants.append(duplicate)
        wrong_preference = copy.deepcopy(self.reviews)
        wrong_preference["reviews"][0]["preference"] = "pass"
        variants.append(wrong_preference)
        wrong_score = copy.deepcopy(self.reviews)
        wrong_score["reviews"][0]["dimensions"]["A"]["subtext"] = True
        variants.append(wrong_score)
        unknown_fact = copy.deepcopy(self.reviews)
        unknown_fact["reviews"][0]["fact_errors"]["A"] = [{"fact_id": "F99", "detail": "不存在"}]
        variants.append(unknown_fact)
        for value in variants:
            with self.assertRaises(runner.SchemaError):
                runner.score(self.packet, self.labels, value)

    def test_unknown_metadata_secret_keys_reserved_ids_and_invalid_numbers_rejected(self):
        variants = []
        secret = copy.deepcopy(self.outputs)
        secret["experiment"]["api_key"] = "synthetic-secret"
        variants.append(secret)
        duplicate = copy.deepcopy(self.outputs)
        duplicate["outputs"].append(copy.deepcopy(duplicate["outputs"][0]))
        variants.append(duplicate)
        invalid_cost = copy.deepcopy(self.outputs)
        invalid_cost["outputs"][0]["candidates"][0]["usage"]["cost_amount"] = float("nan")
        variants.append(invalid_cost)
        reserved = copy.deepcopy(self.outputs)
        reserved["experiment"]["conditions"][0]["id"] = "both_bad"
        variants.append(reserved)
        for value in variants:
            with self.assertRaises(runner.SchemaError):
                runner.prepare(self.corpus, value, 1)

    def test_modified_packet_or_wrong_ratings_id_rejected(self):
        modified = copy.deepcopy(self.packet)
        modified["cases"][0]["candidates"]["A"]["text"] += "篡改"
        with self.assertRaises(runner.SchemaError):
            runner.score(modified, self.labels, self.reviews)
        wrong = copy.deepcopy(self.reviews)
        wrong["packet_id"] = "another-packet"
        with self.assertRaises(runner.SchemaError):
            runner.score(self.packet, self.labels, wrong)

    def test_cli_prepare_score_round_trip_and_no_overwrite(self):
        with tempfile.TemporaryDirectory(prefix="prose-quality-selfcheck-") as temp, \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            directory = Path(temp)
            source = directory / "outputs.json"
            runner.write_json(source, self.outputs)
            result = directory / "round"
            self.assertEqual(runner.main(["prepare", "--outputs", str(source), "--out-dir", str(result)]), 0)
            self.assertTrue((result / "review" / "packet.json").is_file())
            self.assertTrue((result / "private" / "labels.json").is_file())
            args = ["score", "--packet", str(result / "review" / "packet.json"),
                    "--labels", str(result / "private" / "labels.json"),
                    "--ratings", str(result / "review" / "ratings.json"), "--out", str(directory / "report.json")]
            self.assertEqual(runner.main(args), 0)
            self.assertEqual(runner.main(args), 2)
            self.assertEqual(runner.load_json(directory / "report.json")["coverage"]["reviewed_cases"], 0)

    def test_wilson_known_boundary_and_small_sample_uncertainty(self):
        self.assertIsNone(runner.wilson(0, 0))
        lower, upper = runner.wilson(1, 1)
        self.assertLess(lower, 0.21)
        self.assertEqual(upper, 1)
        lower, upper = runner.wilson(10, 20)
        self.assertTrue(0.29 < lower < 0.31)
        self.assertTrue(0.69 < upper < 0.71)

    def test_duplicate_json_object_fields_are_rejected(self):
        with tempfile.TemporaryDirectory(prefix="prose-quality-json-check-") as temp:
            path = Path(temp) / "duplicate.json"
            path.write_text('{"schema_version": 1, "schema_version": 2}', encoding="utf-8")
            with self.assertRaises(runner.SchemaError):
                runner.load_json(path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
