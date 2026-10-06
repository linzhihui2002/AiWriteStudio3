"""审稿服务：硬门禁（确定性）+ 逐项合同验收（模型）+ 三层一致性 + 质量债。

核心纪律（spec）
- **待核实不算通过**：找不到正文证据的合同项一律记「待核实」，结论判不通过；
- 硬门禁（字数/语言/禁词/去AI味密度）零 LLM，未过即阻断落正文并输出定位清单；
- 三层一致性：数值算术（确定性）/ 设定口径（模型）/ 细节连续（跨章状态旧值逐字校验）；
- 质量债分级：债-1 登记放行、债-2 标记待校订、阻断停链回 CONTRACT；
- REVISE 自动回灌最多 2 轮，超轮次转人工。
"""

from __future__ import annotations

import hashlib
import json
import re
from bisect import bisect_right
from pathlib import Path

from .. import db
from ..gates import density_gate, language_gate, novel_format, prose_gate, prose_quality
from . import generation_service, operation_log, prompt_registry_service
from .chapter_service import require_chapter_path
from .contract_service import get_contract
from .errors import InvalidOperationError, NodeNotFoundError
from .fs_utils import count_words, read_text, split_frontmatter
from .project_service import get_project_dir
from .writing_preference_service import chapter_word_range

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


def _model_diagnostic(result: dict, *, label: str, invalid: bool = False) -> dict:
    from .secret_store import redact
    failed = not result.get("ok")
    code = str(result.get("error_code") or "ENGINE_ERROR") if failed else \
        "INVALID_MODEL_OUTPUT" if invalid else ""
    message = str(result.get("error_message") or "模型引擎未完成执行") if failed else \
        "模型返回的结构不符合要求，请重新运行" if invalid else ""
    return {"ai_error_code": code, "ai_error": f"{label}：{redact(message)[:600]}" if message else "",
            "task_id": result.get("task_id")}


def contract_fingerprint(contract: dict | None) -> str:
    """稳定序列化合同快照；无合同返回空值，调用方不能据此放行。"""
    if contract is None:
        return ""
    serialized = json.dumps(contract, ensure_ascii=False, sort_keys=True,
                            separators=(",", ":"), default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


# ─────────────────────────── 硬门禁 ───────────────────────────


def run_hard_gates(text: str, *, min_words: int = MIN_CHAPTER_WORDS,
                   max_words: int | None = None,
                   project_dir: Path | None = None, rel_path: str | None = None) -> dict:
    """确定性硬门禁：字数 / 语言 / 禁词密度 / 散文形状 / 记号泄漏 / 正文格式。

    ``min_words`` 为下限（调用方传入本书区间下限）；``max_words`` 为可选上限，
    仅产出非阻断告警项，不影响 ``passed``。

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
        directory = (root / (rel_path or "章节/正文.txt")).resolve().parent
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

    # 6) 正文格式门（番茄纯文本规范：Markdown 标记泄漏 / 重复标题行 / 引号配对 / 标点混用）
    #    规划记号词表仍以本模块 MARKER_PATTERNS 为唯一事实源（上面的「记号泄漏」门），
    #    格式门不重抄一份，两门并列执行。
    fmt = novel_format.run(body)
    gates.append(
        {
            "key": "格式门",
            "passed": fmt.passed,
            "detail": fmt.report(),
            "blocking": not fmt.passed,
            "findings": [
                {"line": item.line, "column": item.column, "reason": item.reason,
                 "text": item.text}
                for item in fmt.findings[:20]
            ],
            "warnings": [
                {"line": item.line, "reason": item.reason, "text": item.text}
                for item in fmt.warnings[:20]
            ],
        }
    )
    for item in fmt.findings[:20]:
        locations.append({"gate": "格式门", "line": item.line, "text": item.text,
                          "reason": item.reason})

    # 7) 字数上限（非阻断：超出本书区间上限只告警，不拒绝落盘）
    if max_words is not None:
        gates.append(
            {
                "key": "字数上限",
                "passed": words <= max_words,
                "detail": f"正文 {words} 汉字（上限 {max_words} 汉字）",
                "blocking": False,
            }
        )

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
    """Validate actual ledger tables; incomplete rows remain explicitly unverified."""
    path = Path(project_dir) / "状态" / "资源账本.md"
    if not path.is_file():
        return []
    text = split_frontmatter(read_text(path))[1]
    problems: list[dict] = []
    balances: dict[str, float] = {}
    header: dict[str, int] | None = None
    aliases = {"item": {"物品", "资源", "名称", "资产"},
               "chapter": {"章节", "章号", "来源章节"},
               "delta": {"增减", "变动", "本次变动", "变化"},
               "balance": {"结余", "余额", "剩余", "当前余额"},
               "before": {"前值", "上期结余", "之前余额"}}
    for line_no, line in enumerate(text.splitlines(), start=1):
        if not line.lstrip().startswith("|"):
            if line.lstrip().startswith("#"):
                header = None
            continue
        cells = [cell.strip().replace("\\|", "|") for cell in re.split(r"(?<!\\)\|", line.strip().strip("|"))]
        if not cells or all(re.fullmatch(r":?-+:?", cell.replace(" ", "")) for cell in cells):
            continue
        mapped = {key: index for key, names in aliases.items()
                  for index, cell in enumerate(cells) if cell in names}
        if "item" in mapped:
            header = mapped if "delta" in mapped and "balance" in mapped else None
            continue
        current = header
        if current is None:
            # Old ingestion rows had no header but a canonical chapter and signed change.
            if (len(cells) >= 4 and re.fullmatch(r"第\d+章", cells[1])
                    and re.fullmatch(r"[+-]\d+(?:\.\d+)?", cells[2])):
                current = {"item": 0, "chapter": 1, "delta": 2, "balance": 3}
            else:
                continue
        def value(key):
            index = current.get(key)
            return cells[index] if index is not None and index < len(cells) else ""
        item, chapter = value("item"), value("chapter")
        if not item:
            continue
        delta, balance = _to_number(value("delta")), _to_number(value("balance"))
        previous = _to_number(value("before")) if "before" in current else balances.get(item)
        if delta is None or balance is None or previous is None:
            missing = "、".join(name for name, amount in (("前值", previous), ("变动", delta), ("结余", balance)) if amount is None)
            problems.append({"类型": "数值算术", "判定": ITEM_UNVERIFIED,
                             "说明": f"「{item}」在 {chapter} 的{missing}待核实，尚不能验证收支守恒",
                             "依据": line.strip(), "line": line_no})
        elif abs(previous + delta - balance) > 1e-6:
            problems.append({"类型": "数值算术", "说明": f"「{item}」在 {chapter} 的收支不自洽：前值 {previous} + 变动 {delta} ≠ {balance}",
                             "依据": line.strip(), "line": line_no})
        if balance is not None:
            balances[item] = balance
        else:
            balances.pop(item, None)
    return problems


def _to_number(text: str) -> float | None:
    import math
    cleaned = str(text or "").replace(",", "").strip()
    if not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", cleaned):
        return None
    try:
        value = float(cleaned)
        return value if math.isfinite(value) else None
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


def _ai_review(project_id: int, chapter_rel: str, text: str, *,
               should_cancel=None, contract: dict | None = None,
               quality: dict | None = None, history: dict | None = None) -> dict | None:
    prompt = prompt_registry_service.get_prompt("novel.review.contract")
    from .context_service import assemble, to_messages

    context = assemble(project_id, chapter_rel=chapter_rel, query="", retrieval_profile="review",
                       document={"rel_path": chapter_rel, "text": split_frontmatter(text)[1]})
    _system, messages = to_messages(context, system=prompt["body"])
    contract_input = ""
    if contract is not None:
        contract_input = (
            "【本轮章节合同（本次逐项验收依据）】\n" + _json(contract) + "\n"
            "逐项覆盖 plot_points 和 must_connect，每条的「项」照录合同原文。"
            "「证据」只填待审正文中的连续原文，不附解释或省略号；证据位置放在单独字段。\n"
        )
    quality = quality if quality is not None else prose_quality.diagnose_prose(split_frontmatter(text)[1])
    expression_input = ""
    if quality.get("findings"):
        expression_input = (
            "【表达线索，仅供判断，不影响合同判定】\n"
            + "\n".join(f"第{item['line']}行：{item['reason']}；原文：{item['text'][:160]}"
                        for item in quality["findings"][:6])
            + "\n有意复沓可以保留；仅将有依据的表达意见放入「表达建议」，不得作为合同失败。\n"
        )
    from . import prose_history_service
    history = history if history is not None else prose_history_service.diagnose_for_chapter(
        project_id, chapter_rel, split_frontmatter(text)[1])
    history_input = ""
    if history.get("sources"):
        history_input = (
            "【近期正文的跨章表达线索，仅供判断，不影响合同判定】\n"
            + _json({"status": history["status"], "sources": history["sources"],
                     "reference_hash": history["reference_hash"],
                     "findings": history.get("findings", [])[:6]})
            + "\n必要的回声可以保留；不得仅因跨章复读阻断本章或要求自动改稿。\n"
        )
    messages.append({"role": "user", "content": contract_input + expression_input + history_input
                     + f"【待审正文】\n{split_frontmatter(text)[1]}"})

    result = generation_service.run_task(
        project_id=project_id,
        task_type="审稿",
        system=prompt["body"],
        messages=messages,
        prompt_id=prompt["prompt_id"],
        prompt_version=prompt["version"],
        context_snapshot={"chapter": chapter_rel, "tokens": context["total_tokens"],
                          "candidate_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                          "contract_hash": contract_fingerprint(contract),
                          "prose_diagnostics_version": quality["version"],
                          "prose_history": {key: history[key] for key in
                              ("status", "sources", "reference_hash", "truncated")}},
        temperature=0.1,
        should_cancel=should_cancel,
    )
    parsed = _parse_json(str(result.get("text") or "")) if result.get("ok") else None
    diagnostic = _model_diagnostic(result, label="AI 审稿未完成", invalid=parsed is None)
    return {**(parsed or {}), "_diagnostic": diagnostic}


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
    path = require_chapter_path(project_id, chapter_rel)
    text = read_text(path)
    result = review_text(project_id, chapter_rel, text, use_ai=use_ai,
                         register_debt=register_debt)
    from .errors import ServiceError
    try:
        result["source_changed"] = hashlib.sha256(read_text(require_chapter_path(
            project_id, chapter_rel)).encode("utf-8")).hexdigest() != result["source_hash"]
    except (OSError, UnicodeError, ServiceError):
        result["source_changed"] = True
    return result


def _checked_review_item(entry: dict, body: str) -> dict:
    """完成项必须附正文原文；不接受模型编造、解释或省略后的引用。"""
    verdict = str(entry.get("判定") or ITEM_UNVERIFIED)
    if verdict not in (ITEM_DONE, ITEM_TODO, ITEM_UNVERIFIED):
        verdict = ITEM_UNVERIFIED
    evidence = str(entry.get("证据") or "").strip()
    item = {"项": str(entry.get("项") or "").strip(),
            "判定": verdict, "证据": evidence}
    if verdict == ITEM_DONE:
        # The prompt requests plain excerpts, but accept an outer quotation pair.
        # Do not normalize punctuation/whitespace within a quote: its source must
        # actually exist in the candidate being reviewed.
        quote = evidence
        if quote not in body:
            for left, right in (("“", "”"), ("「", "」"), ("『", "』"), ('"', '"'), ("'", "'")):
                if quote.startswith(left) and quote.endswith(right) and len(quote) > 2:
                    quote = quote[1:-1]
                    break
        position = body.find(quote) if quote else -1
        if position < 0:
            item["判定"] = ITEM_UNVERIFIED
            item["核验说明"] = "完成项缺少候选正文中可定位的原文证据"
        else:
            item["证据"] = quote
            item["证据行"] = body.count("\n", 0, position) + 1
            item["证据列"] = position - body.rfind("\n", 0, position)
    return item


def _complete_contract_items(items: list[dict], contract: dict | None) -> list[dict]:
    """补齐合同情节点与承上项；模型少列一项也不能获得通过结论。"""
    if contract is None:
        return items + [{"项": "章节合同", "判定": ITEM_UNVERIFIED, "证据": "",
                         "核验说明": "未找到章节合同，无法逐项验收"}]
    expected: list[dict] = []
    matched_names: set[str] = set()
    for field in ("plot_points", "must_connect"):
        for point in contract.get(field) or []:
            name = str(point).strip()
            if not name:
                continue
            matching = [item for item in items if item["项"] == name]
            # Conflicting duplicate verdicts cannot be resolved by accepting the
            # first successful entry. Preserve the least certain/failing one.
            item = next((item for item in matching if item["判定"] != ITEM_DONE),
                        matching[0] if matching else None)
            expected.append({**(item or {
                "项": name, "判定": ITEM_UNVERIFIED, "证据": "",
                "核验说明": "AI 审稿遗漏合同项，需重新核实",
            }), "合同字段": field})
            matched_names.add(name)
    return expected + [item for item in items if item["项"] not in matched_names]


EXPRESSION_KINDS = frozenset({"重复解释", "人物同声", "模板表达", "机械转场"})
MAX_EXPRESSION_SUGGESTIONS = 12


def _checked_expression_suggestions(entries: object, body: str) -> tuple[list[dict], int]:
    """表达意见只保留可定位原文；不参与合同判定，也不转成自动修改。"""
    if entries is None:
        return [], 0
    if not isinstance(entries, list):
        return [], 1
    checked: list[dict] = []
    rejected = max(0, len(entries) - MAX_EXPRESSION_SUGGESTIONS)
    seen: set[tuple[str, int, str]] = set()
    line_starts = [0, *(match.end() for match in re.finditer(r"\r\n|\r|\n", body))]
    for entry in entries[:MAX_EXPRESSION_SUGGESTIONS]:
        if not isinstance(entry, dict):
            rejected += 1
            continue
        kind, evidence, reason, suggestion = (entry.get(key) for key in ("类型", "证据", "问题", "建议"))
        if (not all(isinstance(value, str) and value.strip() for value in (kind, evidence, reason, suggestion))
                or kind not in EXPRESSION_KINDS or len(evidence) > 500
                or len(reason) > 1000 or len(suggestion) > 1000):
            rejected += 1
            continue
        quote = evidence.strip()
        if quote not in body and len(quote) > 2 and (quote[0], quote[-1]) in (("“", "”"), ("「", "」"), ("『", "』"), ('"', '"')):
            quote = quote[1:-1]
        # An ambiguous excerpt cannot identify which passage the opinion means.
        position = body.find(quote)
        if len(quote) < 4 or position < 0 or body.find(quote, position + 1) >= 0:
            rejected += 1
            continue
        key = (kind, position, suggestion.strip())
        if key in seen:
            rejected += 1
            continue
        seen.add(key)
        line_index = bisect_right(line_starts, position) - 1
        checked.append({"kind": kind, "evidence": quote,
                        "line": line_index + 1, "column": position - line_starts[line_index] + 1,
                        "reason": reason.strip(), "suggestion": suggestion.strip()})
    return checked, rejected


def review_text(
    project_id: int,
    chapter_rel: str,
    text: str,
    *,
    use_ai: bool = True,
    register_debt: bool = False,
    candidate_hash: str = "",
    should_cancel=None,
) -> dict:
    """审给定的候选全文，无需先把新稿写入章节；空的已建章节也会执行 AI 审稿。

    路径仍须是本书现有章节，正文只取 ``text``。``candidate_hash`` 如提供，
    必须等于原始 ``text`` 的 UTF-8 SHA256（不剥 frontmatter、不规范换行）。
    审稿与合同快照摘要落入 payload，供管线确认结论所针对的版本。
    """
    _row, project_dir = get_project_dir(project_id)
    require_chapter_path(project_id, chapter_rel)
    if not isinstance(text, str):
        raise InvalidOperationError("候选正文必须为文本")
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if candidate_hash and candidate_hash != digest:
        raise InvalidOperationError("候选正文 hash 不匹配，拒绝审稿")
    body = split_frontmatter(text)[1]

    contract = None
    try:
        contract = get_contract(project_id, chapter_rel)
    except NodeNotFoundError:
        pass
    contract_hash = contract_fingerprint(contract)

    word_min, word_max = chapter_word_range(project_id)
    hard = run_hard_gates(body, project_dir=project_dir, rel_path=chapter_rel,
                          min_words=word_min, max_words=word_max)
    quality = prose_quality.diagnose_prose(body)
    from . import prose_history_service
    history = prose_history_service.diagnose_for_chapter(project_id, chapter_rel, body)
    expression_suggestions: list[dict] = []
    rejected_expression_suggestions = 0
    items: list[dict] = []
    consistency: list[dict] = []
    revise_instructions: list[str] = []
    ai_used = False
    ai_error = ""
    ai_error_code = ""
    task_id = None

    if use_ai:
        try:
            ai = _ai_review(project_id, chapter_rel, text,
                            should_cancel=should_cancel, contract=contract, quality=quality, history=history)
        except Exception as exc:  # Model/context failures must never become acceptance.
            ai = None
            ai_error = f"AI 审稿异常：{type(exc).__name__}"
            ai_error_code = "REVIEW_EXCEPTION"
        if isinstance(ai, dict):
            diagnostic = ai.get("_diagnostic") or {}
            ai_error = diagnostic.get("ai_error", ai_error)
            ai_error_code = diagnostic.get("ai_error_code", ai_error_code)
            task_id = diagnostic.get("task_id")
        if (isinstance(ai, dict) and isinstance(ai.get("逐项"), list)
                and isinstance(ai.get("一致性", []), list)
                and isinstance(ai.get("修改指令", []), list)):
            entries = [entry for entry in ai["逐项"]
                       if isinstance(entry, dict) and str(entry.get("项") or "").strip()]
            ai_used = bool(entries)
            items.extend(_checked_review_item(entry, body) for entry in entries)
            consistency.extend(
                {"类型": str(item.get("类型") or "设定口径"),
                 "说明": str(item.get("问题") or item.get("说明") or ""),
                 "证据": str(item.get("证据") or "")}
                for item in (ai.get("一致性") or []) if isinstance(item, dict)
            )
            instructions = ai.get("修改指令") or []
            if isinstance(instructions, list):
                revise_instructions.extend(str(item) for item in instructions)
            expression_suggestions, rejected_expression_suggestions = _checked_expression_suggestions(
                ai.get("表达建议"), body)
            if ai.get("结论") == STATUS_FAIL and items and all(
                    item["判定"] == ITEM_DONE for item in items) and not consistency:
                items.append({"项": "审稿结论", "判定": ITEM_UNVERIFIED, "证据": "",
                              "核验说明": "AI 总结不通过，但逐项未说明原因，需重新核实"})
        if not ai_used:
            ai_error = ai_error or "AI 审稿失败或未返回可核验的逐项结果"
            ai_error_code = ai_error_code or "INVALID_MODEL_OUTPUT"
            items.append({"项": "AI 逐项验收", "判定": ITEM_UNVERIFIED, "证据": "",
                          "核验说明": ai_error})

    # 确定性一致性检查（无论 AI 是否可用都要跑）
    consistency.extend(check_ledger_arithmetic(project_dir))
    consistency.extend(check_entity_coverage(project_dir, text))

    items = _complete_contract_items(items, contract)
    if not items:
        items.append({"项": "逐项验收", "判定": ITEM_UNVERIFIED, "证据": "",
                      "核验说明": "合同没有可核验条目且未获得 AI 逐项结果"})

    # Do not certify an old contract when the author changed it while a model
    # was reviewing. Proposal application checks it again under the book lock.
    current_contract = None
    try:
        current_contract = get_contract(project_id, chapter_rel)
    except NodeNotFoundError:
        pass
    contract_changed = contract_fingerprint(current_contract) != contract_hash
    if contract_changed:
        items.append({"项": "章节合同版本", "判定": ITEM_UNVERIFIED, "证据": "",
                      "核验说明": "审稿期间合同已变更，需按当前合同重新审稿"})

    unfinished = [item for item in items if item["判定"] != ITEM_DONE]
    verdict = STATUS_PASS if (hard["passed"] and not unfinished and not consistency) \
        else STATUS_FAIL

    payload = {
        "verdict": verdict,
        "hard_gates": hard,
        "items": items,
        "consistency": consistency,
        "revise_instructions": revise_instructions,
        "prose_quality": quality,
        "prose_history": history,
        "prose_review": expression_suggestions,
        "rejected_expression_suggestions": rejected_expression_suggestions,
        "ai_used": ai_used,
        "ai_error": ai_error,
        "ai_error_code": ai_error_code,
        "task_id": task_id,
        "source_hash": digest,
        "candidate_hash": digest,
        "contract_hash": contract_hash,
        "contract_changed": contract_changed,
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
                      {"review_id": review_id, "verdict": verdict,
                       "candidate_hash": digest, "contract_hash": contract_hash})
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
    from .context_service import assemble, to_messages
    from .file_change_service import content_hash, project_lock

    row, project_dir = get_project_dir(project_id)
    with project_lock(project_id):
        path = require_chapter_path(project_id, chapter_rel)
        text = read_text(path)
    digest = content_hash(text)
    body = split_frontmatter(text)[1]

    comparison = style_service.compare(project_id, text)
    suggestions: list[dict] = []
    rejected_suggestions: list[dict] = []
    ai_used = False
    ai_error = ""
    ai_error_code = ""
    task_id = None
    source_changed = False
    context_snapshot: dict = {"chapter": chapter_rel, "candidate_hash": digest}

    def source_has_changed() -> bool:
        # Resolve again: the file may have disappeared or become an unsafe link
        # while the model was running. Such a source cannot receive a proposal.
        from .errors import ServiceError
        try:
            return content_hash(read_text(require_chapter_path(project_id, chapter_rel))) != digest
        except (ServiceError, OSError, UnicodeError):
            return True

    if use_ai:
        prompt = prompt_registry_service.get_prompt("novel.deslop.soft")
        try:
            context = assemble(project_id, chapter_rel=chapter_rel, query="",
                               retrieval_profile="review", include_style=True,
                               document={"rel_path": chapter_rel, "text": body})
            _system, messages = to_messages(context, system=prompt["body"])
            messages.append({"role": "user", "content": "【待检正文（带行号）】\n"
                             + "\n".join(f"{index}: {line}"
                                         for index, line in enumerate(body.splitlines(), start=1))})
            context_snapshot.update(tokens=context["total_tokens"],
                                    style_reference=context.get("style_reference", {}))
            result = generation_service.run_task(
                project_id=project_id, task_type="去AI味软审", system=prompt["body"],
                messages=messages, prompt_id=prompt["prompt_id"], prompt_version=prompt["version"],
                context_snapshot=context_snapshot, temperature=0.2,
            )
            parsed = _parse_json(str(result.get("text") or "")) if result.get("ok") else None
            task_id = result.get("task_id")
            if isinstance(parsed, dict) and isinstance(parsed.get("建议"), list):
                ai_used = True  # A valid empty array means the model found no suggestions.
                lines = body.splitlines()
                occupied: dict[int, list[tuple[int, int]]] = {}
                entries = parsed["建议"]
                for index, item in enumerate(entries[:50]):
                    reason = ""
                    if not isinstance(item, dict):
                        reason = "建议格式无效"
                    else:
                        line = item.get("行")
                        if isinstance(line, str) and re.fullmatch(r"[0-9]+", line) and len(line) <= 8:
                            line = int(line)
                        original, replacement, issue = (item.get(key) for key in ("原文", "建议", "问题"))
                        if not isinstance(line, int) or isinstance(line, bool) or not 1 <= line <= len(lines):
                            reason = "行号无效或越出正文"
                        elif (not isinstance(original, str) or not original.strip()
                              or not isinstance(replacement, str) or not replacement.strip()
                              or not isinstance(issue, str) or not issue.strip()):
                            reason = "缺少原文、替换正文或问题说明"
                        elif any(char in original or char in replacement for char in ("\n", "\r", "\u2028", "\u2029")):
                            reason = "行级建议不能跨行"
                        elif original == replacement:
                            reason = "建议未产生变化"
                        else:
                            target = lines[line - 1]
                            start = target.find(original)
                            if start < 0 or target.find(original, start + 1) >= 0:
                                reason = "原文未在指定正文行唯一出现"
                            elif text.find(original, text.find(original) + 1) >= 0:
                                reason = "原文在全文出现多次，无法唯一应用精确替换"
                            else:
                                end = start + len(original)
                                if any(start < right and end > left for left, right in occupied.get(line, [])):
                                    reason = "原文与已有建议重复或重叠"
                                else:
                                    occupied.setdefault(line, []).append((start, end))
                                    suggestions.append({"line": line, "issue": issue.strip(),
                                                        "original": original, "replacement": replacement})
                    if reason:
                        rejected_suggestions.append({"index": index, "reason": reason})
                if len(entries) > 50:
                    rejected_suggestions.append({"index": 50, "reason": "建议超过 50 条处理上限",
                                                 "count": len(entries) - 50})
            else:
                diagnostic = _model_diagnostic(result, label="AI 软审未完成", invalid=True)
                ai_error, ai_error_code = diagnostic["ai_error"], diagnostic["ai_error_code"]
        except Exception as exc:
            ai_error = f"AI 软审异常：{type(exc).__name__}，尚未完成表达审查"
            ai_error_code = "REVIEW_EXCEPTION"

    proposal_ids: list[int] = []
    if create_proposals:
        from . import proposal_service

        with project_lock(project_id):
            source_changed = source_has_changed()
            if not source_changed:
                for item in suggestions:
                    proposal = proposal_service.create_proposal(
                        project_id=project_id, kind="deslop",
                        title=f"第 {item['line']} 行：{item['issue'][:20] or '去AI味建议'}",
                        target_path=chapter_rel, content=item["replacement"],
                        meta={"patch": item, "issue": item["issue"], "candidate_hash": digest,
                              "style_reference": context_snapshot.get("style_reference", {})},
                    )
                    proposal_ids.append(proposal["id"])
    else:
        with project_lock(project_id):
            source_changed = source_has_changed()

    return {
        "project_id": project_id,
        "chapter_rel": chapter_rel,
        "suggestions": suggestions,
        "rejected_suggestions": rejected_suggestions,
        "candidate_hash": digest,
        "source_hash": digest,
        "source_changed": source_changed,
        "ai_used": ai_used,
        "ai_error": ai_error,
        "ai_error_code": ai_error_code,
        "task_id": task_id,
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
    word_min, word_max = chapter_word_range(project_id)
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
            hard = run_hard_gates(text, project_dir=project_dir, rel_path=rel_path,
                                  min_words=word_min, max_words=word_max)
            by_key = {gate["key"]: gate for gate in hard["gates"]}
            cells = {label: _cell(bool(by_key.get(key, {}).get("passed")))
                     for label, key in (("字数门", "字数门"), ("语言门", "语言门"),
                                       ("禁词门", "禁词门"), ("去AI味", "去AI味门"),
                                       ("格式门", "格式门"))}
            cells["硬门禁总状态"] = _cell(hard["passed"])
        else:
            cells = {name: "未跑" for name in ("字数门", "语言门", "禁词门", "去AI味", "格式门", "硬门禁总状态")}

        review = latest_reviews.get(rel_path)
        if review and review.get("candidate_hash") and review["candidate_hash"] != \
                hashlib.sha256(text.encode("utf-8")).hexdigest():
            # A candidate-only review is not an acceptance of the on-disk draft.
            review = None
        if review and review.get("contract_hash"):
            current_contract = None
            try:
                current_contract = get_contract(project_id, rel_path)
            except NodeNotFoundError:
                pass
            if contract_fingerprint(current_contract) != review["contract_hash"]:
                review = None
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
        "columns": ["字数门", "语言门", "禁词门", "去AI味", "格式门", "硬门禁总状态", "一致性", "逐项审稿"],
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
    "contract_fingerprint",
    "list_debts",
    "list_reviews",
    "quality_matrix",
    "register_quality_debt",
    "resolve_debt",
    "review_chapter",
    "review_text",
    "run_hard_gates",
    "soft_deslop",
    "verify_state_changes",
]
