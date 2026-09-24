"""拆书资产库（Task 44 / P1）：导入拆解目标 → 自定维度拆解 + 双时间线 + 事实卡 → 按题材召回。

合规前提
--------
拆解维度是**工作台自定**的六维（非复制任何外部项目文本，见 `docs/third-party-notices.md`）：
1. 开篇钩子（前 300 字给冲突的时间点）
2. 情绪节拍（每约 500 字一个可命名的节拍）
3. 主角行动线（决定与代价）
4. 信息节奏（伏笔埋设与揭示位置）
5. 语言特征（句长、对话占比、口语化程度）
6. 可迁移手法（能用于本书的具体写法）

硬性纪律
--------
- **事实卡必须附章节依据**；没有依据的条目显式写「依据缺失」，**不补写、不编造**；
- 事实源是 Markdown：落 ``projects/{书名}/拆书/{目标书名}/`` 下的
  ``原文.md`` / ``拆解.md`` / ``双时间线.md`` / ``事实卡.md``；
- 结构化事实同步进 ``assets`` 索引（kind=``teardown_fact``），**可重建**；
- 召回按**题材匹配**注入上下文（见 :func:`recall_for_genre` 与 ContextService 第 4 级）。
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from .. import db
from . import generation_service, operation_log, prompt_registry_service
from .errors import InvalidNameError, InvalidOperationError, NodeNotFoundError
from .fs_utils import atomic_write_text, compose_document, read_text, split_frontmatter, validate_node_name
from .project_service import get_project_dir
from .snapshot_service import snapshot_file

TEARDOWN_DIR = "拆书"
SOURCE_FILE = "原文.md"
DIMENSION_FILE = "拆解.md"
TIMELINE_FILE = "双时间线.md"
FACT_FILE = "事实卡.md"

# 自定六维（工作台原创表述）
DIMENSIONS: tuple[dict, ...] = (
    {"key": "开篇钩子", "ask": "前 300 字做了什么、第几段给出冲突、用什么抓人"},
    {"key": "情绪节拍", "ask": "每约 500 字一个可命名的节拍（爽/憋/惊/暖），节拍如何交替"},
    {"key": "主角行动线", "ask": "主角做了哪些决定、付出什么代价、目标如何升级"},
    {"key": "信息节奏", "ask": "伏笔埋在哪、何时揭示、读者何时比角色先知道"},
    {"key": "语言特征", "ask": "句长分布、对话占比、口语化程度、标志性用词"},
    {"key": "可迁移手法", "ask": "能直接用于本书的写法，1-3 条，附可复制的结构而非句子"},
)

_HEADING_RE = re.compile(r"^(#{1,4})\s*([^\n#]{1,60})\s*$", re.MULTILINE)
_CHAPTER_RE = re.compile(r"(?:^|\n)\s*(?:#{1,4}\s*)?(第\s*[0-9一二三四五六七八九十百零两]+\s*[章节][^\n]{0,40})")
_SENTENCE_RE = re.compile(r"[。！？!?…]+")
_DIALOG_RE = re.compile(r"[“\"]([^”\"]*)[”\"]")


# ─────────────────────────── 路径 ───────────────────────────


def teardown_root(project_dir: Path) -> Path:
    return Path(project_dir) / TEARDOWN_DIR


def target_dir(project_dir: Path, target: str) -> Path:
    return teardown_root(project_dir) / validate_node_name(target)


def _meta(meta: dict, body: str) -> str:
    return compose_document(meta, body)


def _read(project_dir: Path, target: str, name: str) -> str:
    path = target_dir(project_dir, target) / name
    if not path.is_file():
        return ""
    try:
        return read_text(path)
    except (OSError, UnicodeDecodeError):
        return ""


def _write(project_id: int, project_dir: Path, project_name: str, target: str,
           name: str, text: str) -> None:
    rel = f"{TEARDOWN_DIR}/{target}/{name}"
    snapshot_file(project_id, project_dir, project_name, rel, reason="teardown")
    atomic_write_text(target_dir(project_dir, target) / name, text)


# ─────────────────────────── 导入目标 ───────────────────────────


def import_target(
    project_id: int,
    *,
    target_name: str,
    content: str,
    genre: str = "",
    source_note: str = "",
) -> dict:
    """导入拆解目标（粘贴文本或上传的样章）；原文落 ``拆书/{目标}/原文.md``。"""
    if not (content or "").strip():
        raise InvalidOperationError("导入内容为空")
    try:
        clean = validate_node_name(target_name)
    except InvalidNameError as exc:
        raise InvalidOperationError(f"目标书名不合法：{exc.message}") from exc

    row, project_dir = get_project_dir(project_id)
    directory = target_dir(project_dir, clean)
    directory.mkdir(parents=True, exist_ok=True)

    meta = {
        "标题": clean,
        "题材": genre or "",
        "来源说明": source_note or "",
        "导入时间": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "字数": len(re.sub(r"\s+", "", content)),
    }
    _write(project_id, project_dir, row["name"], clean, SOURCE_FILE,
           _meta(meta, content.strip() + "\n"))
    operation_log.log(project_id, "teardown-import", f"{TEARDOWN_DIR}/{clean}/{SOURCE_FILE}",
                      {"genre": genre, "chars": meta["字数"]})
    return {"target": clean, "genre": genre, "chars": meta["字数"],
            "path": f"{TEARDOWN_DIR}/{clean}/{SOURCE_FILE}"}


def list_targets(project_id: int) -> list[dict]:
    """已导入的拆解目标（含题材与产出状态）。"""
    _row, project_dir = get_project_dir(project_id)
    root = teardown_root(project_dir)
    if not root.is_dir():
        return []
    targets: list[dict] = []
    for path in sorted(root.iterdir()):
        if not path.is_dir():
            continue
        source = path / SOURCE_FILE
        if not source.is_file():
            continue
        meta, body = split_frontmatter(read_text(source))
        targets.append(
            {
                "target": path.name,
                "genre": str(meta.get("题材") or ""),
                "chars": int(meta.get("字数") or len(re.sub(r"\s+", "", body))),
                "imported_at": str(meta.get("导入时间") or ""),
                "has_analysis": (path / DIMENSION_FILE).is_file(),
                "has_facts": (path / FACT_FILE).is_file(),
                "fact_count": len(list_facts(project_id, path.name)),
            }
        )
    return targets


# ─────────────────────────── 确定性拆解（零 LLM 兜底） ───────────────────────────


def _split_chapters(text: str) -> list[tuple[str, str]]:
    """按「第N章」或 Markdown 标题切章；无标记则整体作一章。"""
    body = split_frontmatter(text)[1]
    matches = list(_CHAPTER_RE.finditer(body))
    if not matches:
        return [("（未分章）", body.strip())] if body.strip() else []
    chapters: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        chapters.append((match.group(1).strip(), body[start:end].strip()))
    return chapters


def _stats(segment: str) -> dict:
    text = split_frontmatter(segment)[1]
    han = len(re.findall(r"[\u4e00-\u9fff]", text))
    sentences = [item for item in _SENTENCE_RE.split(text) if item.strip()]
    lengths = [len(re.findall(r"[\u4e00-\u9fff]", item)) for item in sentences] or [0]
    dialog_chars = sum(len(item) for item in _DIALOG_RE.findall(text))
    paragraphs = [item for item in re.split(r"\n\s*\n", text) if item.strip()]
    return {
        "han": han,
        "paragraphs": len(paragraphs),
        "sentences": len(sentences),
        "sentence_len": round(sum(lengths) / len(lengths), 1),
        "dialog_ratio": round(dialog_chars / han, 3) if han else 0.0,
        "dash": text.count("——"),
        "ellipsis": text.count("…"),
        "numbers": len(re.findall(r"\d+", text)),
        "first_sentence": (sentences[0].strip() if sentences else "")[:60],
    }


def analyze_deterministic(text: str) -> dict:
    """确定性拆解：只做**统计与结构**结论（不做语义判断），全部可复核。"""
    chapters = _split_chapters(text)
    overall = _stats(text)
    per_chapter = [{"chapter": name, **_stats(body)} for name, body in chapters]

    first = per_chapter[0] if per_chapter else {"chapter": "", **_stats("")}
    opening_evidence = first.get("first_sentence", "")
    opening = (
        f"开篇首句「{opening_evidence}」；全章 {first.get('han', 0)} 字、"
        f"{first.get('paragraphs', 0)} 段、对话占比 {int(first.get('dialog_ratio', 0) * 100)}%。"
        if opening_evidence
        else "未取到开篇文本"
    )

    beats = [
        f"{item['chapter']}：{item['han']} 字 / {item['paragraphs']} 段 / 句长均值 {item['sentence_len']}"
        for item in per_chapter[:6]
    ]
    language = (
        f"整篇 {overall['han']} 字，句长均值 {overall['sentence_len']}，"
        f"对话占比 {int(overall['dialog_ratio'] * 100)}%，破折号 {overall['dash']} 处、"
        f"省略号 {overall['ellipsis']} 处、数字 {overall['numbers']} 处。"
    )

    facts = [
        {
            "dimension": "开篇钩子",
            "conclusion": opening,
            "evidence": first.get("chapter") or "（未分章）",
            "quote": opening_evidence,
        },
        {
            "dimension": "情绪节拍",
            "conclusion": "按章统计的字数/段数/句长（节拍命名需人工或模型补充）",
            "evidence": "；".join(beats) or "依据缺失",
            "quote": "",
        },
        {
            "dimension": "语言特征",
            "conclusion": language,
            "evidence": "整篇统计",
            "quote": "",
        },
    ]
    return {
        "chapters": [item["chapter"] for item in per_chapter],
        "per_chapter": per_chapter,
        "overall": overall,
        "facts": facts,
        "source": "deterministic",
    }


# ─────────────────────────── 模型拆解（可选增强） ───────────────────────────


def _analyze_ai(project_id: int, text: str) -> dict | None:
    prompt = prompt_registry_service.get_prompt("novel.teardown.dimensions")
    dimensions_text = "\n".join(f"{index + 1}. {item['key']}：{item['ask']}"
                               for index, item in enumerate(DIMENSIONS))
    result = generation_service.run_task(
        project_id=project_id,
        task_type="拆书",
        system=prompt["body"] + "\n\n本次拆解维度（工作台自定）：\n" + dimensions_text,
        messages=[{"role": "user", "content": split_frontmatter(text)[1][:30000]}],
        prompt_id=prompt["prompt_id"],
        prompt_version=prompt["version"],
        temperature=0.4,
    )
    if not result["ok"]:
        return None
    from .review_service import _parse_json

    parsed = _parse_json(result["text"])
    if not parsed:
        return None
    entries = parsed.get("维度") or []
    facts = []
    for item in entries:
        if not isinstance(item, dict) or not item.get("名称"):
            continue
        evidence = str(item.get("章节依据") or "").strip()
        quote = str(item.get("原文摘录") or "").strip()
        facts.append(
            {
                "dimension": str(item["名称"]),
                "conclusion": str(item.get("结论") or "").strip(),
                # 纪律：没有依据时显式标注缺失，不补写
                "evidence": evidence or "依据缺失",
                "quote": quote,
            }
        )
    if not facts:
        return None
    return {"facts": facts, "source": "model"}


# ─────────────────────────── 拆解主流程 ───────────────────────────


def analyze(
    project_id: int,
    target: str,
    *,
    use_ai: bool = True,
    extra_note: str = "",
) -> dict:
    """拆解目标样章：产出 拆解.md / 双时间线.md / 事实卡.md，并同步事实索引。"""
    row, project_dir = get_project_dir(project_id)
    source = _read(project_dir, target, SOURCE_FILE)
    if not source.strip():
        raise NodeNotFoundError(f"未找到拆解目标原文：{TEARDOWN_DIR}/{target}/{SOURCE_FILE}")
    meta, body = split_frontmatter(source)
    genre = str(meta.get("题材") or "")

    deterministic = analyze_deterministic(source)
    ai = _analyze_ai(project_id, source) if use_ai else None
    facts = list(ai["facts"]) if ai else list(deterministic["facts"])
    seen = {(item["dimension"], item["evidence"]) for item in facts}

    # 模型结果缺少的维度用确定性结论补齐（保证六维齐备且可复核）
    for item in deterministic["facts"]:
        if item["dimension"] not in {fact["dimension"] for fact in facts}:
            if (item["dimension"], item["evidence"]) not in seen:
                facts.append(item)

    missing = [item for item in facts if item["evidence"] == "依据缺失"]
    for item in DIMENSIONS:
        if item["key"] not in {fact["dimension"] for fact in facts}:
            facts.append(
                {
                    "dimension": item["key"],
                    "conclusion": "本轮未产出结论（AI 不可用或样章不含对应特征）",
                    "evidence": "依据缺失",
                    "quote": "",
                }
            )
    # 补齐全维度后重新统计，保证与 list_facts / API 的口径一致
    missing = [item for item in facts if item["evidence"] == "依据缺失"]

    # 1) 拆解.md
    lines = [
        f"# {target} · 六维拆解",
        "",
        f"- 题材：{genre or '（未标注）'}",
        f"- 来源：{'模型 + 确定性统计' if ai else '确定性统计（AI 不可用，已降级）'}",
        f"- 拆解时间：{time.strftime('%Y-%m-%dT%H:%M:%S')}",
        "",
        "> 维度为工作台自定（开篇钩子 / 情绪节拍 / 主角行动线 / 信息节奏 / 语言特征 / 可迁移手法）。",
        "",
    ]
    for item in DIMENSIONS:
        entries = [fact for fact in facts if fact["dimension"] == item["key"]]
        lines.append(f"## {item['key']}")
        lines.append("")
        lines.append(f"关注点：{item['ask']}")
        lines.append("")
        for fact in entries:
            lines.append(f"- 结论：{fact['conclusion']}")
            lines.append(f"  - 章节依据：{fact['evidence']}")
            if fact["quote"]:
                lines.append(f"  - 原文摘录：{fact['quote']}")
        lines.append("")
    if extra_note.strip():
        lines += ["## 作者补注", "", extra_note.strip(), ""]

    # 2) 双时间线.md
    timeline_lines = [
        f"# {target} · 双时间线",
        "",
        "> 双时间线 = 事件时间线（故事内发生顺序）与揭示时间线（读者获知顺序）。",
        "> 两条线的错位处，就是悬念与反转的设计位置。",
        "",
        "## 事件时间线（按章节顺序）",
        "",
        "| 章节 | 字数 | 段数 | 句长均值 | 对话占比 | 首句 |",
        "|---|---|---|---|---|---|",
    ]
    for item in deterministic["per_chapter"]:
        timeline_lines.append(
            f"| {item['chapter']} | {item['han']} | {item['paragraphs']} |"
            f" {item['sentence_len']} | {int(item['dialog_ratio'] * 100)}% |"
            f" {item['first_sentence'][:24]} |"
        )
    timeline_lines += [
        "",
        "## 揭示时间线",
        "",
        "- 剖白式信息（读者与角色同时知道）与读者先知情的段落，需人工或模型标注；",
        "- 当前可确定的揭示点：每章首句（见上表「首句」列）与对话中的反问/否认句；",
        "- 本次未产出揭示点清单时，以「依据缺失」记录，不补写。",
        "",
    ]

    # 3) 事实卡.md
    fact_lines = [
        f"# {target} · 事实卡",
        "",
        f"- 题材：{genre or '（未标注）'}",
        f"- 条数：{len(facts)}（其中依据缺失 {len(missing)} 条）",
        "",
        "| 维度 | 结论 | 章节依据 | 原文摘录 |",
        "|---|---|---|---|",
    ]
    for fact in facts:
        fact_lines.append(
            f"| {fact['dimension']} | {fact['conclusion'][:120] or '—'} |"
            f" {fact['evidence'] or '依据缺失'} | {fact['quote'][:60] or '—'} |"
        )
    fact_lines += [
        "",
        "> 纪律：事实卡条目必须附章节依据；无法取证的一律标注「依据缺失」，不补写、不编造。",
        "",
    ]

    _write(project_id, project_dir, row["name"], target, DIMENSION_FILE,
           _meta({"标题": f"{target}·拆解", "题材": genre}, "\n".join(lines)))
    _write(project_id, project_dir, row["name"], target, TIMELINE_FILE,
           _meta({"标题": f"{target}·双时间线", "题材": genre}, "\n".join(timeline_lines)))
    _write(project_id, project_dir, row["name"], target, FACT_FILE,
           _meta({"标题": f"{target}·事实卡", "题材": genre, "条数": len(facts)},
                 "\n".join(fact_lines)))

    _sync_index(project_id, target, genre, facts)
    operation_log.log(project_id, "teardown-analyze",
                      f"{TEARDOWN_DIR}/{target}/{FACT_FILE}",
                      {"facts": len(facts), "missing": len(missing),
                       "source": "model" if ai else "deterministic"})
    return {
        "target": target,
        "genre": genre,
        "dimensions": [item["key"] for item in DIMENSIONS],
        "facts": facts,
        "fact_count": len(facts),
        "missing_evidence": len(missing),
        "source": "model" if ai else "deterministic",
        "files": [f"{TEARDOWN_DIR}/{target}/{name}"
                  for name in (DIMENSION_FILE, TIMELINE_FILE, FACT_FILE)],
    }


# ─────────────────────────── 索引与读取 ───────────────────────────


def _sync_index(project_id: int, target: str, genre: str, facts: list[dict]) -> None:
    with db.get_conn() as conn:
        conn.execute(
            "DELETE FROM assets WHERE project_id = ? AND kind = 'teardown_fact' AND path = ?",
            (project_id, target),
        )
        for fact in facts:
            conn.execute(
                "INSERT INTO assets (project_id, kind, name, path, meta)"
                " VALUES (?, 'teardown_fact', ?, ?, ?)",
                (
                    project_id,
                    f"{target}·{fact['dimension']}",
                    target,
                    json.dumps({"genre": genre, **fact}, ensure_ascii=False),
                ),
            )


def list_facts(project_id: int, target: str | None = None) -> list[dict]:
    """事实卡条目（附目标与题材；依据缺失条目保留并标注）。"""
    sql = ("SELECT name, path, meta FROM assets"
           " WHERE project_id = ? AND kind = 'teardown_fact'")
    params: list = [project_id]
    if target:
        sql += " AND path = ?"
        params.append(target)
    sql += " ORDER BY id ASC"
    with db.get_conn() as conn:
        rows = conn.execute(sql, tuple(params)).fetchall()
    facts: list[dict] = []
    for row in rows:
        try:
            meta = json.loads(row["meta"]) if row["meta"] else {}
        except (json.JSONDecodeError, TypeError):
            meta = {}
        facts.append({"target": row["path"], "genre": meta.get("genre", ""), **meta})
    return facts


def read_artifact(project_id: int, target: str, kind: str) -> dict:
    """读取拆解产物（kind ∈ source/analysis/timeline/facts）。"""
    _row, project_dir = get_project_dir(project_id)
    mapping = {
        "source": SOURCE_FILE,
        "analysis": DIMENSION_FILE,
        "timeline": TIMELINE_FILE,
        "facts": FACT_FILE,
    }
    if kind not in mapping:
        raise InvalidOperationError(
            f"未知产物类型：{kind}（可选：{'/'.join(mapping)}）"
        )
    text = _read(project_dir, target, mapping[kind])
    if not text.strip():
        raise NodeNotFoundError(f"产物不存在：{TEARDOWN_DIR}/{target}/{mapping[kind]}")
    meta, body = split_frontmatter(text)
    return {"target": target, "kind": kind, "meta": meta, "content": body}


def delete_target(project_id: int, target: str) -> dict:
    row, project_dir = get_project_dir(project_id)
    directory = target_dir(project_dir, target)
    if not directory.is_dir():
        raise NodeNotFoundError(f"拆解目标不存在：{target}")
    import shutil

    shutil.rmtree(directory, ignore_errors=True)
    with db.get_conn() as conn:
        conn.execute(
            "DELETE FROM assets WHERE project_id = ? AND kind = 'teardown_fact' AND path = ?",
            (project_id, target),
        )
    operation_log.log(project_id, "teardown-delete", f"{TEARDOWN_DIR}/{target}", None)
    return {"target": target, "deleted": True, "project": row["name"]}


# ─────────────────────────── 按题材召回 ───────────────────────────


def recall_for_genre(
    project_id: int,
    *,
    genre: str = "",
    limit: int = 6,
    dimension: str = "",
) -> dict:
    """按题材召回事实卡（供 ContextService 注入）。

    题材匹配规则：目标题材与本书题材有交集（包含关系）即命中；本书未填题材时全部命中。
    只召回**有章节依据**的条目，依据缺失的条目仅供人工查看（不进上下文）。
    """
    facts = list_facts(project_id)
    if not facts:
        return {"genre": genre, "matched": 0, "facts": []}

    def matches(fact: dict) -> bool:
        if dimension and fact.get("dimension") != dimension:
            return False
        fact_genre = str(fact.get("genre") or "")
        if not genre or not fact_genre:
            return True
        return genre in fact_genre or fact_genre in genre or \
            bool(set(re.split(r"[/、,，]", genre)) & set(re.split(r"[/、,，]", fact_genre)))

    picked = [
        fact for fact in facts
        if matches(fact) and str(fact.get("evidence") or "") not in ("", "依据缺失")
    ]
    # 可迁移手法优先（对本书写作最有用）
    picked.sort(key=lambda item: (item.get("dimension") != "可迁移手法",
                                  item.get("dimension") != "开篇钩子"))
    picked = picked[:limit]
    return {
        "genre": genre,
        "matched": len(picked),
        "facts": [
            {
                "target": fact.get("target", ""),
                "dimension": fact.get("dimension", ""),
                "conclusion": fact.get("conclusion", ""),
                "evidence": fact.get("evidence", ""),
                "quote": fact.get("quote", ""),
            }
            for fact in picked
        ],
    }


def recall_block(project_id: int, *, genre: str = "", limit: int = 6) -> str:
    """把召回结果渲染成可注入文本（无命中时返回空串）。"""
    result = recall_for_genre(project_id, genre=genre, limit=limit)
    if not result["facts"]:
        return ""
    lines = [f"【拆书对标（同题材：{genre or '未标注'}）】"]
    for fact in result["facts"]:
        lines.append(
            f"- [{fact['target']}·{fact['dimension']}] {fact['conclusion']}"
            f"（依据：{fact['evidence']}）"
        )
    return "\n".join(lines)


__all__ = [
    "DIMENSIONS",
    "FACT_FILE",
    "TEARDOWN_DIR",
    "analyze",
    "analyze_deterministic",
    "delete_target",
    "import_target",
    "list_facts",
    "list_targets",
    "read_artifact",
    "recall_block",
    "recall_for_genre",
    "target_dir",
]