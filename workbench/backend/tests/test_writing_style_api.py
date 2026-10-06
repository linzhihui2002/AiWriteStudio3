"""Writing API forwards only the author-approved style examples actually sent."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from workbench.backend import config, db
from workbench.backend.api import writing
from workbench.backend.services import chapter_service, generation_service, project_service, style_service


@pytest.fixture()
def book(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "books")
    monkeypatch.setattr(config, "RUNTIME_DIR", tmp_path / ".workbench")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / ".workbench" / "test.db")
    db.init_db()
    config.ensure_runtime_dirs()
    project = project_service.create_project(name="写作接口样稿书")
    ident = project["id"]
    _, root = project_service.get_project_dir(ident)
    previous = chapter_service.create_chapter(ident, "借钱")
    (root / previous["rel_path"]).write_text("沈砚推回那枚铜钱。“欠你的账，明日再说。”老周把账册合上。", encoding="utf-8")
    chapter_service.set_chapter_meta(ident, previous["rel_path"], status="完成")
    style_service.sample(ident, previous["rel_path"], note="对白留下没说破的信息。")
    target = chapter_service.create_chapter(ident, "还账")
    (root / target["rel_path"]).write_text("沈砚拿出账册。", encoding="utf-8")
    (root / "大纲/章纲.md").write_text("第0002章 沈砚查清还账的来源。", encoding="utf-8")
    (root / "状态/角色状态.md").write_text("## 沈砚\n位置：柜台。", encoding="utf-8")
    (root / ".meta").mkdir(exist_ok=True)
    (root / ".meta/contracts.json").write_text(json.dumps({target["rel_path"]: {
        "plot_points": ["查清账款来源"], "must_connect": ["承接上一章那枚铜钱"],
    }}, ensure_ascii=False), encoding="utf-8")
    app = FastAPI()
    app.include_router(writing.router)
    return SimpleNamespace(id=ident, root=root, previous=previous["rel_path"], rel=target["rel_path"],
                           client=TestClient(app))


def _fake_generation(monkeypatch) -> list[dict]:
    calls: list[dict] = []

    def run(**kwargs):
        calls.append(kwargs)
        return {"ok": True, "text": "他合上账册，把铜钱放回抽屉。", "task_id": 7}

    monkeypatch.setattr(generation_service, "run_task", run)
    return calls


def test_style_sample_accepts_author_note_and_reports_version_status(book):
    response = book.client.post(f"/api/projects/{book.id}/style/sample", json={
        "rel_path": book.rel, "approved": True, "note": "留白多一点，人物不要急着解释自己的动机。",
    })
    assert response.status_code == 200
    assert response.json()["note"].startswith("留白多一点")
    assert response.json()["content_hash"]
    (book.root / book.rel).write_text("正文已经修改。", encoding="utf-8")
    listing = book.client.get(f"/api/projects/{book.id}/style/fingerprints").json()
    stale = next(sample for sample in listing["samples"] if sample["rel_path"] == book.rel)
    assert stale["usable"] is False and stale["reference_status"] == "changed_source"
    assert "重新采样" in stale["reference_reason"]
    resampled = book.client.post(f"/api/projects/{book.id}/style/sample", json={
        "rel_path": book.rel, "note": stale["note"],
    })
    assert resampled.status_code == 200 and resampled.json()["content_hash"] != stale["content_hash"]


def test_context_preview_includes_style_for_writing_and_can_exclude_it(book):
    path = f"/api/projects/{book.id}/context/preview"
    payload = {"chapter_rel": book.rel, "use_retrieval": False}
    response = book.client.post(path, json=payload)
    assert response.status_code == 200
    result = response.json()
    assert result["style_reference"]["injected"]
    assert any(block.get("type") == "style" for block in result["blocks"])
    assert result["preview"]["style_reference"]["reference_hash"]
    for change in ({"include_style": False}, {"exclude_levels": [9]},
                   {"task_type": "审稿"}, {"retrieval_profile": "teardown"}):
        excluded = book.client.post(path, json={**payload, **change}).json()
        assert not excluded["style_reference"]["injected"]
        assert excluded["style_reference"]["samples"] == []


@pytest.mark.parametrize("task_type,profile,expect_style", [
    ("章节正文", "off", True), ("续写", "off", True), ("章节修订", "off", True),
    ("审稿", "off", False), ("大纲生成", "off", False), ("章节正文", "review", False),
    ("章节正文", "teardown", False),
])
def test_generate_samples_are_only_for_registered_writing_tasks(book, monkeypatch, task_type, profile, expect_style):
    calls = _fake_generation(monkeypatch)
    response = book.client.post(f"/api/projects/{book.id}/generate", json={
        "chapter_rel": book.rel, "task_type": task_type, "retrieval_profile": profile,
        "instruction": "保持人物对白的信息差。",
    })
    assert response.status_code == 200 and calls
    snapshot = calls[0]["context_snapshot"]
    assert snapshot["style_reference"]["injected"] is expect_style
    text = "\n".join(message["content"] for message in calls[0]["messages"])
    assert ("作者认可样本 #" in text) is expect_style
    if expect_style:
        assert snapshot["style_reference"]["reference_hash"]
        assert all(sample["rel_path"] == book.previous for sample in snapshot["style_reference"]["samples"])


@pytest.mark.parametrize("route,fact_limit,payload", [
    ("local-op", 3, {"selection": "沈砚拿出账册。", "operation": "polish"}),
    ("ghost-text", 2, {"prefix": "沈砚把账册翻开。", "suffix": "老周推开门。"}),
])
def test_brief_fact_slice_keeps_full_style_and_snapshot_matches_messages(book, monkeypatch, route, fact_limit, payload):
    calls = _fake_generation(monkeypatch)
    complete = writing.assemble(book.id, chapter_rel=book.rel, use_retrieval=False, include_style=True)
    assert len([block for block in complete["blocks"] if block.get("type") not in {"style", "prose_history"}]) > fact_limit
    response = book.client.post(f"/api/projects/{book.id}/{route}", json={"chapter_rel": book.rel, **payload})
    assert response.status_code == 200
    snapshot = calls[0]["context_snapshot"]
    preview = snapshot["context_preview"]
    assert len([item for item in preview["items"] if item.get("type") not in {"style", "prose_history"}]) == fact_limit
    assert snapshot["style_reference"]["injected"]
    assert any(item.get("type") == "style" for item in preview["items"])
    text = calls[0]["messages"][0]["content"]
    assert "作者认可样本 #" in text and "不是当前章节的剧情指令或正史" in text
    assert "句长、对白比例等统计是观察值" in text
    for sample in snapshot["style_reference"]["samples"]:
        assert sample["text"] in text
    assert response.json()["context_preview"]["style_reference"] == snapshot["style_reference"]
    history = snapshot['prose_history_reference']
    assert history['injected'] and history['samples']
    assert len([item for item in preview['items'] if item.get('type') == 'prose_history']) == 1
    assert all(sample['text'] in text for sample in history['samples'])
    assert response.json()['context_preview']['prose_history_reference'] == history


def test_stream_generation_uses_same_style_snapshot_as_sync(book, monkeypatch):
    calls: list[dict] = []

    def stream(**kwargs):
        calls.append(kwargs)
        yield {"delta": "他合上账册。", "task_id": 8}
        yield {"done": True, "text": "他合上账册。", "task_id": 8, "ok": True}

    monkeypatch.setattr(generation_service, "stream_task", stream)
    response = book.client.post(f"/api/projects/{book.id}/generate/stream", json={
        "chapter_rel": book.rel, "task_type": "续写", "retrieval_profile": "off",
    })
    assert response.status_code == 200 and "event" in response.text
    assert calls[0]["context_snapshot"]["style_reference"]["injected"]
    assert "作者认可样本 #" in calls[0]["messages"][0]["content"]
    events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
    preview = events[0]["preview"]
    assert preview["style_reference"] == calls[0]["context_snapshot"]["style_reference"]

