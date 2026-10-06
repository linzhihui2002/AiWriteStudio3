"""Offline harness checks. No model, credentials, application database or listener.

Run explicitly; this opt-in evaluation tooling stays outside pytest and CI.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import zipfile

from comparison_support import prepare_experiment, assert_frozen, frozen_layout, routing_usage, usage_cursor, verified_fault_point, quota_blocker


class EvaluationEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="workbench-eval-harness-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        for folder in ("workbench/backend", "workbench/vendor/dsh", "agents", "skills/example", "rules", "workflows", "templates", "evals/chat-workspace"):
            (self.root / folder).mkdir(parents=True)
        for relative, content in {"workbench/backend/source.py": "after source",
                                  "agents/writer.yaml": "after agent",
                                  "skills/example/SKILL.md": "frozen skill",
                                  "evals/chat-workspace/runner.py": "frozen harness",
                                  "workbench/vendor/dsh/package.json": "locked package",
                                  "workbench/vendor/dsh/package-lock.json": "locked dependencies"}.items():
            (self.root / relative).write_text(content, encoding="utf-8")
        self.archive = self.root / "baseline.zip"
        with zipfile.ZipFile(self.archive, "w") as archive:
            archive.writestr("workbench/backend/source.py", "before source")
            archive.writestr("agents/writer.yaml", "before agent")
        self.output = self.root / "experiment"
        self.parameters = {"cases": [1], "model": "fake", "max_output_tokens": 8192, "temperature": .2}
        self.cases = [{"id": 1, "turns": ["first", "second"]}]

    def freeze(self):
        return prepare_experiment(self.root, self.archive, self.output, self.parameters, self.cases)

    def test_freezes_actual_sources_agents_assets_and_harness(self):
        manifest = self.freeze()
        layout = frozen_layout(self.output)
        self.assertEqual((layout["before_source"] / "workbench/backend/source.py").read_text(), "before source")
        self.assertEqual((layout["after_source"] / "workbench/backend/source.py").read_text(), "after source")
        self.assertEqual((layout["before_source"] / "agents/writer.yaml").read_text(), "before agent")
        self.assertEqual((layout["after_source"] / "agents/writer.yaml").read_text(), "after agent")
        self.assertEqual((layout["common_assets"] / "skills/example/SKILL.md").read_text(), "frozen skill")
        self.assertEqual((layout["harness"] / "runner.py").read_text(), "frozen harness")
        assert_frozen(manifest, self.output)
        from comparison_support import tree_manifest
        with self.assertRaisesRegex(RuntimeError, "拒绝越界"):
            tree_manifest(layout["after_source"])
        with self.assertRaisesRegex(RuntimeError, "绑定遭到改变"):
            tree_manifest(layout["after_source"], bindings={"workbench/vendor": str(self.root / "skills")})
        self.assertEqual(self.freeze()["experiment_fingerprint"], manifest["experiment_fingerprint"])

    def test_mutable_source_cannot_be_adopted_by_existing_experiment(self):
        self.freeze()
        (self.root / "workbench/backend/source.py").write_text("changed source")
        with self.assertRaisesRegex(RuntimeError, "源码"):
            self.freeze()

    def test_changed_parameter_cannot_reuse_experiment(self):
        self.freeze()
        with self.assertRaisesRegex(RuntimeError, "参数"):
            prepare_experiment(self.root, self.archive, self.output, {**self.parameters, "max_output_tokens": 2048}, self.cases)

    def test_tampered_frozen_copy_cannot_reuse_experiment(self):
        manifest = self.freeze()
        (frozen_layout(self.output)["after_source"] / "workbench/backend/source.py").write_text("tampered frozen source")
        with self.assertRaisesRegex(RuntimeError, "遭到改变"):
            assert_frozen(manifest, self.output)
        with self.assertRaisesRegex(RuntimeError, "遭到改变"):
            self.freeze()

    def test_legacy_results_and_unattributed_worker_outputs_are_preserved(self):
        self.output.mkdir()
        old = self.output / "manifest.json"
        old.write_text(json.dumps(self.parameters))
        before = old.read_bytes()
        with self.assertRaisesRegex(RuntimeError, "旧目录"):
            self.freeze()
        self.assertEqual(old.read_bytes(), before)
        another = self.root / "unattributed"
        another.mkdir()
        evidence = another / "result.json"
        evidence.write_text("existing partial evidence")
        with self.assertRaisesRegex(RuntimeError, "已有未归属"):
            prepare_experiment(self.root, self.archive, another, self.parameters, self.cases)
        self.assertEqual(evidence.read_text(), "existing partial evidence")

    def test_intent_usage_is_bounded_and_separate_from_native(self):
        database = self.root / "usage.db"
        with sqlite3.connect(database) as conn:
            conn.execute("CREATE TABLE token_usage(id INTEGER PRIMARY KEY, task_id INT,task_type TEXT,prompt_tokens INT,completion_tokens INT,total_tokens INT)")
            conn.executemany("INSERT INTO token_usage VALUES(?,?,?,?,?,?)", [(1, 5, "意图判定", 10, 2, 12), (2, 6, "对话", 1000, 200, 1200), (3, 7, "意图判定", 20, 4, 24)])
        conn.close()
        self.assertEqual(usage_cursor(database), 3)
        totals = routing_usage(database)
        self.assertEqual(totals["totals"], {"prompt_tokens": 30, "completion_tokens": 6, "total_tokens": 36})
        self.assertEqual(routing_usage(database, after_id=1, through_id=3)["totals"]["total_tokens"], 24)
        self.assertEqual(routing_usage(database, through_id=1)["totals"]["total_tokens"], 12)
        self.assertFalse(routing_usage(self.root / "missing.db")["available"])

    def test_fault_requires_completed_verified_step_and_matching_actual_file(self):
        book = self.root / "book"
        first, second = "设定/第一步.md", "大纲/第二步.md"
        (book / "设定").mkdir(parents=True)
        (book / "大纲").mkdir()
        (book / first).write_text("宵禁为亥时。", encoding="utf-8")
        file_hash = hashlib.sha256((book / first).read_text(encoding="utf-8").encode()).hexdigest()
        step = {"step": 1, "status": "verified", "receipts": [{"path": first, "after_hash": file_hash}]}
        snapshot = {"id": "synthetic-run", "last_seq": 12, "plan_state": {"steps": [step]}}
        fault = lambda: verified_fault_point(snapshot, book, first, second, checkpoint_supported=True)
        self.assertTrue(fault()["first_step_verified"])
        self.assertEqual(fault()["fault_stage"], "before_second_native_stream")
        step["status"] = "running"
        self.assertIsNone(fault())
        step["status"] = "verified"
        (book / first).write_text("作者已经手改。", encoding="utf-8")
        self.assertIsNone(fault())
        (book / first).write_text("宵禁为亥时。", encoding="utf-8")
        (book / second).write_text("第二步已保存。", encoding="utf-8")
        self.assertIsNone(fault())

    def test_baseline_fault_distinguishes_native_completion_from_checkpoint_proof(self):
        book = self.root / "legacy-book"
        (book / "设定").mkdir(parents=True)
        (book / "设定/第一步.md").write_text("首步产物。", encoding="utf-8")
        snapshot = {"id": "baseline-run", "steps": [{"kind": "plan_step", "step": 1, "status": "done"}]}
        evidence = verified_fault_point(snapshot, book, "设定/第一步.md", "大纲/第二步.md", checkpoint_supported=False)
        self.assertTrue(evidence["first_native_step_completed"])
        self.assertFalse(evidence["first_step_verified"])

    def test_quota_error_stops_future_calls_without_misclassifying_other_errors(self):
        for run in ({"error_code": "QUOTA"}, {"error_code": "AUTH", "error_message": "403 insufficient_user_quota 用户额度不足"}):
            self.assertTrue(quota_blocker({"turns": [{"run": run}]}))
        for run in ({"error_code": "AUTH", "error_message": "invalid API key"}, {"error_code": "RATE_LIMIT"}, {"status": "completed", "text": "额度不足是示例文本"}):
            self.assertFalse(quota_blocker({"turns": [{"run": run}]}))


if __name__ == "__main__":
    unittest.main(verbosity=2)
