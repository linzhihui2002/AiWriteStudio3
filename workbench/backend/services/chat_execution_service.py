"""Small deterministic policies for chat budgets, routing context and receipts.

Native history remains owned by dsh. Receipts refer to the existing file journal;
they neither grant authority nor create a second file-change source of truth.
"""
from __future__ import annotations

import hashlib
import json
import re
from . import agent_service, provider_service
from .errors import InvalidOperationError, ServiceError
from ..engine.runtime import estimate_tokens


def material_budget(choice: dict, system: str, specs: list[dict]) -> dict:
    capacity, output = provider_service.DEFAULT_CONTEXT_WINDOW, provider_service.DEFAULT_MAX_TOKENS
    try:
        provider = provider_service.get_provider(choice["provider"])
        model = next((m for m in provider["models"] if m["id"] == choice["model"]), None)
        if model:
            capacity, output = int(model["context_window"]), int(model["max_tokens"])
    except ServiceError:
        pass  # Legacy/fake model targets retain the existing conservative defaults.
    static = estimate_tokens(system) + estimate_tokens(json.dumps(specs, ensure_ascii=False))
    history_reserve, safety = max(1024, capacity // 5), max(512, capacity // 50)
    budget = min(32000, capacity - output - static - history_reserve - safety)
    if budget <= 0:
        raise InvalidOperationError("模型窗口不足以容纳完整系统规则、技能、工具与输出预留，请选择更大窗口的模型")
    return {"material_tokens": budget, "context_window": capacity, "output_reserved": output,
            "static_tokens": static, "history_reserved": history_reserve, "safety_reserved": safety}


def plan_fingerprint(plan: list[dict]) -> str:
    value = [{k: step.get(k) for k in ("agent", "write_targets", "delivery_targets", "read_only_refs", "note")} for step in plan]
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def receipts(changes: list[dict]) -> list[dict]:
    return [{k: c.get(k) for k in ("id", "run_id", "tool_call_id", "path", "destination", "operation", "after_hash")}
            for c in changes if c.get("status") == "applied" and not c.get("reverted_at")]


def verify_receipts(project_id: int, items: list[dict]) -> bool:
    from . import file_change_service as files
    # Journal order wins even if continuation/checkpoint receipts arrive after
    # current-run receipts. Caller ordering cannot resurrect an older version.
    actual = []
    for receipt in items:
        change = next((c for c in files.list_changes(str(receipt["run_id"]), project_id=project_id)
                       if c["id"] == receipt["id"]), None)
        if not change or change["status"] != "applied":
            return False
        actual.append(change)
    expected = {}
    for change in sorted(actual, key=lambda item: item["id"]):
        path, destination, operation = change["path"], change.get("destination"), change["operation"]
        expected[path] = None if operation in {"delete", "move"} else change["after_hash"]
        if destination:
            expected[destination] = change["after_hash"]
    try:
        return bool(items) and all(files.file_state(project_id, path)["hash"] == digest
                                   for path, digest in expected.items())
    except (ServiceError, OSError):
        return False


def covers_targets(items: list[dict], targets: list[str]) -> bool:
    """Every declared artifact needs a matching receipt; one file cannot prove two targets."""
    paths = [p for item in items for p in (item["path"], item.get("destination")) if p]
    return bool(targets) and all(any(path == target or path.startswith(target.rstrip("/") + "/")
                                    for path in paths) for target in targets)


def delivery_targets(text: str, write_targets: list[str], *, active_file: str = "",
                     target_chapter: str = "", fallback: bool = True) -> list[str]:
    """Author-required outputs, independently of the executor's allowed scope.

    A context keeper may be allowed both settings and state, while the author
    requests one state file. Conversely, writing another allowed file cannot
    satisfy a named output. New paths need no existing-file lookup here: the
    workspace tool's path/scope guard remains the authority for every write.
    """
    from . import intent_service as intent

    roots = "|".join(re.escape(name) for name in agent_service.MATERIAL_DIRS)
    paths = re.compile(r"(?:" + roots + r")[/\\][^，,。；;！!\r\n\t`\"'<>（）()\[\]{}]+?\.(?:md|txt|json|ya?ml|csv)", re.I)
    action = re.compile(intent._WRITE_ACTION.pattern + r"|改(?:为|成|好|一下|动)|保存|落盘|记入|记到")
    read_action = re.compile(r"读取|回读|阅读|查看|引用|核对|读")
    spans = intent._scan_material_mentions(text)
    prohibited = intent.forbidden_materials(text)
    out: list[str] = []

    def add(raw: str) -> None:
        path = str(raw or "").replace("\\", "/").strip("/")
        if (not path or path.split("/", 1)[0] not in agent_service.MATERIAL_DIRS
                or any(part in {".", "..", ""} for part in path.split("/"))
                or any(path == p or path.startswith(p.rstrip("/") + "/") for p in prohibited)):
            return
        if path not in out:
            out.append(path)

    for match in paths.finditer(text):
        position = len(intent._normalize(text[:match.start()]))
        top = match.group().replace("\\", "/").split("/", 1)[0]
        mention = next((span for span in spans if span["position"] == position
                        and span["material"] == top), None)
        prefix = re.split(r"[，,。；;！!]", text[:match.start()])[-1]
        suffix = re.split(r"[，,。；;！!]", text[match.end():])[0]
        read_positions = [m.start() for m in read_action.finditer(prefix)]
        write_positions = [m.start() for m in action.finditer(prefix)]
        source_only = (read_positions and (not write_positions or max(read_positions) > max(write_positions))
                       and not action.search(suffix))
        if mention and not mention["basis"] and not mention["locating"] and not source_only:
            add(match.group())

    # Filename words are not additional material types (设定/大纲草案.md is
    # one setting file); references and negated paths are excluded above.
    for material in intent._named_materials(paths.sub("", text))["explicit"]:
        if not any(item.split("/", 1)[0] == material for item in out):
            add(target_chapter if material == "章节" and target_chapter else material)

    if active_file:
        norm = intent._normalize(text)
        for match in re.finditer(r"(?:当前|这个|这份|该|同一个|刚才修改的)(?:文件|人物卡|设定卡)|选区|选中的(?:文字|内容)|这(?:一)?段", norm):
            clause = re.split(r"[，,。；;！!]", norm[:match.start()])[-1]
            prep = max((clause.rfind(p) for p in intent._BASIS_PREPOSITIONS), default=-1)
            basis = prep >= 0 and not action.search(clause[prep + 2:])
            local = re.split(r"[，,。；;！!]", norm[match.start():])[0]
            if not basis and not intent._NEGATE.search(clause + local) and action.search(norm):
                add(active_file)
                break
    if not out and fallback:
        for target in write_targets:
            add(target_chapter if target == "章节" and target_chapter else target)
    return out


def role_is_read_only(agent: str) -> bool:
    return agent in agent_service.READ_ONLY_AGENTS


def fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def verify_report(project_id: int, report: dict, *, context_fingerprint: str, system: str) -> bool:
    """A persisted report is reusable only with versioned, unchanged inputs."""
    from . import file_change_service as files
    versions = report.get("input_versions") or []
    text = report.get("text")
    if (not report.get("reusable") or not versions or not isinstance(text, str) or not text.strip()
            or report.get("text_hash") != fingerprint(text)
            or report.get("context_fingerprint") != context_fingerprint
            or report.get("system_fingerprint") != fingerprint(system)):
        return False
    try:
        return all(isinstance(item, dict) and item.get("path") and item.get("hash")
                   and files.file_state(project_id, item["path"])["hash"] == item["hash"]
                   for item in versions)
    except (ServiceError, OSError, ValueError, TypeError):
        return False
