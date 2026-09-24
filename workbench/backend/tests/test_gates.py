"""写作门禁单测（正反例）。

正例：规范中文散文 —— 对话用引号、破折号极少、无禁用词、无三连排比、无外语泄漏。
反例：三连排比 / 禁用词 / 破折号超限 / 对话密度过低 / HTML / 外语泄漏。
"""

from __future__ import annotations

from pathlib import Path

from workbench.backend.gates import density_gate, language_gate, prose_gate

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
