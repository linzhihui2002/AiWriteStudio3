"""技能管理（Task 39 / 51）：skills/ 源目录 → .dsh/skills 同步 + 注册 + 启停 + AI 辅助生成。

dsh 侧机制（已实测，见调研归档）：
- 技能发现路径 ``<projectRoot>/.dsh/skills``（rank 100），**只支持两层结构**：
  ``.dsh/skills/<name>/SKILL.md``，frontmatter 必须含 ``name``（kebab-case）与 ``description``；
- AGENTS.md 注入预算 65536B，因此技能同步要做**体积校验**，超预算即告警。

纪律：``skills/`` 是源目录，``.dsh/skills/`` 是**同步产物**（禁止手工编辑）。
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import yaml

from .. import config, db
from . import generation_service
from .errors import InvalidNameError, InvalidOperationError, NodeNotFoundError
from .fs_utils import atomic_write_text, read_text

SOURCES = ("builtin", "workbench_custom", "imported")
KEBAB_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
FRONTMATTER_RE = re.compile(r"^---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|$)", re.DOTALL)

SKILL_BUDGET_BYTES = 65536          # 与 AGENTS.md 注入预算同量级
SKILL_FILE_MAX_BYTES = 32768        # 单技能体积上限
MAX_DESCRIPTION_CHARS = 200
SKILL_INJECT_BUDGET_CHARS = 8000    # 单轮对话注入技能正文的总字符预算（技能正文各约 2-3KB）


# ─────────────────────────── 解析与校验 ───────────────────────────


def parse_skill(text: str) -> tuple[dict, str]:
    """拆分 SKILL.md 的 frontmatter 与正文。"""
    match = FRONTMATTER_RE.match(text or "")
    if match is None:
        return {}, text or ""
    try:
        data = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        data = None
    meta = data if isinstance(data, dict) else {}
    return meta, (text or "")[match.end():]


def validate_skill_text(text: str, *, fallback_name: str = "") -> dict:
    """校验 SKILL.md：frontmatter 必须含 kebab-case ``name`` 与非空 ``description``。"""
    meta, body = parse_skill(text)
    name = str(meta.get("name") or fallback_name or "").strip()
    description = str(meta.get("description") or "").strip()

    if not name:
        raise InvalidNameError("SKILL.md frontmatter 缺少 name")
    if not KEBAB_RE.match(name):
        raise InvalidNameError(f"技能 name 必须是 kebab-case（小写字母/数字/连字符）：{name}")
    if not description:
        raise InvalidNameError("SKILL.md frontmatter 缺少 description（dsh 依赖它做技能发现）")
    if len(description) > MAX_DESCRIPTION_CHARS:
        description = description[:MAX_DESCRIPTION_CHARS]
    if not body.strip():
        raise InvalidNameError("SKILL.md 正文为空")
    if len(text.encode("utf-8")) > SKILL_FILE_MAX_BYTES:
        raise InvalidOperationError(
            f"技能文件过大（>{SKILL_FILE_MAX_BYTES}B）：请拆分或精简正文"
        )
    return {"name": name, "description": description, "body": body, "meta": meta,
            "size_bytes": len(text.encode("utf-8"))}


# ─────────────────────────── 源目录扫描 ───────────────────────────


def skills_root() -> Path:
    return Path(config.skills_dir())


def dsh_skills_root() -> Path:
    return Path(config.dsh_skills_dir())


def iter_skill_files(root: Path | None = None) -> list[Path]:
    """列出源目录下的 SKILL.md（支持两层：skills/<name>/SKILL.md）。"""
    base = Path(root or skills_root())
    if not base.is_dir():
        return []
    return sorted(base.glob("*/SKILL.md"))


def list_skills() -> list[dict]:
    """技能列表（源目录 + 同步状态 + 注册信息）。"""
    base = skills_root()
    synced = dsh_skills_root()
    registered = {row["name"]: row for row in _registry_rows()}

    items: list[dict] = []
    for path in iter_skill_files(base):
        text = read_text(path)
        try:
            info = validate_skill_text(text, fallback_name=path.parent.name)
        except (InvalidNameError, InvalidOperationError) as exc:
            items.append(
                {
                    "name": path.parent.name,
                    "description": "",
                    "source": "invalid",
                    "path": str(path),
                    "enabled": False,
                    "size_bytes": len(text.encode("utf-8")),
                    "error": exc.message,
                    "synced": False,
                }
            )
            continue

        row = registered.get(info["name"])
        target = synced / info["name"] / "SKILL.md"
        items.append(
            {
                "name": info["name"],
                "description": info["description"],
                "source": (row["source"] if row else "builtin"),
                "path": str(path),
                "enabled": bool(row["enabled"]) if row else True,
                "size_bytes": info["size_bytes"],
                "synced": target.is_file()
                and target.stat().st_size == info["size_bytes"],
                "synced_path": str(target),
                "error": "",
            }
        )
    return items


def _registry_rows() -> list[dict]:
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT name, description, source, path, enabled, size_bytes FROM skills"
        ).fetchall()
    return [dict(row) for row in rows]


def get_skill(name: str) -> dict:
    for item in list_skills():
        if item["name"] == name:
            path = Path(item["path"])
            text = read_text(path) if path.is_file() else ""
            info = validate_skill_text(text, fallback_name=name) if text else {}
            return {**item, "content": text, **{k: v for k, v in info.items() if k == "meta"}}
    raise NodeNotFoundError(f"技能不存在：{name}")


# ─────────────────────────── 注入用编译（对话系统提示） ───────────────────────────


def skill_digest(names, *, budget_chars: int = SKILL_INJECT_BUDGET_CHARS) -> str:
    """把绑定技能的正文编译为可注入文本（供对话系统提示）。

    顺序去重；跳过不存在与已停用的技能；总长超预算时按顺序截断并标注。
    """
    parts: list[str] = []
    seen: set[str] = set()
    for raw in names or []:
        name = str(raw or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        try:
            item = get_skill(name)
        except NodeNotFoundError:
            continue
        if not item.get("enabled", True):
            continue
        _meta, body = parse_skill(str(item.get("content") or ""))
        if not body.strip():
            continue
        parts.append(f"### 技能 {name}（{item.get('description', '')}）\n{body.strip()}")

    if not parts:
        return ""
    text = "\n\n".join(parts)
    if len(text) > budget_chars:
        text = text[:budget_chars] + "\n…（技能正文超预算，已截断）"
    return text


# ─────────────────────────── 同步器 ───────────────────────────


def sync_skills(*, prune: bool = True) -> dict:
    """把 ``skills/`` 同步到 ``.dsh/skills/``（补 frontmatter 适配、两层拍平、体积校验）。"""
    base = skills_root()
    target_root = dsh_skills_root()
    target_root.mkdir(parents=True, exist_ok=True)

    synced: list[dict] = []
    warnings: list[str] = []
    errors: list[dict] = []
    total_bytes = 0
    expected_dirs: set[str] = set()

    for path in iter_skill_files(base):
        text = read_text(path)
        try:
            info = validate_skill_text(text, fallback_name=path.parent.name)
        except (InvalidNameError, InvalidOperationError) as exc:
            errors.append({"path": str(path), "error": exc.message})
            continue

        name = info["name"]
        expected_dirs.add(name)
        target_dir = target_root / name
        target_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_text(target_dir / "SKILL.md", text)
        total_bytes += info["size_bytes"]

        _register(name, info["description"], path, info["size_bytes"])
        synced.append({"name": name, "path": str(target_dir / "SKILL.md"),
                       "size_bytes": info["size_bytes"]})

    if prune:
        for directory in target_root.iterdir():
            if not directory.is_dir() or directory.name in expected_dirs:
                continue
            shutil.rmtree(directory, ignore_errors=True)
            warnings.append(f"已清理源目录中不存在的同步产物：{directory.name}")

    if total_bytes > SKILL_BUDGET_BYTES:
        warnings.append(
            f"技能总体积 {total_bytes}B 超过预算 {SKILL_BUDGET_BYTES}B，"
            "建议精简技能正文（AGENTS.md 注入预算同量级）"
        )

    with db.get_conn() as conn:
        enabled_names = {
            row["name"] for row in conn.execute(
                "SELECT name FROM skills WHERE enabled = 1").fetchall()
        }
    disabled = [item for item in synced if item["name"] not in enabled_names]

    return {
        "synced": synced,
        "count": len(synced),
        "total_bytes": total_bytes,
        "budget_bytes": SKILL_BUDGET_BYTES,
        "warnings": warnings,
        "errors": errors,
        "managed": len(disabled),
        "target_root": str(target_root),
    }


def _register(name: str, description: str, path: Path, size_bytes: int,
              source: str = "builtin") -> None:
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO skills (name, description, source, path, size_bytes)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(name) DO UPDATE SET"
            " description = excluded.description, path = excluded.path,"
            " size_bytes = excluded.size_bytes",
            (name, description, source, str(path), int(size_bytes)),
        )


# ─────────────────────────── 增删改 / 启停 ───────────────────────────


def create_skill(name: str, description: str, body: str,
                 *, source: str = "workbench_custom", sync: bool = True) -> dict:
    """新建技能（写入 ``skills/<name>/SKILL.md``）。"""
    if source not in SOURCES:
        raise InvalidOperationError(f"来源必须是 {'/'.join(SOURCES)}")
    name = name.strip()
    if not KEBAB_RE.match(name):
        raise InvalidNameError(f"技能名必须是 kebab-case：{name}")

    directory = skills_root() / name
    if (directory / "SKILL.md").exists():
        raise InvalidOperationError(f"同名技能已存在：{name}")
    directory.mkdir(parents=True, exist_ok=True)

    meta = {"name": name, "description": description.strip()}
    text = "---\n" + yaml.safe_dump(meta, allow_unicode=True, sort_keys=False) + \
        "---\n\n" + body.strip() + "\n"
    info = validate_skill_text(text, fallback_name=name)
    atomic_write_text(directory / "SKILL.md", text)
    _register(name, info["description"], directory / "SKILL.md", info["size_bytes"], source)
    if sync:
        sync_skills()
    return get_skill(name)


def update_skill(name: str, text: str, *, sync: bool = True) -> dict:
    """整篇更新 SKILL.md（frontmatter + 正文）。"""
    path = _skill_path(name)
    info = validate_skill_text(text, fallback_name=name)
    if info["name"] != name:
        raise InvalidNameError(f"frontmatter name（{info['name']}）与目录名（{name}）不一致")
    atomic_write_text(path, text)
    _register(name, info["description"], path, info["size_bytes"])
    if sync:
        sync_skills()
    return get_skill(name)


def import_skill(text: str, *, name: str | None = None, source: str = "imported") -> dict:
    """导入技能（从 SKILL.md 文本）。"""
    info = validate_skill_text(text, fallback_name=name or "")
    directory = skills_root() / info["name"]
    if (directory / "SKILL.md").exists():
        raise InvalidOperationError(f"同名技能已存在：{info['name']}；请改名或先删除")
    directory.mkdir(parents=True, exist_ok=True)
    atomic_write_text(directory / "SKILL.md", text)
    _register(info["name"], info["description"], directory / "SKILL.md",
              info["size_bytes"], source)
    sync_skills()
    return get_skill(info["name"])


def set_enabled(name: str, enabled: bool) -> dict:
    """启用/停用技能：停用即从 ``.dsh/skills`` 移除（下次同步生效）。"""
    get_skill(name)
    with db.get_conn() as conn:
        conn.execute("UPDATE skills SET enabled = ? WHERE name = ?",
                     (1 if enabled else 0, name))
    target_dir = dsh_skills_root() / name
    if not enabled:
        shutil.rmtree(target_dir, ignore_errors=True)
    else:
        sync_skills(prune=False)
    return get_skill(name)


def delete_skill(name: str) -> dict:
    item = get_skill(name)
    if item["source"] == "builtin":
        raise InvalidOperationError(
            f"「{name}」是内置技能，不可删除；可复制为自定义技能后修改。"
        )
    shutil.rmtree(Path(item["path"]).parent, ignore_errors=True)
    shutil.rmtree(dsh_skills_root() / name, ignore_errors=True)
    with db.get_conn() as conn:
        conn.execute("DELETE FROM skills WHERE name = ?", (name,))
    return {"name": name, "deleted": True}


def duplicate_skill(name: str, new_name: str) -> dict:
    item = get_skill(name)
    new_name = new_name.strip()
    if not KEBAB_RE.match(new_name):
        raise InvalidNameError(f"技能名必须是 kebab-case：{new_name}")
    text = str(item["content"]).replace(f"name: {name}", f"name: {new_name}", 1)
    if f"name: {new_name}" not in text:
        meta, body = parse_skill(text)
        meta["name"] = new_name
        text = "---\n" + yaml.safe_dump(meta, allow_unicode=True, sort_keys=False) + \
            "---\n\n" + body
    return create_skill(new_name, item["description"], parse_skill(text)[1],
                        source="workbench_custom")


def _skill_path(name: str) -> Path:
    path = skills_root() / name / "SKILL.md"
    if not path.is_file():
        raise NodeNotFoundError(f"技能不存在：{name}")
    return path


# ─────────────────────────── AI 辅助生成（Task 51） ───────────────────────────


def generate_skill_draft(description: str, *, name: str = "", save: bool = False) -> dict:
    """对话式生成 SKILL.md 草案（frontmatter + 正文）；``save=False`` 只返回草案。"""
    prompt = (
        "你是工作台技能编写助手。请为下面这个需求产出一份 SKILL.md，"
        "第一段是 YAML frontmatter（字段：name（kebab-case 英文）、description（一句中文，"
        "说明何时使用）、whenToUse（可选）），其后是正文（Markdown）。\n"
        "要求：正文写「做什么 / 不做什么 / 步骤 / 验收清单」，具体可执行，不写空话；"
        "只输出 SKILL.md 内容本身，不要解释。"
    )
    result = generation_service.run_task(
        project_id=None,
        task_type="技能生成",
        system=prompt,
        messages=[{"role": "user", "content": f"需求：{description}\n建议名称：{name or '（自拟）'}"}],
        temperature=0.6,
    )
    text = result.get("text") or ""
    draft_name = name
    if not draft_name:
        meta, _body = parse_skill(text)
        draft_name = str(meta.get("name") or "").strip() or "custom-skill"

    payload = {
        "ok": result["ok"],
        "name": draft_name,
        "content": text,
        "task_id": result.get("task_id"),
        "error": result.get("error_message"),
        "usage": (f"请检查草案后保存：POST /api/skills（name/description/body）"),
    }
    if save and result["ok"]:
        meta, body = parse_skill(text)
        payload["saved"] = create_skill(
            str(meta.get("name") or draft_name),
            str(meta.get("description") or description)[:MAX_DESCRIPTION_CHARS],
            body,
        )
    return payload


__all__ = [
    "SKILL_BUDGET_BYTES",
    "SKILL_INJECT_BUDGET_CHARS",
    "create_skill",
    "delete_skill",
    "dsh_skills_root",
    "duplicate_skill",
    "generate_skill_draft",
    "get_skill",
    "import_skill",
    "list_skills",
    "parse_skill",
    "set_enabled",
    "skill_digest",
    "skills_root",
    "sync_skills",
    "update_skill",
    "validate_skill_text",
]