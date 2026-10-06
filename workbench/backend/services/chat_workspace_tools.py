"""Native tool contracts for the novel workspace; all writes use one journal."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from . import file_change_service as changes
from . import chat_preference_service, scope_guard, settings_service
from .errors import InvalidOperationError, NodeExistsError, NodeNotFoundError, ServiceError
from .project_service import get_project_dir

#: Agent ``tools`` 中文白名单 → 原生工具名映射（写工具不按写域隐藏，模型仍能发起越界申请）
TOOL_WHITELIST_MAP: dict[str, tuple[str, ...]] = {
    "检索": ("list_files", "read_file", "search_files", "search_story_memory", "list_teardown"),
    "文件读写": ("create_file", "new_chapter", "replace_text", "write_file",
                 "move_file", "delete_file"),
    "审稿门禁": ("run_gates", "check_tracking"),
    "向量检索": ("search_files", "search_story_memory"),
    "收件箱": (),   # 原生路径下写工具直接落盘，没有对应的收件箱工具
    "无": (),
}

#: 任何一轮都保留的工具：向作者提问，以及按需读取技能 references/（渐进披露 L3）
ALWAYS_AVAILABLE_TOOLS = ("ask_user_question", "read_skill_reference")


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


# ─────────────────────────── 写域判定 ───────────────────────────


def _scope_key(ctx: Any) -> str:
    """越界授权与留痕的键：优先 run_id，无 run_id 时退化为会话。"""
    run_id = _get(ctx, "run_id")
    if run_id:
        return str(run_id)
    session_id = _get(ctx, "session_id")
    return f"session:{session_id}" if session_id else ""


_SCOPE_ABSENT = object()


def _scope_check(ctx: Any, rel: str) -> tuple[Any, dict | None]:
    """写域判定：本轮未接入写域（旧调用/未声明）时返回 ``(None, None)``，沿用既有语义。"""
    raw = _get(ctx, "scope", _SCOPE_ABSENT)
    if raw is _SCOPE_ABSENT:
        return None, None
    key = _scope_key(ctx)
    scope = {**raw, "key": key} if isinstance(raw, dict) and key else raw
    return scope, scope_guard.evaluate(scope, rel)


def authorize_revert(ctx: Any, items: list[dict]) -> dict | None:
    """Undo is a file mutation: the same scope and approval policy applies."""
    if _get(ctx, "read_only") or _get(ctx, "role_read_only"):
        return {"ok": False, "code": "read_only", "error": "本轮只读，不能撤回文件修改"}
    paths = list(dict.fromkeys(p for item in items for p in (item["path"], item.get("destination")) if p))
    forbidden = set(_get(ctx, "forbidden_targets", []))
    if any(path.split("/", 1)[0] in forbidden or path in forbidden for path in paths):
        return {"ok": False, "code": "author_prohibition", "error": "撤回会改动作者要求保持不变的材料"}
    checks = [(path, *_scope_check(ctx, path)) for path in paths]
    outside = [(path, scope, decision) for path, scope, decision in checks if decision and not decision["allowed"]]
    prefs = chat_preference_service.get_scope_prefs()
    if outside and prefs["scope_strictness"] == "reject":
        path, scope, decision = outside[0]
        return _scope_rejected(scope, decision, path)
    mode = _get(ctx, "permission_mode", "auto")
    if mode == "full" and (not outside or not prefs["scope_limit_full"]):
        if outside:
            scope_guard.trace(_scope_key(ctx), action="chat-scope-full-allow", message="完全访问已放行撤回范围外材料：" + "、".join(paths),
                              project_id=_get(ctx, "project_id"), run_id=_get(ctx, "run_id"))
        return None
    # The HTTP undo action is a direct author instruction for these exact
    # changes, equivalent to approving this operation once. It does not grant
    # later model calls any material access; deny/strict scope still run first.
    if _get(ctx, "author_confirmed", False):
        if outside:
            scope_guard.trace(_scope_key(ctx), action="chat-scope-author-undo",
                              message="作者已明确撤回所选范围外改动：" + "、".join(paths),
                              project_id=_get(ctx, "project_id"), run_id=_get(ctx, "run_id"))
        return None
    from . import chat_interaction_service as interactions
    if not outside and interactions.has_task_approval(str(_get(ctx, "run_id"))):
        return None
    payload = {"operation": "revert", "path": paths[0] if paths else "", "changes": items,
               "reason": "撤回会恢复此前文件版本，请确认实际涉及的材料"}
    if outside:
        payload["scope"] = _scope_card(outside[0][1], outside[0][2])
    answer = interactions.ask(str(_get(ctx, "run_id")), str(_get(ctx, "tool_call_id")), "approval", payload,
        should_cancel=_get(ctx, "should_cancel"), wait_control=_get(ctx, "wait_control"))
    if answer["decision"] != "approve":
        return {"ok": False, "code": "approval_rejected", "error": "作者已拒绝撤回，文件保持原样"}
    if answer.get("scope") == "material":
        for _, _, decision in outside:
            scope_guard.grant(_scope_key(ctx), decision["material"])
    return None


def _scope_label(scope: Any) -> str:
    label = scope.get("label") if isinstance(scope, dict) else ""
    return str(label or "") or "未声明的材料"


def _scope_meta(scope: Any, decision: dict) -> dict:
    return {"out_of_scope": True, "material": str(decision.get("material") or ""),
            "reason": str(decision.get("reason") or ""), "label": scope_guard.describe(scope)}


def _scope_card(scope: Any, decision: dict) -> dict:
    """越界批准卡的范围信息（沿用批准卡既有结构，不新造授权机制）。"""
    return {**_scope_meta(scope, decision),
            "options": [{"id": "once", "label": "仅此次批准"},
                        {"id": "material", "label": "本任务内允许改这类材料"}]}


def _scope_rejected(scope: Any, decision: dict, rel: str) -> dict:
    material = str(decision.get("material") or "")
    reason = str(decision.get("reason") or "")
    return {"ok": False, "applied": False, "code": "out_of_scope",
            "summary": f"越界：本轮范围是 {_scope_label(scope)}，不含 {material}/（严格模式已拒绝）",
            "text": f"越界：{reason}。本轮不写 {rel}，文件保持原样。",
            "scope": _scope_meta(scope, decision)}


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
            if name.startswith(".") or not name.lower().endswith((".md", ".txt")):
                continue
            rel = (Path(current) / name).relative_to(root).as_posix()
            try:
                changes.managed_path(root, rel)
            except Exception:
                continue
            result.append(rel)
    return result


def _read_skill_reference(args: dict) -> dict:
    """读取技能 ``references/`` 下的明细清单（与本书目录无关，越界/不存在即报错）。"""
    from . import skill_service

    skill = str(args.get("skill") or "").strip()
    rel = str(args.get("path") or "").strip()
    if not skill or not rel:
        raise InvalidOperationError("需要同时提供 skill 与 path")
    reference = skill_service.read_reference(skill, rel)
    summary = (f"已读取技能 {reference['skill']} 的 {reference['path']} · "
               f"约 {reference['estimated_tokens']:,} token（估算值）")
    # 原生工具会序列化整个返回值；正文仅放在 data，避免 text 再复制一份。
    return {"ok": True, "summary": summary, "text": summary, "data": reference}


_CHECK_TRACKING_TARGETS: dict[str, str] = {"账本": "状态/资源账本.md", "大纲": "大纲/章纲.md"}


def _check_tracking(args: dict, ctx: Any) -> dict:
    """确定性校验（账本算术 / 章纲 / 设定字段）：只报告，不改动任何文件。"""
    from ..gates import outline_gate, setting_gate, tracking_gate

    target = str(args.get("target") or "").strip()
    rel = str(args.get("rel_path") or "").strip()
    category = str(args.get("category") or "").strip()
    if target not in {"账本", "大纲", "设定"}:
        raise InvalidOperationError("target 只支持：账本 / 大纲 / 设定")
    if target == "设定":
        options = "、".join(setting_gate.REQUIRED_FIELDS)
        if not rel and not category:
            raise InvalidOperationError(f"target=设定 需要给出 category（{options}）或 rel_path")
        if not rel:
            rel = setting_gate.CATEGORY_FILES.get(category, "")
            if not rel:
                # 未知分类：由门禁给出可选分类，不抛异常给对话层
                return _ok(f"设定分类不可用：{category}", {**setting_gate.run("", category=category),
                                                          "path": None})
        elif not category:
            category = setting_gate.category_from_path(rel)
    else:
        rel = rel or _CHECK_TRACKING_TARGETS[target]

    project_id = int(_get(ctx, "project_id"))
    state = changes.file_state(project_id, rel)
    if not state["exists"]:
        raise NodeNotFoundError("文件不存在：" + rel)
    _hashes(ctx)[state["path"]] = state["hash"]
    content = state["content"] or ""
    if target == "账本":
        result = tracking_gate.run(content)
    elif target == "大纲":
        result = outline_gate.run(content)
    else:
        result = setting_gate.run(content, category=category)
    summary = f"{state['path']} 校验{'通过' if result['passed'] else '未通过'}：{result['detail']}"
    return _ok(summary, {**result, "path": state["path"]})


def _list_teardown(args: dict, ctx: Any) -> dict:
    """召回拆书对标资产（只读）：有 ``target`` 列该目标的事实卡，否则按题材召回。

    行为对齐旧路径 ``chat_tools.tool_list_teardown``：无资产 / 索引缺失不阻断对话，
    退化为降级提示（``ok:false`` 或空结果说明）。
    """
    from . import teardown_service

    try:
        limit = max(1, min(int(args.get("limit") or 3), 10))
        target = str(args.get("target") or "").strip()
        genre = str(args.get("genre") or "").strip()
        project_id = int(_get(ctx, "project_id"))
        if target:
            facts = teardown_service.list_facts(project_id, target)[: limit * 4]
            if not facts:
                return _ok(f"{target} 暂无事实卡", {"target": target, "facts": []})
            return _ok(f"{target} 事实卡 {len(facts)} 条", {
                "target": target,
                "facts": [
                    {"dimension": item.get("dimension", ""),
                     "conclusion": item.get("conclusion", ""),
                     "chapter_ref": item.get("evidence", ""),
                     "quote": item.get("quote", "")}
                    for item in facts
                ],
            })
        block = teardown_service.recall_block(project_id, genre=genre, limit=limit)
    except Exception as exc:  # noqa: BLE001 - 无资产 / 索引缺失不阻断对话
        return {"ok": False, "summary": "拆书资产暂不可用", "text": f"拆书资产暂不可用（{exc}）"}
    if not block:
        return _ok("暂无同题材拆书资产", {"genre": genre, "block": ""})
    return _ok(f"已召回拆书资产（题材：{genre or '未标注'}）", {"genre": genre, "block": block})


def _read(name: str, args: dict, ctx: Any) -> dict:
    if name == "read_skill_reference":
        return _read_skill_reference(args)
    if name == "check_tracking":
        return _check_tracking(args, ctx)
    if name == "list_teardown":
        return _list_teardown(args, ctx)
    project_id = int(_get(ctx, "project_id"))
    _, root = get_project_dir(project_id)
    if name == "search_story_memory":
        from . import knowledge_service
        query = str(args.get("query") or "").strip()
        if not query:
            raise InvalidOperationError("检索问题不能为空")
        profile = str(_get(ctx, "retrieval_profile", "history") or "history")
        # 参考语料只能由拆书场景或作者显式选择启用。
        requested = str(args.get("profile") or profile)
        if requested == "teardown" and profile != "teardown":
            raise InvalidOperationError("本轮未启用拆书参考检索，请先明确选择拆书场景")
        document = args.get("document")
        if document and document not in (_get(ctx, "authorized_documents", []) or []):
            raise InvalidOperationError("草稿检索只允许作者本轮指定的目标章节或附件")
        result = knowledge_service.search(project_id, query, mode="hybrid", profile=requested,
            chapter_rel=_get(ctx, "chapter_rel"), document=document,
            limit=min(max(int(args.get("limit") or 6), 1), 6))
        return _ok(f"召回 {len(result.get('hits', []))} 条本书依据；请核对来源，检索结果不代表验收通过", result)
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
        from .writing_preference_service import chapter_word_range
        word_min, word_max = chapter_word_range(project_id)
        return _ok("章节硬门禁检查", run_hard_gates(state["content"], project_dir=root,
                                                rel_path=state["path"], min_words=word_min,
                                                max_words=word_max))
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


#: 去 AI 味类门禁：只有这些门失败才触发落盘前自动去味重写（字数门、语言门不触发）。
DESLOP_GATE_KEYS = frozenset({"禁词门", "去AI味门", "格式门"})
#: 自动去味重写的轮数硬上限（配置超限即夹紧）。
MAX_DESLOP_ROUNDS = 3
_DESLOP_FALLBACK_SYSTEM = (
    "你是中文网文去 AI 味编辑。改写正文，去掉禁用词、叙述层推测词、三连排比与升华腔，"
    "降低破折号密度，保证对话引号密度；保持情节、人物与信息不变，只改叙述方式。")
_DESLOP_OUTPUT_RULE = (
    "只输出重写后的章节正文纯文本：不要标题、不要解释、不要任何 Markdown 标记、"
    "不要前后说明，直接给正文。")
_DESLOP_PRESERVE_RULE = (
    "修订时保留原稿已经明确的事实、事件顺序、时间与数值、人物动机、视角与叙述距离，"
    "保留人物知道和不知道的信息以及对白的试探、隐瞒和潜台词；不要补写新事件，"
    "不要替人物解释未说出口的真实动机，不要把作者认可样稿里的剧情或措辞搬进原稿。")
_DESLOP_FRAGMENT_RULE = (
    "本次只允许修订【待修订片段】。前后文和文风样稿都是只读参考，不属于输出范围。"
    "只输出修订后的片段纯文本，不要输出前后文，不要补写整章，不要标题、解释或 Markdown。"
    "不要为满足整章字数或对白比例而在片段中增加事件；片段外的问题不能扩大范围修复。")


def _warn(ctx: Any, message: str) -> None:
    """把自动去味的告警写进本轮运行事件（落盘失败不阻断写操作）。"""
    run_id = _get(ctx, "run_id")
    if not run_id:
        return
    cancel = _get(ctx, "should_cancel")
    if cancel and cancel():
        return
    try:
        from . import chat_run_service
        chat_run_service.emit(str(run_id), "context_warning", message=message)
    except Exception:  # noqa: BLE001 - 只是记录，不影响落盘
        return


def _deslop_settings() -> tuple[bool, int]:
    writing = settings_service.read_settings().get("writing")
    config = writing.get("auto_deslop") if isinstance(writing, dict) else None
    if not isinstance(config, dict):
        return False, 0
    try:
        rounds = int(config.get("max_rounds") or 0)
    except (TypeError, ValueError):
        rounds = 0
    return bool(config.get("enabled", False)), max(0, min(rounds, MAX_DESLOP_ROUNDS))


def _deslop_failed(gates: dict | None) -> bool:
    if not isinstance(gates, dict):
        return False
    return any(item.get("key") in DESLOP_GATE_KEYS and not item.get("passed")
               for item in gates.get("gates") or [])


def _rewrite_chapter_body(project_id: int, text: str, ctx: Any, *,
                          scope: str = "chapter", chapter_rel: str | None = None,
                          reference_context: dict | None = None,
                          gate_feedback: dict | None = None) -> str | None:
    """修订获准的正文范围；语义保留由提示约束，片段写入边界由调用方机械保证。"""
    from . import generation_service, skill_service, style_service
    from .prompt_registry_service import PROSE_CRAFT_GUIDANCE
    from .fs_utils import split_frontmatter

    cancel = _get(ctx, "should_cancel")
    if (cancel and cancel()) or not text.strip():
        return None
    control = _get(ctx, "wait_control")
    remaining = getattr(control, "remaining_seconds", None)

    skill_text = skill_service.skill_digest(["human-linguistics"]).strip()
    fragment = scope == "fragment"
    output_rule = _DESLOP_FRAGMENT_RULE if fragment else _DESLOP_OUTPUT_RULE
    system = "\n\n".join((skill_text or _DESLOP_FALLBACK_SYSTEM,
                              _DESLOP_PRESERVE_RULE, PROSE_CRAFT_GUIDANCE, output_rule))
    rel = chapter_rel or _get(ctx, "chapter_rel")
    try:
        style = style_service.generation_reference(
            project_id, chapter_rel=rel, query=text, max_tokens=1200)
    except Exception:  # noqa: BLE001 - 样稿不可用不扩大修订范围或阻断确定性门禁
        style = {"text": "", "samples": [], "reference_hash": "", "degradation": "unavailable"}
    parts = []
    if style.get("text"):
        parts.append("【作者认可文风参照：只学写法，不继承剧情事实】\n" + str(style["text"]))
    if fragment:
        context = reference_context or {}
        parts.extend(("【只读前文：禁止输出或修改】\n" + str(context.get("before") or ""),
                      "【只读后文：禁止输出或修改】\n" + str(context.get("after") or "")))
    if gate_feedback:
        failures = [{"key": item.get("key"), "detail": str(item.get("detail") or "")[:600]}
                    for item in gate_feedback.get("gates") or []
                    if item.get("key") in DESLOP_GATE_KEYS and not item.get("passed")]
        if failures:
            parts.append("【候选整章门禁反馈：仅在获准范围内修订】\n" +
                         json.dumps(failures, ensure_ascii=False))
    parts.append(("【待修订片段】\n" if fragment else "【待重写正文】\n") + text)
    if cancel and cancel():
        return None
    try:
        result = generation_service.run_task(
            project_id=project_id,
            task_type="去AI味重写",
            system=system,
            messages=[{"role": "user", "content": "\n\n".join(parts)}],
            provider=str(_get(ctx, "provider") or ""),
            model=str(_get(ctx, "model") or ""),
            temperature=0.6,
            should_cancel=cancel,
            timeout_seconds=max(1, int(remaining())) if callable(remaining) else None,
            context_snapshot={"chapter": rel, "rewrite_scope": scope,
                              "input_hash": changes.content_hash(text),
                              "style_reference": {key: style.get(key) for key in
                                                  ("reference_hash", "samples", "degradation")}},
        )
    except Exception:  # noqa: BLE001 - 模型失败仍由原候选稿的硬门禁拒绝落盘
        return None
    if cancel and cancel():
        return None
    if not result.get("ok"):
        return None
    rewritten = split_frontmatter(str(result.get("text") or ""))[1].strip()
    if fragment and rewritten:
        # 片段边缘空白决定与外围原文的拼接，保持调用方提交的分段方式。
        leading = text[:len(text) - len(text.lstrip())]
        trailing = text[len(text.rstrip()):]
        return leading + rewritten + trailing
    return rewritten or None


def _deslop_candidate_kwargs(kwargs: dict, rewritten: str) -> dict:
    """保留原操作及版本锚点；精确替换只调整 new_text，不升级成整章覆盖。"""
    if kwargs.get("operation") in {"create", "rewrite"}:
        return {**kwargs, "content": rewritten}
    return {**kwargs, "new_text": rewritten}


def _maybe_deslop_chapter(project_id: int, kwargs: dict, ctx: Any) -> dict:
    """章节正文落盘前的自动去味：去 AI 味类门禁失败时按 human-linguistics 重写并重跑门禁。

    只作用于新产出的章节正文（create / rewrite / replace_exact）；重写文本会成为被预览、
    被审批、被落盘的那一份。重写失败或仍不过门禁时原样返回，由门禁按既有语义拒绝落盘。
    """
    rel = str(kwargs.get("rel_path") or "")
    operation = str(kwargs.get("operation") or "")
    if not rel.startswith("章节/") or operation not in {"create", "rewrite", "replace_exact"}:
        return kwargs
    enabled, max_rounds = _deslop_settings()
    if not enabled or max_rounds <= 0:
        return kwargs
    preview_args = {key: kwargs.get(key) for key in (
        "operation", "rel_path", "content", "expected_hash", "old_text", "new_text",
        "destination", "title", "status")}
    try:
        preview = changes.preview_change(project_id, **preview_args)
    except ServiceError:
        return kwargs  # 冲突 / 不存在等交给原流程报错
    if not _deslop_failed(preview.get("gates")):
        return kwargs
    fragment = operation == "replace_exact"
    current = str(kwargs.get("new_text") or "") if fragment else str(preview.get("after_content") or "")
    reference_context = None
    if fragment:
        before = str(preview.get("before_content") or "")
        old_text = str(kwargs.get("old_text") or "")
        offset = before.index(old_text)  # 唯一匹配已经由 preview_change 校验。
        reference_context = {"before": before[max(0, offset - 1200):offset],
                             "after": before[offset + len(old_text):offset + len(old_text) + 1200]}
    for round_index in range(1, max_rounds + 1):
        cancel = _get(ctx, "should_cancel")
        if cancel and cancel():
            return kwargs
        rewritten = _rewrite_chapter_body(
            project_id, current, ctx, scope="fragment" if fragment else "chapter",
            chapter_rel=rel, reference_context=reference_context, gate_feedback=preview.get("gates"))
        if not rewritten:
            _warn(ctx, f"章节「{rel}」自动去味重写失败（第 {round_index} 轮），"
                       "已跳过并按门禁处理")
            return kwargs
        candidate = _deslop_candidate_kwargs(kwargs, rewritten)
        try:
            preview = changes.preview_change(project_id, **{
                **preview_args, "operation": candidate["operation"],
                "content": candidate.get("content"), "old_text": candidate.get("old_text"),
                "new_text": candidate.get("new_text")})
        except ServiceError:
            return kwargs
        if not _deslop_failed(preview.get("gates")):
            action = "修订替换片段" if fragment else "重写正文"
            _warn(ctx, f"章节「{rel}」未过去 AI 味门禁，已自动{action}（第 {round_index} 轮）并复验")
            return candidate
        current = rewritten if fragment else str(preview.get("after_content") or rewritten)
    _warn(ctx, f"章节「{rel}」自动去味重写 {max_rounds} 轮仍未通过门禁，按门禁拒绝落盘")
    return kwargs


def _write(name: str, args: dict, ctx: Any) -> dict:
    if _get(ctx, "read_only", False) or _get(ctx, "role_read_only", False):
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
    forbidden = set(_get(ctx, "forbidden_targets", []) or [])
    targets = [rel, str(args.get("destination") or "").replace("\\", "/")]
    if any(target.split("/", 1)[0] in forbidden or target in forbidden for target in targets if target):
        raise InvalidOperationError("作者明确要求保留该材料，不能通过批准或权限档位绕过")
    # 判定顺序：只读/仅讨论（上方已拒绝）→ 写域 → 批准强度。
    scope, scope_decision = _scope_check(ctx, rel)
    out_of_scope = bool(scope_decision and not scope_decision["allowed"])
    scope_prefs = chat_preference_service.get_scope_prefs()
    if out_of_scope and scope_prefs["scope_strictness"] == "reject":
        scope_guard.trace(_scope_key(ctx), action="chat-scope-reject",
                          message=f"越界写入已拒绝：{rel}（{scope_guard.describe(scope)}）",
                          rel_path=rel, project_id=project_id, run_id=run_id)
        return _scope_rejected(scope, scope_decision, rel)
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
                "file_change": proposal_arguments,
                **({"scope": _scope_meta(scope, scope_decision)} if out_of_scope else {})}
        proposal = proposal_service.create_proposal(project_id=project_id,
            kind="chapter_draft" if rel.startswith("章节/") else "other",
            title=f"{operation}：{rel}"[:80], target_path=rel,
            content=str(args.get("content") or args.get("new_text") or ""), meta=meta)
        proposal_ids = _get(ctx, "proposals")
        if proposal_ids is not None:
            proposal_ids.append(proposal["id"])
        return _ok("文件修改已进入收件箱，尚未落盘", {"proposal_id": proposal["id"], "path": rel},
                   proposal_ids=[proposal["id"]], applied=False)
    source_request = {"tool": name, "arguments": args,
                      "resolved": {key: value for key, value in kwargs.items()
                                   if key not in {"read_only", "should_cancel"}}}
    if prior is not None and request.get("source_request") is not None:
        if (json.dumps(source_request, ensure_ascii=False, sort_keys=True) !=
                json.dumps(request["source_request"], ensure_ascii=False, sort_keys=True)):
            raise InvalidOperationError("重复工具调用编号的来源参数不一致")
        # 仅在完整来源相同后复用有效参数；apply_change仍核验其摘要、绑定和旧状态。
        kwargs = {**kwargs, **{key: request.get("path" if key == "rel_path" else key)
                              for key in ("operation", "rel_path", "content", "expected_hash",
                                          "old_text", "new_text", "destination", "title", "status")},
                  "source_request": source_request}
    elif prior is None:
        # 落盘前自动去味：重写后的文本成为被预览、被审批、被落盘的那一份。
        revised = _maybe_deslop_chapter(project_id, kwargs, ctx)
        if revised != kwargs:
            revised = {**revised, "source_request": source_request}
        kwargs = revised
    permission_mode = _get(ctx, "permission_mode")
    if permission_mode is not None and permission_mode not in {"ask", "auto", "full"}:
        raise InvalidOperationError("未知的对话权限模式")
    # 写域越界：ask/auto 档一律降级为请求批准；full 档默认放行但强制留痕。
    scope_requires_approval = out_of_scope
    scope_allow_note = None
    if out_of_scope and permission_mode == "full" and not scope_prefs["scope_limit_full"]:
        scope_requires_approval = False
        scope_allow_note = {**_scope_meta(scope, scope_decision), "resolved": "full_allow"}
        scope_guard.trace(_scope_key(ctx), action="chat-scope-full-allow",
                          message=f"越界写入已按完全访问放行：{rel}（{scope_guard.describe(scope)}）",
                          rel_path=rel, project_id=project_id, run_id=run_id)
    approved_preview = None
    approval = None
    if (permission_mode in {"ask", "auto"} or scope_requires_approval) and prior is None:
        approved_preview = changes.preview_change(project_id, **{
            key: value for key, value in kwargs.items()
            if key not in {"run_id", "session_id", "tool_call_id", "read_only", "should_cancel", "source_request"}})
        # A rejected candidate retains the usual journal and cannot become approvable.
        if approved_preview.get("gates") and not approved_preview["gates"]["passed"]:
            changes.apply_change(project_id, **kwargs)
        from . import chat_interaction_service as interactions
        # 作者已在本任务内选择「本任务内不再询问」时，本 run 内后续写操作直接落盘（任务结束即失效）。
        task_approved = interactions.has_task_approval(str(run_id))
        # 默认条件：ask 模式逐次审批；auto 模式只对删除、移动这类破坏性操作请求批准，改写与整章替换直接落盘。
        # 越界写不受「本任务内不再询问」豁免，必须单独批准。
        needs_approval = scope_requires_approval or (not task_approved and (
            operation in {"move", "delete"} or permission_mode == "ask"))
        if needs_approval:
            payload = {
                **approved_preview, "tool": name, "label": _BY_NAME[name]["label"],
                "reason": "本轮设置为逐次请求批准" if permission_mode == "ask" else "此操作会删除或移动已有文稿",
            }
            if out_of_scope:
                payload["reason"] = (f"越界：本轮范围是 {_scope_label(scope)}，"
                                     f"此操作会改 {str(scope_decision.get('material') or '')}/")
                payload["scope"] = _scope_card(scope, scope_decision)
            decision = interactions.ask(str(run_id), call_id, "approval", payload,
                                        should_cancel=cancel, wait_control=_get(ctx, "wait_control"))
            approval = next(item for item in interactions.list_interactions(str(run_id))
                            if item["tool_call_id"] == call_id and item["kind"] == "approval")
            if decision["decision"] != "approve":
                if out_of_scope:
                    scope_guard.trace(_scope_key(ctx), action="chat-scope-reject",
                                      message=f"作者已拒绝越界写入：{rel}（{scope_guard.describe(scope)}）",
                                      rel_path=rel, project_id=project_id, run_id=run_id)
                    return {"ok": False, "applied": False, "code": "approval_rejected",
                            "summary": (f"用户已拒绝此修改（越界：本轮范围是 {_scope_label(scope)}，"
                                        f"此操作会改 {str(scope_decision.get('material') or '')}/），"
                                        "文件保持原样"),
                            "text": "用户已拒绝此修改，文件保持原样",
                            "interaction_id": approval["id"],
                            "scope": _scope_meta(scope, scope_decision)}
                return {"ok": False, "summary": "用户已拒绝此修改，文件保持原样", "text": "用户已拒绝此修改，文件保持原样",
                        "code": "approval_rejected", "applied": False, "interaction_id": approval["id"]}
            if out_of_scope and decision.get("scope") == "material":
                # 作者选择「本任务内允许这类材料」：run 级授权，run 结束即失效。
                scope_guard.grant(_scope_key(ctx), str(scope_decision.get("material") or ""))
    try:
        result = changes.apply_change(project_id, **kwargs, approved_preview=approved_preview)
    except (changes.FileConflictError, NodeExistsError, NodeNotFoundError) as exc:
        if approval is not None:
            interactions.expire(str(run_id), approval["id"])
            raise interactions.InteractionEndedError("expired") from exc
        raise
    if result["status"] == "applied":
        _hashes(ctx)[rel] = None if operation in {"delete", "move"} else result["after_hash"]
        if result.get("destination"):
            _hashes(ctx)[result["destination"]] = result["after_hash"]
    ids = _get(ctx, "change_ids")
    if ids is not None and result["id"] not in ids:
        ids.append(result["id"])
    summary = (f"已{ {'create': '新建', 'rewrite': '保存', 'replace_exact': '替换', 'move': '移动', 'delete': '删除'}[operation]} {rel}，可撤回"
               if result["status"] == "applied" else f"此前修改状态为 {result['status']}，重放未重新执行 {rel}")
    return _ok(summary, result, applied=result["status"] == "applied", change_id=result["id"], changes=[result],
               **({"scope": scope_allow_note} if scope_allow_note else {}))


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
    _spec("search_story_memory", "检索本书记忆",
          "按事件含义、人物关系或伏笔检索本书历史，返回可回读的原文依据、行号和版本。"
          "只提供候选证据；当前状态与已确认设定优先，冲突须指出。草稿仅在 document 指定时检索。",
          {"query": {"type": "string"}, "limit": {"type": "integer"},
           "profile": {"type": "string", "enum": ["history", "planning", "review", "setting", "continuity", "foreshadow", "teardown"]},
           "document": {"type": "string", "description": "作者本轮明确指定的本书草稿相对路径（可选）"}}, ["query"]),
    _spec("create_file", "新建文档", "创建尚不存在的文档；章节需第NNNN章.txt命名，正文须通过整章门禁。",
          {"rel_path": _PATH, "content": {"type": "string"}, "title": {"type": "string"}}, ["rel_path", "content"], True),
    _spec("new_chapter", "新建下一章", "自动选择下一章节编号，新建完整正文并保存标题。正文须至少2000汉字并通过全部门禁。",
          {"content": {"type": "string"}, "title": {"type": "string"}}, ["content", "title"], True),
    _spec("replace_text", "精确修改文字", "仅替换唯一精确匹配的原文；重复匹配时请扩充上下文。章节校验修改后的整章。",
          {"rel_path": _PATH, "old_text": {"type": "string"}, "new_text": {"type": "string"}, "expected_hash": _HASH},
          ["rel_path", "old_text", "new_text"], True),
    _spec("write_file", "重写整份文档", "提交完整文档内容。先读取版本；章节元信息保留，整章未过门禁则不写入。",
          {"rel_path": _PATH, "content": {"type": "string"}, "expected_hash": _HASH}, ["rel_path", "content"], True),
    _spec("move_file", "移动或改名", "仅在用户明确要求移动/改名时使用；目标必须不存在，章节名称保持第NNNN章.txt。可撤回。",
          {"rel_path": _PATH, "destination": _PATH, "expected_hash": _HASH}, ["rel_path", "destination"], True),
    _spec("delete_file", "删除文档", "仅在用户明确要求删除时使用；移入回收站并记录本轮撤回数据。",
          {"rel_path": _PATH, "expected_hash": _HASH}, ["rel_path"], True),
    _spec("run_gates", "检查章节门禁", "只读检查整章正文汉字数、语言、禁词、去AI味规则，不自动修改。",
          {"rel_path": _PATH}, ["rel_path"]),
    _spec("check_tracking", "校验账本与大纲",
          "只读运行确定性校验：账本算术与条目ID唯一性、章纲章号唯一/连续与卷末钩子对应、"
          "设定卡必填字段与来源分级。只报告不改写，不改动任何文件。",
          {"target": {"type": "string", "enum": ["账本", "大纲", "设定"],
                      "description": "校验目标：账本=状态/资源账本.md；大纲=大纲/章纲.md；设定=按分类的设定卡"},
           "rel_path": {"type": "string", "description": "本书相对路径；不填则用该目标的默认文件"},
           "category": {"type": "string", "description": "target=设定 时的分类：人物/世界/势力/物品/技能/场景/伏笔/状态"}},
          ["target"]),
    _spec("list_teardown", "召回拆书资产",
          "只读召回拆书对标资产：给定 target 时列出该对标目标的事实卡（含章节依据）；"
          "否则按 genre 题材召回已入库事实卡。无资产或索引缺失时降级提示，不阻断对话。",
          {"target": {"type": "string", "description": "对标作品名；不填则按题材召回"},
           "genre": {"type": "string", "description": "题材，用于召回同题材对标事实卡"},
           "limit": {"type": "integer", "description": "召回条数（默认 3，上限 10）"}},
          []),
    _spec("read_skill_reference", "读技能引用",
          "读取某个技能 references/ 下的详细清单（如设定字段规范、审稿细则）；正文里给的相对路径可直接传入。",
          {"skill": {"type": "string", "description": "技能名（kebab-case），如 novel-setting"},
           "path": {"type": "string",
                    "description": "该技能 references/ 下的相对路径，如 references/设定字段规范.md"}},
          ["skill", "path"]),
)
_BY_NAME = {tool["name"]: tool for tool in TOOLS}


def list_specs(*, read_only: bool = False, agent_tools=None) -> list[dict]:
    """本轮工具清单：只按 Agent 的 ``tools`` 白名单与只读状态过滤。

    写工具不因「不在本轮写域内」而消失，否则模型无法发起越界申请；
    无权工具一律不出现在清单中。``agent_tools=None`` 表示不做白名单过滤（旧调用）。
    """
    allowed = None
    if agent_tools is not None:
        allowed = set(ALWAYS_AVAILABLE_TOOLS)
        for tag in agent_tools:
            allowed.update(TOOL_WHITELIST_MAP.get(str(tag or "").strip(), ()))
    return [dict(tool) for tool in TOOLS
            if (not read_only or not tool["write"])
            and (allowed is None or tool["name"] in allowed)]


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
