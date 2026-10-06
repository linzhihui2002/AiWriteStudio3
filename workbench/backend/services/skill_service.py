"""技能管理（Task 39 / 51）：skills/ 源目录 → .dsh/skills 同步 + 注册 + 启停 + AI 辅助生成。

dsh 侧机制（已实测，见调研归档）：
- 技能发现路径 ``<projectRoot>/.dsh/skills``（rank 100），**只支持两层结构**：
  ``.dsh/skills/<name>/SKILL.md``，frontmatter 必须含 ``name``（kebab-case）与 ``description``；
- 命中技能正文完整注入；引用文件按需完整读取，用量仅作 token 估算展示。

渐进披露（L1/L2/L3）：
- L1 元数据：frontmatter 的 ``name`` / ``description`` / ``appliesTo``，随技能列表常驻；
- L2 正文：``SKILL.md`` 正文按命中注入对话系统提示（见 :func:`skill_digest`）；
- L3 引用：``skills/<name>/references/**.md`` 明细清单，**不注入**，由模型按需读取
  （见 :func:`read_reference` 与对话工具 ``read_skill_reference``）。

纪律：``skills/`` 是源目录，``.dsh/skills/`` 是**同步产物**（禁止手工编辑）。
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import yaml

from .. import config, db
from ..engine.runtime import estimate_tokens
from . import generation_service
from .errors import InvalidNameError, InvalidOperationError, NodeNotFoundError
from .fs_utils import atomic_write_text, read_text

SOURCES = ("builtin", "workbench_custom", "imported")
KEBAB_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
FRONTMATTER_RE = re.compile(r"^---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|$)", re.DOTALL)

MAX_DESCRIPTION_CHARS = 200

#: 附带明细清单所在目录（L3），仅同步该目录下的纯文本文件
REFERENCE_DIR_NAME = "references"
REFERENCE_SUFFIXES = (".md", ".txt")

#: frontmatter 允许的可选字段（未知字段忽略、不报错）
KNOWN_FRONTMATTER_FIELDS = frozenset({
    "name", "description", "whenToUse", "appliesTo", "license",
    "compatibility", "metadata", "allowed-tools", "version",
})


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


def _normalize_applies_to(raw) -> list[str]:
    """把 frontmatter 的 ``appliesTo`` 规整为字符串数组；类型不符即报错。"""
    if raw is None or raw == "":
        return []
    if not isinstance(raw, list):
        raise InvalidNameError("appliesTo 必须是字符串数组，如 [设定, 状态]")
    values: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            raise InvalidNameError("appliesTo 必须是字符串数组，元素只能是字符串")
        value = item.strip()
        if value:
            values.append(value)
    return values


def validate_skill_text(text: str, *, fallback_name: str = "",
                        expected_name: str = "") -> dict:
    """校验 SKILL.md：frontmatter 必须含 kebab-case ``name`` 与非空 ``description``。

    ``expected_name`` 非空时，frontmatter 的 ``name`` 必须与技能目录名一致。
    ``appliesTo`` 可选，必须是字符串数组。未知 frontmatter 字段忽略、不报错。
    """
    meta, body = parse_skill(text)
    name = str(meta.get("name") or fallback_name or "").strip()
    description = str(meta.get("description") or "").strip()

    if not name:
        raise InvalidNameError("SKILL.md frontmatter 缺少 name")
    if not KEBAB_RE.match(name):
        raise InvalidNameError(f"技能 name 必须是 kebab-case（小写字母/数字/连字符）：{name}")
    expected = str(expected_name or "").strip()
    if expected and name != expected:
        raise InvalidNameError(f"frontmatter name（{name}）与技能目录名（{expected}）不一致")
    if not description:
        raise InvalidNameError("SKILL.md frontmatter 缺少 description（dsh 依赖它做技能发现）")
    if len(description) > MAX_DESCRIPTION_CHARS:
        description = description[:MAX_DESCRIPTION_CHARS]
    applies_to = _normalize_applies_to(meta.get("appliesTo"))
    if not body.strip():
        raise InvalidNameError("SKILL.md 正文为空")
    return {"name": name, "description": description, "body": body, "meta": meta,
            "applies_to": applies_to, "size_bytes": len(text.encode("utf-8")),
            "estimated_tokens": estimate_tokens(body.strip())}


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


def iter_reference_files(skill_dir: Path) -> list[Path]:
    """列出技能目录 ``references/`` 下的附带明细文件（``.md`` / ``.txt``）。"""
    base = Path(skill_dir) / REFERENCE_DIR_NAME
    if not base.is_dir():
        return []
    found: list[Path] = []
    for path in sorted(base.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        if path.name.startswith(".") or path.suffix.lower() not in REFERENCE_SUFFIXES:
            continue
        found.append(path)
    return found


def _reference_entries(skill_dir: Path) -> list[dict]:
    """技能目录下的引用文件（路径为相对技能目录的 posix 路径，如 ``references/x.md``）。"""
    entries: list[dict] = []
    for path in iter_reference_files(skill_dir):
        try:
            size = path.stat().st_size
        except OSError:
            continue
        entries.append({"path": path.relative_to(skill_dir).as_posix(),
                        "size_bytes": int(size),
                        "estimated_tokens": estimate_tokens(read_text(path))})
    return entries


def list_skills() -> list[dict]:
    """技能列表（源目录 + 同步状态 + 注册信息 + 引用文件与应用范围）。"""
    base = skills_root()
    synced = dsh_skills_root()
    registered = {row["name"]: row for row in _registry_rows()}

    items: list[dict] = []
    for path in iter_skill_files(base):
        text = read_text(path)
        references = _reference_entries(path.parent)
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
                    "estimated_tokens": 0,
                    "error": exc.message,
                    "synced": False,
                    "reference_files": [item["path"] for item in references],
                    "references_bytes": sum(item["size_bytes"] for item in references),
                    "references_estimated_tokens": sum(item["estimated_tokens"] for item in references),
                    "appliesTo": [],
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
                "estimated_tokens": info["estimated_tokens"],
                "synced": target.is_file()
                and target.stat().st_size == info["size_bytes"],
                "synced_path": str(target),
                "error": "",
                "reference_files": [item["path"] for item in references],
                "references_bytes": sum(item["size_bytes"] for item in references),
                "references_estimated_tokens": sum(item["estimated_tokens"] for item in references),
                "appliesTo": list(info["applies_to"]),
            }
        )
    return items


def _registry_rows() -> list[dict]:
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT name, description, source, path, enabled, size_bytes FROM skills"
        ).fetchall()
    return [dict(row) for row in rows]


def binding_report() -> dict:
    """技能绑定关系报告（供设置页展示；只读，**不自动改写任何 Agent 定义**）。

    返回 ``{技能名: {...}}``，每项含：

    - ``agents``：``skills`` 字段含该技能名的 Agent 名（按名称排序）；
    - ``suggested_agents``：仅当 ``agents`` 为空时给出——按技能 ``appliesTo``
      与 Agent ``materials`` 的交集推荐可绑定的 Agent 名（仅建议，不写回）；
    - ``synced``：``.dsh/skills/<name>/SKILL.md`` 是否存在且体积与源一致；
    - ``missing_references``：源 ``references/`` 里已声明、但同步产物里缺失的相对路径。
    """
    from . import agent_service        # 函数内导入：避免与 agent_service 循环依赖

    agents = sorted(agent_service.list_all_agents(), key=lambda item: item["name"])
    bindings: dict[str, list[str]] = {}
    for agent in agents:
        for name in agent.get("skills") or []:
            bindings.setdefault(str(name), []).append(agent["name"])

    synced_root = dsh_skills_root()
    report: dict[str, dict] = {}
    for item in list_skills():
        name = item["name"]
        bound = sorted(set(bindings.get(name, [])))
        suggested: list[str] = []
        if not bound:
            applies_to = {str(value) for value in (item.get("appliesTo") or [])}
            if applies_to:
                suggested = sorted(
                    agent["name"] for agent in agents
                    if applies_to & set(agent.get("materials") or [])
                )
        missing = [
            reference["path"]
            for reference in _reference_entries(Path(item["path"]).parent)
            if not (synced_root / name / reference["path"]).is_file()
        ]
        report[name] = {
            "agents": bound,
            "suggested_agents": suggested,
            "synced": bool(item.get("synced")),
            "missing_references": missing,
        }
    return report


def get_skill(name: str) -> dict:
    for item in list_skills():
        if item["name"] == name:
            path = Path(item["path"])
            text = read_text(path) if path.is_file() else ""
            info = validate_skill_text(text, fallback_name=name) if text else {}
            return {**item, "content": text, **{k: v for k, v in info.items() if k == "meta"}}
    raise NodeNotFoundError(f"技能不存在：{name}")


def read_reference(name: str, rel_path: str) -> dict:
    """读取技能 ``references/`` 下的明细清单（L3 按需加载）。

    只允许读 ``skills/<name>/references/`` 内的 ``.md`` / ``.txt`` 文件；
    技能不存在 → :class:`NodeNotFoundError`，文件不存在或路径越界（``..`` /
    绝对路径 / 跨技能）→ :class:`InvalidOperationError`。完整返回内容与 token 估算，
    ``truncated`` 固定为 False，保留该键以兼容既有工具。
    """
    skill = str(name or "").strip()
    skill_dir = skills_root() / skill
    if not KEBAB_RE.match(skill) or not (skill_dir / "SKILL.md").is_file():
        raise NodeNotFoundError(f"技能不存在：{name}")

    raw = str(rel_path or "").strip().replace("\\", "/")
    if not raw:
        raise InvalidOperationError("引用文件路径不能为空")
    first = raw.split("/", 1)[0]
    if raw.startswith("/") or raw.startswith("~") or ":" in first:
        raise InvalidOperationError("引用文件路径必须是技能目录内的相对路径")
    if first != REFERENCE_DIR_NAME:
        raw = f"{REFERENCE_DIR_NAME}/{raw}"
    parts = [part for part in raw.split("/") if part not in ("", ".")]
    rel = "/".join(parts)
    if len(parts) < 2 or parts[0] != REFERENCE_DIR_NAME or ".." in parts:
        raise InvalidOperationError("只能读取该技能 references/ 目录内的文件")
    if Path(rel).suffix.lower() not in REFERENCE_SUFFIXES:
        raise InvalidOperationError("只能读取 references/ 下的 .md 或 .txt 文件")

    reference_root = (skill_dir / REFERENCE_DIR_NAME).resolve()
    target = (skill_dir / rel).resolve()
    if reference_root not in target.parents:
        raise InvalidOperationError("引用文件路径不得越出该技能的 references/ 目录")
    if not target.is_file():
        raise InvalidOperationError(f"引用文件不存在：{rel}")

    content = read_text(target)
    return {"skill": skill, "path": rel, "content": content,
            "truncated": False, "estimated_tokens": estimate_tokens(content)}


# ─────────────────────────── 注入用编译（对话系统提示） ───────────────────────────


def compile_skills(names, *, priority_names=None) -> dict:
    """完整编译技能正文及实际加载清单，估算标题、说明和正文的 token。

    顺序去重；优先技能按输入顺序排在其余技能之前。缺失、停用或格式错误
    返回 ``unavailable``，不计入实际加载清单；引用仍只在模型主动读取时加载。
    """
    priority = {str(raw).strip() for raw in (priority_names or []) if str(raw).strip()}
    loaded: list[tuple[str, str]] = []
    unavailable: list[dict] = []
    seen: set[str] = set()
    enabled = {row["name"]: bool(row["enabled"]) for row in _registry_rows()}
    for raw in names or []:
        name = str(raw or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        try:
            if not KEBAB_RE.match(name):
                raise InvalidNameError(f"技能 name 必须是 kebab-case：{name}")
            # 注入编译只读取选中的正文，不为展示元数据扫描其他技能与引用全文。
            item = validate_skill_text(read_text(_skill_path(name)), expected_name=name)
        except (NodeNotFoundError, InvalidNameError, InvalidOperationError) as exc:
            unavailable.append({"name": name, "error": exc.message})
            continue
        if not enabled.get(name, True):
            unavailable.append({"name": name, "error": "技能已停用"})
            continue
        body = item["body"]
        if not body.strip():
            continue
        loaded.append((name, f"### 技能 {name}（{item.get('description', '')}）\n{body.strip()}"))
    must = [part for part in loaded if part[0] in priority]
    rest = [part for part in loaded if part[0] not in priority]
    ordered = must + rest
    text = "\n\n".join(part for _, part in ordered)
    return {"text": text,
            "skills": [{"name": name, "estimated_tokens": estimate_tokens(part)}
                       for name, part in ordered],
            "estimated_tokens": estimate_tokens(text), "unavailable": unavailable}


def skill_digest(names, *, budget_chars: int | None = None, priority_names=None) -> str:
    """完整的技能正文；旧 ``budget_chars`` 参数仅为调用兼容保留，不再限制内容。"""
    return compile_skills(names, priority_names=priority_names)["text"]


# ─────────────────────────── 同步器 ───────────────────────────


def _prune_directory(directory: Path, keep: set[str]) -> list[str]:
    """删除同步目录中源目录已不存在的产物文件（保留 ``keep`` 中的相对路径）。"""
    warnings: list[str] = []
    # 先文件后目录：按层级倒序处理，文件删净后空目录一并清掉
    for path in sorted(directory.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if path.is_symlink() or path.is_file():
            rel = path.relative_to(directory).as_posix()
            if rel in keep:
                continue
            path.unlink(missing_ok=True)
            warnings.append(f"已清理源目录中不存在的同步产物：{directory.name}/{rel}")
        elif path.is_dir():
            try:
                if not any(path.iterdir()):
                    path.rmdir()
            except OSError:
                continue
    return warnings


def sync_skills(*, prune: bool = True) -> dict:
    """把 ``skills/`` 同步到 ``.dsh/skills/``（补 frontmatter 适配、两层拍平）。

    除 ``SKILL.md`` 外，同步各技能 ``references/`` 下的附带明细文件到
    ``.dsh/skills/<name>/references/``（dsh 仍只认两层结构，发现不受影响）。
    ``prune`` 会同时做目录级与文件级清理；体积统计覆盖附带文件。

    体积口径：``total_bytes`` 含 ``SKILL.md`` 与附带文件；``core_bytes``
    只含各技能 ``SKILL.md`` 之和。``estimated_tokens`` 只估算技能正文，
    引用文件按需完整读取；所有体积统计仅用于同步诊断，不设预算或大小上限。
    """
    base = skills_root()
    target_root = dsh_skills_root()
    target_root.mkdir(parents=True, exist_ok=True)

    synced: list[dict] = []
    warnings: list[str] = []
    errors: list[dict] = []
    total_bytes = 0
    core_bytes = 0
    estimated_tokens = 0
    reference_files = 0
    expected_files: dict[str, set[str]] = {}

    for path in iter_skill_files(base):
        text = read_text(path)
        try:
            info = validate_skill_text(text, fallback_name=path.parent.name)
        except (InvalidNameError, InvalidOperationError) as exc:
            errors.append({"path": str(path), "error": exc.message})
            continue

        name = info["name"]
        skill_dir = path.parent
        target_dir = target_root / name
        target_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_text(target_dir / "SKILL.md", text)
        total_bytes += info["size_bytes"]
        core_bytes += info["size_bytes"]
        estimated_tokens += info["estimated_tokens"]
        keep = {"SKILL.md"}

        for reference in _reference_entries(skill_dir):
            rel = reference["path"]
            source = skill_dir / rel
            destination = target_dir / rel
            destination.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(destination, read_text(source))
            total_bytes += reference["size_bytes"]
            reference_files += 1
            keep.add(rel)

        expected_files[name] = keep
        _register(name, info["description"], path, info["size_bytes"])
        synced.append({"name": name, "path": str(target_dir / "SKILL.md"),
                       "size_bytes": info["size_bytes"],
                       "estimated_tokens": info["estimated_tokens"]})

    if prune:
        for directory in target_root.iterdir():
            if not directory.is_dir() or directory.is_symlink():
                continue
            if directory.name not in expected_files:
                shutil.rmtree(directory, ignore_errors=True)
                warnings.append(f"已清理源目录中不存在的同步产物：{directory.name}")
                continue
            warnings.extend(_prune_directory(directory, expected_files[directory.name]))

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
        "core_bytes": core_bytes,
        "estimated_tokens": estimated_tokens,
        "reference_files": reference_files,
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
    info = validate_skill_text(text, fallback_name=name, expected_name=name)
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
    "REFERENCE_DIR_NAME",
    "binding_report",
    "compile_skills",
    "create_skill",
    "delete_skill",
    "dsh_skills_root",
    "duplicate_skill",
    "generate_skill_draft",
    "get_skill",
    "import_skill",
    "iter_reference_files",
    "iter_skill_files",
    "list_skills",
    "parse_skill",
    "read_reference",
    "set_enabled",
    "skill_digest",
    "skills_root",
    "sync_skills",
    "update_skill",
    "validate_skill_text",
]
