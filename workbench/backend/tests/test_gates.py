"""写作门禁单测（正反例）。

正例：规范中文散文 —— 对话用引号、破折号极少、无禁用词、无三连排比、无外语泄漏。
反例：三连排比 / 禁用词 / 破折号超限 / 对话密度过低 / HTML / 外语泄漏。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from workbench.backend import config, db
from workbench.backend.gates import (
    density_gate, language_gate, novel_format, outline_gate, prose_gate, setting_gate,
    tracking_gate,
)
from workbench.backend.services import (
    chat_tools, chat_workspace_tools, project_service, review_service,
)
from workbench.backend.services.chat_tools import ToolContext

# ── 正例：规范中文散文 ────────────────────────────────────────
GOOD_PROSE = """\
雨停的时候，巷口的灯刚亮。

「你去哪儿了？」她把伞收起来，靠在墙边。

「去了一趟码头。」他说，「船昨天夜里走的，货还在。」

她点点头，没再问。屋檐上的水一滴一滴落进铁桶，声音很密。巷子尽头有人在收摊，木板拖过地面，吱呀一声。

「明天还去吗？」她问。

「去。」他答得很短。

她把手插进外套口袋，摸到一张折了两回的纸。纸上是上个月的账，数字写得歪歪扭扭，末尾那笔还差三块二。她把纸递过去。

「少了三块二。」她说，「账要平，我不能含糊。」

他接过去看了很久，最后把纸折好，塞回她手里。

「后天补上。」他说。
"""

# ── 反例素材 ──────────────────────────────────────────────────
TRIPLE_ANAPHORA = "他不是害怕，不是怨恨，不是疲惫，只是累了。"

FORBIDDEN_WORD_TEXT = "他突然站起来，走到窗边。"

DASH_HEAVY = "他站住——风很大——灯灭了——巷子空了——他还在等。"

LOW_DIALOGUE_NARRATION = (
    "他沿着河堤走了很久。风从上游吹下来，带着水草的气味。"
    "堤岸的石头缝里长着草，草叶被踩得东倒西歪。"
    "远处的桥灯一盏接一盏亮起来，倒影在水里晃。"
    "他停下，摸出烟盒，发现只剩一根。他把烟点上，吸了一口。"
    "他把烟按在栏杆上，火星掉进水里，噗的一声就没了。"
    "河面上漂着一段木头，被水推着，转了个方向。"
    "「走吧。」他说。"
    "然后他继续往前走，直到看不见桥灯。"
)

HTML_TEXT = "<p>他站住，回头看了一眼。</p>"

FOREIGN_TEXT = "他打开 Alpha-7 的箱子，里面是空的。"


# ── 正例断言 ──────────────────────────────────────────────────
def test_density_gate_passes_good_prose() -> None:
    result = density_gate.run(GOOD_PROSE)

    assert result.passed is True, result.report()
    assert result.issues == []
    assert result.dash_count == 0
    assert result.quote_count >= 1
    assert result.dialog_ok is True
    assert result.forbidden_hits == {}
    assert result.triple_hits == []


def test_prose_gate_passes_good_prose() -> None:
    result = prose_gate.run(GOOD_PROSE)

    assert result.passed is True, result.report()
    assert result.failures == []
    assert result.han_count > 0


def test_language_gate_passes_good_prose() -> None:
    result = language_gate.run(GOOD_PROSE)

    assert result.passed is True, result.report()
    assert result.findings == []


# ── 反例：密度门禁 ────────────────────────────────────────────
def test_density_gate_blocks_triple_anaphora() -> None:
    result = density_gate.run(TRIPLE_ANAPHORA)

    assert result.passed is False
    assert result.triple_hits, result.report()
    assert density_gate.ISSUE_FAIL in result.issues


def test_density_gate_blocks_forbidden_words() -> None:
    result = density_gate.run(FORBIDDEN_WORD_TEXT)

    assert result.passed is False
    assert result.forbidden_hits == {"突然": 1}
    assert density_gate.ISSUE_FAIL in result.issues


def test_density_gate_blocks_dash_over_limit() -> None:
    result = density_gate.run(DASH_HEAVY)

    assert result.passed is False
    assert result.dash_count == 4
    assert result.dash_ok is False
    assert density_gate.ISSUE_FAIL in result.issues


def test_density_gate_flags_low_dialogue_density() -> None:
    result = density_gate.run(LOW_DIALOGUE_NARRATION)

    assert result.passed is False
    assert result.dialog_ok is False
    assert density_gate.ISSUE_DIALOG in result.issues


def test_density_gate_blocks_no_dialogue_at_all() -> None:
    result = density_gate.run("他沿着河堤走了很久，风从上游吹下来，带着水草的气味。")

    assert result.passed is False
    assert result.quote_count == 0
    assert density_gate.ISSUE_FAIL in result.issues


def test_density_gate_threshold_is_configurable() -> None:
    """阈值参数化生效：放宽破折号红线后同一文本可通过破折号项。"""
    strict = density_gate.run(DASH_HEAVY)
    relaxed = density_gate.run(DASH_HEAVY, dash_limit=1)

    assert strict.dash_ok is False
    assert relaxed.dash_ok is True


# ── 反例：prose 门禁 ─────────────────────────────────────────
def test_prose_gate_blocks_hard_stop() -> None:
    result = prose_gate.run("说白了，他不想去。")

    assert result.passed is False
    assert any("硬停词" in item for item in result.failures)


def test_prose_gate_blocks_jargon() -> None:
    result = prose_gate.run("我们要赋能每一位创作者，形成能力沉淀。")

    assert result.passed is False
    assert sum("黑话" in item for item in result.failures) >= 2


def test_prose_gate_blocks_road_sign() -> None:
    result = prose_gate.run("值得注意的是，天已经黑了。")

    assert result.passed is False
    assert any("模型路标" in item for item in result.failures)


def test_prose_gate_blocks_pivot_sentence() -> None:
    result = prose_gate.run("他不是不想去，而是没时间。")

    assert result.passed is False
    assert any("禁用翻案句" in item for item in result.failures)


# ── 反例 / 边界：语言门 ──────────────────────────────────────
def test_language_gate_blocks_html_markup() -> None:
    result = language_gate.run(HTML_TEXT)

    assert result.passed is False
    assert {f.type for f in result.findings} == {"forbidden-markup"}
    assert len(result.blocking) == 2  # <p> 与 </p>


def test_language_gate_blocks_foreign_letters() -> None:
    result = language_gate.run(FOREIGN_TEXT)

    assert result.passed is False
    assert {f.type for f in result.findings} == {"mixed-language"}
    assert result.findings[0].text == "Alpha-7"


def test_language_gate_whitelist_is_exact() -> None:
    """精确登记后豁免；未登记的子串式写法不豁免。"""
    allowed = language_gate.run(FOREIGN_TEXT, ["Alpha-7"])
    assert allowed.passed is True, allowed.report()

    not_allowed = language_gate.run(FOREIGN_TEXT, ["Alpha"])
    assert not_allowed.passed is False


def test_language_gate_protects_non_narrative_structures() -> None:
    """URL / 代码 / 文件名等明确非叙事结构由检测器机械保护，不算泄漏。"""
    text = "参考 https://example.com/a 与 `Alpha-7`，见 docs/readme.md。"

    result = language_gate.run(text)

    assert result.passed is True, result.report()


# ── 契约漂移检查（移植自源契约文件） ─────────────────────────
def test_check_contract_docs_flags_missing_needles(tmp_path: Path) -> None:
    doc = tmp_path / "weak.md"
    doc.write_text("只有语言门三个字。", encoding="utf-8")

    errors = language_gate.check_contract_docs(["weak.md"], root=tmp_path)

    assert errors, "缺少契约 needle 时应报错"
    assert any("HTML" in item for item in errors)


def test_check_contract_docs_passes_locked_contract(tmp_path: Path) -> None:
    doc = tmp_path / "locked.md"
    doc.write_text(
        "语言门为第一关；HTML 标签、注释与实体一律阻断；"
        "只机械保护明确非叙事结构；其他外语须精确登记。",
        encoding="utf-8",
    )

    assert language_gate.check_contract_docs(["locked.md"], root=tmp_path) == []


# ── 统一接口（import 调用） ──────────────────────────────────
def test_all_gates_expose_run_passed_report() -> None:
    for module in (density_gate, prose_gate, language_gate):
        result = module.run(GOOD_PROSE)
        assert hasattr(result, "passed")
        assert isinstance(result.report(), str)


# ── 小说正文格式门禁（番茄纯文本规范）─────────────────────────
def test_novel_format_passes_plain_prose() -> None:
    """合规纯正文（空行分段、引号成对、全角标点）通过。"""
    result = novel_format.run(GOOD_PROSE)

    assert result.passed is True, result.report()
    assert result.findings == []
    assert isinstance(result.report(), str)


@pytest.mark.parametrize(
    ("sample", "reason"),
    [
        ("# 第一章 起风\n\n他站着。\n", "Markdown 标题行"),
        ("他站着，**很冷**。\n", "Markdown 加粗"),
        ("他站着，*很冷*。\n", "Markdown 斜体"),
        ("- 甲\n- 乙\n", "Markdown 无序列表"),
        ("1. 甲\n2. 乙\n", "Markdown 有序列表"),
        ("> 他站着。\n", "Markdown 引用"),
        ("甲\n\n---\n\n乙\n", "Markdown 分隔线"),
        ("```\n甲\n```\n", "Markdown 代码围栏"),
        ("他站着`很冷`。\n", "行内代码"),
        ("见[链接](https://example.com)。\n", "Markdown 链接"),
        ("他站着<br>回头看。\n", "HTML 标签"),
        ("甲&nbsp;乙\n", "HTML 实体"),
    ],
)
def test_novel_format_blocks_markdown_leaks(sample: str, reason: str) -> None:
    result = novel_format.run(sample)

    assert result.passed is False, result.report()
    hits = [item for item in result.findings if item.reason == reason]
    assert hits, result.report()
    # 定位清单必须给出行号与命中片段
    assert hits[0].line >= 1
    assert hits[0].text


def test_novel_format_blocks_repeated_chapter_title_line() -> None:
    """正文里又写一遍「第12章 xxx」当标题 —— 标题以数据库为准。"""
    result = novel_format.run("第12章 起风\n\n他站在巷口。\n")

    assert result.passed is False
    assert any(item.reason == "正文重复章节标题行" for item in result.findings)


def test_novel_format_blocks_unpaired_quotes() -> None:
    result = novel_format.run("「你去哪儿了？\n\n他站在巷口。\n")

    assert result.passed is False
    hits = [item for item in result.findings if item.reason.startswith("成对引号不配对")]
    assert hits, result.report()
    assert hits[0].line == 1


def test_novel_format_blocks_halfwidth_punctuation() -> None:
    dots = novel_format.run("他站着...\n")
    dashes = novel_format.run("他站着--风很大。\n")

    assert dots.passed is False
    assert any("半角省略号" in item.reason for item in dots.findings)
    assert dashes.passed is False
    assert any("半角破折号" in item.reason for item in dashes.findings)


def test_novel_format_warns_long_block_without_blank_lines() -> None:
    """连续多行无空行只给建议级提示，不阻断。"""
    result = novel_format.run("甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲\n"
                              "乙乙乙乙乙乙乙乙乙乙乙乙乙乙乙乙乙乙乙乙\n"
                              "丙丙丙丙丙丙丙丙丙丙丙丙丙丙丙丙丙丙丙丙\n")

    assert result.passed is True, result.report()
    assert result.warnings
    assert result.warnings[0].line == 1


def test_novel_format_threshold_is_configurable() -> None:
    short_block = ("甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲\n"
                   "乙乙乙乙乙乙乙乙乙乙乙乙乙乙乙乙乙乙乙乙\n"
                   "丙丙丙丙丙丙丙丙丙丙丙丙丙丙丙丙丙丙丙丙\n")

    assert novel_format.run(short_block).warnings
    relaxed = novel_format.run(short_block, block_warn_lines=4)
    assert relaxed.warnings == []


# ── 硬门禁集成：格式门并入 run_hard_gates ─────────────────────
def test_run_hard_gates_includes_format_gate() -> None:
    result = review_service.run_hard_gates(GOOD_PROSE)

    keys = [gate["key"] for gate in result["gates"]]
    assert "格式门" in keys
    assert "记号泄漏" in keys
    format_gate = next(gate for gate in result["gates"] if gate["key"] == "格式门")
    assert format_gate["passed"] is True


def test_run_hard_gates_blocks_markdown_leak_with_locations() -> None:
    result = review_service.run_hard_gates(GOOD_PROSE + "\n# 第12章 起风\n")

    assert result["passed"] is False
    assert "格式门" in result["blocking_gates"]
    located = [item for item in result["locations"] if item["gate"] == "格式门"]
    assert located
    assert located[0]["line"] >= 1
    assert located[0]["text"]


# ── 确定性校验：账本算术与条目 ID（tracking_gate） ────────────
LEDGER_SAMPLE = (
    "第0012章｜前值 10 ＋ 变动 ＋5 ＝ 结余 15\n"      # 通过
    "第0013章｜10 ＋ 5 ＝ 20\n"                       # 不通过：应为 15
    "第0014章｜前值 12 两 ＋ 18 两 ＝ 结余 待核实\n"   # 无法解析（结余没写数）
)


def test_tracking_gate_ledger_arithmetic_reports_line_and_diff() -> None:
    result = tracking_gate.run_ledger(LEDGER_SAMPLE)

    assert result["key"] == "账本算术"
    assert result["passed"] is False
    assert [item["line"] for item in result["findings"]] == [2]
    finding = result["findings"][0]
    assert finding["expected"] == "15"
    assert finding["actual"] == "20"
    assert finding["diff"] == "5"
    assert result["unparsed"] == [3]
    assert result["detail"] == "检查 3 行：1 行不满足，1 行无法解析"


def test_tracking_gate_ledger_passes_balanced_rows() -> None:
    result = tracking_gate.run_ledger("第0012章｜前值 12 两 ＋ 收货款 18 两 － 购麻绳 0 两 ＝ 结余 30 两\n")

    assert result["passed"] is True, result["detail"]
    assert result["findings"] == []


def test_tracking_gate_run_aggregates_both_checks() -> None:
    text = LEDGER_SAMPLE + "FACT-0001 沈砚身份\nFACT-0001 沈砚身份（重复登记）\n"

    result = tracking_gate.run(text)

    assert result["key"] == "追踪校验"
    assert result["passed"] is False
    assert [check["key"] for check in result["checks"]] == ["账本算术", "条目ID"]


def test_tracking_gate_ids_unique_passes() -> None:
    result = tracking_gate.run_ids(
        "FACT-0001 沈砚身份（依据：第0003章）\nEVENT-0002 码头交货\nFX-01 铜印\n")

    assert result["key"] == "条目ID"
    assert result["passed"] is True, result["detail"]
    assert result["findings"] == []


def test_tracking_gate_ids_duplicate_fails() -> None:
    result = tracking_gate.run_ids("FORESHADOW-0007 铜印埋设\nFORESHADOW-0007 铜印（重复登记）\n")

    assert result["passed"] is False
    assert result["findings"][0]["line"] == 2
    assert "重复" in result["findings"][0]["message"]


# ── 确定性校验：大纲 / 章纲（outline_gate） ──────────────────
def test_outline_gate_flags_duplicate_chapter_number_as_error() -> None:
    result = outline_gate.run("第0001章 ｜ 交货\n第0001章 ｜ 收货\n")

    assert result["key"] == "大纲校验"
    assert result["passed"] is False
    errors = [item for item in result["findings"] if item["severity"] == "error"]
    assert len(errors) == 1
    assert errors[0]["line"] == 2
    assert "章号重复" in errors[0]["message"]


def test_outline_gate_flags_chapter_gap_as_warn() -> None:
    result = outline_gate.run("第0001章 ｜ 交货\n第0004章 ｜ 收货\n")

    assert result["passed"] is True
    warns = [item for item in result["findings"] if item["severity"] == "warn"]
    assert warns and warns[0]["line"] == 2
    assert "跳号" in warns[0]["message"]


def test_outline_gate_flags_chapter_line_without_event() -> None:
    result = outline_gate.run("第0001章 ｜\n第0002章 ｜ 交货\n")

    assert result["passed"] is False
    assert result["findings"][0]["line"] == 1
    assert "事件" in result["findings"][0]["message"]


def test_outline_gate_warns_unpaired_volume_goal() -> None:
    result = outline_gate.run("## 第1卷 起风\n- 卷级目标：攒够三十两赎身\n")

    assert result["passed"] is True
    assert [item["severity"] for item in result["findings"]] == ["warn"]
    assert "钩子" in result["findings"][0]["message"]


# ── 确定性校验：设定卡字段与来源分级（setting_gate） ─────────
CARD_MISSING_FIELD = (
    "## 沈砚\n"
    "- 姓名：沈砚\n"
    "- 身份：据点跑货人\n"
    "- 来源：author（作者在第 3 次对话中确认）\n"
)

CARD_NO_SOURCE = (
    "## 老周\n"
    "- 姓名：老周\n"
    "- 别名：老周头\n"
    "- 身份：码头管事\n"
    "- 目标：还清船钱\n"
    "- 动机：给儿子还债\n"
    "- 能力边界：会看水路，打不过镖师\n"
    "- 关系：与沈砚是旧交\n"
    "- 语言习惯：短句\n"
    "- 已知信息边界：不知道货主是谁\n"
    "- 状态锚点：见 状态/角色状态.md\n"
)


def test_setting_gate_flags_missing_required_field_as_error() -> None:
    result = setting_gate.run(CARD_MISSING_FIELD, category="人物")

    assert result["key"] == "设定校验"
    assert result["passed"] is False
    errors = [item for item in result["findings"] if item["severity"] == "error"]
    assert any("能力边界" in item["message"] for item in errors)
    assert {item["line"] for item in errors} == {1}


def test_setting_gate_warns_missing_source_grade() -> None:
    result = setting_gate.run(CARD_NO_SOURCE, category="人物")

    assert result["passed"] is True
    assert [item["severity"] for item in result["findings"]] == ["warn"]
    assert "来源分级" in result["findings"][0]["message"]


def test_setting_gate_requires_known_category() -> None:
    empty = setting_gate.run(CARD_MISSING_FIELD, category="")
    unknown = setting_gate.run(CARD_MISSING_FIELD, category="阵法")

    assert empty["passed"] is False and "人物" in empty["detail"]
    assert unknown["passed"] is False and "人物" in unknown["detail"]


# ── 工具层：check_tracking（两条对话路径） ───────────────────
LEDGER_FILE = (
    "---\n标题: 资源账本\n---\n"
    "第0012章｜前值 10 ＋ 变动 ＋5 ＝ 结余 15\n"
    "第0013章｜10 ＋ 5 ＝ 20\n"
)


@pytest.fixture()
def book(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "books")
    monkeypatch.setattr(config, "RUNTIME_DIR", tmp_path / ".workbench")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / ".workbench" / "test.db")
    db.init_db()
    config.ensure_runtime_dirs()
    project = project_service.create_project(name="校验小说")
    _row, root = project_service.get_project_dir(project["id"])
    (root / "状态").mkdir(exist_ok=True)
    (root / "状态" / "资源账本.md").write_text(LEDGER_FILE, encoding="utf-8")
    (root / "设定").mkdir(exist_ok=True)
    (root / "设定" / "人物设定.md").write_text(CARD_MISSING_FIELD, encoding="utf-8")
    return SimpleNamespace(id=project["id"], root=root)


def test_check_tracking_tool_native(book: SimpleNamespace) -> None:
    ctx = {"project_id": book.id, "read_hashes": {}}

    result = chat_workspace_tools.execute("check_tracking", {"target": "账本"}, ctx)
    assert result["ok"] is True
    assert result["data"]["key"] == "追踪校验"
    assert result["data"]["passed"] is False
    assert [check["key"] for check in result["data"]["checks"]] == ["账本算术", "条目ID"]
    assert result["data"]["checks"][0]["findings"][0]["line"] == 5  # 前 3 行是 frontmatter

    setting = chat_workspace_tools.execute(
        "check_tracking", {"target": "设定", "category": "人物"}, ctx)
    assert setting["ok"] is True
    assert setting["data"]["key"] == "设定校验"
    assert any("能力边界" in item["message"] for item in setting["data"]["findings"])

    # 只给 rel_path：按文件名推断分类
    inferred = chat_workspace_tools.execute(
        "check_tracking", {"target": "设定", "rel_path": "设定/人物设定.md"}, ctx)
    assert inferred["ok"] is True
    assert inferred["data"]["category"] == "人物"

    # 用法错误：target=设定 既没 category 也没 rel_path
    assert chat_workspace_tools.execute("check_tracking", {"target": "设定"}, ctx)["ok"] is False

    # 越界路径被拒
    escaped = chat_workspace_tools.execute(
        "check_tracking", {"target": "账本", "rel_path": "../../x.md"}, ctx)
    assert escaped["ok"] is False


def test_check_tracking_tool_legacy(book: SimpleNamespace) -> None:
    ctx = ToolContext(project_id=book.id, session_id=1)

    result = chat_tools.execute("check_tracking", {"target": "账本"}, ctx)
    assert result["ok"] is True
    assert result["data"]["key"] == "追踪校验"
    assert "账本算术" in result["text"]

    assert chat_tools.execute(
        "check_tracking", {"target": "账本", "rel_path": "../../x.md"}, ctx)["ok"] is False
    assert chat_tools.execute("check_tracking", {"target": "设定"}, ctx)["ok"] is False
