"""Opt-in paid model comparison. Isolated books, SQLite and DSH_HOME; no port.

Run from the repository root. This is never imported by pytest or CI. Provider
secrets travel to workers over stdin, live in DPAPI storage/environment only,
and are excluded from reports. Baseline source is the pre-upgrade archive.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from threading import Event
from comparison_support import prepare_experiment, frozen_layout, assert_frozen, routing_usage, usage_cursor, verified_fault_point, quota_blocker

ROOT = Path(__file__).resolve().parents[2]
BASELINE_ARCHIVE = ROOT / ".workbench/upgrade-baseline-2026-10-03/source.zip"
CASES = [
    {"id": 1, "name": "setting_delivery", "turns": [
        "请创建设定/评测人物.md，内容为人物卡：姓名李长歌，年龄十八，目标找到失散家人；实际保存后简述。",
        "将设定/评测人物.md 的姓名改为沈砚，保留年龄和目标；请读取当前版本再修改并保存。"]},
    {"id": 2, "name": "outline_delivery", "turns": [
        "请创建大纲/评测卷纲.md，写三章简短卷纲：入城、调查旧案、发现失散家人的线索；请实际保存。",
        "请补全大纲/评测卷纲.md 的第二章，加入调查账本的线索，不修改设定，实际保存。"]},
    {"id": 3, "name": "ordered_multi_step", "turns": [
        "先创建设定/评测规则.md，规定铜币只来自委托报酬；再创建大纲/评测任务.md，用该规则安排一章调查任务。两份都必须实际保存。",
        "根据已保存的设定/评测规则.md，只完善大纲/评测任务.md 的章末钩子，实际保存。"]},
    {"id": 4, "name": "negated_targets", "turns": [
        "不改大纲，只完善设定。请创建设定/评测边界.md，记录城门宵禁为亥时，实际保存。",
        "保持设定不变，只完善大纲。请创建大纲/评测边界.md，安排宵禁前调查的简短纲要并保存。"]},
    {"id": 5, "name": "reference_vs_output", "turns": [
        "根据世界设定、人物设定和场景设定列大纲，创建大纲/评测依据.md，只写一章纲要并实际保存，依据材料不修改。",
        "根据大纲/评测依据.md 更新状态/评测连续性.md，记录当前地点和资源，不修改大纲，实际保存。"]},
    {"id": 6, "name": "readonly_review", "turns": [
        "审稿当前章节，只报告连续性问题，不能改稿。请回读章节及世界设定，引用本书文件和具体句子。",
        "对刚才指出的问题只提修改建议，不要直接改正文；区分已核实与待核实。"]},
    {"id": 7, "name": "readonly_fallback_and_memo", "turns": [
        "我还没想好下一步，请说明当前最需要我明确的两个问题，先不修改任何文件。",
        "请将灵感记入备忘录/评测灵感.md：调查者在茶馆账本发现家人的笔迹。请实际保存。"]},
    {"id": 8, "name": "author_fact_continuity", "turns": [
        "主人公姓名是李长歌，代币只来自委托报酬，当前有三枚铜币。请复述这些作者确认的事实，只讨论。",
        "上轮我确认的姓名、代币来源和铜币数量是什么？只根据本会话依据回答，不补造背景。"]},
    {"id": 9, "name": "historical_choice_context", "turns": [
        "只讨论开篇方案。请给两个编号明确的方案：一是先入城再查账本，二是先查账本再入城；先不落盘。",
        "按第二个方案来，请创建大纲/评测选择.md 并实际保存，保持先查账本再入城的顺序。"]},
    {"id": 10, "name": "selected_file_context", "active": "设定/评测初始人物.md", "turns": [
        "把当前文件选区中的年龄十八改为十九并保存，保留其余内容；当前文件和选区已经附带。",
        "请读取刚才修改的同一个人物卡，报告其姓名、年龄和目标，引用实际文件，不再修改。"]},
    {"id": 11, "name": "idempotence_and_followup", "duplicate": True, "turns": [
        "创建备忘录/评测幂等.md，记录灵感：茶杯缺了一角。实际保存。",
        "请接着完善上一轮便签，补充一条：缺口与账本上的圆形印记对应。请实际保存。"]},
    {"id": 12, "name": "injected_fault_checkpoint_resume", "fault": True, "turns": [
        "先创建设定/评测续接规则.md，写城门宵禁为亥时；再创建大纲/评测续接任务.md，据此安排宵禁前查账本。按顺序分别实际保存。",
        "继续完成上一轮未完成步骤。先核对已经保存的文件，不重复创建已经完成的材料。",
        "继续核验上一轮交付，完成尚未落盘的材料；已经保存且符合要求的内容保留，不重复创建。"]},
]


def dump(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def inventory(book: Path) -> dict:
    result = {}
    for folder in ("设定", "大纲", "章节", "状态", "备忘录"):
        for file in sorted((book / folder).rglob("*")):
            if file.is_file():
                content = file.read_bytes()
                result[file.relative_to(book).as_posix()] = {"hash": hashlib.sha256(content).hexdigest(),
                    "content": content.decode("utf-8", errors="replace")}
    return result


def worker(args) -> int:
    payload = json.load(sys.stdin)
    source, work = Path(args.source).resolve(), Path(args.work).resolve()
    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    assert_frozen(manifest, Path(args.manifest).parent)
    if manifest["experiment_fingerprint"] != args.fingerprint:
        raise RuntimeError("worker 与冻结实验指纹不一致")
    layout = frozen_layout(Path(args.manifest).parent)
    if source != layout[f"{args.arm}_source"] or Path(args.assets).resolve() != layout["common_assets"]:
        raise RuntimeError("worker只能使用本实验的冻结代码与资产")
    for name, expected in manifest["inputs"]["vendor_lock"].items():
        if hashlib.sha256((Path(args.vendor) / name).read_bytes()).hexdigest() != expected:
            raise RuntimeError("vendor锁定版本在实验冻结后改变")
    sys.path.insert(0, str(source))
    from workbench.backend import config, db
    from workbench.backend.engine import dsh_paths, dsh_chat_profile, dsh_chat
    from workbench.backend.services import agent_service, project_service, provider_service, chat_service
    from workbench.backend.services import chat_run_service as runs, chat_interaction_service as interactions
    from workbench.backend.services import settings_service, secret_store, chapter_service
    case = next(c for c in CASES if c["id"] == args.case)
    work.mkdir(parents=True, exist_ok=True)
    for folder in ("agents", "skills", "rules", "workflows", "templates"):
        origin = source / folder if folder == "agents" else Path(args.assets) / folder
        if origin.is_dir():
            shutil.copytree(origin, work / folder, dirs_exist_ok=True)
        else:
            (work / folder).mkdir(exist_ok=True)
    config.PROJECT_ROOT, config.PROJECTS_DIR = work, work / "books"
    config.RUNTIME_DIR, config.DB_PATH = work / ".workbench", work / ".workbench/workbench.db"
    dsh_paths._PROJECT_ROOT, dsh_paths.DSH_HOME_DIR = work, work / ".workbench/dsh-home"
    dsh_paths.VENDOR_DSH_DIR = Path(args.vendor).resolve()
    dsh_paths.VENDOR_DSH_NODE_MODULES = dsh_paths.VENDOR_DSH_DIR / "node_modules"
    dsh_paths.VENDOR_DSH_BIN_DIR = dsh_paths.VENDOR_DSH_NODE_MODULES / ".bin"
    config.ensure_runtime_dirs()
    db.init_db()
    agent_service.ensure_builtin_agents()
    chat_service.schedule_title = lambda *_a, **_kw: None  # Exclude unrelated title calls in both arms.
    provider_service._refresh_cloud_knowledge = lambda *_: None
    original_projection = provider_service.project_to_dsh_home
    def env_only_projection():
        original_get = provider_service.get_secret
        provider_service.get_secret = lambda _: ""
        try:
            return original_projection()
        finally:
            provider_service.get_secret = original_get
    provider_service.project_to_dsh_home = env_only_projection
    model = dict(payload["model"], max_tokens=args.max_output_tokens)
    provider = payload["provider"]
    os.environ[provider_service._env_name(provider["provider_id"])] = payload["secret"]
    provider_service.upsert_provider(provider["provider_id"], display_name=provider["display_name"],
        base_url=provider["base_url"], models=[model], api_key=payload["secret"], enabled=True,
        timeout_seconds=120, retry_policy={"max_retries": 0})
    provider_service.set_default_target(provider["provider_id"], model["id"])
    # Explicit sampling waterfall, applied identically to the unmodified vendor
    # and both workbench source arms; effective request/header proves settings.
    plugin = work / "fixed-eval-parameters.mjs"
    plugin.write_text('export const name="fixed-eval-parameters"; export function apply(ctx) { ctx.on("agent/request", async (_payload,next)=>({...await next(),temperature:0.2,maxTokens:' + str(args.max_output_tokens) + '})); }\n', encoding="utf-8")
    original_patch = dsh_chat_profile.profile_patch
    dsh_chat_profile.profile_patch = lambda: [*original_patch(), {"insert": [{"id": "fixed-eval-parameters", "name": plugin.as_uri()}]}]
    settings_service.update_settings({"chat": {"intent_llm": True}, "writing": {"auto_deslop": {"enabled": False}}})
    project = project_service.create_project(name=f"合成对照{case['id']:02d}")
    _, book = project_service.get_project_dir(project["id"])
    seeds = {"设定/世界设定.md": "城门宵禁为亥时。铜币只来自委托报酬。茶馆位于东门内。\n",
             "设定/人物设定.md": "李长歌，十八岁。目标是找到失散家人。当前持有三枚铜币。\n",
             "设定/场景设定.md": "茶馆有旧账本和带缺口的茶杯。东门守卫按宵禁关闭城门。\n",
             "设定/评测初始人物.md": "姓名李长歌，年龄十八，目标找到失散家人。\n",
             "状态/当前状态.md": "李长歌在茶馆，三枚铜币，没有领取新的委托报酬。\n"}
    for rel, content in seeds.items():
        target = book / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    chapter = chapter_service.create_chapter(project["id"], title="合成茶馆")
    chapter_path = chapter.get("rel_path") or chapter.get("path") or "章节/第0001章.txt"
    if Path(chapter_path).is_absolute():
        chapter_path = Path(chapter_path).relative_to(book).as_posix()
    (book / chapter_path).write_text("李长歌将三枚铜币摆在桌面。店主把旧账本推来。\n“这页字是谁写的？”\n店主指了指缺口茶杯。李长歌收起铜币，沿账本上的墨迹辨认家人的笔画。\n", encoding="utf-8")
    session = chat_service.create_session(project_id=project["id"])
    chat_service.update_session(session["id"], {"permission_mode": "auto", "provider_id": provider["provider_id"], "model_id": model["id"]})
    injected = {"requested": bool(case.get("fault")), "applied": False, "evidence": None}
    current_run = {}
    native = dsh_chat.DshChatRuntime(timeout_seconds=300, idle_timeout_seconds=120)
    class FaultRuntime:
        def stream(self, **kwargs):
            if case.get("fault") and not injected["applied"] and current_run.get("id"):
                evidence = verified_fault_point(runs.get_run(current_run["id"]), book,
                    "设定/评测续接规则.md", "大纲/评测续接任务.md", checkpoint_supported=args.arm == "after")
                if evidence:
                    injected.update(applied=True, evidence=evidence)
                    yield {"type": "error", "code": "EVAL_TRANSPORT_FAULT",
                           "message": "评测主动注入：第一步已完成并核验、第二步模型请求开始前连接中断"}
                    return
            iterator = native.stream(**kwargs)
            try:
                for event in iterator:
                    yield event
            finally:
                iterator.close()
        def close_all(self): native.close_all()
        def close(self, session_id): native.close(session_id)
    runs._runtime = FaultRuntime()
    results = []
    initial = inventory(book)
    for turn_index, text in enumerate(case["turns"]):
        context = {}
        if case.get("active"):
            context = {"active_file": case["active"], "files": [case["active"]]}
            if turn_index == 0:
                context["selection"] = {"text": "年龄十八"}
        elif case["id"] == 6:
            context = {"active_file": chapter_path, "target_chapter": chapter_path}
        kwargs = {"text": text, "context": context, "client_request_id": f"case-{case['id']}-turn-{turn_index}"}
        if case.get("fault") and turn_index:
            kwargs["continue_from_run_id"] = results[-1]["run"]["id"]
        before = inventory(book)
        started = time.monotonic()
        intent_cursor = usage_cursor(config.DB_PATH)
        created = runs.create_run(session["id"], **kwargs)
        current_run["id"] = created["id"]
        duplicate = None
        if case.get("duplicate") and turn_index == 0:
            duplicate = runs.create_run(session["id"], **kwargs)["id"] == created["id"]
        handled = set()
        timed_out = False
        first_text_ms = None
        while True:
            snapshot = runs.get_run(created["id"])
            if first_text_ms is None and snapshot.get("text", "").strip():
                first_text_ms = round((time.monotonic() - started) * 1000)
            if snapshot["status"] in runs.TERMINAL:
                break
            if not timed_out and time.monotonic() - started > 360:
                runs.cancel(created["id"])
                timed_out = True
            for item in snapshot.get("interactions", []):
                if item["status"] != "pending" or item["id"] in handled:
                    continue
                handled.add(item["id"])
                if item["kind"] == "approval":
                    # Same author policy in both arms: decline unrelated writes.
                    response = {"decision": "reject" if item["payload"].get("scope", {}).get("out_of_scope") else "approve"}
                else:
                    response = {"answers": [{"id": q["id"], "selected": [], "custom": "只按本轮明确要求和现有材料执行；缺失依据请报告，不补造。"}
                                              for q in item["payload"]["questions"]]}
                interactions.respond(created["id"], item["id"], response)
            time.sleep(0.15)
        events = []
        cursor = 0
        while True:
            page = runs.events_after(created["id"], cursor)
            if not page:
                break
            events.extend(page)
            cursor = page[-1]["seq"]
        after = inventory(book)
        results.append({"input": kwargs, "run": snapshot, "events": events, "elapsed_ms": round((time.monotonic()-started)*1000),
            "first_text_ms": first_text_ms,
            "routing_usage": routing_usage(config.DB_PATH, after_id=intent_cursor),
            "duplicate_same_id": duplicate, "harness_timeout": timed_out,
            "changed_paths": [p for p in sorted(set(before)|set(after)) if before.get(p) != after.get(p)], "files_after": after})
        dump(work / "result.json", {"case": case, "arm": args.arm, "providerid": provider["provider_id"], "model": model["id"],
            "temperature": 0.2, "max_output_tokens": args.max_output_tokens,
            "experiment_fingerprint": args.fingerprint, "fault_injection": injected, "initial_files": initial, "turns": results})
        if snapshot.get("error_code") in {"AUTH", "AUTH_ERROR", "RATE_LIMIT", "QUOTA"}:
            break
    runs.shutdown()
    native.close_all()
    print(json.dumps({"case": case["id"], "arm": args.arm, "statuses": [r["run"]["status"] for r in results],
                      "result": str(work / "result.json")}, ensure_ascii=False), flush=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--source")
    parser.add_argument("--assets")
    parser.add_argument("--vendor")
    parser.add_argument("--manifest")
    parser.add_argument("--fingerprint")
    parser.add_argument("--work")
    parser.add_argument("--case", type=int)
    parser.add_argument("--arm", choices=("before", "after"))
    parser.add_argument("--cases", default="1-12")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--max-parallel", type=int, default=2)
    parser.add_argument("--max-output-tokens", type=int, default=8192)
    parser.add_argument("--freeze-only", action="store_true", help="Freeze code, assets and parameters without loading secrets or calling models")
    args = parser.parse_args()
    if not 1024 <= args.max_output_tokens <= 32000:
        parser.error("--max-output-tokens must be between 1024 and 32000")
    if args.worker:
        return worker(args)
    sys.path.insert(0, str(ROOT))
    from workbench.backend.services import provider_service, secret_store
    providers = provider_service.list_providers()
    provider = next((p for p in providers if p["provider_id"] == "Fluxl" and p["enabled"] and p["has_secret"]), None)
    if provider is None:
        raise RuntimeError("固定评测供应商 Fluxl 不可用；没有发送模型请求")
    model = next((m for m in provider["models"] if m["id"] == "glm-5.3-flash"), None)
    if model is None:
        raise RuntimeError("固定模型 glm-5.3-flash 不可用")
    output = (args.output or ROOT / ".workbench" / ("chat-comparison-" + time.strftime("%Y%m%d-%H%M%S"))).resolve()
    if not output.is_relative_to(ROOT / ".workbench"):
        raise RuntimeError("评测输出必须在本仓库 .workbench 下")
    identifiers = list(range(1, 13)) if args.cases == "1-12" else [int(part) for part in args.cases.split(",")]
    for identifier in identifiers:
        if identifier not in range(1, 13):
            raise RuntimeError("用例编号必须为1至12")
    parameters = {"archive": str(BASELINE_ARCHIVE), "archive_sha256": hashlib.sha256(BASELINE_ARCHIVE.read_bytes()).hexdigest(),
                "model": model["id"], "providerid": provider["provider_id"], "temperature": 0.2, "max_output_tokens": args.max_output_tokens,
                "model_parameters": {**model, "max_tokens": args.max_output_tokens}, "provider_base_url": provider["base_url"],
                "native_timeout_seconds": 300, "native_idle_timeout_seconds": 120, "harness_turn_timeout_seconds": 360,
                "cases": identifiers, "common_skills": "frozen common skills and rules in both arms",
                "raw_native_metadata": "Each arm/case/.workbench/dsh-home/chat-sessions; credentials are env-only"}
    manifest = prepare_experiment(ROOT, BASELINE_ARCHIVE, output, parameters,
                                  [case for case in CASES if case["id"] in identifiers])
    layout = frozen_layout(output)
    from workbench.backend.engine import dsh_paths
    node = str(dsh_paths.get_dsh_cli_command()[0])
    preflight = []
    for arm in ("before", "after"):
        runner = layout[f"{arm}_source"] / "workbench/backend/engine/dsh_chat_runner.mjs"
        code = "const m=await import(" + json.dumps(runner.as_uri()) + ");if(typeof m.apply!=='function')throw new Error('runner apply unavailable');"
        check = subprocess.run([node, "--input-type=module", "--eval", code], capture_output=True,
            text=True, encoding="utf-8", timeout=30,
            env={**os.environ, "DSH_HOME": str(output / "preflight-dsh-home")})
        preflight.append({"arm": arm, "runner": str(runner), "exit_code": check.returncode})
        if check.returncode:
            raise RuntimeError(f"冻结{arm}原生runner初始化失败；未发送模型请求")
    dump(output / "native-preflight.json", {"model_calls": 0, "checks": preflight})
    if args.freeze_only:
        print(json.dumps({"frozen": str(output), "fingerprint": manifest["experiment_fingerprint"], "model_calls": 0}, ensure_ascii=False))
        return 0
    secret = secret_store.get_secret(provider["secret_ref"])
    payload = json.dumps({"provider": {k: provider[k] for k in ("provider_id", "display_name", "base_url")}, "model": model, "secret": secret})
    provider_blocked = Event()
    def execute(arm, identifier):
        if provider_blocked.is_set():
            return {"case": identifier, "arm": arm, "skipped": True, "reason": "供应商已明确报告额度不足，剩余用例未发送"}
        work = output / arm / f"case-{identifier:02d}"
        if (work / "result.json").exists():
            prior = json.loads((work / "result.json").read_text(encoding="utf-8"))
            if (prior.get("experiment_fingerprint") != manifest["experiment_fingerprint"]
                    or prior.get("max_output_tokens") != args.max_output_tokens or prior.get("model") != model["id"]
                    or prior.get("case", {}).get("turns") != CASES[identifier-1]["turns"]):
                raise RuntimeError("已有结果的参数或用例不同，请使用新的 --output 目录")
            if quota_blocker(prior):
                provider_blocked.set()
            return {"case": identifier, "arm": arm, "existing": True, "result": str(work / "result.json")}
        if work.exists() and any(work.iterdir()):
            raise RuntimeError("worker已有未完成的执行产物，状态不明时禁止自动重放，请使用新的输出目录")
        command = [sys.executable, str(layout["harness"] / "run_comparison.py"), "--worker", "--source", str(layout[f"{arm}_source"]),
                   "--assets", str(layout["common_assets"]), "--vendor", str(ROOT / "workbench/vendor/dsh"),
                   "--manifest", str(output / "manifest.json"), "--fingerprint", manifest["experiment_fingerprint"],
                   "--work", str(work), "--case", str(identifier), "--arm", arm, "--max-output-tokens", str(args.max_output_tokens)]
        process = subprocess.run(command, input=payload, text=True, encoding="utf-8", capture_output=True,
                                 timeout=450 * len(CASES[identifier-1]["turns"]) + 120,
                                 env={**os.environ, "PYTHONUTF8": "1"}, cwd=work.parent if work.parent.exists() else output)
        summary = {"case": identifier, "arm": arm, "exit_code": process.returncode, "result": str(work / "result.json")}
        if (work / "result.json").exists() and quota_blocker(json.loads((work / "result.json").read_text(encoding="utf-8"))):
            provider_blocked.set()
            summary.update(provider_blocked=True, reason="供应商额度不足")
        # Redact with the originating store before saving any infrastructure diagnostics.
        diagnostic = secret_store.redact(process.stderr.replace(secret, "[REDACTED]"))[-8000:]
        if diagnostic:
            work.mkdir(parents=True, exist_ok=True)
            (work / "worker-diagnostic.txt").write_text(diagnostic, encoding="utf-8")
        return summary
    summaries = []
    with ThreadPoolExecutor(max_workers=max(1, min(args.max_parallel, 4))) as pool:
        futures = [pool.submit(execute, arm, identifier) for identifier in identifiers for arm in ("before", "after")]
        for future in as_completed(futures):
            summary = future.result()
            summaries.append(summary)
            dump(output / "summary.json", summaries)
            print(json.dumps(summary, ensure_ascii=False), flush=True)
    print(str(output), flush=True)
    return int(any(s.get("exit_code", 0) for s in summaries))


if __name__ == "__main__":
    raise SystemExit(main())
