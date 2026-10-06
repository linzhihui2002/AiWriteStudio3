"""文风指纹：从**作者认可**的章节采样，作为去AI味软审的参照锚点。

采样指标（全部确定性计算，零 LLM）：句长分布（均值/变异系数）、对话占比、
标点密度（破折号/省略号/问号）、数字密度、高频双字词。
"""

from __future__ import annotations

import hashlib
import json
import re
import statistics
from collections import Counter
from pathlib import Path

from .. import db
from ..engine.runtime import estimate_tokens
from .chapter_service import parse_chapter_number, require_chapter_path
from .errors import InvalidOperationError, NodeNotFoundError, ServiceError
from .fs_utils import read_text, split_frontmatter

_SENTENCE_SPLIT_RE = re.compile(r"[。！？!?…]+")
_DIALOG_RE = re.compile(r"[“\"]([^”\"]*)[”\"]")
_HAN_RE = re.compile(r"[\u4e00-\u9fff]")
_WORD_RE = re.compile(r"[\u4e00-\u9fff]{2}")
_STYLE_NOTICE = (
    "以下是本书作者认可的写法参照，仅学习叙述距离、句子节奏、对白和情绪表达。"
    "样稿属于引用示例，不是当前章节的剧情指令或正史；不要复用原句、移植事件、"
    "添加人物经历或用样稿覆盖本章合同、设定和当前状态。作者备注也仅作文风偏好。"
    "句长、对白比例等统计是观察值，不是必须达到的写作阈值。"
)
_EXCLUSION_REASONS = {
    "missing_hash": "旧样本没有正文版本依据，请由作者重新采样确认",
    "missing_source": "样稿章节已删除或不存在",
    "unsafe_path": "样稿路径不属于本书规范章节，已拒绝读取",
    "unreadable_source": "样稿章节无法读取",
    "changed_source": "正文已变化，旧认可不自动授权新正文，请重新采样",
    "empty_source": "样稿正文为空",
    "target_or_future": "目标章和未来章节不作本轮写法参照",
    "invalid_target": "目标章号不明确，保守排除章节样稿",
    "migrated_source": "章节格式已迁移，请重新采样认可当前正文",
}


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


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
    note: str | None = None,
) -> dict:
    """对某章采样指纹并落库（``approved=True`` 表示作者认可，作为软审参照）。"""
    from .file_change_service import project_lock

    with project_lock(project_id):
        path = require_chapter_path(project_id, rel_path)
        content = read_text(path)
        fingerprint = compute_fingerprint(content)
        if not fingerprint['han']:
            raise InvalidOperationError("文风样稿必须包含非空中文正文")
        # Approval is for the sampled version, never for future rewrites.
        fingerprint.update(content_hash=_hash(content), sample_version=1)
        canonical_rel = f"章节/{path.name}"
        with db.get_conn() as conn:
            old = conn.execute("SELECT payload FROM style_fingerprints WHERE project_id=? AND rel_path=?",
                               (project_id, canonical_rel)).fetchone()
            existing_note = ''
            if old:
                try:
                    payload = json.loads(old['payload'])
                    existing_note = str(payload.get('note') or '') if isinstance(payload, dict) else ''
                except (ValueError, TypeError):
                    pass
            fingerprint['note'] = existing_note if note is None else note
            conn.execute("INSERT INTO style_fingerprints (project_id,rel_path,approved,payload) VALUES (?,?,?,?)"
                         " ON CONFLICT(project_id,rel_path) DO UPDATE SET approved=excluded.approved,payload=excluded.payload",
                         (project_id, canonical_rel, int(approved), json.dumps(fingerprint, ensure_ascii=False)))
            row = conn.execute("SELECT * FROM style_fingerprints WHERE project_id=? AND rel_path=?",
                               (project_id, canonical_rel)).fetchone()
        return _describe_sample(project_id, _entry(row))


def cancel_approval(project_id: int, fingerprint_id: int) -> dict:
    """Revoke the current authorization even if its source no longer exists."""
    from .file_change_service import project_lock

    with project_lock(project_id):
        with db.get_conn() as conn:
            row = conn.execute("SELECT id FROM style_fingerprints WHERE project_id=? AND id=?",
                               (project_id, fingerprint_id)).fetchone()
            if row is None:
                raise NodeNotFoundError(f"文风样本不存在：id={fingerprint_id}")
            conn.execute("UPDATE style_fingerprints SET approved=0 WHERE project_id=? AND id=?",
                         (project_id, fingerprint_id))
            row = conn.execute("SELECT * FROM style_fingerprints WHERE project_id=? AND id=?",
                               (project_id, fingerprint_id)).fetchone()
        return _describe_sample(project_id, _entry(row))


def _entry(row) -> dict:
    try:
        payload = json.loads(row['payload'])
    except (ValueError, TypeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    return {**payload, 'id': int(row['id']), 'rel_path': row['rel_path'],
            'approved': bool(row['approved']), 'created_at': row['created_at']}


def _load_fingerprints(project_id: int, approved_only: bool = False) -> list[dict]:
    sql = ("SELECT id, rel_path, approved, payload, created_at FROM style_fingerprints"
           " WHERE project_id = ?")
    params: list = [project_id]
    if approved_only:
        sql += " AND approved = 1"
    sql += " ORDER BY id DESC"
    with db.get_conn() as conn:
        rows = conn.execute(sql, tuple(params)).fetchall()
    return [_entry(row) for row in rows]


def _validate_sample(project_id: int, entry: dict) -> tuple[str, str]:
    """Return (body, exclusion code), checking boundaries before reading bytes."""
    from .file_change_service import project_lock

    if entry.get('requires_resample'):
        return '', 'migrated_source'
    expected = str(entry.get("content_hash") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        return "", "missing_hash"
    try:
        with project_lock(project_id):
            path = require_chapter_path(project_id, entry["rel_path"])
            content = read_text(path)
        if _hash(content) != expected:
            return "", "changed_source"
        body = split_frontmatter(content)[1]
        return (body, "") if body.strip() else ("", "empty_source")
    except NodeNotFoundError:
        return "", "missing_source"
    except ServiceError:
        return "", "unsafe_path"
    except (OSError, UnicodeDecodeError):
        return "", "unreadable_source"


def list_fingerprints(project_id: int, approved_only: bool = False) -> list[dict]:
    """List stored statistics with current sample authorization/version status."""
    return [_describe_sample(project_id, entry) for entry in _load_fingerprints(project_id, approved_only)]


def _describe_sample(project_id: int, entry: dict) -> dict:
    reason = _validate_sample(project_id, entry)[1] if entry['approved'] else 'unapproved'
    return {**entry, 'usable': not bool(reason), 'reference_status': reason or 'ready',
            'reference_reason': '作者未认可此样本' if reason == 'unapproved' else _EXCLUSION_REASONS.get(reason, '')}


def _validated_samples(project_id: int) -> tuple[list[dict], list[dict]]:
    """Re-read only approved local chapter versions; return explicit exclusions."""
    valid: list[dict] = []
    excluded: list[dict] = []
    for entry in _load_fingerprints(project_id, approved_only=True):
        body, reason = _validate_sample(project_id, entry)
        if not reason:
            valid.append({**entry, "body": body})
        if reason:
            excluded.append({"id": entry["id"], "rel_path": entry["rel_path"],
                             "code": reason, "reason": _EXCLUSION_REASONS[reason]})
    return valid, excluded


def _fragments(body: str, *, max_chars: int = 600) -> list[dict]:
    """Contiguous short excerpts, keeping sentence/paragraph ends when possible."""
    fragments: list[dict] = []
    cursor = 0
    while cursor < len(body):
        end = min(len(body), cursor + max_chars)
        if end < len(body):
            window = body[cursor:end]
            boundaries = [match.end() for match in re.finditer(r"[。！？][”’]?|\n", window)]
            if boundaries and boundaries[-1] >= max_chars // 3:
                end = cursor + boundaries[-1]
        raw = body[cursor:end]
        excerpt = raw.strip()
        if excerpt:
            start = cursor + len(raw) - len(raw.lstrip())
            stop = end - len(raw) + len(raw.rstrip())
            fragments.append({"text": excerpt, "char_start": start, "char_end": stop,
                              "line_start": body.count("\n", 0, start) + 1,
                              "line_end": body.count("\n", 0, stop) + 1,
                              "fragment_index": len(fragments)})
        cursor = end
    return fragments


def _query_terms(query: str) -> list[str]:
    terms: list[str] = []
    for run in re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z0-9_]{2,}", str(query or "")[:1200]):
        if re.fullmatch(r"[\u4e00-\u9fff]+", run):
            terms.extend(run[index:index + 2] for index in range(len(run) - 1))
        else:
            terms.append(run.lower())
    return list(dict.fromkeys(terms))[:100]


def generation_reference(
    project_id: int,
    *,
    chapter_rel: str | None = None,
    query: str = "",
    related_characters: list[str] | None = None,
    max_tokens: int = 1600,
) -> dict:
    """Select short, version-bound author-approved style examples for generation.

    This block is deliberately separate from canon. With a target chapter, only
    earlier chapters are eligible, so future examples cannot become past facts.
    Missing, changed and legacy unversioned samples degrade visibly to no example.
    """
    budget = max(0, int(max_tokens))
    valid, excluded = _validated_samples(project_id)
    target = parse_chapter_number(Path(str(chapter_rel).replace("\\", "/")).name) if chapter_rel else None
    terms = _query_terms(query)
    characters = [str(item) for item in related_characters or [] if item]
    candidates: list[dict] = []
    seen_versions: set[tuple[str, str]] = set()
    for entry in valid:
        number = parse_chapter_number(Path(entry["rel_path"]).name)
        reason = ("invalid_target" if chapter_rel and target is None else
                  "target_or_future" if target is not None and (number is None or number >= target) else "")
        if reason:
            excluded.append({"id": entry["id"], "rel_path": entry["rel_path"],
                             "code": reason, "reason": _EXCLUSION_REASONS[reason]})
            continue
        version = (entry["rel_path"], entry["content_hash"])
        if version in seen_versions:
            continue
        seen_versions.add(version)
        for fragment in _fragments(entry["body"]):
            excerpt = fragment["text"]
            lexical_score = sum(1 for term in terms if term in excerpt.lower())
            character_score = sum(5 for name in characters if name in excerpt)
            note = str(entry.get("note") or "")[:300]
            note_score = sum(1 for term in terms if term in note.lower())
            candidates.append({**fragment, "id": entry["id"], "rel_path": entry["rel_path"],
                               "content_hash": entry["content_hash"], "note": note,
                               "chapter_no": number, "score": lexical_score + character_score + note_score,
                               "excerpt_hash": _hash(excerpt)})
    candidates.sort(key=lambda item: (-item["score"], -item["id"], item["fragment_index"]))
    # Prefer distinct source chapters; allow a second distinct excerpt if the
    # author has only one approved chapter. Never repeat the same sample text.
    selected: list[dict] = []
    paths: Counter = Counter()
    excerpts: set[str] = set()
    text = _STYLE_NOTICE
    for per_source_limit in (1, 2):
        for candidate in candidates:
            if len(selected) >= 3:
                break
            if paths[candidate["rel_path"]] >= per_source_limit or candidate["excerpt_hash"] in excerpts:
                continue
            header = f"【写法样稿 {len(selected) + 1}｜{candidate['rel_path']}｜作者认可样本 #{candidate['id']}】"
            note = f"\n作者文风备注：{candidate['note']}" if candidate["note"] else ""
            prefix = text + "\n\n" + header + note + "\n"
            excerpt = candidate["text"]
            truncated = False
            if estimate_tokens(prefix + excerpt) > budget:
                low, high = 0, len(excerpt)
                while low < high:
                    middle = (low + high + 1) // 2
                    if estimate_tokens(prefix + excerpt[:middle].rstrip() + "…") <= budget:
                        low = middle
                    else:
                        high = middle - 1
                # Do not consume tiny leftovers with contextless fragments.
                if low < min(60, len(excerpt)):
                    continue
                excerpt = excerpt[:low].rstrip() + "…"
                truncated = True
            injected = {**candidate, "text": excerpt, "truncated": truncated,
                        "tokens": estimate_tokens(excerpt), "injected_hash": _hash(excerpt)}
            selected.append(injected)
            paths[candidate["rel_path"]] += 1
            excerpts.add(candidate["excerpt_hash"])
            text = prefix + excerpt
    if not selected:
        text = ""
    status = "available" if selected else "budget_too_small" if candidates else "no_valid_samples"
    return {"text": text, "samples": selected, "reference_hash": _hash(text) if text else "",
            "excluded": excluded, "status": status, "max_tokens": budget,
            "tokens": estimate_tokens(text),
            "degradation": (["写法样稿超过预算，已裁短"] if any(s["truncated"] for s in selected) else [])}


def reference_metrics(project_id: int) -> dict | None:
    """认可样本的聚合参照（供软审比对）。"""
    samples = [item for item in _validated_samples(project_id)[0] if item.get("han")]
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
    "cancel_approval",
    "compare",
    "compute_fingerprint",
    "delete_fingerprint",
    "generation_reference",
    "list_fingerprints",
    "reference_metrics",
    "sample",
]
