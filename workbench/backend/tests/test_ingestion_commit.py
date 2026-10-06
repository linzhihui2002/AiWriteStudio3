"""Grounded extraction and safe state application, entirely inside tmp_path."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from workbench.backend import config, db
from workbench.backend.services import (
    chapter_service, character_service, file_change_service as changes,
    generation_service, ingestion_commit as commits, ingestion_service as ingestion,
    knowledge_service, project_service, prompt_registry_service, proposal_service,
)
from workbench.backend.services.errors import InvalidOperationError

BODY = ("次日清晨，陆衡获得 3 枚铜钱。陆衡把钱收进布袋。\n"
        "伏笔：船工衣袖里的铜钥匙来历不明。\n"
        "船工把木箱搬到南码头。陆衡消耗 1 枚铜钱。")


@pytest.fixture()
def book(tmp_path: Path, monkeypatch) -> SimpleNamespace:
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setattr(config, "RUNTIME_DIR", tmp_path / ".workbench")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / ".workbench/test.db")
    db.init_db()
    config.ensure_runtime_dirs()
    prompt_registry_service.ensure_registry()
    monkeypatch.setattr(knowledge_service, "enqueue", lambda *a, **k: None)
    monkeypatch.setattr(generation_service, "run_task", lambda **k: pytest.fail("No real model is authorized"))
    template = tmp_path / "template"
    for directory in ("章节", "状态", "设定", "大纲"):
        (template / directory).mkdir(parents=True)
    project = project_service.create_project("提取测试书", template=template)
    _, root = project_service.get_project_dir(project["id"])
    chapter = chapter_service.create_chapter(project["id"], "船资")
    path = root / chapter["rel_path"]
    path.write_text(BODY, encoding="utf-8")
    chapter_service.set_chapter_meta(project["id"], chapter["rel_path"], status="完成")
    (root / "设定/人物设定.md").write_text("# 主角：陆衡\n- 性格：沉默\n", encoding="utf-8")
    return SimpleNamespace(id=project["id"], root=root, rel=chapter["rel_path"], path=path)


def _files(book) -> dict:
    return {rel: (book.root / rel).read_text(encoding="utf-8") if (book.root / rel).is_file() else None
            for rel in (ingestion.TIMELINE_FILE, ingestion.LEDGER_FILE, ingestion.FORESHADOW_FILE,
                        ingestion.SUMMARIES_FILE, "状态/角色状态.md", "状态/角色记忆.md")}


def _model(monkeypatch, payload, during=None):
    calls = []
    def extract(*args):
        calls.append(args)
        if during:
            during()
        return payload
    monkeypatch.setattr(ingestion, "extract_structured", extract)
    return calls


def test_same_version_replay_never_reextracts_or_rewrites(book, monkeypatch):
    calls = _model(monkeypatch, {"时间线索": [], "状态变更": [], "资源变更": [], "伏笔": []})
    first = ingestion.ingest_chapter(book.id, book.rel)
    before = _files(book)
    second = ingestion.ingest_chapter(book.id, book.rel)
    assert len(calls) == 1
    assert not first["replayed"] and second["replayed"]
    assert second["run_id"] == first["run_id"]
    assert second["timeline_added"] == second["ledger_added"] == second["foreshadow_added"] == 0
    assert second["proposals_created"] == [] and _files(book) == before
    assert second["counts"]["ledger"]["skipped"] == 2


def test_windows_path_alias_replays_same_commit(book):
    first = ingestion.ingest_chapter(book.id, book.rel, use_ai=False)
    alias = ingestion.ingest_chapter(book.id, book.rel.replace("/", "\\"), use_ai=False)
    assert alias["replayed"] and alias["run_id"] == first["run_id"]
    assert len(ingestion.list_ledger(book.id)) == 2


def test_model_private_event_offsets_do_not_break_grounded_items(book, monkeypatch):
    _model(monkeypatch, {"资源变更": [{"物品": "铜钱", "增减": 3,
        "依据": BODY.splitlines()[0], "_event_start": "broken", "_event_end": None}],
        "状态变更": [], "时间线索": [], "伏笔": []})
    result = ingestion.ingest_chapter(book.id, book.rel)
    assert result["ledger_added"] == 2


def test_rebound_chapter_updates_receipt_summary_and_keeps_payoff(book):
    ingestion.ingest_chapter(book.id, book.rel, use_ai=False)
    clue = ingestion.list_foreshadows(book.id)[0]
    ingestion.confirm_payoff(book.id, clue["line"], expected_hash=clue["document_hash"])
    new_rel = "章节/第0002章.txt"
    book.path.rename(book.root / new_rel)
    chapter_service.move_chapter_meta(book.id, book.rel, new_rel)
    summaries = json.loads((book.root / ingestion.SUMMARIES_FILE).read_text(encoding="utf-8"))
    assert new_rel in summaries and book.rel not in summaries
    ingestion.ingest_chapter(book.id, new_rel, use_ai=False)
    assert len(ingestion.list_ledger(book.id)) == 2
    assert ingestion.list_foreshadows(book.id)[0]["status"] == "已回收"


def test_regex_and_ai_same_event_do_not_duplicate_but_two_occurrences_remain(book, monkeypatch):
    body = "次日，陆衡获得 3 枚铜钱。陆衡获得 3 枚铜钱。\n伏笔：陆衡发现铜钥匙。"
    book.path.write_text(body, encoding="utf-8")
    _model(monkeypatch, {"资源变更": [{"物品": "铜钱", "增减": 3, "依据": body.splitlines()[0]}],
                        "伏笔": [], "时间线索": [], "状态变更": []})
    result = ingestion.ingest_chapter(book.id, book.rel)
    assert result["ledger_added"] == 2
    assert len(ingestion.list_ledger(book.id)) == 2
    assert all(row["delta"] == "+3" and row["before"] == "待核实" and row["balance"] == "待核实"
               and row["evidence"] in body for row in ingestion.list_ledger(book.id))


@pytest.mark.parametrize("bad", [
    {"物品": "铜钱", "增减": "not-a-number", "依据": BODY.splitlines()[0]},
    {"物品": "铜钱", "增减": float("nan"), "依据": BODY.splitlines()[0]},
    {"物品": "铜钱", "增减": float("inf"), "依据": BODY.splitlines()[0]},
    {"物品": "铜钱", "增减": True, "依据": BODY.splitlines()[0]},
    {"物品": "铜钱", "增减": -3, "依据": BODY.splitlines()[0]},
    {"物品": "铜钱", "增减": 999, "依据": BODY.splitlines()[0]},
    {"物品": "铜钱", "增减": 3, "依据": "从未出现的模型引文"},
])
def test_invalid_ai_resource_is_reported_without_hiding_valid_deterministic_items(book, monkeypatch, bad):
    _model(monkeypatch, {"资源变更": [bad], "状态变更": [], "时间线索": [], "伏笔": []})
    result = ingestion.ingest_chapter(book.id, book.rel)
    assert result["counts"]["ledger"]["rejected"] == 1
    assert result["ledger_added"] == 2 and result["rejected"]
    assert len(ingestion.list_ledger(book.id)) == 2


def test_model_exception_falls_back_and_never_writes_omniscient_memory(book, monkeypatch):
    def broken(*args):
        raise RuntimeError("isolated provider failure")
    monkeypatch.setattr(ingestion, "extract_structured", broken)
    result = ingestion.ingest_chapter(book.id, book.rel)
    assert not result["ai_used"] and result["warnings"]
    assert result["memory_added"] == 0 and "陆衡" in result["characters_present"]
    assert not (book.root / "状态/角色记忆.md").exists()
    with db.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM character_memory").fetchone()[0] == 0


def test_source_change_or_delete_during_model_leaves_no_projection(book, monkeypatch):
    before = _files(book)
    _model(monkeypatch, {"伏笔": []}, during=lambda: book.path.write_text(BODY + "\n作者已改稿。", encoding="utf-8"))
    with pytest.raises(changes.FileConflictError, match="正文"):
        ingestion.ingest_chapter(book.id, book.rel)
    assert _files(book) == before
    with db.get_conn() as conn:
        assert conn.execute("SELECT status FROM ingestion_runs").fetchone()[0] == "rolled_back"


def test_concurrent_same_chapter_is_rejected_while_the_original_claim_finishes(book, monkeypatch):
    def during():
        with pytest.raises(changes.FileConflictError, match="正在提取"):
            ingestion.ingest_chapter(book.id, book.rel)
    calls = _model(monkeypatch, {"伏笔": []}, during=during)
    result = ingestion.ingest_chapter(book.id, book.rel)
    assert result["run_id"] and len(calls) == 1


def test_changed_version_replaces_own_rows_and_preserves_author_material_and_payoff(book):
    manual = "# 时间线\n\n作者原有记录，不属于托管条目。\n"
    (book.root / ingestion.TIMELINE_FILE).write_text(manual, encoding="utf-8")
    ingestion.ingest_chapter(book.id, book.rel, use_ai=False)
    clue = ingestion.list_foreshadows(book.id)[0]
    ingestion.confirm_payoff(book.id, clue["line"], expected_hash=clue["document_hash"], planned_chapter="第0008章")
    book.path.write_text(BODY.replace("获得 3", "获得 5"), encoding="utf-8")
    result = ingestion.ingest_chapter(book.id, book.rel, use_ai=False)
    assert not result["replayed"]
    assert manual.strip() in (book.root / ingestion.TIMELINE_FILE).read_text(encoding="utf-8")
    assert [entry["delta"] for entry in ingestion.list_ledger(book.id)] == ["+5", "-1"]
    foreshadows = ingestion.list_foreshadows(book.id)
    assert len(foreshadows) == 1
    assert foreshadows[0]["status"] == "已回收" and foreshadows[0]["planned_chapter"] == "第0008章"


def test_author_edit_inside_managed_block_is_not_overwritten(book):
    ingestion.ingest_chapter(book.id, book.rel, use_ai=False)
    path = book.root / ingestion.TIMELINE_FILE
    path.write_text(path.read_text(encoding="utf-8").replace("次日", "作者修订的时间", 1), encoding="utf-8")
    before = _files(book)
    book.path.write_text(BODY + "\n新的收尾。", encoding="utf-8")
    with pytest.raises(changes.FileConflictError, match="人工修改"):
        ingestion.ingest_chapter(book.id, book.rel, use_ai=False)
    assert _files(book) == before


@pytest.mark.parametrize("stage", ["second_file", "finish_after_summary"])
def test_interrupted_commit_rolls_back_all_own_files_and_summary(book, monkeypatch, stage):
    before = _files(book)
    if stage == "second_file":
        original = changes.apply_change
        calls = []
        def broken(*args, **kwargs):
            calls.append(kwargs["rel_path"])
            if len(calls) == 2:
                raise OSError("injected disk failure")
            return original(*args, **kwargs)
        monkeypatch.setattr(changes, "apply_change", broken)
    else:
        monkeypatch.setattr(commits, "_finish", lambda *a: (_ for _ in ()).throw(OSError("injected finish failure")))
    with pytest.raises(OSError):
        ingestion.ingest_chapter(book.id, book.rel, use_ai=False)
    assert _files(book) == before
    with db.get_conn() as conn:
        assert conn.execute("SELECT status FROM ingestion_runs").fetchone()[0] == "rolled_back"
        assert conn.execute("SELECT COUNT(*) FROM proposals").fetchone()[0] == 0


def test_recovery_completes_all_written_commit_without_duplicate_proposals(book, monkeypatch):
    # Simulate a process dying after all disk writes but before publishing the
    # receipt: abandon does not run on abrupt termination.
    finish = commits._finish
    monkeypatch.setattr(commits, "_finish", lambda *a: (_ for _ in ()).throw(OSError("process stopped")))
    monkeypatch.setattr(commits, "abandon", lambda *a: None)
    with pytest.raises(OSError):
        ingestion.ingest_chapter(book.id, book.rel, use_ai=False)
    before = _files(book)
    monkeypatch.setattr(commits, "_finish", finish)
    replay = ingestion.ingest_chapter(book.id, book.rel, use_ai=False)
    assert replay["replayed"] and _files(book) == before
    with db.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM ingestion_runs WHERE status='committed'").fetchone()[0] == 1


def test_original_version_after_newer_version_is_submitted_again(book):
    first = ingestion.ingest_chapter(book.id, book.rel, use_ai=False)
    book.path.write_text(BODY.replace("获得 3", "获得 5"), encoding="utf-8")
    ingestion.ingest_chapter(book.id, book.rel, use_ai=False)
    book.path.write_text(BODY, encoding="utf-8")
    restored = ingestion.ingest_chapter(book.id, book.rel, use_ai=False)
    assert not restored["replayed"] and restored["run_id"] != first["run_id"]
    assert ingestion.list_ledger(book.id)[0]["delta"] == "+3"


def _state(book, monkeypatch):
    before = "# 陆衡（开书基准）\n\n| 项目 | 值 | 来源 |\n| --- | --- | --- |\n| 位置 | 北码头 | author |\n| 伤势 | 无伤 | author |\n\n## 船工\n- 位置：船上\n- 心理：戒备\n"
    (book.root / "状态/角色状态.md").write_text(before, encoding="utf-8")
    quote = BODY.splitlines()[-1]
    _model(monkeypatch, {"状态变更": [
        {"角色": "陆衡", "字段": "持有物", "新值": "铜钱", "依据": BODY.splitlines()[0]},
        {"角色": "船工", "字段": "位置", "新值": "南码头", "依据": quote},
    ], "伏笔": [], "时间线索": [], "资源变更": []})
    return before


def test_one_full_document_state_proposal_preserves_other_roles_fields_and_tables(book, monkeypatch):
    before = _state(book, monkeypatch)
    result = ingestion.ingest_chapter(book.id, book.rel)
    assert len(result["proposals_created"]) == 1
    proposal = proposal_service.get_proposal(result["proposals_created"][0])
    assert proposal["kind"] == "state" and proposal["base_hash"] == changes.content_hash(before)
    assert "| 持有物 | 铜钱 |  |" in proposal["content"]
    assert "| 伤势 | 无伤 | author |" in proposal["content"]
    assert "- 位置：南码头" in proposal["content"] and "- 心理：戒备" in proposal["content"]
    assert (book.root / "状态/角色状态.md").read_text(encoding="utf-8") == before
    proposal_service.apply_proposal(proposal["id"])
    assert character_service.list_states(book.id)["陆衡"]["持有物"] == "铜钱"
    assert character_service.list_states(book.id)["船工"]["心理"] == "戒备"


def test_state_existing_property_cell_escapes_column_delimiters():
    base = "# 陆衡\n\n| 项目 | 值 | 来源 |\n| --- | --- | --- |\n| 位置 | 北码头 | author |\n| 伤势 | 无伤 | author |\n"
    candidate = character_service.compose_state_document(base, [{"角色": "陆衡", "字段": "位置", "新值": "码头|渡口"}])
    assert "| 位置 | 码头\\|渡口 | author |" in candidate
    assert "| 伤势 | 无伤 | author |" in candidate


@pytest.mark.parametrize("change", ["source", "target"])
def test_state_proposal_verifies_both_source_and_target_versions(book, monkeypatch, change):
    _state(book, monkeypatch)
    result = ingestion.ingest_chapter(book.id, book.rel)
    proposal_id = result["proposal_ids"][0]
    path = book.path if change == "source" else book.root / "状态/角色状态.md"
    path.write_text(path.read_text(encoding="utf-8") + "\n作者有新修改。", encoding="utf-8")
    before = path.read_text(encoding="utf-8")
    with pytest.raises(changes.FileConflictError):
        proposal_service.apply_proposal(proposal_id)
    assert path.read_text(encoding="utf-8") == before


def test_explicit_reproposal_preserves_new_manual_roles_without_reextracting(book, monkeypatch):
    _state(book, monkeypatch)
    result = ingestion.ingest_chapter(book.id, book.rel)
    old_id = result["proposal_ids"][0]
    path = book.root / "状态/角色状态.md"
    path.write_text(path.read_text(encoding="utf-8") + "\n## 作者新角色\n- 位置：茶馆\n", encoding="utf-8")
    current = path.read_text(encoding="utf-8")
    rebase = proposal_service.rebase_state_proposal(old_id)
    assert rebase["proposal_id"] != old_id and "作者新角色" in rebase["proposal"]["content"]
    assert path.read_text(encoding="utf-8") == current
    assert proposal_service.get_proposal(old_id)["status"] == "discarded"
    replay = ingestion.ingest_chapter(book.id, book.rel)
    assert replay["replayed"] and replay["proposals_created"] == []
    assert replay["proposal_ids"] == [rebase["proposal_id"]]
    proposal_service.apply_proposal(rebase["proposal_id"])
    assert character_service.list_states(book.id)["作者新角色"]["位置"] == "茶馆"


def test_reproposal_refuses_changed_source_and_leaves_original_pending(book, monkeypatch):
    _state(book, monkeypatch)
    result = ingestion.ingest_chapter(book.id, book.rel)
    old_id = result["proposal_ids"][0]
    book.path.write_text(BODY + "\n作者新改稿。", encoding="utf-8")
    with pytest.raises(changes.FileConflictError, match="来源正文"):
        proposal_service.rebase_state_proposal(old_id)
    assert proposal_service.get_proposal(old_id)["status"] == "pending"
    assert len(proposal_service.list_proposals(book.id)) == 1


def test_reproposal_of_already_matching_state_creates_no_extra_candidate(book, monkeypatch):
    _state(book, monkeypatch)
    result = ingestion.ingest_chapter(book.id, book.rel)
    old = proposal_service.get_proposal(result["proposal_ids"][0])
    path = book.root / "状态/角色状态.md"
    path.write_text(old["content"], encoding="utf-8")
    rebase = proposal_service.rebase_state_proposal(old["id"])
    assert rebase["already_current"] and rebase["proposal_id"] is None
    assert proposal_service.list_proposals(book.id) == []


def test_legacy_fragment_read_diff_and_apply_share_preserving_materialization(book):
    base = "## 陆衡\n- 位置：北码头\n- 伤势：无伤\n\n## 船工\n- 心理：戒备\n"
    (book.root / "状态/角色状态.md").write_text(base, encoding="utf-8")
    proposal = proposal_service.create_proposal(project_id=book.id, kind="state", title="旧状态片段",
        target_path="状态/角色状态.md", content="- 位置：南码头", meta={"ingest": True, "character": "陆衡",
        "field": "位置", "chapter": book.rel, "evidence": BODY.splitlines()[0]})
    assert "- 伤势：无伤" in proposal["content"] and "## 船工" in proposal["content"]
    listed = proposal_service.list_proposals(book.id)[0]
    assert listed["content"] == proposal["content"]
    assert not any(item["type"] == "remove" and "船工" in item["text"] for item in proposal["diff"])
    proposal_service.apply_proposal(proposal["id"])
    assert (book.root / "状态/角色状态.md").read_text(encoding="utf-8") == proposal["content"]


def test_unsafe_legacy_fragment_missing_base_cannot_be_applied(book):
    proposal = proposal_service.create_proposal(project_id=book.id, kind="state", title="无基准旧提案",
        target_path="状态/角色状态.md", content="- 位置：南码头", meta={"ingest": True, "character": "陆衡", "field": "位置"})
    assert proposal["meta"]["application_error"]
    with pytest.raises(InvalidOperationError, match="基准"):
        proposal_service.apply_proposal(proposal["id"])
    assert not (book.root / "状态/角色状态.md").exists()


def test_payoff_is_precise_idempotent_and_stale_hash_is_rejected(book):
    text = "# 伏笔管理\n\n| 编号 | 内容 | 状态 | 埋设章 | 计划回收 | 依据 |\n| --- | --- | --- | --- | --- | --- |\n| F1 | 铜钥匙 | 待回收 | 第0001章 | 第0008章 | 铜钥匙出现 |\n| F2 | 木箱 | 候选 | 未定 | 未定 | 待补 |\n"
    path = book.root / ingestion.FORESHADOW_FILE
    path.write_text(text, encoding="utf-8")
    first, second = ingestion.list_foreshadows(book.id)
    result = ingestion.confirm_payoff(book.id, first["line"], expected_hash=first["document_hash"], planned_chapter="第0010章")
    assert "F1 | 铜钥匙 | 已回收 | 第0001章 | 第0010章" in path.read_text(encoding="utf-8")
    assert "F2 | 木箱 | 候选" in path.read_text(encoding="utf-8")
    with pytest.raises(changes.FileConflictError):
        ingestion.confirm_payoff(book.id, first["line"], expected_hash=first["document_hash"])
    again = ingestion.confirm_payoff(book.id, first["line"], expected_hash=result["document_hash"], planned_chapter="第0010章")
    assert again["idempotent"]
    with pytest.raises(InvalidOperationError, match="候选"):
        ingestion.confirm_payoff(book.id, second["line"], expected_hash=result["document_hash"])


def test_legacy_unique_generated_rows_are_adopted_and_payoff_is_preserved(book):
    chapter = Path(book.rel).stem
    quote = BODY.splitlines()[1]
    text = f"# 伏笔管理\n作者自由笔记保留。\n- [已回收] 船工衣袖里的铜钥匙来历不明｜埋设：{chapter}｜依据：{quote}｜计划回收：第0008章\n"
    (book.root / ingestion.FORESHADOW_FILE).write_text(text, encoding="utf-8")
    clue = ingestion.extract_time_clues(BODY)[0]
    (book.root / ingestion.TIMELINE_FILE).write_text(
        f"- {chapter}｜{clue['线索']}｜旧系统摘要｜依据：{clue['依据']}\n", encoding="utf-8")
    result = ingestion.ingest_chapter(book.id, book.rel, use_ai=False)
    rows = ingestion.list_foreshadows(book.id)
    assert len(rows) == 1 and rows[0]["status"] == "已回收" and rows[0]["planned_chapter"] == "第0008章"
    assert len(ingestion.list_timeline(book.id)) == 2  # 次日、清晨，各一个事件
    assert any("接管" in warning for warning in result["warnings"])
    assert "作者自由笔记保留。" in (book.root / ingestion.FORESHADOW_FILE).read_text(encoding="utf-8")


def test_ambiguous_legacy_duplicates_are_kept_and_reported(book):
    chapter = Path(book.rel).stem
    quote = BODY.splitlines()[1]
    row = f"- [待回收] 船工衣袖里的铜钥匙来历不明｜埋设：{chapter}｜依据：{quote}\n"
    (book.root / ingestion.FORESHADOW_FILE).write_text(row + row, encoding="utf-8")
    result = ingestion.ingest_chapter(book.id, book.rel, use_ai=False)
    assert any("2 条可能重复" in warning for warning in result["warnings"])
    assert (book.root / ingestion.FORESHADOW_FILE).read_text(encoding="utf-8").startswith(row + row)


def test_ai_quote_extension_preserves_confirmed_same_foreshadow(book, monkeypatch):
    body = "陆衡在码头看到黑色铜钥匙。"
    book.path.write_text(body, encoding="utf-8")
    _model(monkeypatch, {"伏笔": [{"内容": "黑色铜钥匙", "依据": body}]})
    ingestion.ingest_chapter(book.id, book.rel)
    clue = ingestion.list_foreshadows(book.id)[0]
    ingestion.confirm_payoff(book.id, clue["line"], expected_hash=clue["document_hash"], planned_chapter="第0008章")
    revised = body + "他等了一会儿。"
    book.path.write_text(revised, encoding="utf-8")
    _model(monkeypatch, {"伏笔": [{"内容": "黑色铜钥匙", "依据": revised}]})
    ingestion.ingest_chapter(book.id, book.rel)
    rows = ingestion.list_foreshadows(book.id)
    assert len(rows) == 1 and rows[0]["status"] == "已回收" and rows[0]["planned_chapter"] == "第0008章"


def test_removed_confirmed_foreshadow_is_retained_with_warning(book):
    ingestion.ingest_chapter(book.id, book.rel, use_ai=False)
    clue = ingestion.list_foreshadows(book.id)[0]
    ingestion.confirm_payoff(book.id, clue["line"], expected_hash=clue["document_hash"])
    book.path.write_text(BODY.replace(BODY.splitlines()[1], "作者删掉了这一段。"), encoding="utf-8")
    result = ingestion.ingest_chapter(book.id, book.rel, use_ai=False)
    assert ingestion.list_foreshadows(book.id)[0]["status"] == "已回收"
    assert any("未再次提及" in warning for warning in result["warnings"])
    assert ingestion.ingest_chapter(book.id, book.rel, use_ai=False)["replayed"]


def test_reproposal_refuses_to_discard_saved_manual_candidate_edits(book, monkeypatch):
    _state(book, monkeypatch)
    result = ingestion.ingest_chapter(book.id, book.rel)
    original = proposal_service.get_proposal(result["proposal_ids"][0])
    edited = original["content"].replace("南码头", "茶馆")
    proposal_service.update_proposal_content(original["id"], edited)
    with pytest.raises(InvalidOperationError, match="人工编辑"):
        proposal_service.rebase_state_proposal(original["id"])
    retained = proposal_service.get_proposal(original["id"])
    assert retained["status"] == "pending" and retained["content"] == edited
