"""Story retrieval contracts exercised through real creation/context/chat entrypoints."""
from __future__ import annotations

import json
from collections import Counter

import pytest

from workbench.backend import config, db
from workbench.backend.api import writing
from workbench.backend.services import (
    agent_service, chapter_service, chat_run_service, chat_workspace_tools,
    context_service, contract_service, generation_service, index_service,
    knowledge_service, outline_service, project_service, prompt_registry_service,
    review_service, routing_service, workflow_service,
    pipeline_service,
)


@pytest.fixture()
def book(tmp_path, monkeypatch):
    for name in ("projects", "agents", "skills", "rules", "workflows"):
        (tmp_path / name).mkdir()
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setattr(config, "RUNTIME_DIR", tmp_path / ".workbench")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / ".workbench/workbench.db")
    db.init_db()
    config.ensure_runtime_dirs()
    agent_service.ensure_builtin_agents()
    prompt_registry_service.ensure_registry()
    # These entry tests must never download models or start background cloud work.
    monkeypatch.setattr(knowledge_service, "enqueue", lambda *args, **kwargs: None)
    project = project_service.create_project(name="检索接入验收")
    _, root = project_service.get_project_dir(project["id"])
    return project["id"], root


def _hit(path, line=1, text="沈砚许诺归还青铜鱼符。"):
    return {"rel_path": path, "line_start": line, "line_end": line + 1,
            "hash": "original-version", "chapter_no": 1, "source": "hybrid",
            "kind": "chapter", "node_ids": ["grounded-event"], "score": 0.8,
            "text": text, "snippet": text}


def test_capabilities_drive_profile_for_custom_keywords_and_manual_override(book):
    agent_service.create_agent("story-librarian", system_prompt="只查本书依据。",
        capabilities=[{"intent": "旧事定位", "triggers": ["铜符往事"], "skill": "",
                       "task_type": "历史定位", "retrieval_profile": "foreshadow"}],
        retrieval_profile="continuity")
    route = routing_service.classify("铜符往事")
    assert route["agent"] == "story-librarian"
    assert route["retrieval_profile"] == "foreshadow"
    manual = routing_service.route(None, "普通提问", override_agent="story-librarian", record=False)
    assert manual["retrieval_profile"] == "continuity"
    assert any(cap["retrieval_profile"] == "foreshadow" for cap in agent_service.capability_index()
               if cap["agent"] == "story-librarian")


def test_context_derives_queries_and_keeps_individual_grounded_hits(book, monkeypatch):
    project_id, root = book
    (root / "大纲/章纲.md").write_text("第0002章 沈砚追查鱼符，向老周索要旧诺。", encoding="utf-8")
    (root / ".meta").mkdir(exist_ok=True)
    (root / ".meta/contracts.json").write_text(json.dumps({"章节/第0002章.txt": {
        "entities": ["沈砚", "青铜鱼符"], "plot_points": ["沈砚追查鱼符旧诺"]}}, ensure_ascii=False), encoding="utf-8")
    captured = []
    def search(project_id, query, **kwargs):
        captured.append((query, kwargs))
        return {"knowledge_key": "this-book", "hits": [
            _hit("章节/第0001章.txt", line) for line in (1, 4, 8)] +
            [_hit("章节/第0000章.txt", 1)], "degradation": "embedding_unavailable"}
    monkeypatch.setattr(knowledge_service, "search", search)
    assembly = context_service.assemble(project_id, chapter_rel="章节/第0002章.txt", query="继续写这一章")
    assert 1 <= len(captured) <= 3
    assert any("追查鱼符" in query for query, _ in captured)
    assert any("沈砚" in query for query, _ in captured)
    assert all(kwargs["profile"] == "history" for _, kwargs in captured)
    recalled = [b for b in assembly["blocks"] if b["level"] == 8]
    assert len(recalled) == 3
    assert max(Counter(b["retrieval"]["rel_path"] for b in recalled).values()) == 2
    assert sum(b["tokens"] for b in recalled) <= 2400
    assert all(b["retrieval"]["hash"] == "original-version" for b in recalled)
    assert "embedding_unavailable" in assembly["preview"]["retrieval"]["degradation"]


def test_context_budget_and_off_profile_are_visible(book, monkeypatch):
    project_id, _ = book
    calls = []
    monkeypatch.setattr(knowledge_service, "search", lambda *a, **k: calls.append(k) or {
        "hits": [_hit("章节/第0001章.txt", text="旧诺依据。" * 40)], "degradation": []})
    assembly = context_service.assemble(project_id, query="青铜鱼符", explicit="作者要求。" * 40,
                                        budget_tokens=150)
    assert assembly["degradation"]
    assert not any(hit["injected"] for hit in assembly["retrieval"]["hits"])
    calls.clear()
    context_service.assemble(project_id, query="青铜鱼符", retrieval_profile="off")
    assert calls == []


def test_long_memory_evidence_is_clipped_with_its_original_citation(book, monkeypatch):
    project_id, _ = book
    long_quote = "沈砚记得归还鱼符的誓言。" * 400
    monkeypatch.setattr(knowledge_service, "search", lambda *a, **k: {
        "hits": [_hit("章节/第0001章.txt", line=21, text=long_quote)],
        "degradation": [], "knowledge_key": "this-book",
    })
    blocks, audit = context_service._level8(project_id, ["鱼符旧诺"], profile="history",
                                            chapter_rel="章节/第0002章.txt")
    assert len(blocks) == 1
    assert blocks[0]["tokens"] <= context_service.RETRIEVAL_TOKEN_BUDGET
    assert blocks[0]["text"].endswith("…")
    assert blocks[0]["source"] == "章节/第0001章.txt:21-22"
    assert blocks[0]["retrieval"]["excerpt_truncated"] is True
    assert blocks[0]["retrieval"]["hash"] == "original-version"
    assert audit["degradation"]


def test_story_memory_is_read_only_and_available_to_retrieval_agents(book, monkeypatch):
    project_id, _ = book
    calls = []
    monkeypatch.setattr(knowledge_service, "search", lambda *a, **k: calls.append(k) or {
        "hits": [_hit("章节/第0001章.txt")], "degradation": []})
    specs = chat_workspace_tools.list_specs(read_only=True, agent_tools=["向量检索"])
    assert "search_story_memory" in {s["name"] for s in specs}
    assert not any(s["write"] for s in specs)
    result = chat_workspace_tools.execute("search_story_memory", {"query": "之前谁答应归还令牌"},
        {"project_id": project_id, "read_only": True, "chapter_rel": "章节/第0002章.txt",
         "retrieval_profile": "review"})
    assert result["ok"] and not result["write"]
    assert calls[0]["profile"] == "review"
    assert result["data"]["hits"][0]["line_start"] == 1
    denied = chat_workspace_tools.execute("search_story_memory", {"query": "对标", "profile": "teardown"},
        {"project_id": project_id, "retrieval_profile": "history"})
    assert not denied["ok"]


def test_next_plan_step_rereads_saved_materials(book, monkeypatch):
    project_id, root = book
    monkeypatch.setattr(knowledge_service, "search", lambda *a, **k: {"hits": [], "degradation": []})
    path = root / "设定/人物设定.md"
    path.write_text("## 沈砚\n目标：归还旧债。", encoding="utf-8")
    step = {"target_chapter": None, "retrieval_profile": "planning", "read_only_refs": ["设定"]}
    request = {"instruction": "按新设定写大纲", "files": [], "selection": {}, "mode": "只写大纲"}
    old, _ = chat_run_service._fresh_plan_context(project_id, step, request)
    path.write_text("## 沈砚\n目标：找回青铜鱼符。", encoding="utf-8")
    new, preview = chat_run_service._fresh_plan_context(project_id, step, request)
    assert "归还旧债" in old and "找回青铜鱼符" in new
    assert "归还旧债" not in new
    assert preview["retrieval"]["profile"] == "planning"


def test_real_preview_generation_review_and_workflow_entry_profiles(book, monkeypatch):
    project_id, root = book
    chapter = chapter_service.create_chapter(project_id, "查验旧诺")
    rel = chapter["rel_path"]
    (root / "大纲/章纲.md").write_text("第0001章 沈砚查验青铜鱼符的旧诺。", encoding="utf-8")
    captured = []
    monkeypatch.setattr(knowledge_service, "search", lambda *a, **k: captured.append(k) or {
        "hits": [_hit("章节/第0000章.txt")], "degradation": []})
    preview = writing.context_preview(project_id, writing.ContextPreviewIn(chapter_rel=rel))
    assert preview["retrieval"]["queries"] and captured[-1]["profile"] == "history"
    writing._assist_messages(project_id, writing.GenerateIn(chapter_rel=rel, task_type="审稿"))
    assert captured[-1]["profile"] == "review"
    monkeypatch.setattr(generation_service, "run_task", lambda **kwargs: {"ok": True, "text": "{}"})
    review_service._ai_review(project_id, rel, "沈砚说青铜鱼符已归还。")
    assert captured[-1]["profile"] == "review"
    assert captured[-1]["document"]["text"] == "沈砚说青铜鱼符已归还。"
    definition = workflow_service.validate_definition({"name": "带检索场景的流程", "nodes": [{
        "id": "review", "type": "审稿", "retrieval_profile": "review", "instruction": "查鱼符旧诺"}]})
    assert definition["nodes"][0]["retrieval_profile"] == "review"
    assert definition["nodes"][0]["instruction"] == "查鱼符旧诺"
    workflow_service._run_chapter(definition, project_id, rel, use_ai=True)
    assert captured[-1]["profile"] == "review"
    assert any("查鱼符" in q for q in context_service.retrieval_request(
        root, rel, definition["nodes"][0]["instruction"], [])[0])
    contract_service.generate_contract(project_id, rel, use_ai=False)
    assert captured[-1]["profile"] == "history"
    draft_path = root / ".meta/retrieval-test-draft.txt"
    pipeline_service._draft(project_id, rel, draft_path, {}, use_ai=True)
    assert captured[-1]["profile"] == "history"
    calls_before = len(captured)
    writing.local_operation(project_id, writing.LocalOpIn(chapter_rel=rel, selection="一句待润色的正文。",
                                                          operation="polish"))
    writing.ghost_text(project_id, writing.GhostTextIn(chapter_rel=rel, prefix="沈砚抬起头。"))
    assert len(captured) == calls_before


def test_drafts_and_future_summaries_never_enter_automatic_history(book, monkeypatch):
    project_id, root = book
    monkeypatch.setattr(knowledge_service, "search", lambda *a, **k: {"hits": [], "degradation": []})
    chapters = [chapter_service.create_chapter(project_id, f"第{i}章") for i in range(1, 5)]
    for number, chapter in enumerate(chapters, 1):
        (root / chapter["rel_path"]).write_text(f"{number} 章结尾。", encoding="utf-8")
    for chapter in (chapters[0], chapters[3]):
        chapter_service.set_chapter_meta(project_id, chapter["rel_path"], status="完成")
    (root / ".meta").mkdir(exist_ok=True)
    (root / ".meta/summaries.json").write_text(json.dumps({
        chapters[0]["rel_path"]: "已完成旧事", chapters[1]["rel_path"]: "未确认草稿故事",
        chapters[3]["rel_path"]: "未来事件"}, ensure_ascii=False), encoding="utf-8")
    assembly = context_service.assemble(project_id, chapter_rel=chapters[2]["rel_path"])
    history = "\n".join(b["text"] for b in assembly["blocks"] if b["level"] in (3, 7))
    assert "已完成旧事" in history
    assert "未确认草稿故事" not in history and "未来事件" not in history
    previous = next(b for b in assembly["blocks"] if b["level"] == 3)
    assert previous["source"] == chapters[0]["rel_path"]


def test_authoritative_writes_and_state_changes_invalidate_before_sync(book, monkeypatch):
    project_id, root = book
    calls = []
    monkeypatch.setattr(knowledge_service, "invalidate", lambda pid, rel=None: calls.append(("invalidate", pid, rel)))
    monkeypatch.setattr(knowledge_service, "enqueue", lambda pid, rel=None: calls.append(("enqueue", pid, rel)))
    chapter = chapter_service.create_chapter(project_id, "状态变更")
    chapter_service.set_chapter_meta(project_id, chapter["rel_path"], status="完成")
    outline_service.save_outline(project_id, "沈砚找回鱼符。")
    index_service.remove_from_index(project_id, chapter["rel_path"])
    assert any(action == "invalidate" and rel == "大纲/大纲.md" for action, _, rel in calls)
    for position, call in enumerate(calls):
        if call[0] == "enqueue":
            assert calls[position - 1] == ("invalidate", *call[1:])
