"""Standard-library evidence helpers for the opt-in model comparison.

These helpers never load a provider, call a model or open a listening socket.
"""
from __future__ import annotations

from collections import Counter
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import zipfile

ASSET_FOLDERS = ("agents", "skills", "rules", "workflows", "templates")
IGNORED_PARTS = {"__pycache__", ".pytest_cache", ".mypy_cache"}
SCHEMA = 3


def quota_blocker(result: dict) -> bool:
    """Stop remaining experiments on a verified provider quota error."""
    return any(turn.get("run", {}).get("error_code") == "QUOTA"
               or any(marker in str(turn.get("run", {}).get("error_message") or "")
                      for marker in ("insufficient_user_quota", "额度不足", "预扣费额度失败"))
               for turn in result.get("turns", []))


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def eligible(relative: Path) -> bool:
    return not any(part in IGNORED_PARTS for part in relative.parts) and relative.suffix not in {".pyc", ".pyo"}


def tree_manifest(origin: Path, *, bindings: dict | None = None) -> dict:
    """Hash directory structure and files; refuse links escaping the asset root."""
    origin = origin.resolve()
    if not origin.exists():
        return {"present": False, "directories": [], "files": {}, "sha256": digest({"present": False})}
    files, directories, links = {}, [], {}
    def visit(directory: Path):
        for path in sorted(directory.iterdir()):
            relative = path.relative_to(origin)
            if not eligible(relative):
                continue
            name = relative.as_posix()
            if name in (bindings or {}):
                target = Path(bindings[name]).resolve()
                if path.resolve() != target or not (path.is_symlink() or path.is_junction()):
                    raise RuntimeError(f"锁定vendor绑定遭到改变：{name}")
                links[name] = str(target)
                continue  # Never walk a runtime binding into the source snapshot.
            if not path.resolve().is_relative_to(origin):
                raise RuntimeError(f"评测快照拒绝越界链接：{name}")
            if path.is_dir():
                directories.append(name)
                visit(path)
            elif path.is_file():
                content = path.read_bytes()
                files[name] = {"sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content)}
    visit(origin)
    if set(links) != set(bindings or {}):
        raise RuntimeError("锁定vendor绑定缺失")
    value = {"present": True, "directories": directories, "files": files, "links": links}
    return {**value, "sha256": digest(value)}


def copy_tree_once(origin: Path, destination: Path, expected: dict) -> None:
    if destination.exists():
        raise RuntimeError(f"冻结目录已经存在，禁止覆盖：{destination}")
    destination.mkdir(parents=True)
    for directory in expected["directories"]:
        (destination / directory).mkdir(parents=True, exist_ok=True)
    for relative, entry in expected["files"].items():
        content = (origin / relative).read_bytes()
        if hashlib.sha256(content).hexdigest() != entry["sha256"]:
            raise RuntimeError(f"冻结期间源文件改变，请使用新的输出目录：{relative}")
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    frozen = tree_manifest(destination)
    # Missing source folders deliberately become empty worker asset directories.
    expected_frozen = expected if expected["present"] else tree_manifest(destination)
    if frozen["sha256"] != expected_frozen["sha256"]:
        raise RuntimeError("冻结副本校验失败")


def archive_inventory(archive_path: Path) -> dict:
    files = {}
    with zipfile.ZipFile(archive_path) as archive:
        for item in archive.infolist():
            relative = Path(item.filename)
            if (item.is_dir() or not eligible(relative)
                    or not item.filename.startswith(("workbench/backend/", "agents/"))):
                continue
            if relative.is_absolute() or ".." in relative.parts:
                raise RuntimeError("基线归档路径越界")
            content = archive.read(item)
            files[item.filename] = {"sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content)}
    return {"files": files, "sha256": digest(files)}


def experiment_inputs(root: Path, archive: Path, parameters: dict, cases: list[dict]) -> dict:
    harness = tree_manifest(root / "evals/chat-workspace")
    backend = tree_manifest(root / "workbench/backend")
    assets = {folder: tree_manifest(root / folder) for folder in ASSET_FOLDERS}
    return {"schema_version": SCHEMA, "parameters": parameters, "case_definitions": cases,
            "before_archive": {"path": str(archive), "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                               "contents": archive_inventory(archive)},
            "after_backend": backend, "current_assets": assets, "harness": harness,
            "vendor_lock": {name: hashlib.sha256((root / "workbench/vendor/dsh" / name).read_bytes()).hexdigest()
                            for name in ("package.json", "package-lock.json") if (root / "workbench/vendor/dsh" / name).is_file()}}


def frozen_layout(output: Path) -> dict:
    return {"before_source": output / "frozen/before-source", "after_source": output / "frozen/after-source",
            "common_assets": output / "frozen/common-assets", "harness": output / "frozen/harness"}


def bind_locked_vendor(source: Path, vendor: Path) -> None:
    """Only the declared locked vendor may escape a frozen code root."""
    link = source / "workbench/vendor"
    if link.exists() or link.is_symlink():
        raise RuntimeError("vendor绑定已经存在，禁止覆盖")
    link.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        subprocess.run(["powershell", "-NoProfile", "-Command",
                        "New-Item -ItemType Junction -Path $env:WORKBENCH_EVAL_LINK -Target $env:WORKBENCH_EVAL_VENDOR | Out-Null"],
                       env={**os.environ, "WORKBENCH_EVAL_LINK": str(link), "WORKBENCH_EVAL_VENDOR": str(vendor)},
                       check=True, capture_output=True)
    else:
        link.symlink_to(vendor, target_is_directory=True)
    if link.resolve() != vendor.resolve():
        raise RuntimeError("vendor绑定目标不一致")


def assert_frozen(manifest: dict, output: Path) -> None:
    for name, entry in manifest["frozen_directories"].items():
        target = (output / entry["relative_path"]).resolve()
        if not target.is_relative_to(output.resolve()) or tree_manifest(target, bindings=entry.get("bindings"))["sha256"] != entry["sha256"]:
            raise RuntimeError(f"已冻结目录遭到改变，禁止复用：{name}")


def prepare_experiment(root: Path, archive: Path, output: Path, parameters: dict, cases: list[dict]) -> dict:
    """Freeze before any worker starts; never overwrite or adopt legacy results."""
    inputs = experiment_inputs(root, archive, parameters, cases)
    identity = digest(inputs)
    output.mkdir(parents=True, exist_ok=True)
    manifest_path = output / "manifest.json"
    if manifest_path.exists():
        previous = json.loads(manifest_path.read_text(encoding="utf-8"))
        if previous.get("schema_version") != SCHEMA or previous.get("experiment_fingerprint") != identity:
            raise RuntimeError("已有目录的参数、源码、资产或评测工具不同（旧目录亦不可续写），请使用新的 --output 目录")
        assert_frozen(previous, output)
        return previous
    if any(output.iterdir()):
        raise RuntimeError("输出目录已有未归属的产物，禁止覆盖或重放；请使用新目录")
    layout = frozen_layout(output)
    before = layout["before_source"]
    before.mkdir(parents=True)
    with zipfile.ZipFile(archive) as baseline:
        for relative, entry in inputs["before_archive"]["contents"]["files"].items():
            content = baseline.read(relative)
            if hashlib.sha256(content).hexdigest() != entry["sha256"]:
                raise RuntimeError("基线归档在冻结期间改变")
            target = (before / relative).resolve()
            if not target.is_relative_to(before.resolve()):
                raise RuntimeError("归档路径越界")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
    copy_tree_once(root / "workbench/backend", layout["after_source"] / "workbench/backend", inputs["after_backend"])
    for folder, entry in inputs["current_assets"].items():
        copy_tree_once(root / folder, layout["common_assets"] / folder, entry)
    copy_tree_once(root / "agents", layout["after_source"] / "agents", inputs["current_assets"]["agents"])
    copy_tree_once(root / "evals/chat-workspace", layout["harness"], inputs["harness"])
    # A live edit while files were copied must not masquerade as a coherent snapshot.
    if digest(experiment_inputs(root, archive, parameters, cases)) != identity:
        raise RuntimeError("冻结期间工作树改变，产物保留但不会启动worker，请使用新目录")
    vendor = (root / "workbench/vendor").resolve()
    directories = {}
    for name, path in layout.items():
        bindings = {"workbench/vendor": str(vendor)} if name.endswith("_source") else {}
        if bindings:
            bind_locked_vendor(path, vendor)
        directories[name] = {"relative_path": path.relative_to(output).as_posix(), "bindings": bindings,
                             "sha256": tree_manifest(path, bindings=bindings)["sha256"]}
    manifest = {**parameters, "schema_version": SCHEMA, "experiment_fingerprint": identity,
                "inputs": inputs, "frozen_directories": directories,
                "actual_assets": {arm: {folder: {"frozen_path": str((layout[f"{arm}_source"] / folder) if folder == "agents" else layout["common_assets"] / folder),
                                                  "worker_path": f"{arm}/case-NN/{folder}"}
                                         for folder in ASSET_FOLDERS} for arm in ("before", "after")},
                "native_history_authority": "dsh", "source_policy": "All workers import frozen code; each copies only frozen assets.",
                "auxiliary_calls": "session title disabled; semantic intent usage is recorded separately from native execution"}
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def routing_usage(database: Path, *, after_id: int = 0, through_id: int | None = None) -> dict:
    """Count authoritative intent rows once; native journal tokens stay separate."""
    if not database.exists():
        return {"available": False, "reason": "isolated database unavailable", "rows": [], "totals": {}}
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(token_usage)")}
        if not {"id", "task_type", "prompt_tokens", "completion_tokens", "total_tokens"}.issubset(columns):
            return {"available": False, "reason": "legacy database lacks intent usage fields", "rows": [], "totals": {}}
        sql = "SELECT id, task_id, prompt_tokens, completion_tokens, total_tokens FROM token_usage WHERE task_type=? AND id>?"
        values: list = ["意图判定", after_id]
        if through_id is not None:
            sql += " AND id<=?"; values.append(through_id)
        rows = [{"usage_id": row[0], "task_id": row[1], "prompt_tokens": int(row[2]),
                 "completion_tokens": int(row[3]), "total_tokens": int(row[4])}
                for row in connection.execute(sql + " ORDER BY id", values)]
    totals = Counter()
    for row in rows:
        totals.update({key: row[key] for key in ("prompt_tokens", "completion_tokens", "total_tokens")})
    return {"available": True, "source": "isolated token_usage where task_type='意图判定'", "rows": rows,
            "calls_with_reported_usage": len(rows), "totals": dict(totals)}


def usage_cursor(database: Path) -> int:
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
        return int(connection.execute("SELECT COALESCE(MAX(id),0) FROM token_usage").fetchone()[0])


def verified_fault_point(snapshot: dict, book: Path, first_path: str, second_path: str, *, checkpoint_supported: bool) -> dict | None:
    """The first durable step must finish before injection, with no second file."""
    first, second = book / first_path, book / second_path
    if not first.is_file() or second.exists():
        return None
    file_hash = hashlib.sha256(first.read_text(encoding="utf-8").encode("utf-8")).hexdigest()
    checkpoint = next((step for step in snapshot.get("plan_state", {}).get("steps", []) if step.get("step") == 1), None)
    receipt = next((item for item in (checkpoint or {}).get("receipts", []) if item.get("path") == first_path and item.get("after_hash") == file_hash), None)
    if checkpoint_supported:
        if not checkpoint or checkpoint.get("status") != "verified" or not receipt:
            return None
    else:
        completed = any(step.get("kind") == "plan_step" and step.get("step") == 1 and step.get("status") == "done" for step in snapshot.get("steps", []))
        if not completed:
            return None
    return {"run_id": snapshot["id"], "first_path": first_path, "first_hash": file_hash,
            "second_path": second_path, "second_exists": False, "observed_last_seq": snapshot.get("last_seq"),
            "checkpoint_supported": checkpoint_supported, "first_step_verified": bool(checkpoint_supported),
            "first_native_step_completed": True, "checkpoint": checkpoint, "receipt": receipt,
            "fault_stage": "before_second_native_stream"}
