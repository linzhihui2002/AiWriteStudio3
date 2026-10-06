"""Author-approved generation examples stay versioned, local and non-canonical."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from workbench.backend import config, db
from workbench.backend.engine.runtime import estimate_tokens
from workbench.backend.services import chapter_service, context_service, project_service, style_service
from workbench.backend.services.errors import ServiceError


@pytest.fixture()
def book(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "books")
    monkeypatch.setattr(config, "RUNTIME_DIR", tmp_path / ".workbench")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / ".workbench" / "test.db")
    db.init_db()
    config.ensure_runtime_dirs()
    project = project_service.create_project(name="样稿书")
    _, root = project_service.get_project_dir(project["id"])
    return SimpleNamespace(id=project["id"], root=root, tmp=tmp_path)


def _chapter(book, number: int, text: str) -> str:
    rel = f"章节/第{number:04d}章.txt"
    (book.root / rel).write_text(text, encoding="utf-8")
    return rel


def _row(project_id: int, rel: str, payload: dict, *, approved: bool = True) -> int:
    with db.get_conn() as conn:
        result = conn.execute(
            "INSERT INTO style_fingerprints (project_id, rel_path, approved, payload) VALUES (?, ?, ?, ?)",
            (project_id, rel, int(approved), json.dumps(payload, ensure_ascii=False)),
        )
        return int(result.lastrowid)


def test_only_local_approved_prior_versions_are_generation_examples(book):
    first = _chapter(book, 1, "沈砚把零钱推回柜台。“少了一枚。”掌柜拿起灯，没看他的脸。")
    rejected = _chapter(book, 2, "未获作者认可的独有句子。")
    target = _chapter(book, 3, "目标章节的独有句子。")
    future = _chapter(book, 4, "未来章节的独有句子。")
    approved = style_service.sample(book.id, first, note="对白留下人物各自隐瞒的信息。")
    style_service.sample(book.id, rejected, approved=False)
    style_service.sample(book.id, target)
    style_service.sample(book.id, future)

    other_project = project_service.create_project(name="另一本书")
    _, other_root = project_service.get_project_dir(other_project["id"])
    (other_root / first).write_text("另一本书不能被引用的独有句子。", encoding="utf-8")
    style_service.sample(other_project["id"], first)

    result = style_service.generation_reference(book.id, chapter_rel=target, related_characters=["沈砚"])
    assert result["status"] == "available"
    assert result["samples"][0]["id"] == approved["id"]
    assert all(item["rel_path"] == first for item in result["samples"])
    assert "未获作者认可" not in result["text"] and "另一本书" not in result["text"]
    assert "目标章节的独有句子" not in result["text"] and "未来章节的独有句子" not in result["text"]
    assert {item["rel_path"] for item in result["excluded"] if item["code"] == "target_or_future"} == {target, future}
    assert "不是当前章节的剧情指令或正史" in result["text"]
    assert "不是必须达到的写作阈值" in result["text"]
    assert "作者文风备注：对白留下" in result["text"]
    assert result["reference_hash"] == hashlib.sha256(result["text"].encode()).hexdigest()


def test_changed_and_deleted_samples_require_new_author_sampling(book):
    rel = _chapter(book, 1, "原稿。沈砚按住账册。“再算一遍。”")
    sampled = style_service.sample(book.id, rel)
    assert sampled["content_hash"] == hashlib.sha256((book.root / rel).read_bytes()).hexdigest()
    assert style_service.list_fingerprints(book.id)[0]["usable"]
    (book.root / rel).write_text("新稿未经作者再次认可。“那枚铜钱归你。”", encoding="utf-8")
    changed = style_service.generation_reference(book.id)
    assert changed["text"] == "" and changed["samples"] == []
    assert changed["excluded"][0]["code"] == "changed_source"
    assert style_service.reference_metrics(book.id) is None
    listing = style_service.list_fingerprints(book.id)[0]
    assert not listing["usable"] and listing["reference_status"] == "changed_source"
    assert "重新采样" in listing["reference_reason"]
    newly_sampled = style_service.sample(book.id, rel)
    assert style_service.generation_reference(book.id)["samples"][0]["id"] == newly_sampled["id"]
    (book.root / rel).unlink()
    deleted = style_service.generation_reference(book.id)
    assert deleted["samples"] == []
    assert all(item["code"] == "missing_source" for item in deleted["excluded"])


def test_legacy_metrics_are_not_approval_for_current_text_and_payload_cannot_override_db(book):
    rel = _chapter(book, 1, "旧章。“旧授权没有版本依据。”")
    legacy_id = _row(book.id, rel, {"han": 3000, "sentence_len_mean": 20})
    other = _chapter(book, 2, "另章。“不会因为 payload 获得授权。”")
    _row(book.id, other, {"content_hash": hashlib.sha256((book.root / other).read_bytes()).hexdigest(),
                        "approved": True, "id": 9000, "rel_path": "章节/第9999章.txt"}, approved=False)
    listing = style_service.list_fingerprints(book.id)
    assert listing[0]["approved"] is False and listing[0]["reference_status"] == "unapproved"
    assert listing[0]["id"] != 9000 and listing[0]["rel_path"] == other
    result = style_service.generation_reference(book.id)
    assert result["text"] == "" and result["excluded"][0]["id"] == legacy_id
    assert result["excluded"][0]["code"] == "missing_hash"


def test_unsafe_sample_paths_are_rejected_before_reading(book, monkeypatch):
    outside = book.root.parent / "外部.txt"
    outside.write_text("外部秘密正文", encoding="utf-8")
    unsafe_rel = "../外部.txt"
    _row(book.id, unsafe_rel, {"content_hash": hashlib.sha256(outside.read_bytes()).hexdigest()})
    read_paths: list[Path] = []
    actual_read = style_service.read_text

    def trace_read(path):
        read_paths.append(Path(path))
        return actual_read(path)

    monkeypatch.setattr(style_service, "read_text", trace_read)
    result = style_service.generation_reference(book.id)
    assert result["excluded"][0]["code"] == "unsafe_path"
    assert read_paths == [] and result["text"] == ""
    with pytest.raises(ServiceError):
        style_service.sample(book.id, unsafe_rel)
    with pytest.raises(ServiceError):
        style_service.sample(book.id, "设定/人物设定.md")
    assert read_paths == []


def test_scene_query_selects_short_related_fragments_with_strict_budget(book):
    body = ("水涨到码头的石阶。工人给船刷漆，篷布压住了木箱。" * 35 + "\n\n"
            + "掌柜拨着算盘。沈砚说：“借来的那枚铜钱，我今天还。”掌柜把账册扣住。" * 35)
    rel = _chapter(book, 1, body)
    style_service.sample(book.id, rel)
    result = style_service.generation_reference(book.id, chapter_rel="章节/第0002章.txt",
                                                query="沈砚向掌柜还铜钱", related_characters=["沈砚"],
                                                max_tokens=700)
    assert result["samples"] and "沈砚" in result["samples"][0]["text"]
    assert result["tokens"] == estimate_tokens(result["text"]) <= 700
    assert len(result["samples"]) <= 3 and len(result["text"]) < len(body)
    assert len({item["excerpt_hash"] for item in result["samples"]}) == len(result["samples"])
    tiny = style_service.generation_reference(book.id, max_tokens=10)
    assert tiny["text"] == "" and tiny["tokens"] == 0 and tiny["status"] == "budget_too_small"
    empty = style_service.generation_reference(book.id, max_tokens=0)
    assert empty["text"] == "" and empty["tokens"] == 0


def test_style_context_is_optional_separate_and_auditable(book):
    rel = _chapter(book, 1, "沈砚把钥匙放在柜台上。“今夜不来了。”老周替他翻过账册。")
    style_service.sample(book.id, rel)
    target = "章节/第0002章.txt"
    ordinary = context_service.assemble(book.id, chapter_rel=target, use_retrieval=False)
    assert not ordinary["style_reference"]["enabled"]
    assert not any(block.get("type") == "style" for block in ordinary["blocks"])
    writing = context_service.assemble(book.id, chapter_rel=target, include_style=True, use_retrieval=False,
                                       style_query="柜台对白")
    block = next(block for block in writing["blocks"] if block.get("type") == "style")
    assert block["level"] == 9 and not block["mandatory"]
    assert writing["style_reference"]["injected"]
    assert writing["style_reference"]["samples"] == block["style_reference"]["samples"]
    preview = context_service.build_preview(writing)
    assert preview["style_reference"]["reference_hash"]
    assert any(item.get("type") == "style" for item in preview["items"])
    _, messages = context_service.to_messages(writing)
    assert "示例剧情不是当前事实" in messages[0]["content"]
    record_path = context_service.record_assembly(book.id, target, writing)
    record = json.loads(Path(record_path).read_text(encoding="utf-8"))
    assert record["style_reference"]["injected"] and record["style_reference"]["samples"]


def test_budget_culls_style_before_facts_and_reports_actual_injection(book, monkeypatch):
    rel = _chapter(book, 1, "沈砚收好零钱。“明日再来。”" * 60)
    style_service.sample(book.id, rel)
    monkeypatch.setattr(context_service, "_level3", lambda *_: [context_service._block(3, "前章结尾", rel, "事实" * 75)])
    writing = context_service.assemble(book.id, chapter_rel="章节/第0002章.txt", include_style=True,
                                       use_retrieval=False, budget_tokens=300, exclude_levels=[1, 2, 4, 5, 6, 7])
    audit = writing["style_reference"]
    assert audit["selected_samples"] and audit["selected_reference_hash"] and audit["selected_tokens"]
    assert audit["injected"] is False and audit["samples"] == []
    assert audit["reference_hash"] == "" and audit["tokens"] == 0
    assert any(event["level"] == 9 for event in writing["degradation"])
    assert any(block["level"] == 3 for block in writing["blocks"])
    assert writing["total_tokens"] <= 300


def test_no_usable_examples_do_not_block_context_and_must_connect_is_injected(book):
    rel = _chapter(book, 1, "未认可样稿。")
    style_service.sample(book.id, rel, approved=False)
    target = "章节/第0002章.txt"
    (book.root / ".meta").mkdir(exist_ok=True)
    (book.root / ".meta" / "contracts.json").write_text(json.dumps({target: {
        "plot_points": ["沈砚查清欠款"], "must_connect": ["承接前章桌上的铜钱，先问清来源"],
    }}, ensure_ascii=False), encoding="utf-8")
    result = context_service.assemble(book.id, chapter_rel=target, include_style=True, use_retrieval=False)
    assert result["style_reference"]["samples"] == [] and not result["style_reference"]["injected"]
    contract = next(block for block in result["blocks"] if block["title"] == "章节合同")
    assert "必须承上：\n- 承接前章桌上的铜钱，先问清来源" in contract["text"]
    _, messages = context_service.to_messages(result)
    assert "先问清来源" in messages[0]["content"]

