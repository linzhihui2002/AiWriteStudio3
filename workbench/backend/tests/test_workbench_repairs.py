"""Regression coverage for transport, diagnostics, ledger and outline repairs."""
from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from workbench.backend import config, db
from workbench.backend.engine import dsh_engine
from workbench.backend.engine.runtime import GenerationRequest
from workbench.backend.services import (chapter_service, generation_service,
    knowledge_service, outline_service, project_service, prompt_registry_service, review_service)


@pytest.fixture()
def book(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "books")
    monkeypatch.setattr(config, "RUNTIME_DIR", tmp_path / ".workbench")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / ".workbench" / "test.db")
    db.init_db()
    config.ensure_runtime_dirs()
    prompt_registry_service.ensure_registry()
    monkeypatch.setattr(knowledge_service, "enqueue", lambda *args, **kwargs: None)
    monkeypatch.setattr(knowledge_service, "search", lambda *args, **kwargs: {"hits": [], "degradation": []})
    monkeypatch.setattr(generation_service, "run_task", lambda **kwargs: pytest.fail("Unexpected real model call"))
    project = project_service.create_project("功能修复隔离书")
    _, root = project_service.get_project_dir(project["id"])
    chapter = chapter_service.create_chapter(project["id"])
    path = root / chapter["rel_path"]
    path.write_text("陆衡把铜钱推到桌边。\n“先付船资。”", encoding="utf-8")
    return SimpleNamespace(id=project["id"], root=root, rel=chapter["rel_path"], path=path, tmp=tmp_path)


@pytest.mark.parametrize("length", [9000, 40000])
def test_headless_input_preserves_unicode_without_shell_or_long_argv(book, monkeypatch, length):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node unavailable")
    entry = book.tmp / "fake cli entry.mjs"
    entry.write_text("console.log(JSON.stringify({args:process.argv.slice(2),home:process.env.DSH_HOME}));", encoding="utf-8")
    monkeypatch.setattr(dsh_engine, "get_dsh_cli_command", lambda: [node, str(entry)])
    engine = dsh_engine.DshEngine()
    monkeypatch.setattr(engine, "_profile", lambda: "test-profile")
    monkeypatch.setattr(engine, "_patch_file", lambda *args: None)
    content = "文" * length + '\n"引号" $literal `literal`\r\n尾行'
    request = GenerationRequest(task_type="审稿", system="仅核验传输", messages=[{"role":"user", "content":content}], project_root=str(book.tmp))
    result = engine._attempt(request, "transport", None, 1, time.time())
    assert result.ok
    captured = json.loads(result.text)
    assert captured["args"] == ["--profile", "test-profile", engine._task_text(request)]
    assert content in captured["args"][-1]
    assert captured["home"] != str(Path.home() / ".dsh")
    checkpoint = engine.list_checkpoints()[0]
    assert checkpoint["input_transport"] == "stdin-utf8"
    assert content not in json.dumps(checkpoint, ensure_ascii=False)
    assert max(map(len, checkpoint["argv_tail"])) < 1000


def test_headless_timeout_drains_without_resending_stdin(book, monkeypatch):
    engine = dsh_engine.DshEngine()
    monkeypatch.setattr(dsh_engine, "get_dsh_cli_command", lambda: ["node", "vendor-entry.js"])
    monkeypatch.setattr(engine, "_profile", lambda: "test")
    monkeypatch.setattr(engine, "_patch_file", lambda *args: None)
    calls = []
    class Process:
        returncode = -1
        pid = 12345
        def communicate(self, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise subprocess.TimeoutExpired("fake", 1)
            return "", "stopped"
    monkeypatch.setattr(dsh_engine.subprocess, "Popen", lambda *args, **kwargs: Process())
    killed = []
    monkeypatch.setattr(engine, "_kill_tree", lambda process: killed.append(process.pid))
    result = engine._attempt(GenerationRequest(task_type="审稿", timeout_seconds=1), "timeout", None, 1, time.time())
    assert result.error_code == "TIMEOUT"
    assert "input" in calls[0] and calls[1] == {}
    assert killed == [12345] and not engine._procs


def test_soft_review_exposes_task_and_redacted_engine_failure(book, monkeypatch):
    monkeypatch.setattr(generation_service, "run_task", lambda **kwargs: {
        "ok":False, "task_id":17, "error_code":"NETWORK_ERROR",
        "error_message":"Authorization: Bearer abcdefghijklmnopqrst 请求失败"})
    result = review_service.soft_deslop(book.id, book.rel)
    assert result["ai_error_code"] == "NETWORK_ERROR" and result["task_id"] == 17
    assert "abcdefghijklmnopqrst" not in result["ai_error"]
    assert "请求失败" in result["ai_error"] and result["source_hash"]
    assert result["proposal_ids"] == []


def test_contract_review_preserves_engine_failure_instead_of_json_error(book, monkeypatch):
    monkeypatch.setattr(generation_service, "run_task", lambda **kwargs: {
        "ok":False, "task_id":18, "error_code":"TIMEOUT", "error_message":"执行超时"})
    result = review_service.review_chapter(book.id, book.rel)
    assert result["ai_error_code"] == "TIMEOUT" and result["task_id"] == 18
    assert "执行超时" in result["ai_error"] and not result["ai_used"]
    assert result["verdict"] == "不通过"


def test_soft_review_distinguishes_invalid_schema_from_valid_empty(book, monkeypatch):
    monkeypatch.setattr(generation_service, "run_task", lambda **kwargs: {"ok":True,"task_id":19,"text":"{}"})
    invalid = review_service.soft_deslop(book.id, book.rel)
    assert invalid["ai_error_code"] == "INVALID_MODEL_OUTPUT"
    monkeypatch.setattr(generation_service, "run_task", lambda **kwargs: {"ok":True,"task_id":20,"text":'{"建议":[]}'})
    valid = review_service.soft_deslop(book.id, book.rel)
    assert valid["ai_used"] and valid["ai_error_code"] == "" and valid["task_id"] == 20


def test_quality_matrix_maps_gate_keys_and_includes_format_total(book, monkeypatch):
    monkeypatch.setattr(review_service, "run_hard_gates", lambda *args, **kwargs: {
        "passed":False, "gates":[{"key":key, "passed":key != "格式门"} for key in
            ("格式门", "记号泄漏", "去AI味门", "禁词门", "字数门", "语言门")]})
    matrix = review_service.quality_matrix(book.id)
    cells = matrix["chapters"][0]["cells"]
    assert cells["格式门"] == cells["硬门禁总状态"] == "未通过"
    assert all(cells[key] == "通过" for key in ("字数门", "语言门", "禁词门", "去AI味"))


def test_ledger_description_tables_are_not_arithmetic_and_unknowns_cannot_pass(book):
    ledger = book.root / "状态/资源账本.md"
    ledger.write_text("| 项目 | 用途 | 来源 |\n| --- | --- | --- |\n| 代币2 | 购买升级 | 作者 |\n\n"
        "| 物品 | 章节 | 前值 | 增减 | 结余 | 依据 |\n| --- | --- | --- | --- | --- | --- |\n"
        "| 铜钱 | 第0001章 | 待核实 | +2 | 待核实 | 他收了两枚钱。 |\n"
        "| 灵石 | 第0001章 | 10 | -3 | 7 | 支出三块。 |\n", encoding="utf-8")
    problems = review_service.check_ledger_arithmetic(book.root)
    assert len(problems) == 1 and "待核实" in problems[0]["说明"]
    assert problems[0]["判定"] == "待核实"
    assert review_service._to_number("1e309") is None
    assert review_service._to_number("约2枚") is None


def test_outline_lock_keeps_history_added_by_save(book):
    outline_service.save_outline(book.id, "作者原大纲")
    meta = outline_service._read_meta(book.root)
    meta["candidates"] = [{"title":"候选一", "content":"新的候选大纲"}]
    outline_service._write_meta(book.root, meta)
    before = len(meta["history"])
    outline_service.lock_candidate(book.id, 0)
    after = outline_service._read_meta(book.root)
    assert len(after["history"]) == before + 1
    assert after["locked"]["title"] == "候选一"
