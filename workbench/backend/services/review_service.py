"""审稿服务：硬门禁（确定性）+ 逐项合同验收（模型）+ 三层一致性 + 质量债。

核心纪律（spec）
- **待核实不算通过**：找不到正文证据的合同项一律记「待核实」，结论判不通过；
- 硬门禁（字数/语言/禁词/去AI味密度）零 LLM，未过即阻断落正文并输出定位清单；
- 三层一致性：数值算术（确定性）/ 设定口径（模型）/ 细节连续（跨章状态旧值逐字校验）；
- 质量债分级：债-1 登记放行、债-2 标记待校订、阻断停链回 CONTRACT；
- REVISE 自动回灌最多 2 轮，超轮次转人工。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .. import db
from ..gates import density_gate, language_gate, prose_gate
from . import generation_service, operation_log, prompt_registry_service
from .chapter_service import require_chapter_path
from .contract_service import get_contract
from .errors import InvalidOperationError, NodeNotFoundError
from .fs_utils import count_words, read_text, split_frontmatter
from .project_service import get_project_dir

MIN_CHAPTER_WORDS = 2000
MAX_REVISE_ROUNDS = 2

# 规划记号泄漏：正文里不该出现的「写作过程标记」
MARKER_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"情节点\s*[：:1-9]", "规划记号：情节点"),
    (r"字数预算", "规划记号：字数预算"),
    (r"钩子类型", "规划记号：钩子类型"),
    (r"第\s*\d+\s*章\s*[（(【\[]?\s*(?:细纲|情节点|草稿|待写)", "规划记号：章节规划"),
    (r"\bwatcher\b", "规划记号：watcher 代号"),
    (r"【(?:必读文件|产物落盘要求|输出要求)】", "任务指令泄漏"),
    (r"(?:作为|身为)(?:一个)?AI(?:助手|模型)?", "身份泄漏：AI 自称"),
    (r"(?:以下|下面)是(?:符合|根据)要求的(?:正文|内容)", "生成腔：引导语"),
)

STATUS_PASS = "通过"
STATUS_FAIL = "不通过"
ITEM_DONE = "已完成"
ITEM_TODO = "未完成"
ITEM_UNVERIFIED = "待核实"


def _json(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def _loads(text: str | None, fallback: object = None):
    if not text:
        return fallback
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return fallback


# ─────────────────────────── 硬门禁 ───────────────────────────


def run_hard_gates(text: str, *, min_words: int = MIN_CHAPTER_WORDS,
                   project_dir: Path | None = None, rel_path: str | None = None) -> dict:
    """确定性硬门禁：字数 / 语言 / 禁词密度 / 散文形状 / 记号泄漏。

    :return: ``{"passed", "gates": [...], "locations": [...]}``
    """
    body = split_frontmatter(text or "")[1]
    words = prose_gate.han_count(body)
    gates: list[dict] = []
    locations: list[dict] = []

    # 1) 字数门
    gates.append(
        {
            "key": "字数门",
            "passed": words >= min_words,
            "detail": f"正文 {words} 汉字（下限 {min_words} 汉字）",
            "blocking": words < min_words,
        }
    )

    # 2) 语言门
    whitelist: list[str] = []
    if project_dir is not None:
        # Never use language_gate.load_whitelist: it walks beyond this book.
        root = Path(project_dir).resolve()
        directory = (root / (rel_path or "章节/正文.md")).resolve().parent
        if directory != root and root not in directory.parents:
            raise InvalidOperationError("白名单读取路径越出本书")
        while True:
            candidate = directory / language_gate.WHITELIST_FILENAME
            if candidate.is_file():
                if candidate.is_symlink() or (candidate.resolve() != root and root not in candidate.resolve().parents):
                    raise InvalidOperationError("白名单不能指向本书以外")
                whitelist = language_gate.parse_whitelist(read_text(candidate))
                break
            if directory == root:
                break
            directory = directory.parent
    lang = language_gate.run(body, whitelist)
    gates.append(
        {
            "key": "语言门",
            "passed": lang.passed,
            "detail": lang.report(),
            "blocking": not lang.passed,
            "findings": [
                {"line": item.line, "column": item.column, "type": item.type, "text": item.text}
                for item in lang.findings[:50]
            ],
        }
    )
    for item in lang.findings[:50]:
        locations.append({"gate": "语言门", "line": item.line, "text": item.text,
                          "reason": item.type})

    # 3) 禁词/密度门（去 AI 味硬门）
    density = density_gate.run(body)
    gates.append(
        {
            "key": "禁词门",
            "passed": density.passed,
            "detail": density.report(),
            "blocking": not density.passed,
            "hits": {
                "禁词": density.forbidden_hits,
                "推测词": density.guess_hits,
                "三连排比": density.triple_hits,
                "升华腔": density.upgrade_hits,
                "破折号": density.dash_count,
                "对话引号": density.quote_count,
            },
        }
    )
    for word, count in list(density.forbidden_hits.items())[:20]:
        locations.append({"gate": "禁词门", "line": 0, "text": word,
                          "reason": f"禁用词命中 {count} 次"})
    for word, count in list(density.guess_hits.items())[:20]:
        locations.append({"gate": "禁词门", "line": 0, "text": word,
                          "reason": f"叙述层推测词 {count} 次"})
    for item in density.triple_hits[:10]:
        locations.append({"gate": "禁词门", "line": 0, "text": item, "reason": "三连排比"})

    # 4) 散文形状门（模型化形状）
    prose = prose_gate.run(body)
    gates.append(
        {
            "key": "去AI味门",
            "passed": prose.passed,
            "detail": prose.report(),
            "blocking": not prose.passed,
            "failures": prose.failures[:20],
            "warnings": prose.warnings[:20],
        }
    )
    for item in prose.failures[:10]:
        locations.append({"gate": "去AI味门", "line": 0, "text": item[:80],
                          "reason": "模型化形状"})

    # 5) 记号泄漏
    leaks: list[dict] = []
    for line_no, line in enumerate(body.splitlines(), start=1):
        for pattern, reason in MARKER_PATTERNS:
            match = re.search(pattern, line)
            if match:
                leaks.append({"line": line_no, "text": match.group(0), "reason": reason})
    gates.append(
        {
            "key": "记号泄漏",
            "passed": not leaks,
            "detail": "无规划记号泄漏" if not leaks else f"发现 {len(leaks)} 处泄漏",
            "blocking": bool(leaks),
            "leaks": leaks[:20],
        }
    )
    for item in leaks[:20]:
        locations.append({"gate": "记号泄漏", **item})

    blocking = [gate for gate in gates if gate.get("blocking")]
    return {
        "passed": not blocking,
        "words": words,
        "gates": gates,
        "locations": locations,
        "blocking_gates": [gate["key"] for gate in blocking],
    }


# ─────────────────────────── 三层一致性 ───────────────────────────


def check_ledger_arithmetic(project_dir: Path) -> list[dict]:
    """数值算术（确定性）：检查 ``状态/资源账本.md`` 的收支与前值是否自洽。"""
    path = Path(project_dir) / "状态" / "资源账本.md"
    if not path.is_file():
        return []
    text = split_frontmatter(read_text(path))[1]
    problems: list[dict] = []
    balances: dict[str, float] = {}

    row_re = re.compile(r"^\s*\|([^|]+)\|([^|]*)\|([^|]*)\|([^|]*)\|")
    for line_no, line in enumerate(text.splitlines(), start=1):
        match = row_re.match(line)
        if not match:
            continue
        item = match.group(1).strip()
        if not item or item in ("物品", "资源", "名称") or set(item) <= {"-", " "}:
            continue
        chapter = match.group(2).strip()
        delta_text = match.group(3).strip()
        balance_text = match.group(4).strip()

        delta = _to_number(delta_text)
        balance = _to_number(balance_text)
        if delta is None or balance is None:
            continue
        previous = balances.get(item)
        if previous is not None and abs(previous + delta - balance) > 1e-6:
            problems.append(
                {
                    "类型": "数值算术",
                    "说明": f"「{item}」在 {chapter} 的收支不自洽：前值 {previous} + 变动 {delta} ≠ {balance}",
                    "依据": line.strip(),
                    "line": line_no,
                }
            )
        balances[item] = balance
    return problems


def _to_number(text: str) -> float | None:
    if not text:
        return None
    cleaned = text.replace(",", "").replace("+", "").strip()
    match = re.search(r"-?\d+(?:\.\d+)?", cleaned)
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def verify_state_changes(project_id: int, chapter_rel: str, changes: list[dict]) -> list[dict]:
    """跨章连续性守卫：状态变更必须逐字报出旧值，不符即判不通过。

    :param changes: ``[{"角色","字段","旧值","新值"}]``（来自摄取或模型输出）
    """
    from .character_service import get_state_value

    problems: list[dict] = []
    for change in changes:
        character = str(change.get("角色") or change.get("character") or "").strip()
        field = str(change.get("字段") or change.get("field") or "").strip()
        reported_old = str(change.get("旧值") or change.get("old") or "").strip()
        if not character or not field:
            continue
        actual = get_state_value(project_id, character, field)
        if actual is None:
            continue
        if reported_old and reported_old != actual:
            problems.append(
                {
                    "类型": "细节连续",
                    "说明": f"{character}.{field} 旧值报错：模型报「{reported_old}」，实际「{actual}」",
                    "证据": f"依据字段 {character}/{field}",
                    "旧值": actual,
                    "新值": str(change.get("新值") or change.get("new") or ""),
                    "chapter": chapter_rel,
                }
            )
        elif not reported_old:
            problems.append(
                {
                    "类型": "细节连续",
                    "说明": f"{character}.{field} 变更未报旧值（无法校验，按不通过处理）",
                    "证据": f"当前实际值「{actual}」",
                    "旧值": actual,
                    "新值": str(change.get("新值") or ""),
                    "chapter": chapter_rel,
                }
            )
    return problems


def check_entity_coverage(project_dir: Path, text: str) -> list[dict]:
    """设定口径（确定性前置）：正文出现的「疑似专名」是否在设定中有登记。"""
    known: set[str] = set()
    setting_dir = Path(project_dir) / "设定"
    if setting_dir.is_dir():
        for path in setting_dir.rglob("*.md"):
            content = split_frontmatter(read_text(path))[1]
            for match in re.finditer(r"^\s*#{2,4}\s*([^\n#]{2,20})\s*$", content, re.MULTILINE):
                known.add(match.group(1).strip())
            for match in re.finditer(r"^\s*[-*]\s*\*\*([^*]{2,20})\*\*", content, re.MULTILINE):
                known.add(match.group(1).strip())

    if not known:
        return []
    # 出场但未登记的三人称专名（粗筛：连续 2-3 个汉字的重复出现）
    counter: dict[str, int] = {}
    for match in re.finditer(r"[\u4e00-\u9fff]{2,3}", split_frontmatter(text)[1]):
        name = match.group(0)
        counter[name] = counter.get(name, 0) + 1
    unregistered = [
        {"类型": "设定口径", "说明": f"「{name}」在正文出现 {count} 次，但设定中未见登记",
         "证据": ""}
        for name, count in counter.items()
        if count >= 4 and name not in known and name not in {"我们", "他们", "自己", "什么",
                                                             "这个时候", "那个", "这个"}
    ]
    return unregistered[:10]


# ─────────────────────────── 逐项审稿 ───────────────────────────


def _ai_review(project_id: int, chapter_rel: str, text: str) -> dict | None:
    prompt = prompt_registry_service.get_prompt("novel.review.contract")
    from .context_service import assemble, to_messages

    context = assemble(project_id, chapter_rel=chapter_rel, query="")
    _system, messages = to_messages(context, system=prompt["body"])
    messages.append({"role": "user", "content": f"【待审正文】\n{split_frontmatter(text)[1]}"})

    result = generation_service.run_task(
        project_id=project_id,
        task_type="审稿",
        system=prompt["body"],
        messages=messages,
        prompt_id=prompt["prompt_id"],
        prompt_version=prompt["version"],
        context_snapshot={"chapter": chapter_rel, "tokens": context["total_tokens"]},
        temperature=0.1,
    )
    if not result["ok"]:
        return None
    return _parse_json(result["text"])


def _parse_json(text: str) -> dict | None:
    if not text:
        return None
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidate = fenced.group(1) if fenced else None
    if candidate is None:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            return None
        candidate = text[start:end + 1]
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def review_chapter(
    project_id: int,
    chapter_rel: str,
    *,
    use_ai: bool = True,
    register_debt: bool = True,
) -> dict:
    """对单章执行完整审稿（硬门禁 + 逐项验收 + 一致性），结果落 ``reviews`` 表。"""
    row, project_dir = get_project_dir(project_id)
    path = require_chapter_path(project_id, chapter_rel)
    text = read_text(path)
    body = split_frontmatter(text)[1]

    hard = run_hard_gates(body, project_dir=project_dir, rel_path=chapter_rel)
    items: list[dict] = []
    consistency: list[dict] = []
    revise_instructions: list[str] = []
    ai_used = False

    if use_ai:
        ai = _ai_review(project_id, chapter_rel, text)
        if ai:
            ai_used = True
            for entry in ai.get("逐项") or []:
                verdict = str(entry.get("判定") or ITEM_UNVERIFIED)
                if verdict not in (ITEM_DONE, ITEM_TODO, ITEM_UNVERIFIED):
                    verdict = ITEM_UNVERIFIED
                items.append(
                    {
                        "项": str(entry.get("项") or ""),
                        "判定": verdict,
                        "证据": str(entry.get("证据") or ""),
                    }
                )
            consistency.extend(
                {"类型": str(item.get("类型") or "设定口径"),
                 "说明": str(item.get("问题") or item.get("说明") or ""),
                 "证据": str(item.get("证据") or "")}
                for item in (ai.get("一致性") or [])
            )
            revise_instructions.extend(str(item) for item in (ai.get("修改指令") or []))

    # 确定性一致性检查（无论 AI 是否可用都要跑）
    consistency.extend(check_ledger_arithmetic(project_dir))
    consistency.extend(check_entity_coverage(project_dir, text))

    # 合同项兜底：无 AI 时按合同情节点逐项列「待核实」（待核实不算通过）
    contract = None
    try:
        contract = get_contract(project_id, chapter_rel)
    except NodeNotFoundError:
        contract = None
    if not items and contract:
        items = [
            {"项": str(point), "判定": ITEM_UNVERIFIED, "证据": ""}
            for point in contract.get("plot_points") or []
        ]

    unfinished = [item for item in items if item["判定"] != ITEM_DONE]
    verdict = STATUS_PASS if (hard["passed"] and not unfinished and not consistency) \
        else STATUS_FAIL

    payload = {
        "verdict": verdict,
        "hard_gates": hard,
        "items": items,
        "consistency": consistency,
        "revise_instructions": revise_instructions,
        "ai_used": ai_used,
        "words": hard["words"],
        "contract_present": contract is not None,
        "counts": {
            "已完成": sum(1 for item in items if item["判定"] == ITEM_DONE),
            "未完成": sum(1 for item in items if item["判定"] == ITEM_TODO),
            "待核实": sum(1 for item in items if item["判定"] == ITEM_UNVERIFIED),
        },
    }

    with db.get_conn() as conn:
        cursor = conn.execute(
            "INSERT INTO reviews (project_id, chapter_id, rel_path, kind, verdict, payload)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (project_id, None, chapter_rel, "contract", verdict, _json(payload)),
        )
        review_id = int(cursor.lastrowid or 0)

    if register_debt and verdict == STATUS_FAIL:
        register_quality_debt(project_id, chapter_rel, payload)

    operation_log.log(project_id, "review", chapter_rel,
                      {"review_id": review_id, "verdict": verdict})
    return {"review_id": review_id, "rel_path": chapter_rel, "verdict": verdict, **payload}


# ─────────────────────────── 去AI味软审（Task 36） ───────────────────────────


def soft_deslop(
    project_id: int,
    chapter_rel: str,
    *,
    use_ai: bool = True,
    create_proposals: bool = True,
) -> dict:
    """软审：模型逐行建议 → 收件箱（行级 patch，应用时校验原行是否仍匹配）。

    同时给出**文风指纹比对**（候选文本 vs 作者认可样本），供人工参照（不阻断）。
    """
    from . import style_service

    row, project_dir = get_project_dir(project_id)
    path = require_chapter_path(project_id, chapter_rel)
    text = read_text(path)
    body = split_frontmatter(text)[1]

    comparison = style_service.compare(project_id, text)
    suggestions: list[dict] = []

    if use_ai:
        prompt = prompt_registry_service.get_prompt("novel.deslop.soft")
        result = generation_service.run_task(
            project_id=project_id,
            task_type="去AI味软审",
            system=prompt["body"],
            messages=[
                {
                    "role": "user",
                    "content": "【待检正文（带行号）】\n"
                    + "\n".join(f"{index}: {line}"
                                for index, line in enumerate(body.splitlines(), start=1)),
                }
            ],
            prompt_id=prompt["prompt_id"],
            prompt_version=prompt["version"],
            temperature=0.2,
        )
        if result["ok"]:
            parsed = _parse_json(result["text"]) or {}
            for item in parsed.get("建议") or []:
                if not isinstance(item, dict):
                    continue
                suggestions.append(
                    {
                        "line": int(item.get("行") or 0),
                        "issue": str(item.get("问题") or ""),
                        "original": str(item.get("原文") or ""),
                        "replacement": str(item.get("建议") or ""),
                    }
                )

    proposal_ids: list[int] = []
    if create_proposals:
        from . import proposal_service

        for item in suggestions:
            if not item["replacement"] or item["line"] < 1:
                continue
            proposal = proposal_service.create_proposal(
                project_id=project_id,
                kind="deslop",
                title=f"第 {item['line']} 行：{item['issue'][:20] or '去AI味建议'}",
                target_path=chapter_rel,
                content=item["replacement"],
                meta={"patch": item, "issue": item["issue"]},
            )
            proposal_ids.append(proposal["id"])

    return {
        "project_id": project_id,
        "chapter_rel": chapter_rel,
        "suggestions": suggestions,
        "proposal_ids": proposal_ids,
        "style_comparison": comparison,
        "project_name": row["name"],
    }


# ─────────────────────────── 质量债 ───────────────────────────

DEBT_LEVELS = {
    "债-1": "章级瑕疵（登记放行）",
    "债-2": "一致性问题（放行但标记待校订）",
    "阻断": "剧情路线冲突 / 合同未达成（停链回 CONTRACT）",
}


def classify_debt(payload: dict) -> str:
    """按审稿结果判定质量债等级。"""
    hard = payload.get("hard_gates") or {}
    if hard.get("blocking_gates"):
        return "阻断"
    if payload.get("consistency"):
        return "债-2"
    if payload.get("counts", {}).get("未完成"):
        return "阻断"
    return "债-1"


def register_quality_debt(project_id: int, chapter_rel: str, payload: dict) -> dict:
    """登记质量债（同章同等级去重，取最新）。"""
    level = classify_debt(payload)
    notes = []
    if payload.get("consistency"):
        notes.append(f"{len(payload['consistency'])} 项一致性问题")
    counts = payload.get("counts") or {}
    if counts.get("未完成"):
        notes.append(f"{counts['未完成']} 项合同未完成")
    if counts.get("待核实"):
        notes.append(f"{counts['待核实']} 项待核实")
    hard = payload.get("hard_gates") or {}
    if hard.get("blocking_gates"):
        notes.append("硬门禁阻断：" + "、".join(hard["blocking_gates"]))

    with db.get_conn() as conn:
        conn.execute(
            "UPDATE quality_debts SET status = 'superseded'"
            " WHERE project_id = ? AND rel_path = ? AND status = 'open'",
            (project_id, chapter_rel),
        )
        cursor = conn.execute(
            "INSERT INTO quality_debts (project_id, rel_path, level, status, note)"
            " VALUES (?, ?, ?, 'open', ?)",
            (project_id, chapter_rel, level, "；".join(notes) or "审稿未通过"),
        )
        debt_id = int(cursor.lastrowid or 0)
    return {"id": debt_id, "level": level, "note": "；".join(notes), "status": "open"}


def list_debts(project_id: int, status: str | None = "open") -> list[dict]:
    sql = ("SELECT id, project_id, rel_path, level, status, note, created_at"
           " FROM quality_debts WHERE project_id = ?")
    params: list = [project_id]
    if status:
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY id DESC LIMIT 200"
    with db.get_conn() as conn:
        rows = conn.execute(sql, tuple(params)).fetchall()
    return [dict(row) for row in rows]


def resolve_debt(debt_id: int, status: str = "resolved") -> dict:
    if status not in ("resolved", "waived", "open"):
        raise InvalidOperationError("status 只能是 resolved / waived / open")
    with db.get_conn() as conn:
        row = conn.execute("SELECT id FROM quality_debts WHERE id = ?", (debt_id,)).fetchone()
        if row is None:
            raise NodeNotFoundError(f"质量债不存在：id={debt_id}")
        conn.execute("UPDATE quality_debts SET status = ? WHERE id = ?", (status, debt_id))
    return {"id": debt_id, "status": status}


# ─────────────────────────── 质检进度表 ───────────────────────────


def quality_matrix(project_id: int) -> dict:
    """逐章检查矩阵：字数门 / 语言门 / 禁词门 / 一致性 / 逐项审稿 / 去AI味。"""
    from .chapter_service import list_chapters

    chapters = list_chapters(project_id)
    row, project_dir = get_project_dir(project_id)

    with db.get_conn() as conn:
        review_rows = conn.execute(
            "SELECT rel_path, payload, created_at FROM reviews WHERE project_id = ?"
            " ORDER BY id DESC",
            (project_id,),
        ).fetchall()
        debt_rows = conn.execute(
            "SELECT rel_path, level, status FROM quality_debts WHERE project_id = ?",
            (project_id,),
        ).fetchall()

    latest_reviews: dict[str, dict] = {}
    for item in review_rows:  # 已按 id DESC，取每章最新
        latest_reviews.setdefault(item["rel_path"], _loads(item["payload"], {}) or {})

    debts: dict[str, list[dict]] = {}
    for item in debt_rows:
        debts.setdefault(item["rel_path"], []).append(dict(item))

    # 逐章硬门禁（确定性，实时跑；字数/空章快速判断）
    matrix: list[dict] = []
    for chapter in chapters:
        rel_path = chapter["rel_path"]
        text = ""
        path = Path(project_dir) / rel_path
        try:
            text = read_text(path)
        except (OSError, UnicodeDecodeError):
            text = ""
        if text.strip():
            hard = run_hard_gates(text, project_dir=project_dir, rel_path=rel_path)
            cells = {
                "字数门": _cell(hard["gates"][0]["passed"]),
                "语言门": _cell(hard["gates"][1]["passed"]),
                "禁词门": _cell(hard["gates"][2]["passed"]),
                "去AI味": _cell(hard["gates"][3]["passed"]),
            }
        else:
            cells = {"字数门": "未跑", "语言门": "未跑", "禁词门": "未跑", "去AI味": "未跑"}

        review = latest_reviews.get(rel_path)
        if review:
            counts = review.get("counts") or {}
            cells["逐项审稿"] = _cell(
                review.get("verdict") == STATUS_PASS,
                detail=f"待核实 {counts.get('待核实', 0)} · 未完成 {counts.get('未完成', 0)}",
            )
            cells["一致性"] = _cell(not review.get("consistency"))
        else:
            cells["逐项审稿"] = "未跑"
            cells["一致性"] = "未跑"

        matrix.append(
            {
                "rel_path": rel_path,
                "number": chapter["number"],
                "title": chapter["title"],
                "status": chapter["status"],
                "word_count": chapter["word_count"],
                "cells": cells,
                "debt": debts.get(rel_path, []),
                "reviewed_at": None,
            }
        )

    summary: dict[str, int] = {}
    for entry in matrix:
        for name, value in entry["cells"].items():
            key = f"{name}:{value}"
            summary[key] = summary.get(key, 0) + 1

    return {
        "project_id": project_id,
        "project_name": row["name"],
        "chapters": matrix,
        "summary": summary,
        "columns": ["字数门", "语言门", "禁词门", "去AI味", "一致性", "逐项审稿"],
    }


def _cell(passed: bool, detail: str = "") -> str:
    return "通过" if passed else "未通过"


def list_reviews(project_id: int, rel_path: str | None = None, limit: int = 50) -> list[dict]:
    sql = ("SELECT id, rel_path, verdict, payload, created_at FROM reviews"
           " WHERE project_id = ?")
    params: list = [project_id]
    if rel_path:
        sql += " AND rel_path = ?"
        params.append(rel_path)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(int(limit))
    with db.get_conn() as conn:
        rows = conn.execute(sql, tuple(params)).fetchall()
    return [
        {
            "id": int(item["id"]),
            "rel_path": item["rel_path"],
            "verdict": item["verdict"],
            "created_at": item["created_at"],
            "payload": _loads(item["payload"], {}),
        }
        for item in rows
    ]


__all__ = [
    "DEBT_LEVELS",
    "MAX_REVISE_ROUNDS",
    "MIN_CHAPTER_WORDS",
    "classify_debt",
    "check_entity_coverage",
    "check_ledger_arithmetic",
    "list_debts",
    "list_reviews",
    "quality_matrix",
    "register_quality_debt",
    "resolve_debt",
    "review_chapter",
    "run_hard_gates",
    "soft_deslop",
    "verify_state_changes",
]
