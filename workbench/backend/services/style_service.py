"""文风指纹：从**作者认可**的章节采样，作为去AI味软审的参照锚点。

采样指标（全部确定性计算，零 LLM）：句长分布（均值/变异系数）、对话占比、
标点密度（破折号/省略号/问号）、数字密度、高频双字词。
"""

from __future__ import annotations

import json
import re
import statistics
from collections import Counter

from .. import db
from .chapter_service import read_chapter
from .errors import NodeNotFoundError
from .fs_utils import split_frontmatter

_SENTENCE_SPLIT_RE = re.compile(r"[。！？!?…]+")
_DIALOG_RE = re.compile(r"[“\"]([^”\"]*)[”\"]")
_HAN_RE = re.compile(r"[\u4e00-\u9fff]")
_WORD_RE = re.compile(r"[\u4e00-\u9fff]{2}")


def compute_fingerprint(text: str) -> dict:
    """计算文本的文风指纹（对正文，不含 frontmatter）。"""
    body = split_frontmatter(text or "")[1]
    han = len(_HAN_RE.findall(body))
    sentences = [item.strip() for item in _SENTENCE_SPLIT_RE.split(body) if item.strip()]
    lengths = [len(_HAN_RE.findall(item)) for item in sentences]
    dialog_chars = sum(len(item) for item in _DIALOG_RE.findall(body))

    def density(pattern: str, unit: int = 1000) -> float:
        count = body.count(pattern)
        return round(count * unit / han, 2) if han else 0.0

    return {
        "han": han,
        "sentences": len(sentences),
        "sentence_len_mean": round(statistics.fmean(lengths), 2) if lengths else 0.0,
        "sentence_len_stdev": round(statistics.pstdev(lengths), 2) if len(lengths) > 1 else 0.0,
        "sentence_len_cv": (
            round(statistics.pstdev(lengths) / statistics.fmean(lengths), 3)
            if len(lengths) > 1 and statistics.fmean(lengths) else 0.0
        ),
        "dialog_ratio": round(dialog_chars / han, 3) if han else 0.0,
        "dialog_count": len(_DIALOG_RE.findall(body)),
        "dash_per_1000": density("——"),
        "ellipsis_per_1000": density("…"),
        "question_per_1000": density("？"),
        "number_per_1000": len(re.findall(r"\d+", body)) * 1000 / han if han else 0.0,
        "top_bigrams": [
            {"word": word, "count": count}
            for word, count in Counter(_WORD_RE.findall(body)).most_common(15)
        ],
    }


def sample(
    project_id: int,
    rel_path: str,
    *,
    approved: bool = True,
    note: str = "",
) -> dict:
    """对某章采样指纹并落库（``approved=True`` 表示作者认可，作为软审参照）。"""
    detail = read_chapter(project_id, rel_path)
    fingerprint = compute_fingerprint(detail["content"])
    fingerprint["note"] = note
    with db.get_conn() as conn:
        cursor = conn.execute(
            "INSERT INTO style_fingerprints (project_id, rel_path, approved, payload)"
            " VALUES (?, ?, ?, ?)",
            (project_id, rel_path, 1 if approved else 0,
             json.dumps(fingerprint, ensure_ascii=False)),
        )
        fingerprint_id = int(cursor.lastrowid or 0)
    return {"id": fingerprint_id, "rel_path": rel_path, "approved": approved,
            **fingerprint}


def list_fingerprints(project_id: int, approved_only: bool = False) -> list[dict]:
    sql = ("SELECT id, rel_path, approved, payload, created_at FROM style_fingerprints"
           " WHERE project_id = ?")
    params: list = [project_id]
    if approved_only:
        sql += " AND approved = 1"
    sql += " ORDER BY id DESC LIMIT 100"
    with db.get_conn() as conn:
        rows = conn.execute(sql, tuple(params)).fetchall()
    result = []
    for row in rows:
        try:
            payload = json.loads(row["payload"])
        except (json.JSONDecodeError, TypeError):
            payload = {}
        result.append(
            {
                "id": int(row["id"]),
                "rel_path": row["rel_path"],
                "approved": bool(row["approved"]),
                "created_at": row["created_at"],
                **payload,
            }
        )
    return result


def reference_metrics(project_id: int) -> dict | None:
    """认可样本的聚合参照（供软审比对）。"""
    samples = [item for item in list_fingerprints(project_id, approved_only=True)
               if item.get("han")]
    if not samples:
        return None

    def mean(key: str) -> float:
        values = [float(item.get(key) or 0) for item in samples]
        return round(sum(values) / len(values), 3) if values else 0.0

    words: Counter = Counter()
    for item in samples:
        for entry in item.get("top_bigrams") or []:
            words[str(entry.get("word"))] += int(entry.get("count") or 0)
    return {
        "samples": len(samples),
        "sentence_len_mean": mean("sentence_len_mean"),
        "sentence_len_cv": mean("sentence_len_cv"),
        "dialog_ratio": mean("dialog_ratio"),
        "dash_per_1000": mean("dash_per_1000"),
        "ellipsis_per_1000": mean("ellipsis_per_1000"),
        "top_bigrams": [{"word": word, "count": count}
                        for word, count in words.most_common(15)],
    }


def compare(project_id: int, text: str) -> dict | None:
    """把候选文本与参照样本比对，返回偏差提示（软审用，不阻断）。"""
    reference = reference_metrics(project_id)
    if reference is None:
        return None
    current = compute_fingerprint(text)
    deltas = {
        key: round(float(current.get(key) or 0) - float(reference.get(key) or 0), 3)
        for key in ("sentence_len_mean", "sentence_len_cv", "dialog_ratio",
                    "dash_per_1000", "ellipsis_per_1000")
    }
    notes: list[str] = []
    if deltas["dialog_ratio"] < -0.1:
        notes.append("对话占比明显低于参照样本（叙述可能偏堆砌）")
    if deltas["sentence_len_cv"] < -0.1:
        notes.append("句长变异不足（句子节奏偏均匀，AI 腔特征）")
    if deltas["dash_per_1000"] > 2:
        notes.append("破折号密度高于参照样本")
    return {"reference": reference, "current": current, "deltas": deltas, "notes": notes}


def delete_fingerprint(fingerprint_id: int) -> dict:
    with db.get_conn() as conn:
        row = conn.execute("SELECT id FROM style_fingerprints WHERE id = ?",
                           (fingerprint_id,)).fetchone()
        if row is None:
            raise NodeNotFoundError(f"文风样本不存在：id={fingerprint_id}")
        conn.execute("DELETE FROM style_fingerprints WHERE id = ?", (fingerprint_id,))
    return {"id": fingerprint_id, "deleted": True}


__all__ = [
    "compare",
    "compute_fingerprint",
    "delete_fingerprint",
    "list_fingerprints",
    "reference_metrics",
    "sample",
]