"""章节合同服务：生成 / 确认 / 冻结 / CHECK 前置依赖门控。

- 合同是章节生产的输入契约：情节点、字数预算、钩子类型、涉及实体、禁止事项；
- 存 ``.meta/contracts.json``（供编辑器字数里程碑与上下文组装读取）+ ``chapter_contracts`` 索引；
- **冻结后才允许 DRAFT**；冻结后再改必须走差异确认（记录 diff 与确认时间）。
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from .. import config, db
from . import generation_service, operation_log, prompt_registry_service
from .chapter_service import require_chapter_path
from .errors import InvalidOperationError, NodeNotFoundError
from .fs_utils import atomic_write_text, read_text
from .project_service import get_project_dir

CONTRACT_FILE = ".meta/contracts.json"
GATE_FILE = ".meta/gates.json"
HOOK_TYPES = ("悬念", "反转", "情绪", "信息", "危机")


def contracts_path(project_dir: Path) -> Path:
    return Path(project_dir) / CONTRACT_FILE


def _read_all(project_dir: Path) -> dict:
    path = contracts_path(project_dir)
    if not path.is_file():
        return {}
    try:
        data = json.loads(read_text(path))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_all(project_dir: Path, data: dict) -> None:
    atomic_write_text(
        contracts_path(project_dir),
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
    )


def _sync_index(project_id: int, chapter_rel: str, contract: dict) -> None:
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO chapter_contracts (project_id, rel_path, status, plot_points,"
            " word_budget, hook_type, entities, frozen_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(project_id, rel_path) DO UPDATE SET"
            " status = excluded.status, plot_points = excluded.plot_points,"
            " word_budget = excluded.word_budget, hook_type = excluded.hook_type,"
            " entities = excluded.entities, frozen_at = excluded.frozen_at",
            (
                project_id,
                chapter_rel,
                contract.get("status", "draft"),
                json.dumps(contract.get("plot_points") or [], ensure_ascii=False),
                int(contract.get("word_budget") or 0),
                contract.get("hook_type", ""),
                json.dumps(contract.get("entities") or [], ensure_ascii=False),
                contract.get("frozen_at"),
            ),
        )


# ─────────────────────────── 生成 ───────────────────────────


def _deterministic_contract(project_dir: Path, chapter_rel: str) -> dict:
    """零 LLM 兜底合同：从 ``大纲/章纲.md`` 抓本章要点（AI 不可用时的降级路径）。"""
    from .context_service import _chapter_outline_entry  # 复用章纲提取

    entry = _chapter_outline_entry(Path(project_dir), chapter_rel)
    plot_points: list[str] = []
    for line in (entry or "").splitlines():
        stripped = re.sub(r"^[\s*\-•·\d\.、]+", "", line).strip()
        if stripped and not stripped.startswith("#") and len(stripped) > 3:
            plot_points.append(stripped[:120])
    return {
        "plot_points": plot_points[:6] or ["（未找到章纲条目，请手工补充情节点）"],
        "word_budget": config.DEFAULT_WORD_BUDGET,
        "hook_type": "悬念",
        "entities": [],
        "must_connect": [],
        "constraints": [],
        "source": "fallback",
        "status": "draft",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }


def _parse_json_block(text: str) -> dict | None:
    """从模型输出里抠出 JSON（容忍 Markdown 围栏与前后寒暄）。"""
    if not text:
        return None
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidate = fenced.group(1) if fenced else None
    if candidate is None:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            return None
        candidate = text[start:end + 1]
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def generate_contract(
    project_id: int,
    chapter_rel: str,
    *,
    use_ai: bool = True,
    word_budget: int | None = None,
) -> dict:
    """生成章节合同（AI 优先，失败自动降级到章纲提取）。"""
    require_chapter_path(project_id, chapter_rel)
    row, project_dir = get_project_dir(project_id)
    prompt = prompt_registry_service.get_prompt("novel.chapter.contract")

    from .context_service import assemble, to_messages

    context = assemble(project_id, chapter_rel=chapter_rel, query="章节情节点")
    _system, messages = to_messages(context, system=prompt["body"])
    messages.append(
        {
            "role": "user",
            "content": (
                f"请为 {chapter_rel} 产出章节合同。"
                + (f"字数预算按 {word_budget} 字。" if word_budget else "")
            ),
        }
    )

    contract: dict | None = None
    if use_ai:
        result = generation_service.run_task(
            project_id=project_id,
            task_type="章节合同",
            system=prompt["body"],
            messages=messages,
            prompt_id=prompt["prompt_id"],
            prompt_version=prompt["version"],
            context_snapshot={"chapter": chapter_rel, "tokens": context["total_tokens"]},
            temperature=0.4,
        )
        if result["ok"]:
            parsed = _parse_json_block(result["text"])
            if parsed:
                contract = {
                    "plot_points": [str(item) for item in parsed.get("情节点") or []],
                    "word_budget": int(parsed.get("字数预算") or word_budget
                                       or config.DEFAULT_WORD_BUDGET),
                    "hook_type": str(parsed.get("钩子类型") or "悬念"),
                    "entities": [str(item) for item in parsed.get("涉及实体") or []],
                    "must_connect": [str(item) for item in parsed.get("必须承上") or []],
                    "constraints": [str(item) for item in parsed.get("禁止事项") or []],
                    "source": "model",
                }

    if contract is None:
        contract = _deterministic_contract(project_dir, chapter_rel)
        if word_budget:
            contract["word_budget"] = int(word_budget)

    contract.update(
        {
            "status": "draft",
            "chapter_rel": chapter_rel,
            "project_name": row["name"],
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
    )

    data = _read_all(project_dir)
    data[chapter_rel] = contract
    _write_all(project_dir, data)
    _sync_index(project_id, chapter_rel, contract)
    operation_log.log(project_id, "contract-generate", chapter_rel,
                      {"source": contract.get("source")})
    return contract


# ─────────────────────────── 读写与冻结 ───────────────────────────


def get_contract(project_id: int, chapter_rel: str) -> dict:
    require_chapter_path(project_id, chapter_rel)
    _row, project_dir = get_project_dir(project_id)
    contract = _read_all(project_dir).get(chapter_rel)
    if not isinstance(contract, dict):
        raise NodeNotFoundError(f"该章节尚无合同：{chapter_rel}")
    return contract


def list_contracts(project_id: int) -> list[dict]:
    _row, project_dir = get_project_dir(project_id)
    data = _read_all(project_dir)
    return [{"chapter_rel": rel, **value} for rel, value in sorted(data.items())
            if isinstance(value, dict)]


def update_contract(
    project_id: int,
    chapter_rel: str,
    patch: dict,
    *,
    confirm: bool = False,
) -> dict:
    """修改合同；**已冻结**时必须 ``confirm=True``（差异确认）并记录差异。"""
    require_chapter_path(project_id, chapter_rel)
    row, project_dir = get_project_dir(project_id)
    data = _read_all(project_dir)
    current = data.get(chapter_rel)
    if not isinstance(current, dict):
        raise NodeNotFoundError(f"该章节尚无合同：{chapter_rel}")

    if current.get("status") == "frozen" and not confirm:
        raise InvalidOperationError(
            "合同已冻结，修改需差异确认（confirm=true）；确认后将记录本次差异。"
        )

    allowed = {"plot_points", "word_budget", "hook_type", "entities",
               "must_connect", "constraints"}
    diff = {}
    for key, value in (patch or {}).items():
        if key not in allowed:
            continue
        if current.get(key) != value:
            diff[key] = {"old": current.get(key), "new": value}
        current[key] = value
    if diff:
        current["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        current.setdefault("history", []).append({"diff": diff, "confirmed": bool(confirm)})

    data[chapter_rel] = current
    _write_all(project_dir, data)
    _sync_index(project_id, chapter_rel, current)
    operation_log.log(project_id, "contract-update", chapter_rel, {"diff": list(diff)})
    return current


def freeze_contract(project_id: int, chapter_rel: str, *, confirm: bool = True) -> dict:
    """冻结合同（DRAFT 前置条件）。"""
    require_chapter_path(project_id, chapter_rel)
    row, project_dir = get_project_dir(project_id)
    data = _read_all(project_dir)
    contract = data.get(chapter_rel)
    if not isinstance(contract, dict):
        raise NodeNotFoundError(f"该章节尚无合同：{chapter_rel}")
    if not confirm:
        raise InvalidOperationError("冻结需要作者确认（confirm=true）")

    contract["status"] = "frozen"
    contract["frozen_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    data[chapter_rel] = contract
    _write_all(project_dir, data)
    _sync_index(project_id, chapter_rel, contract)
    operation_log.log(project_id, "contract-freeze", chapter_rel, None)
    return contract


def contract_budget(project_id: int, chapter_rel: str) -> int:
    """取本章字数预算（无合同时返回 0，供编辑器里程碑显示）。"""
    try:
        contract = get_contract(project_id, chapter_rel)
    except (NodeNotFoundError, Exception):  # noqa: BLE001
        return 0
    return int(contract.get("word_budget") or 0)


# ─────────────────────────── CHECK 前置依赖门控 ───────────────────────────


def read_gates(project_dir: Path) -> dict:
    path = Path(project_dir) / GATE_FILE
    if not path.is_file():
        return {}
    try:
        data = json.loads(read_text(path))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def write_gates(project_dir: Path, patch: dict) -> dict:
    data = {**read_gates(project_dir), **(patch or {})}
    path = Path(project_dir) / GATE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    return data


def freeze_outline(project_id: int, *, confirm: bool = True) -> dict:
    """冻结大纲（CHECK 门控的一项）。"""
    _row, project_dir = get_project_dir(project_id)
    if not confirm:
        raise InvalidOperationError("冻结大纲需要作者确认")
    data = write_gates(project_dir, {"outline_frozen": True,
                                     "outline_frozen_at": time.strftime("%Y-%m-%dT%H:%M:%S")})
    operation_log.log(project_id, "outline-freeze", "大纲/大纲.md", None)
    return data


def check_prerequisites(project_id: int, chapter_rel: str) -> dict:
    """CHECK 步：前置依赖门控（大纲冻结 / 文风样本 / 知识门控；缺失即阻断并给提示）。

    注意：章节合同不在此处校验——合同由 CONTRACT 步生成并冻结（见 :func:`check_contract_ready`），
    否则 CHECK 与 CONTRACT 会互相等待。
    """
    require_chapter_path(project_id, chapter_rel)
    _row, project_dir = get_project_dir(project_id)
    gates = read_gates(project_dir)
    checks: list[dict] = []

    outline_text = ""
    outline_path = Path(project_dir) / "大纲" / "大纲.md"
    if outline_path.is_file():
        from .fs_utils import split_frontmatter

        outline_text = split_frontmatter(read_text(outline_path))[1].strip()
    checks.append(
        {
            "key": "outline",
            "label": "大纲已就绪并冻结",
            "ok": bool(outline_text) and bool(gates.get("outline_frozen")),
            "hint": "" if outline_text and gates.get("outline_frozen")
            else "请先在大纲页完善并冻结大纲（冻结后才能进入章节生产）",
        }
    )

    samples = 0
    try:
        from .style_service import list_fingerprints

        samples = len(list_fingerprints(project_id))
    except Exception:  # noqa: BLE001 - 文风样本缺失不阻断（软门控提示）
        samples = 0
    checks.append(
        {
            "key": "style",
            "label": "文风样本（可跳过）",
            "ok": samples > 0,
            "optional": True,
            "hint": "" if samples else "尚未标记认可章节作为文风锚点，去AI味软审将缺少参照",
        }
    )

    blocked = [item for item in checks if not item["ok"] and not item.get("optional")]
    return {
        "project_id": project_id,
        "chapter_rel": chapter_rel,
        "checks": checks,
        "blocked": bool(blocked),
        "reasons": [item["hint"] for item in blocked],
    }


def check_contract_ready(project_id: int, chapter_rel: str) -> dict:
    """DRAFT 前置条件：合同必须存在且已冻结（冻结后才允许生成正文）。"""
    require_chapter_path(project_id, chapter_rel)
    try:
        contract = get_contract(project_id, chapter_rel)
    except NodeNotFoundError:
        return {
            "ok": False,
            "hint": "本章尚无合同：请先生成并冻结章节合同（CONTRACT 步）",
            "contract": None,
        }
    frozen = contract.get("status") == "frozen"
    return {
        "ok": frozen,
        "hint": "" if frozen else "章节合同尚未冻结：请确认合同内容后冻结（冻结后修改需差异确认）",
        "contract": contract,
    }


__all__ = [
    "HOOK_TYPES",
    "check_contract_ready",
    "check_prerequisites",
    "contract_budget",
    "freeze_contract",
    "freeze_outline",
    "generate_contract",
    "get_contract",
    "list_contracts",
    "read_gates",
    "update_contract",
    "write_gates",
]
