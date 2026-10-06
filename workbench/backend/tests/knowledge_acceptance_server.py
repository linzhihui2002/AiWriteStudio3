"""Isolated browser fixture: original materials and an explicitly local model stub.

Run with ``python -m workbench.backend.tests.knowledge_acceptance_server``.
This host never imports books, databases, credentials or model weights from the
primary checkout. It is not imported by the production application.
"""
from __future__ import annotations

import json
import os
import re
import time

from workbench.backend import config, db
from workbench.backend.engine import dsh_paths, router
from workbench.backend.engine.direct_api import DirectApiEngine
from workbench.backend.engine.runtime import GenerationResult
from workbench.backend.services import (
    chapter_service, index_service, knowledge_candidate_service as candidates,
    knowledge_service, project_service, vector_service,
)


def _local_attempt(self, request, session_id, should_cancel, attempts, started):
    payload = json.loads(request.messages[-1]["content"])
    text = payload["text"]
    result = {"entities": [], "fields": [], "relations": []}
    for name, value, quote in (
        ("沈砚", "巡桥人", "沈砚在第三章接任巡桥人。"),
        ("阿迟", "守桥人", "阿迟在第四章成为守桥人。"),
    ):
        if quote in text:
            result["fields"].append({"entity": name, "field": "身份", "value": value, "quote": quote})
    quote = "沈砚将把青铜鱼符交给阿迟。"
    if quote in text:
        result["relations"].append({"subject": "沈砚", "object": "阿迟", "relation": "交付鱼符", "quote": quote})
    quote = "许澄是北岸新来的账房。"
    if quote in text:
        result["entities"].append({"name": "许澄", "kind": "character", "description": "北岸账房", "quote": quote})
    quote = "沈砚的年龄更新为二十一岁。"
    if quote in text:
        result["fields"].append({"entity": "沈砚", "field": "年龄", "value": "二十一岁", "quote": quote})
    # Intentionally invalid evidence proves that such output never appears in review.
    result["fields"].append({"entity": "沈砚", "field": "年龄", "value": "八十岁", "quote": "沈砚今年八十岁。"})
    for _ in range(6):
        if should_cancel and should_cancel():
            return GenerationResult(ok=False, cancelled=True, error_message="本地验收抽取已取消")
        time.sleep(0.08)
    return GenerationResult(ok=True, text=json.dumps(result, ensure_ascii=False),
                            engine="direct-api", provider="acceptance-local-stub", model="原创样例固定模型",
                            prompt_tokens=100, completion_tokens=90, duration_ms=480)


def _local_stream(self, request, session_id=None, should_cancel=None):
    self._last_error = None
    if "模型不可用" in str(request.messages):
        self._last_error = RuntimeError("本地验收模拟：问答模型不可用")
        return
    for part in ("可核对", "本书资料", "中的记录", "[1]", "。"):
        if should_cancel and should_cancel():
            return
        time.sleep(0.1)
        yield part


def main():
    from workbench.backend.app import check_port_available, create_app
    import uvicorn

    check_port_available(config.HOST, 8790)
    run_name = os.environ.get("WORKBENCH_ACCEPTANCE_RUN", "knowledge-acceptance")
    if not re.fullmatch(r"knowledge-acceptance(?:-[a-z0-9]+)*", run_name):
        raise ValueError("验收目录名称必须以 knowledge-acceptance 开头且仅包含小写字母、数字和连字符")
    lab = config.runtime_dir() / run_name
    config.PROJECT_ROOT = lab
    config.PROJECTS_DIR = lab / "books"
    config.RUNTIME_DIR = lab / ".workbench"
    config.DB_PATH = config.RUNTIME_DIR / "workbench.db"
    dsh_paths.DSH_HOME_DIR = config.RUNTIME_DIR / "dsh-home"
    config.ensure_runtime_dirs()
    db.init_db()
    # Keep native/model isolation while using the real task, candidate and file services.
    router.resolve_model = lambda **kwargs: {"provider": "acceptance-local-stub", "model": "原创样例固定模型"}
    DirectApiEngine.available = lambda self: True
    DirectApiEngine.get_model_status = lambda self: {"model": "原创样例固定模型", "available": True}
    DirectApiEngine._attempt = _local_attempt
    DirectApiEngine.stream_agent = _local_stream
    vector_service.resolve_embedder = lambda project_id: (_ for _ in ()).throw(RuntimeError("验收环境使用关键词索引"))
    books = project_service.list_projects()
    if not books:
        project = project_service.create_project(name="雾桥记 · 知识验收")
        _, root = project_service.get_project_dir(project["id"])
        characters = ["沈砚", "阿迟", "顾杉", "周遥", "陆青", "秦舟", "江令", "温曲", "苏禾", "方洵", "纪晚", "叶执"]
        (root / "设定/人物设定.md").write_text("# 人物设定\n\n" + "\n\n".join(
            f"## {name}\n- 身份：{'守桥人' if index == 0 else '北岸居民'}\n" +
            (f"- 别名：{name[-1]}生\n" if index < 6 else "") +
            "- 简介：在雾桥镇整理旧账，寻找失去的鱼符。"
            for index, name in enumerate(characters)), encoding="utf-8")
        (root / "设定/物品设定.md").write_text("# 物品设定\n\n## 青铜鱼符\n持有者：沈砚\n用途：开启北岸仓门。\n\n## 雾灯\n持有者：阿迟\n用途：照亮桥洞。\n", encoding="utf-8")
        (root / "设定/世界设定.md").write_text("# 世界设定\n\n## 雾桥镇\n北岸与南岸由石桥相连。\n\n## 北岸仓门\n沈砚保管青铜鱼符，阿迟负责雾灯。\n", encoding="utf-8")
        (root / "状态/角色状态.md").write_text("# 角色状态\n\n## 沈砚\n位置：北岸仓门\n\n## 阿迟\n位置：雾桥镇\n", encoding="utf-8")
        (root / "大纲/大纲.md").write_text("# 后续计划\n沈砚在第三章接任巡桥人。\n阿迟在第四章成为守桥人。\n沈砚将把青铜鱼符交给阿迟。\n", encoding="utf-8")
        chapter = chapter_service.create_chapter(project["id"], title="北岸账房")
        (root / chapter["rel_path"]).write_text("许澄是北岸新来的账房。\n沈砚递给阿迟一盏雾灯。\n", encoding="utf-8")
        index_service.rebuild_index(project["id"])
        second = project_service.create_project(name="悬灯宅 · 隔离验收")
        _, second_root = project_service.get_project_dir(second["id"])
        (second_root / "设定/人物设定.md").write_text("# 人物设定\n\n## 莫衡\n身份：悬灯宅主人。\n", encoding="utf-8")
        knowledge_service.sync(second["id"], background=False, use_ai=False)
        knowledge_service.sync(project["id"], background=False, use_ai=False)
        candidates.extract(project["id"], paths=["大纲/大纲.md", chapter["rel_path"]], background=False)
        archive_project = project_service.create_project(name="原文快照 · 归档验收")
        _, archive_root = project_service.get_project_dir(archive_project["id"])
        archive_path = "设定/人物设定.md"
        (archive_root / archive_path).write_text("# 人物设定\n\n## 沈砚\n- 年龄：二十岁\n- 依据：沈砚的年龄更新为二十一岁。\n", encoding="utf-8")
        knowledge_service.sync(archive_project["id"], background=False, use_ai=False)
        candidates.extract(archive_project["id"], paths=[archive_path], background=False)
    else:
        project = next(book for book in books if book["name"] == "雾桥记 · 知识验收")
    print(f"Knowledge fixture: http://127.0.0.1:8790/project/{project['id']}/knowledge", flush=True)
    uvicorn.run(create_app(), host=config.HOST, port=8790, log_level="warning")


if __name__ == "__main__":
    main()
