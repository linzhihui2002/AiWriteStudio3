"""M8 向量检索与去AI味软审单测（离线嵌入，不依赖网络与模型下载）。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from workbench.backend import config, db
from workbench.backend.services import (
    chapter_service,
    context_service,
    project_service,
    prompt_registry_service,
    provider_service,
    proposal_service,
    review_service,
    vector_service,
)


@pytest.fixture()
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    projects = tmp_path / "projects"
    runtime = tmp_path / ".workbench"
    skills = tmp_path / "skills"
    for directory in (projects, runtime, skills):
        directory.mkdir()
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(config, "PROJECTS_DIR", projects)
    monkeypatch.setattr(config, "RUNTIME_DIR", runtime)
    monkeypatch.setattr(config, "DB_PATH", runtime / "workbench.db")
    db.init_db()
    config.ensure_runtime_dirs()
    prompt_registry_service.ensure_registry()

    from workbench.backend.engine import router

    monkeypatch.setattr(router.get_engine("dsh-headless"), "available", lambda: False)
    return SimpleNamespace(projects=projects, runtime=runtime, root=tmp_path, skills=skills)


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self.status_code = 200
        self._payload = payload
        self.text = "{}"

    def json(self) -> dict:
        return self._payload


def test_local_embedder_is_deterministic_and_offline(workspace: SimpleNamespace) -> None:
    embedder = vector_service.resolve_embedder(None)
    info = embedder.info()
    assert info["name"] == vector_service.EMBEDDER_LOCAL
    assert info["offline"] is True
    assert info["dim"] == vector_service.DIM_LOCAL

    first = vector_service.embed_local("沈砚在南码头等货")
    second = vector_service.embed_local("沈砚在南码头等货")
    assert first == second
    assert abs(sum(value * value for value in first) - 1.0) < 1e-6

    # 语义相近 > 无关
    similar = vector_service.embed_local("沈砚在南码头等货")
    unrelated = vector_service.embed_local("北境大雪封山")
    dot = lambda a, b: sum(x * y for x, y in zip(a, b))
    assert dot(first, similar) > dot(first, unrelated)

    model_info = vector_service.model_info()
    assert model_info["options"][0]["download_required"] is False
    assert model_info["explicit_path_supported"] is True


def test_vector_rebuild_incremental_and_recall(workspace: SimpleNamespace) -> None:
    project = project_service.create_project(name="向量书")
    project_id = project["id"]
    (workspace.projects / "向量书" / "设定" / "人物设定.md").write_text(
        "---\n标题: 人物设定\n---\n\n## 沈砚\n- 目标：把北边的货取回来\n", encoding="utf-8"
    )

    chapter = chapter_service.create_chapter(project_id, "第一章")
    chapter_service.save_chapter(
        project_id, chapter["rel_path"],
        "沈砚在码头清点铜钱，一共十七枚。他打算先付三成定金。",
    )
    other = chapter_service.create_chapter(project_id, "第二章")
    chapter_service.save_chapter(project_id, other["rel_path"], "北境三年一雪，商队改道南行。")

    assert vector_service.vector_ready(project_id) is False

    rebuilt = vector_service.rebuild(project_id)
    assert rebuilt["chunks"] > 0
    assert vector_service.vector_ready(project_id) is True

    hits = vector_service.semantic_search(project_id, "铜钱定金", limit=3)
    assert hits
    assert hits[0]["source"] == "vector"
    assert hits[0]["score"] > 0
    assert "章节/第0001章.md" in hits[0]["rel_path"]

    # 增量：章节保存后自动入索引（向量库已就绪时）
    chapter_service.save_chapter(project_id, other["rel_path"], "北境盐铁互市，沈砚换到了鱼符。")
    hits2 = vector_service.semantic_search(project_id, "鱼符互市", limit=3)
    assert any("第0002章" in hit["rel_path"] for hit in hits2)

    stats = vector_service.stats(project_id)
    assert stats["documents"] >= 2
    assert stats["embedder"]["name"] == vector_service.EMBEDDER_LOCAL

    # 删除文档 → 向量同步移除
    vector_service.remove_document(project_id, other["rel_path"])
    assert not any("第0002章" in hit["rel_path"]
                   for hit in vector_service.semantic_search(project_id, "鱼符互市", limit=5))


def test_context_level8_uses_vector(workspace: SimpleNamespace) -> None:
    project = project_service.create_project(name="召回书")
    project_id = project["id"]
    chapter = chapter_service.create_chapter(project_id, "第一章")
    chapter_service.save_chapter(project_id, chapter["rel_path"],
                                 "沈砚把青铜鱼符按在桌上，问老周认不认识这个记号。")
    vector_service.rebuild(project_id)

    assembly = context_service.assemble(project_id, query="青铜鱼符")
    level8 = [block for block in assembly["blocks"] if block["level"] == 8]
    assert level8, "检索召回应进入上下文第 8 级"
    assert "相似度" in level8[0]["text"] or "第0001章" in level8[0]["text"]


def test_soft_deslop_creates_line_patches(workspace: SimpleNamespace, monkeypatch) -> None:
    provider_service.upsert_provider("fake", base_url="https://fake.local/v1",
                                     models=[{"id": "m"}], api_key="sk-test-abcdefgh",
                                     enabled=True)
    payload = (
        '{"建议": [{"行": 1, "问题": "解释腔", "原文": "他感到一阵愤怒",'
        ' "建议": "他把碗按在桌上，指节发白。"}]}'
    )
    monkeypatch.setattr(
        "workbench.backend.engine.direct_api.httpx.post",
        lambda *a, **k: _FakeResponse({"choices": [{"message": {"content": payload}}]}),
    )

    project = project_service.create_project(name="软审书")
    project_id = project["id"]
    chapter = chapter_service.create_chapter(project_id, "第一章")
    from workbench.backend.tests.test_gates import GOOD_PROSE
    chapter_service.save_chapter(project_id, chapter["rel_path"],
                                 "他感到一阵愤怒，桌上的碗晃了一下。\n\n" + GOOD_PROSE * 12)

    result = review_service.soft_deslop(project_id, chapter["rel_path"])
    assert result["suggestions"], "软审应产出建议"
    assert result["proposal_ids"], "建议必须进收件箱"

    proposal = proposal_service.get_proposal(result["proposal_ids"][0])
    assert proposal["kind"] == "deslop"
    assert proposal["status"] == "pending"

    applied = proposal_service.apply_proposal(result["proposal_ids"][0])
    assert applied["meta"]["applied"] is True
    text = (workspace.projects / "软审书" / "章节" / "第0001章.md").read_text(encoding="utf-8")
    assert "他把碗按在桌上，指节发白。" in text
    assert "他感到一阵愤怒，桌上的碗晃了一下。" not in text

    # 原行已变化 → 过期建议拒绝应用
    stale = proposal_service.create_proposal(
        project_id=project_id, kind="deslop", title="过期建议",
        target_path=chapter["rel_path"], content="替换内容",
        meta={"patch": {"line": 1, "original": "不存在的一行", "replacement": "替换内容"}},
    )
    proposals_before = proposal_service.get_proposal(stale["id"])
    assert proposals_before["status"] == "pending"
    with pytest.raises(Exception) as excinfo:
        proposal_service.apply_proposal(stale["id"])
    assert "过期" in str(excinfo.value) or "已变化" in str(excinfo.value)
