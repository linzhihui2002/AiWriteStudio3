"""写域守卫：把路由声明的本轮可写材料（``write_targets``）接入既有对话权限判定链。

判定顺序固定为「只读 / 仅讨论拒绝（deny 优先）→ **写域** → 批准强度（ask/auto/full）」，
本模块只负责第二个维度，不新造并行的授权机制：

- 范围外写入按档位升级为「请求批准」，作者可选择「仅此次」或「本任务内允许这类材料」；
- 授权以 ``scope_key``（``run_id``，无 ``run_id`` 时 ``session:<id>``）为键**仅存进程内**，
  run 结束即失效；越界授权不纳入既有「本任务内不再询问」的免询范围；
- 授权 / 放行 / 拒绝都会写入运行事件（``context_warning``）与 ``operation_log`` 供审计。

本模块只做纯判定与留痕，不落盘、不加表、不加列。
"""

from __future__ import annotations

import threading
import uuid
from pathlib import PurePosixPath

from .fs_utils import resolve_within

#: 本书可写材料的顶层目录（与 ``agent_service.MATERIAL_DIRS`` 同源）
MATERIAL_TOP_DIRS = ("章节", "设定", "大纲", "状态", "备忘录")

#: 自由便签区：作者随手记的便签不受本轮写域限制（它不构成「改错材料」，
#: 也不会顶替本轮真正要产出的材料），因此任何一轮都可写。
ALWAYS_WRITABLE = ("备忘录",)

_grants: dict[str, set[str]] = {}
_grants_lock = threading.Lock()


def _norm(value: object) -> str:
    return str(value or "").replace("\\", "/").strip("/")


def _is_run_id(value: object) -> bool:
    try:
        uuid.UUID(str(value))
        return True
    except (ValueError, TypeError, AttributeError):
        return False


def _label(dirs: list[str], files: list[str]) -> str:
    return "、".join([*(f"{name}/" for name in dirs), *files])


def build_scope(*, project_id: int | None, write_targets, target_chapter: str = "") -> dict:
    """把路由的 ``write_targets`` 归一为 ``{"dirs", "files", "declared", "label"}``。

    - 元素是 ``MATERIAL_TOP_DIRS`` 里的目录名 → 进 ``dirs``；
    - 元素是具体相对路径（含 ``/``）→ 用 ``fs_utils.resolve_within`` 校验并归一成 posix
      相对路径后进 ``files``；该路径只授权自己，**不放开**它所属的整个顶层目录；
    - ``project_id`` 为空时只做目录名白名单与路径形态校验；
    - 非法项（``..``、绝对路径、隐藏路径、越界）直接丢弃；``dirs`` 与 ``files`` 皆空 →
      ``declared=False``（本轮未声明产出材料）。

    自由便签区（``ALWAYS_WRITABLE``，当前仅 ``备忘录/``）不在写域判定范围内：
    作者随手记的便签不应该被写域拦住——它既不是本轮声明的产出材料，也不会
    构成「改错材料」（即写错地方、动到别的书稿），故 :func:`evaluate` 对它的
    顶层目录一律放行，无论本轮是否声明、声明了什么。
    """
    dirs: list[str] = []
    files: list[str] = []
    candidates: list[object] = [*(write_targets or []), target_chapter]
    for raw in candidates:
        text = str(raw or "").replace("\\", "/").strip()
        if not text or text.startswith("/") or ":" in text:
            continue
        item = text.strip("/")
        if not item:
            continue
        if "/" not in item:
            if item in MATERIAL_TOP_DIRS and item not in dirs:
                dirs.append(item)
            continue
        parts = PurePosixPath(item).parts
        if not parts or any(part in {".", ".."} or part.startswith(".") for part in parts):
            continue
        rel = item
        if project_id is not None:
            try:
                from .project_service import get_project_dir

                _, root = get_project_dir(int(project_id))
                rel = resolve_within(root, item).relative_to(root.resolve()).as_posix()
            except Exception:  # noqa: BLE001 - 非法项一律丢弃，不影响本轮其他材料
                continue
        if rel and rel not in files:
            files.append(rel)
    return {"dirs": dirs, "files": files, "declared": bool(dirs or files),
            "label": _label(dirs, files)}


def evaluate(scope: dict | None, rel_path: str) -> dict:
    """判定某个相对路径是否在本轮写域内。

    → ``{"allowed", "material", "reason"}``（命中 run 级越界授权时附 ``"granted": True``）。
    """
    rel = _norm(rel_path)
    material = rel.split("/", 1)[0] if rel else ""
    if material and material in ALWAYS_WRITABLE:
        return {"allowed": True, "material": material,
                "reason": f"{material}是自由便签区，不受本轮写域限制"}
    if not isinstance(scope, dict) or not scope.get("declared"):
        return {"allowed": False, "material": material, "reason": "本轮未声明产出材料"}
    dirs = [str(item) for item in scope.get("dirs") or []]
    files = [str(item) for item in scope.get("files") or []]
    if rel and rel in files:
        return {"allowed": True, "material": material, "reason": "在本轮写域内"}
    if material and material in dirs:
        return {"allowed": True, "material": material, "reason": "在本轮写域内"}
    key = str(scope.get("key") or scope.get("scope_key") or "")
    if key and material and material in grants(key):
        return {"allowed": True, "material": material, "granted": True,
                "reason": f"已获本任务越界授权（{material}/）"}
    label = str(scope.get("label") or "") or "未声明"
    return {"allowed": False, "material": material,
            "reason": f"本轮范围是 {label}，不含 {material}/"}


def grant(scope_key: str, material: str) -> None:
    """记录 run 级越界授权（材料目录名）；run 结束即失效。"""
    key = str(scope_key or "")
    name = _norm(material)
    if not key or not name:
        return
    with _grants_lock:
        _grants.setdefault(key, set()).add(name)
    trace(key, action="chat-scope-grant",
          message=f"已授权本任务内可写 {name}/（本轮越界写入无需再次批准）")


def grants(scope_key: str) -> set[str]:
    """读回该 run 已被授权的材料目录名。"""
    with _grants_lock:
        return set(_grants.get(str(scope_key or ""), ()))


def describe(scope: dict | None) -> str:
    """中文范围描述，用于提示词与批准卡。"""
    if not isinstance(scope, dict) or not scope.get("declared"):
        return "本轮未声明可写材料"
    label = str(scope.get("label") or "")
    return f"本轮范围：{label}" if label else "本轮范围：仅限已列出的材料"


def trace(scope_key: str, *, action: str, message: str, rel_path: str = "",
          project_id: int | None = None, run_id: str = "") -> None:
    """把写域判定结果写入运行事件与操作日志（失败不阻断写操作）。"""
    try:
        from . import operation_log

        operation_log.log(project_id, action, rel_path or None,
                          {"scope_key": str(scope_key or ""), "message": message})
    except Exception:  # noqa: BLE001 - 日志失败不影响主流程
        pass
    target = str(run_id or "") or (str(scope_key) if _is_run_id(scope_key) else "")
    if not target:
        return
    try:
        from . import chat_run_service

        chat_run_service.emit(target, "context_warning", message=message)
    except Exception:  # noqa: BLE001 - 事件失败不影响主流程
        pass


__all__ = [
    "ALWAYS_WRITABLE",
    "MATERIAL_TOP_DIRS",
    "build_scope",
    "describe",
    "evaluate",
    "grant",
    "grants",
    "trace",
]
