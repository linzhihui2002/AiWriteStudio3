"""Native tool contracts for the novel workspace; all writes use one journal."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from . import file_change_service as changes
from .errors import InvalidOperationError, NodeExistsError, NodeNotFoundError
from .project_service import get_project_dir
from .fs_utils import split_frontmatter


def _get(ctx: Any, name: str, default=None):
    return ctx.get(name, default) if isinstance(ctx, dict) else getattr(ctx, name, default)


def _set(ctx: Any, name: str, value) -> None:
    if isinstance(ctx, dict):
        ctx[name] = value
    else:
        setattr(ctx, name, value)


def _hashes(ctx: Any) -> dict:
    hashes = _get(ctx, "read_hashes")
    if hashes is None:
        hashes = {}
        _set(ctx, "read_hashes", hashes)
    return hashes


def _ok(summary: str, data=None, **extra) -> dict:
    return {"ok": True, "summary": summary,
            "text": json.dumps(data, ensure_ascii=False) if data is not None else summary,
            "data": data, **extra}


def _files(root: Path) -> list[str]:
    result = []
    for current, directories, files in os.walk(root, followlinks=False):
        directories[:] = sorted(d for d in directories if not d.startswith(".")
                                and not (Path(current) / d).is_symlink()
                                and not (hasattr(Path(current) / d, "is_junction")
                                         and (Path(current) / d).is_junction()))
        for name in sorted(files):
            if name.startswith(".") or not name.lower().endswith(".md"):
                continue
            rel = (Path(current) / name).relative_to(root).as_posix()
            try:
                changes.managed_path(root, rel)
            except Exception:
                continue
            result.append(rel)
    return result


def _read(name: str, args: dict, ctx: Any) -> dict:
    project_id = int(_get(ctx, "project_id"))
    _, root = get_project_dir(project_id)
    if name == "list_files":
        prefix = str(args.get("directory") or "").replace("\\", "/").strip("/")
        if prefix and any(p.startswith(".") or ":" in p for p in prefix.split("/")):
            raise InvalidOperationError("只能列出本书可见目录")
        paths = [p for p in _files(root) if not prefix or p.startswith(prefix + "/")]
        limit = min(max(int(args.get("limit") or 300), 1), 1000)
        offset = max(int(args.get("offset") or 0), 0)
        return _ok(f"本书共 {len(paths)} 份匹配文档", {"files": paths[offset:offset + limit],
                   "total": len(paths), "offset": offset, "truncated": offset + limit < len(paths)})
    if name == "search_files":
        query = str(args.get("query") or "")
        if not query:
            raise InvalidOperationError("搜索内容不能为空")
        hits = []
        limit = min(max(int(args.get("limit") or 40), 1), 100)
        for rel in _files(root):
            state = changes.file_state(project_id, rel)
            for line_no, line in enumerate((state["content"] or "").splitlines(), 1):
                if query.casefold() in line.casefold():
                    hits.append({"path": rel, "line": line_no, "text": line[:800]})
                    if len(hits) >= limit:
                        return _ok(f"找到至少 {len(hits)} 处，结果已截断", {"matches": hits, "truncated": True})
        return _ok(f"找到 {len(hits)} 处", {"matches": hits, "truncated": False})
    rel = str(args.get("rel_path") or _get(ctx, "chapter_rel") or "")
    state = changes.file_state(project_id, rel)
    if not state["exists"]:
        raise NodeNotFoundError("文件不存在：" + rel)
    _hashes(ctx)[state["path"]] = state["hash"]
    if name == "run_gates":
        from .review_service import run_hard_gates
        return _ok("章节硬门禁检查", run_hard_gates(state["content"], project_dir=root,
                                                rel_path=state["path"]))
    lines = (state["content"] or "").splitlines(keepends=True)
    start = max(1, int(args.get("start_line") or 1))
    end = min(len(lines), int(args.get("end_line") or len(lines)))
    max_chars = min(max(int(args.get("max_chars") or 18000), 1), 60000)
    selected = "".join(lines[start - 1:end])
    clipped = selected[:max_chars]
    return _ok("已读取 " + state["path"], {**state, "content": clipped,
               "start_line": start, "total_lines": len(lines),
               "truncated": len(clipped) < len(selected) or start > 1 or end < len(lines),
               "instruction": "hash 对应完整文件。若内容截断，请分段读取；不可用截断内容整文覆盖。"})


def _write(name: str, args: dict, ctx: Any) -> dict:
    if _get(ctx, "read_only", False):
        raise InvalidOperationError("审稿模式只读，不能修改小说文件")
    cancel = _get(ctx, "should_cancel")
    if cancel and cancel():
        raise InvalidOperationError("本轮已停止")
    project_id = int(_get(ctx, "project_id"))
    _, root = get_project_dir(project_id)
    run_id = _get(ctx, "run_id")
    call_id = str(_get(ctx, "tool_call_id") or "")
    if run_id is None or not call_id:
        raise InvalidOperationError("文件操作缺少本轮运行或工具调用编号")
    prior = changes.get_call(run_id, call_id, project_id)
    request = prior.get("request", {}) if prior else {}
    from . import proposal_service
    auto_apply = _get(ctx, "permission_mode") is not None or _get(ctx, "auto_apply", True)
    staged = proposal_service.find_tool_proposal(project_id, run_id, call_id) if not auto_apply else None
    recorded = (staged.get("meta") or {}).get("file_change") or {} if staged else {}
    if recorded:
        request = {"path": recorded.get("rel_path"), "expected_hash": recorded.get("expected_hash")}
    rel = str(args.get("rel_path") or _get(ctx, "chapter_rel") or "")
    operation = {"create_file": "create", "new_chapter": "create", "write_file": "rewrite",
                 "replace_text": "replace_exact", "move_file": "move", "delete_file": "delete"}[name]
    if name == "new_chapter":
        from .chapter_service import chapter_filename, next_chapter_number
        number = next_chapter_number(root)
        if not auto_apply and not request.get("path"):
            pending = {p["target_path"] for p in proposal_service.list_proposals(project_id, limit=10000)}
            while "章节/" + chapter_filename(number) in pending:
                number += 1
        rel = request.get("path") or "章节/" + chapter_filename(number)
    rel, _ = changes.managed_path(root, rel, write=True)
    expected = args.get("expected_hash", request.get("expected_hash", _hashes(ctx).get(rel)))
    if operation != "create" and expected is None:
        raise changes.FileConflictError("先调用 read_file 读取目标，再修改该文件", path=rel)
    kwargs = {"run_id": run_id, "session_id": _get(ctx, "session_id"), "tool_call_id": call_id,
              "operation": operation, "rel_path": rel, "expected_hash": expected,
              "content": args.get("content"), "old_text": args.get("old_text"),
              "new_text": args.get("new_text"), "destination": args.get("destination"),
              "title": args.get("title"), "read_only": False, "should_cancel": cancel}
    if not auto_apply:
        # Keep legacy review-before-apply sessions functional, including structural operations.
        proposal_arguments = {k: v for k, v in kwargs.items() if k not in {"should_cancel", "read_only"}}
        if staged:
            if recorded != proposal_arguments:
                raise InvalidOperationError("重复工具调用编号的参数不一致")
            return _ok("此工具调用已生成收件箱提案", {"proposal_id": staged["id"], "path": rel},
                       proposal_ids=[staged["id"]], applied=staged["status"] == "applied")
        state = changes.file_state(project_id, rel)
        changes.assert_version(rel, state["content"], expected)
        meta = {"from": "workspace-tool", "run_id": str(run_id), "session_id": _get(ctx, "session_id"),
                "file_change": proposal_arguments}
        proposal = proposal_service.create_proposal(project_id=project_id,
            kind="chapter_draft" if rel.startswith("章节/") else "other",
            title=f"{operation}：{rel}"[:80], target_path=rel,
            content=str(args.get("content") or args.get("new_text") or ""), meta=meta)
        proposal_ids = _get(ctx, "proposals")
        if proposal_ids is not None:
            proposal_ids.append(proposal["id"])
        return _ok("文件修改已进入收件箱，尚未落盘", {"proposal_id": proposal["id"], "path": rel},
                   proposal_ids=[proposal["id"]], applied=False)
    permission_mode = _get(ctx, "permission_mode")
    if permission_mode is not None and permission_mode not in {"ask", "auto", "full"}:
        raise InvalidOperationError("未知的对话权限模式")
    approved_preview = None
    approval = None
    if permission_mode in {"ask", "auto"} and prior is None:
        approved_preview = changes.preview_change(project_id, **{
            key: value for key, value in kwargs.items()
            if key not in {"run_id", "session_id", "tool_call_id", "read_only", "should_cancel"}})
        # A rejected candidate retains the usual journal and cannot become approvable.
        if approved_preview.get("gates") and not approved_preview["gates"]["passed"]:
            changes.apply_change(project_id, **kwargs)
        before = approved_preview["before_content"] or ""
        body = split_frontmatter(before)[1].strip()
        whole_replacement = operation == "replace_exact" and bool(body) and (
            str(args.get("old_text") or "").strip() in {before.strip(), body})
        needs_approval = permission_mode == "ask" or operation in {"move", "delete"} or (
            operation == "rewrite" and bool(body)) or whole_replacement
        if needs_approval:
            from . import chat_interaction_service as interactions
            decision = interactions.ask(str(run_id), call_id, "approval", {
                **approved_preview, "tool": name, "label": _BY_NAME[name]["label"],
                "reason": "本轮设置为逐次请求批准" if permission_mode == "ask" else "此操作会删除、移动或整体替换已有文稿",
            }, should_cancel=cancel, wait_control=_get(ctx, "wait_control"))
            approval = next(item for item in interactions.list_interactions(str(run_id))
                            if item["tool_call_id"] == call_id and item["kind"] == "approval")
            if decision["decision"] != "approve":
                return {"ok": False, "summary": "用户已拒绝此修改，文件保持原样", "text": "用户已拒绝此修改，文件保持原样",
                        "code": "approval_rejected", "applied": False, "interaction_id": approval["id"]}
        else:
            approved_preview = None
    try:
        result = changes.apply_change(project_id, **kwargs, approved_preview=approved_preview)
    except (changes.FileConflictError, NodeExistsError, NodeNotFoundError) as exc:
        if approval is not None:
            interactions.expire(str(run_id), approval["id"])
            raise interactions.InteractionEndedError("expired") from exc
        raise
    _hashes(ctx)[rel] = None if operation in {"delete", "move"} else result["after_hash"]
    if result.get("destination"):
        _hashes(ctx)[result["destination"]] = result["after_hash"]
    ids = _get(ctx, "change_ids")
    if ids is not None and result["id"] not in ids:
        ids.append(result["id"])
    return _ok(f"已{ {'create': '新建', 'rewrite': '保存', 'replace_exact': '替换', 'move': '移动', 'delete': '删除'}[operation]} {rel}，可撤回",
               result, applied=True, change_id=result["id"], changes=[result])


def _ask_user(args: dict, ctx: Any) -> dict:
    from . import chat_interaction_service
    response = chat_interaction_service.ask(str(_get(ctx, "run_id") or ""),
        str(_get(ctx, "tool_call_id") or ""), "question", args,
        should_cancel=_get(ctx, "should_cancel"), wait_control=_get(ctx, "wait_control"))
    return _ok("用户已回答，可继续当前任务", response, **response)


def _spec(name: str, label: str, description: str, properties: dict,
          required: list[str], write: bool = False) -> dict:
    return {"name": name, "label": label, "description": description, "write": write,
            "parameters": {"type": "object", "properties": properties, "required": required,
                           "additionalProperties": False}}


_PATH = {"type": "string", "description": "本书相对路径，使用 / 分隔；仅支持可见 Markdown 文档"}
_HASH = {"type": "string", "description": "read_file 返回的 hash；不填则使用本轮最近一次读取的版本"}
TOOLS = (
    _spec("ask_user_question", "请作者选择", "需要作者确定创作方向、补充信息或选择方案时使用。每次1至3问，推荐项放第一位。界面自动提供其他及自填输入，不要另列其他选项。收到回答后继续任务。批准文件修改由权限系统处理。",
          {"questions": {"type": "array", "minItems": 1, "maxItems": 3, "items": {
              "type": "object", "properties": {
                  "id": {"type": "string"}, "question": {"type": "string"}, "header": {"type": "string"},
                  "multi_select": {"type": "boolean"}, "options": {"type": "array", "maxItems": 6, "items": {
                      "type": "object", "properties": {"label": {"type": "string"}, "description": {"type": "string"}},
                      "required": ["label"], "additionalProperties": False}}},
              "required": ["id", "question"], "additionalProperties": False}}}, ["questions"]),
    _spec("list_files", "浏览本书文件", "列出本书所有可见 Markdown 文件，包含章节、设定、大纲、状态、备忘录和自建目录。",
          {"directory": {"type": "string"}, "limit": {"type": "integer"}, "offset": {"type": "integer"}}, []),
    _spec("read_file", "读取文件", "读取文件内容和版本。修改已有文件前必须读取；截断时分段读取，不能把截断内容整文写回。",
          {"rel_path": _PATH, "start_line": {"type": "integer"}, "end_line": {"type": "integer"},
           "max_chars": {"type": "integer"}}, ["rel_path"]),
    _spec("search_files", "搜索本书", "按字面文字搜索所有可见 Markdown 文件，返回文件路径和行号。",
          {"query": {"type": "string"}, "limit": {"type": "integer"}}, ["query"]),
    _spec("create_file", "新建文档", "创建尚不存在的文档；章节需第NNNN章.md命名，正文须通过整章门禁。",
          {"rel_path": _PATH, "content": {"type": "string"}, "title": {"type": "string"}}, ["rel_path", "content"], True),
    _spec("new_chapter", "新建下一章", "自动选择下一章节编号，新建完整正文并保存标题。正文须至少2000汉字并通过全部门禁。",
          {"content": {"type": "string"}, "title": {"type": "string"}}, ["content", "title"], True),
    _spec("replace_text", "精确修改文字", "仅替换唯一精确匹配的原文；重复匹配时请扩充上下文。章节校验修改后的整章。",
          {"rel_path": _PATH, "old_text": {"type": "string"}, "new_text": {"type": "string"}, "expected_hash": _HASH},
          ["rel_path", "old_text", "new_text"], True),
    _spec("write_file", "重写整份文档", "提交完整文档内容。先读取版本；章节元信息保留，整章未过门禁则不写入。",
          {"rel_path": _PATH, "content": {"type": "string"}, "expected_hash": _HASH}, ["rel_path", "content"], True),
    _spec("move_file", "移动或改名", "仅在用户明确要求移动/改名时使用；目标必须不存在，章节名称保持第NNNN章.md。可撤回。",
          {"rel_path": _PATH, "destination": _PATH, "expected_hash": _HASH}, ["rel_path", "destination"], True),
    _spec("delete_file", "删除文档", "仅在用户明确要求删除时使用；移入回收站并记录本轮撤回数据。",
          {"rel_path": _PATH, "expected_hash": _HASH}, ["rel_path"], True),
    _spec("run_gates", "检查章节门禁", "只读检查整章正文汉字数、语言、禁词、去AI味规则，不自动修改。",
          {"rel_path": _PATH}, ["rel_path"]),
)
_BY_NAME = {tool["name"]: tool for tool in TOOLS}


def list_specs(*, read_only: bool = False) -> list[dict]:
    return [dict(tool) for tool in TOOLS if not read_only or not tool["write"]]


def execute(name: str, args: dict, ctx: Any) -> dict:
    tool = _BY_NAME.get(name)
    if tool is None:
        return {"ok": False, "summary": "未知工具：" + str(name), "code": "unknown_tool"}
    try:
        if _get(ctx, "project_id") is None:
            raise InvalidOperationError("请先打开一本小说")
        if not isinstance(args, dict):
            raise InvalidOperationError("工具参数必须是对象")
        result = _ask_user(args, ctx) if name == "ask_user_question" else (
            _write(name, args, ctx) if tool["write"] else _read(name, args, ctx))
    except Exception as exc:
        return {"ok": False, "summary": str(exc), "text": str(exc), "tool": name,
                "write": tool["write"], **getattr(exc, "details", {"code": "tool_error"})}
    return {**result, "tool": name, "label": tool["label"], "write": tool["write"]}
