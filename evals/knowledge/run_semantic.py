"""Run the real fixed ONNX model against an isolated original novel corpus.

Run from repository root: .venv/Scripts/python.exe evals/knowledge/run_semantic.py
No user novel, global DSH_HOME, cloud API or historical shared vector DB is used.
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from workbench.backend import config, db
from workbench.backend.services import chapter_service, embedding_service, knowledge_service as kb, project_service
from workbench.backend.services.settings_service import update_settings


def main():
    cases = json.loads(Path(__file__).with_name("cases.json").read_text(encoding="utf-8"))
    model_dir = embedding_service.model_directory().resolve()
    report = {"model": embedding_service.MODEL_ID, "revision": embedding_service.REVISION, "cases": []}
    eval_root = config.RUNTIME_DIR / "evals"
    eval_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="novel-knowledge-eval-", dir=eval_root) as folder:
        base = Path(folder)
        config.PROJECT_ROOT, config.PROJECTS_DIR = base, base / "projects"
        config.RUNTIME_DIR, config.DB_PATH = base / ".workbench", base / ".workbench/workbench.db"
        config.PROJECTS_DIR.mkdir()
        config.ensure_runtime_dirs()
        db.init_db()
        update_settings({"vector": {"mode": "local", "model_dir": str(model_dir), "enabled": False}})
        long_text = "沈砚沿旧港石阶寻找鱼符。柳青在渡口等候。" * 300
        windows = embedding_service.chunk_document(long_text, embedding_service.resolve_embedder())
        assert windows and windows[0]["start"] == 0 and windows[-1]["end"] == len(long_text)
        assert all(long_text[item["start"]:item["end"]] == item["text"] for item in windows)
        assert all(right["start"] <= left["end"] for left, right in zip(windows, windows[1:]))
        report["long_paragraph_chunks"] = len(windows)
        # Control lifecycle explicitly; no incidental background extraction or cloud calls.
        kb.enqueue = lambda *args, **kwargs: None
        ident = project_service.create_project(name="原创语义验收书")["id"]
        gold = []
        for index, case in enumerate(cases, 1):
            row = chapter_service.create_chapter(ident, f"验收样例{index}")
            chapter_service.save_chapter(ident, row["rel_path"], case["text"], status="完成")
            gold.append(row["rel_path"])
        started = time.perf_counter()
        result = kb.sync(ident, background=False, use_ai=False, enable=False)
        report["sync_seconds"] = round(time.perf_counter() - started, 3)
        report["chunks"] = kb.overview(ident)["counts"]["chunks"]
        report["fingerprint"] = embedding_service.resolve_embedder(ident).fingerprint
        for case, target in zip(cases, gold):
            started = time.perf_counter()
            response = kb.search(ident, case["query"], limit=5)
            found = [hit["rel_path"] for hit in response["hits"]]
            report["cases"].append({"kind": case["kind"], "query": case["query"], "gold": target,
                "rank": found.index(target) + 1 if target in found else None,
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
                "degraded_reason": response["degraded_reason"], "hits": found})
        kb.shutdown()
    report["recall_at_5"] = sum(case["rank"] is not None for case in report["cases"]) / len(cases)
    timings = sorted(case["elapsed_ms"] for case in report["cases"])
    report["median_query_ms"] = timings[len(timings) // 2]
    report["p95_query_ms"] = timings[int(len(timings) * .95)]
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes
        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("faults", wintypes.DWORD),
                ("peak_working_set", ctypes.c_size_t), ("working_set", ctypes.c_size_t),
                ("peak_paged_pool", ctypes.c_size_t), ("paged_pool", ctypes.c_size_t),
                ("peak_nonpaged_pool", ctypes.c_size_t), ("nonpaged_pool", ctypes.c_size_t),
                ("pagefile", ctypes.c_size_t), ("peak_pagefile", ctypes.c_size_t)]
        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        get_process = ctypes.windll.kernel32.GetCurrentProcess
        get_process.restype = wintypes.HANDLE
        get_memory = ctypes.windll.psapi.GetProcessMemoryInfo
        get_memory.argtypes = (wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD)
        get_memory.restype = wintypes.BOOL
        handle = get_process()
        ok = get_memory(handle, ctypes.byref(counters), counters.cb)
        report["process_rss_mb"] = round(counters.working_set / 1024 ** 2, 1) if ok else None
        report["process_peak_rss_mb"] = round(counters.peak_working_set / 1024 ** 2, 1) if ok else None
    else:
        import resource
        usage = resource.getrusage(resource.RUSAGE_SELF)
        report["process_peak_rss_mb"] = round(usage.ru_maxrss / 1024, 1)
    output = Path(__file__).with_name("last-result.json")
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "cases"}, ensure_ascii=False))
    print(f"Report: {output}")
    return 0 if report["recall_at_5"] >= .8 and not any(c["degraded_reason"] for c in report["cases"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
