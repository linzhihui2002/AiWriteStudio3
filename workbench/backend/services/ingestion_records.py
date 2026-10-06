"""Grounded extraction normalization and preservation of chapter-owned rows."""
from __future__ import annotations

import hashlib
import json
import math
import re

from .. import db
from .errors import InvalidOperationError
from .file_change_service import FileConflictError

TRACKS = ("timeline", "ledger", "foreshadow", "state")
_PROJECTION_OPEN = re.compile(
    r"^[ \t]*<!--[ \t]*wb-ingest:([^:\s>]+):([^\s>]+)[ \t]*-->[ \t]*\r?$", re.MULTILINE)


def _blank(text: str) -> str:
    return "".join(char if char in "\r\n" else " " for char in text)


def project_text(project_id: int, text: str, preserve_offsets: bool = True,
                 chapter_before: int | None = None) -> str:
    """Expose only tracker blocks whose finalized source still matches its latest receipt.

    Free author prose stays untouched. Stale, unknown or malformed managed blocks
    are hidden, while the source files and the author's payoff decisions remain
    intact. Preserving offsets keeps knowledge evidence addressable in that file.
    """
    openings = list(_PROJECTION_OPEN.finditer(text or ""))
    if not openings:
        return text
    from . import ingestion_commit as commits
    from .chapter_service import parse_chapter_number
    from .project_service import get_project_dir

    receipts = {}
    # Corpus reads also run under the knowledge worker lock. Acquiring the
    # author's write lock here would reverse the save -> knowledge lock order.
    # The worker revalidates the entire corpus immediately before publishing.
    row, _ = get_project_dir(project_id)
    with db.get_conn() as conn:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ingestion_runs'").fetchone():
            runs = conn.execute("SELECT * FROM ingestion_runs WHERE book_key=? AND status='committed'"
                                " ORDER BY id DESC", (row["knowledge_key"],)).fetchall()
        else:
            runs = []
    seen = set()
    for value in runs:
        run = dict(value)
        if run["chapter_rel"] in seen:
            continue
        seen.add(run["chapter_rel"])
        try:
            plan = json.loads(run["plan_json"])
            source_key = plan.get("source_key")
            number = parse_chapter_number(run["chapter_rel"].replace("\\", "/").rsplit("/", 1)[-1])
            if chapter_before is not None and (number is None or number >= chapter_before):
                continue
            if source_key and commits.source_is_current(run):
                receipts[source_key] = plan.get("projections", {})
        except (ValueError, TypeError, AttributeError):
            continue

    result, cursor = [], 0
    for index, opening in enumerate(openings):
        next_start = openings[index + 1].start() if index + 1 < len(openings) else len(text)
        source_key, track = opening.group(1), opening.group(2)
        close_pattern = re.compile(r"^[ \t]*<!--[ \t]*/wb-ingest:"
                                   + re.escape(source_key) + ":" + re.escape(track)
                                   + r"[ \t]*-->[ \t]*\r?$", re.MULTILINE)
        closing = close_pattern.search(text, opening.end(), next_start)
        end = closing.end() if closing else next_start
        body_start = opening.end()
        if text[body_start:body_start + 2] == "\r\n":
            body_start += 2
        elif text[body_start:body_start + 1] == "\n":
            body_start += 1
        allowed = bool(closing and track in {"timeline", "ledger", "foreshadow"}
                       and (receipts.get(source_key, {}).get(track) or {}).get("block"))
        result.append(text[cursor:opening.start()])
        if preserve_offsets:
            if allowed:
                result.extend((_blank(text[opening.start():body_start]),
                               text[body_start:closing.start()], _blank(text[closing.start():end])))
            else:
                result.append(_blank(text[opening.start():end]))
        elif allowed:
            result.append(text[body_start:closing.start()])
        cursor = end
    result.append(text[cursor:])
    return "".join(result)


def _id(*values) -> str:
    return hashlib.sha256(json.dumps(values, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:24]


def _evidence(body: str, item: dict) -> tuple[str, int, int] | None:
    quote = item.get("依据")
    if not isinstance(quote, str) or not quote.strip() or len(quote) > 3000:
        return None
    quote = quote.strip()
    start = item.get("依据起点", item.get("_start"))
    if isinstance(start, int) and not isinstance(start, bool) and "_start" in item:
        while 0 <= start < len(body) and body[start] in " \t":
            start += 1
    if isinstance(start, int) and not isinstance(start, bool) and 0 <= start <= len(body) - len(quote) and body[start:start + len(quote)] == quote:
        return quote, start, start + len(quote)
    if body.count(quote) != 1:
        return None
    start = body.index(quote)
    return quote, start, start + len(quote)


def normalize(body: str, book_key: str, chapter_rel: str, deterministic: dict,
              structured: dict | None) -> tuple[dict, list[dict]]:
    records = {track: [] for track in TRACKS}
    rejected = []
    keys = {"timeline": "时间线索", "ledger": "资源变更", "foreshadow": "伏笔", "state": "状态变更"}
    for track in TRACKS:
        entries = [(item, False) for item in deterministic.get(track, [])]
        ai_entries = (structured or {}).get(keys[track], [])
        if not isinstance(ai_entries, list):
            rejected.append({"kind": track, "reason": "条目数组格式无效"})
            ai_entries = []
        entries.extend((item, True) for item in ai_entries[:100])
        for index, (item, ai) in enumerate(entries):
            reason = ""
            evidence = _evidence(body, item) if isinstance(item, dict) else None
            if evidence is None:
                reason = "原文依据无法唯一定位"
            else:
                quote, start, end = evidence
                # Private offsets belong to our deterministic detectors, not
                # to the model's untrusted object fields.
                event_start = start if ai else item.get("_event_start", start)
                event_end = end if ai else item.get("_event_end", end)
                result = {"evidence": quote, "start": start, "end": end, "ai": ai,
                          "event_start": event_start, "event_end": event_end}
                if track == "timeline":
                    value = item.get("线索")
                    if not isinstance(value, str) or not value.strip() or value.strip() not in quote:
                        reason = "时间线索不在原文依据中"
                    else:
                        result.update(subject=value.strip(), value=value.strip())
                elif track == "ledger":
                    subject, raw_delta = item.get("物品"), item.get("增减")
                    try:
                        delta = float(raw_delta) if not isinstance(raw_delta, bool) else float("nan")
                    except (TypeError, ValueError, OverflowError):
                        delta = float("nan")
                    if (not isinstance(subject, str) or not subject.strip() or subject.strip() not in quote
                            or not math.isfinite(delta) or delta == 0):
                        reason = "资源名称或有限增减值无效"
                    elif ai and (not re.search(r"(?<![\d.])" + re.escape(f"{abs(delta):g}") + r"(?![\d.])", quote)
                                 or not re.search(r"获得|得到|拿到|入账|奖励|捡到|收取" if delta > 0 else
                                                  r"消耗|失去|花掉|用掉|支出|扣除|耗费", quote)):
                        reason = "资源数值或增减方向缺少直接原文依据"
                    else:
                        result.update(subject=subject.strip(), value=delta, unit=str(item.get("单位") or ""))
                elif track == "foreshadow":
                    value = item.get("内容")
                    if not isinstance(value, str) or not value.strip() or len(value) > 500:
                        reason = "伏笔内容无效"
                    else:
                        result.update(subject=value.strip(), value=value.strip())
                else:
                    character, field, value = (item.get(key) for key in ("角色", "字段", "新值"))
                    if (not all(isinstance(value, str) and value.strip() for value in (character, field, value))
                            or character.strip() not in quote or any(char in character + field + value for char in "\r\n")):
                        reason = "角色状态字段或角色原文依据无效"
                    else:
                        result.update(subject=character.strip(), field=field.strip(), value=value.strip())
                if not reason:
                    # An AI quote may cover the surrounding sentence while the
                    # deterministic detector owns the exact resource occurrence.
                    duplicates = [prior for prior in records[track]
                                  if prior["value"] == result["value"]
                                  and (prior["subject"] == result["subject"] or
                                       track == "ledger" and (prior["subject"] in result["subject"] or result["subject"] in prior["subject"]))
                                  and prior.get("field") == result.get("field")
                                  and start < prior["end"] and end > prior["start"]
                                  and (ai or prior["ai"] or event_start == prior["event_start"])]
                    if duplicates:
                        continue
                    event_quote = body[event_start:event_end]
                    occurrence = body[:event_start].count(event_quote) if event_quote else 0
                    result["event_quote"], result["occurrence"] = event_quote, occurrence
                    result["key"] = _id(book_key, chapter_rel, track, result["subject"], result.get("field"),
                                         result["value"], event_quote, occurrence)
                    records[track].append(result)
            if reason:
                rejected.append({"kind": track, "index": index, "reason": reason})
    for track in TRACKS:
        records[track].sort(key=lambda item: (item["start"], item["end"], item["key"]))
    # A character can change location several times in a chapter. The last
    # grounded occurrence determines its final state, rather than model order.
    latest = {}
    for record in records["state"]:
        key = record["subject"], record["field"]
        if key in latest and latest[key]["start"] == record["start"] and latest[key]["value"] != record["value"]:
            raise InvalidOperationError("同一原文位置产生冲突的角色状态，请重新提取")
        latest[key] = record
    records["state"] = sorted(latest.values(), key=lambda item: item["start"])
    return records, rejected


def _display(value) -> str:
    return str(value).replace("\r", " ").replace("\n", " ").replace("｜", "／")


def render(track: str, record: dict, chapter: str, summary: str) -> str:
    if track == "timeline":
        return f"- {chapter}｜{_display(record['value'])}｜{_display(summary[:60])}｜依据：{_display(record['evidence'])}"
    if track == "foreshadow":
        return f"- [待回收] {_display(record['value'])}｜埋设：{chapter}｜依据：{_display(record['evidence'])}"
    if track == "ledger":
        subject = record["subject"] + (f"（{record['unit']}）" if record.get("unit") else "")
        values = [subject, chapter, "待核实", f"{record['value']:+g}", "待核实", record["evidence"]]
        return "| " + " | ".join(_display(value).replace("|", "\\|") for value in values) + " |"
    raise InvalidOperationError("未知追踪条目")


def _payoff_core(line: str) -> str:
    line = re.sub(r"\[(待回收|疑似回收|已回收|作废)\]", "[待回收]", line, count=1)
    return re.sub(r"｜计划回收(?:章)?[：:].*$", "", line)


def adopt_legacy(text: str | None, track: str, source_key: str, records: list[dict],
                 chapter: str, body: str) -> tuple[str | None, dict | None, int, int]:
    """Take over only uniquely grounded rows in the exact old generated format.

    Ambiguous copies, free-form/table material and ledger rows without evidence
    remain author material. Report overlap rather than guessing their ownership.
    """
    from .material_parser import parse_timeline, parse_foreshadows, parse_ledger
    if not text or not records:
        return text, None, 0, 0
    # Never take a row out of another journal's managed area.
    unmanaged = re.sub(r"<!-- wb-ingest:[^>]+ -->.*?<!-- /wb-ingest:[^>]+ -->",
                       lambda match: " " * len(match[0]), text, flags=re.S)
    parser = {"timeline": parse_timeline, "foreshadow": parse_foreshadows, "ledger": parse_ledger}[track]
    matches = []
    for row in parser(unmanaged):
        if row.get("planted_in" if track == "foreshadow" else "chapter") != chapter:
            continue
        candidates = []
        for record in records:
            if track == "timeline":
                same = row["story_time"] == record["value"]
            elif track == "foreshadow":
                same = row["content"] == record["value"]
            else:
                try:
                    same = row["item"] == record["subject"] and float(row["change"]) == record["value"]
                except (ValueError, TypeError):
                    same = False
            if same and (not row.get("evidence") or row["evidence"] == record["evidence"]):
                candidates.append(record)
        if candidates:
            matches.append((row, candidates))
    selected = []
    for row, candidates in matches:
        if len(candidates) != 1 or row.get("format") != "list" or track == "ledger":
            continue
        record = candidates[0]
        if (row.get("evidence") != record["evidence"] or body.count(record["evidence"]) != 1
                or sum(record in peers for _, peers in matches) != 1):
            continue
        raw = row["raw"].strip()
        expected = (f"- {chapter}｜{record['value']}｜{row['event']}｜依据：{record['evidence']}"
                    if track == "timeline" else render(track, record, chapter, ""))
        if (raw if track == "timeline" else _payoff_core(raw)) != expected:
            continue
        selected.append((row, record, raw))
    if not selected:
        return text, None, 0, len(matches)
    value = text
    for row, _, _ in sorted(selected, key=lambda item: item[0]["source_location"]["start"], reverse=True):
        start, end = row["source_location"]["start"], row["source_location"]["end"]
        value = value[:start] + value[end:]
    selected.sort(key=lambda item: (item[1]["start"], item[1]["end"], item[1]["key"]))
    block = (f"<!-- wb-ingest:{source_key}:{track} -->\n" + "\n".join(item[2] for item in selected)
             + f"\n<!-- /wb-ingest:{source_key}:{track} -->")
    value += ("\n" if value and not value.endswith("\n") else "") + "\n" + block + "\n"
    return value, {"block": block, "records": [item[1] for item in selected]}, len(selected), len(matches) - len(selected)


def projection(text: str | None, track: str, source_key: str, records: list[dict],
               previous: dict | None, chapter: str, summary: str) -> tuple[str | None, dict]:
    """Replace only the known chapter-owned block, and reject unexplained edits."""
    start_marker = f"<!-- wb-ingest:{source_key}:{track} -->"
    end_marker = f"<!-- /wb-ingest:{source_key}:{track} -->"
    value = text or ""
    begin, end = value.find(start_marker), value.find(end_marker)
    previous = previous or {}
    old_records = {item["key"]: item for item in previous.get("records", [])}
    current_rows = []
    if begin >= 0 or end >= 0:
        if begin < 0 or end <= begin or value.count(start_marker) != 1 or value.count(end_marker) != 1:
            raise FileConflictError("本章托管区域标记已变化，请核对原材料")
        end += len(end_marker)
        current = value[begin:end]
        expected = previous.get("block")
        if not expected:
            raise FileConflictError("托管区域缺少版本记录，请核对原材料")
        if track == "foreshadow":
            current_rows = current.splitlines()[1:-1]
        if current != expected:
            if track != "foreshadow":
                raise FileConflictError("本章托管条目被人工修改，提取不会覆盖它")
            current_rows = current.splitlines()[1:-1]
            expected_rows = expected.splitlines()[1:-1]
            if len(current_rows) != len(expected_rows) or any(_payoff_core(a) != _payoff_core(b)
                                                            for a, b in zip(current_rows, expected_rows)):
                raise FileConflictError("本章伏笔条目被人工修改，提取不会覆盖它")
    elif previous.get("block"):
        raise FileConflictError("本章托管条目已被删除，提取不会重新覆盖人工决定")
    preserved = {}
    if track == "foreshadow" and current_rows:
        for old_record, row in zip(previous.get("records", []), current_rows):
            preserved[old_record["key"]] = row
    rows = []
    projected_records, retained_keys, warnings = [], set(), []
    def author_confirmed(row: str) -> bool:
        return bool(re.search(r"\[(疑似回收|已回收|作废)\]|｜计划回收(?:章)?[：:]", row))
    for record in records:
        rendered = render(track, record, chapter, summary)
        old_row = preserved.get(record["key"])
        matched_key = record["key"]
        if track == "foreshadow" and not old_row:
            # A harmless sentence extension can change an AI quote/key. Keep
            # the author's decision when one same event can be aligned safely.
            peers = [item for item in previous.get("records", []) if item["value"] == record["value"]]
            new_peers = [item for item in records if item["value"] == record["value"]]
            if len(peers) == len(new_peers) == 1:
                old = peers[0]
                if old["evidence"] in record["evidence"] or record["evidence"] in old["evidence"]:
                    matched_key = old["key"]
                    old_row = preserved.get(matched_key)
            if not old_row and any(author_confirmed(preserved.get(item["key"], "")) for item in peers):
                raise FileConflictError("已确认伏笔无法与新正文唯一对齐，请先核对原材料；原确认结果已保留")
        if old_row:
            retained_keys.add(matched_key)
            status = re.search(r"\[(待回收|疑似回收|已回收|作废)\]", old_row)
            plan = re.search(r"｜计划回收(?:章)?[：:].*$", old_row)
            if status:
                rendered = rendered.replace("[待回收]", status[0], 1)
            if plan:
                rendered += plan[0]
        rows.append(rendered)
        projected_records.append(record)
    if track == "foreshadow":
        for old in previous.get("records", []):
            old_row = preserved.get(old["key"], "")
            if old["key"] not in retained_keys and author_confirmed(old_row):
                rows.append(old_row)
                projected_records.append(old)
                warnings.append("新正文未再次提及的已确认伏笔或回收计划已保留，请核对原材料。")
    if track == "ledger" and rows:
        rows = ["| 物品 | 章节 | 前值 | 增减 | 结余 | 依据 |", "| --- | --- | --- | --- | --- | --- |", *rows]
    block = start_marker + "\n" + "\n".join(rows) + ("\n" if rows else "") + end_marker
    if begin >= 0:
        value = value[:begin] + block + value[end:]
    elif rows:
        value = value + ("\n" if value and not value.endswith("\n") else "") + "\n" + block + "\n"
    else:
        return text, {"block": "", "records": projected_records, "warnings": warnings}
    return value, {"block": block, "records": projected_records, "warnings": warnings}
