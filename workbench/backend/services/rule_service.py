"""规则体系（Task 49）：全局级 / 作品级 / 技能内置，按优先级链解析冲突并编译。

优先级链（高 → 低）
1. **作者确认**（规则条目标记 ``confirmed: true``）
2. **作品级**（``projects/{书名}/.meta/rules.json``）
3. **全局级**（``rules/*.md``）
4. **技能内置**（SKILL.md 正文里的硬约束，只做展示与参照，不参与改写）

冲突判定：规则可在 frontmatter 声明 ``keys``（主题关键词，如「破折号」「称谓」）；
同一 key 出现在多个作用域即视为潜在冲突 → 按优先级链给出胜出方与依据。

编译产物：``.dsh/skills/workbench-rules/SKILL.md``（不写根 AGENTS.md，避免挤占 8KB 路由预算）。
作品级规则同时以「第 1 级 用户显式材料」形式注入上下文（见 :func:`rules_digest`）。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

from .. import config, db
from .errors import InvalidNameError, InvalidOperationError, NodeNotFoundError
from .fs_utils import atomic_write_text, read_text
from .operation_log import log as log_operation
from .project_service import get_project_dir

RULES_FILE_SUFFIX = ".md"
PROJECT_RULES_FILE = ".meta/rules.json"
COMPILED_SKILL_NAME = "workbench-rules"
COMPILED_SKILL_DIR = f".dsh/skills/{COMPILED_SKILL_NAME}"

SCOPES = ("global", "project")
_DEFAULT_PRIORITY = 100


def rules_root() -> Path:
    return Path(config.rules_dir())


# ─────────────────────────── 读取 ───────────────────────────


def _parse_rule_file(path: Path, scope: str, project_id: int | None = None) -> dict | None:
    text = read_text(path)
    meta: dict = {}
    body = text
    match = re.match(r"^---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|$)", text, re.DOTALL)
    if match:
        try:
            loaded = yaml.safe_load(match.group(1))
            meta = loaded if isinstance(loaded, dict) else {}
        except yaml.YAMLError:
            meta = {}
        body = text[match.end():]
    name = str(meta.get("name") or path.stem)
    return {
        "id": f"{scope}:{name}",
        "scope": scope,
        "project_id": project_id,
        "name": name,
        "body": body.strip(),
        "keys": [str(item) for item in (meta.get("keys") or [])],
        "priority": int(meta.get("priority") or _DEFAULT_PRIORITY),
        "confirmed": bool(meta.get("confirmed")),
        "enabled": bool(meta.get("enabled", True)),
        "source": str(meta.get("source") or ("project" if scope == "project" else "global")),
        "path": str(path),
    }


def list_global_rules() -> list[dict]:
    root = rules_root()
    if not root.is_dir():
        return []
    return [rule for rule in (_parse_rule_file(path, "global")
                             for path in sorted(root.glob(f"*{RULES_FILE_SUFFIX}"))
                             if path.name.lower() not in ("readme.md",))
            if rule is not None]


def list_project_rules(project_id: int) -> list[dict]:
    _row, project_dir = get_project_dir(project_id)
    path = Path(project_dir) / PROJECT_RULES_FILE
    if not path.is_file():
        return []
    try:
        data = json.loads(read_text(path))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return []
    items = data if isinstance(data, list) else data.get("rules", [])
    rules: list[dict] = []
    for item in items or []:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        rules.append(
            {
                "id": f"project:{item['name']}",
                "scope": "project",
                "project_id": project_id,
                "name": str(item["name"]),
                "body": str(item.get("body") or ""),
                "keys": [str(key) for key in (item.get("keys") or [])],
                "priority": int(item.get("priority") or _DEFAULT_PRIORITY),
                "confirmed": bool(item.get("confirmed")),
                "enabled": bool(item.get("enabled", True)),
                "source": str(item.get("source") or "project"),
                "path": str(path),
            }
        )
    return rules


def list_skill_rules() -> list[dict]:
    """技能内置规则（只展示，供冲突说明与人工参照）。"""
    from .skill_service import list_skills, parse_skill

    items: list[dict] = []
    for skill in list_skills():
        path = Path(skill["path"])
        if not path.is_file():
            continue
        _meta, body = parse_skill(read_text(path))
        hard = [line.strip() for line in body.splitlines()
                if line.strip().startswith(("- 禁止", "- 禁止：", "- 必须", "硬约束"))][:5]
        if hard:
            items.append({"id": f"skill:{skill['name']}", "scope": "skill",
                          "name": skill["name"], "body": "\n".join(hard),
                          "keys": [], "priority": 1000, "confirmed": False,
                          "enabled": True, "source": "skill_builtin",
                          "path": str(path)})
    return items


def list_rules(project_id: int | None = None) -> dict:
    global_rules = list_global_rules()
    project_rules = list_project_rules(project_id) if project_id else []
    return {
        "global": global_rules,
        "project": project_rules,
        "skill_builtin": list_skill_rules(),
        "scopes": SCOPES,
    }


# ─────────────────────────── 优先级链解析 ───────────────────────────

_SCOPE_RANK = {"skill": 40, "global": 20, "project": 10}  # 数字越小优先级越高


def resolve_rules(project_id: int | None = None) -> dict:
    """按优先级链解析：作者确认 > 作品级 > 全局 > 技能内置；输出冲突与胜出依据。"""
    rules = []
    rules.extend(list_global_rules())
    if project_id:
        rules.extend(list_project_rules(project_id))
    rules.extend(list_skill_rules())
    rules = [rule for rule in rules if rule.get("enabled")]

    def rank(rule: dict) -> tuple[int, int]:
        confirmed_rank = 0 if rule.get("confirmed") else 1
        return (confirmed_rank, _SCOPE_RANK.get(rule["scope"], 30))

    ordered = sorted(rules, key=lambda rule: (rank(rule), rule["priority"]))

    # 冲突：同一 key 出现在不同作用域
    by_key: dict[str, list[dict]] = {}
    for rule in ordered:
        for key in rule.get("keys") or []:
            by_key.setdefault(key, []).append(rule)

    conflicts: list[dict] = []
    for key, contenders in by_key.items():
        scopes = {rule["scope"] for rule in contenders}
        if len(scopes) < 2:
            continue
        winner = contenders[0]
        conflicts.append(
            {
                "key": key,
                "winner": {"id": winner["id"], "scope": winner["scope"],
                           "name": winner["name"]},
                "losers": [
                    {"id": rule["id"], "scope": rule["scope"], "name": rule["name"]}
                    for rule in contenders[1:]
                ],
                "basis": (
                    f"胜出依据：{'作者确认' if winner.get('confirmed') else '作用域'}优先级"
                    f"（{winner['scope']} 高于其余作用域）"
                ),
            }
        )

    effective = [rule for rule in ordered
                 if not any(rule["id"] == conflict_rule["id"]
                            for conflict in conflicts
                            for conflict_rule in conflict["losers"])]
    return {
        "project_id": project_id,
        "effective": effective,
        "conflicts": conflicts,
        "order": ["作者确认", "作品级", "全局", "技能内置"],
        "counts": {
            "global": len(list_global_rules()),
            "project": len(list_project_rules(project_id)) if project_id else 0,
            "skill": len(list_skill_rules()),
        },
    }


def rules_digest(project_id: int | None = None, limit_chars: int = 2000) -> str:
    """编译为可注入文本（供上下文第 1 级「用户显式材料」）。"""
    resolved = resolve_rules(project_id)
    lines = ["【写作规则（优先级：作者确认 > 作品级 > 全局 > 技能内置）】"]
    for rule in resolved["effective"]:
        if rule["scope"] == "skill":
            continue
        tag = "作者确认" if rule.get("confirmed") else rule["scope"]
        lines.append(f"- [{tag}] {rule['name']}：{rule['body'].strip()[:200]}")
    for conflict in resolved["conflicts"]:
        lines.append(
            f"- 冲突提示（{conflict['key']}）：以「{conflict['winner']['name']}」为准"
            f"（{conflict['basis']}）"
        )
    text = "\n".join(lines)
    return text[:limit_chars]


# ─────────────────────────── 增删改 ───────────────────────────


def _validate_name(name: str) -> str:
    clean = (name or "").strip()
    if not clean or len(clean) > 40:
        raise InvalidNameError("规则名不能为空且不超过 40 字符")
    if any(char in clean for char in '\\/:*?"<>|'):
        raise InvalidNameError(f"规则名含非法字符：{clean}")
    return clean


def upsert_global_rule(name: str, body: str, *, keys: list[str] | None = None,
                       priority: int = _DEFAULT_PRIORITY, confirmed: bool = False,
                       enabled: bool = True) -> dict:
    clean = _validate_name(name)
    root = rules_root()
    root.mkdir(parents=True, exist_ok=True)
    meta = {"name": clean, "priority": int(priority), "confirmed": bool(confirmed),
            "enabled": bool(enabled), "keys": list(keys or []), "source": "global"}
    text = "---\n" + yaml.safe_dump(meta, allow_unicode=True, sort_keys=False) + \
        "---\n\n" + (body or "").strip() + "\n"
    atomic_write_text(root / f"{clean}{RULES_FILE_SUFFIX}", text)
    _register("global", None, clean, body, priority, enabled)
    compile_rules()
    return _parse_rule_file(root / f"{clean}{RULES_FILE_SUFFIX}", "global")  # type: ignore[return-value]


def upsert_project_rule(project_id: int, name: str, body: str, *,
                        keys: list[str] | None = None, priority: int = _DEFAULT_PRIORITY,
                        confirmed: bool = False, enabled: bool = True) -> dict:
    clean = _validate_name(name)
    _row, project_dir = get_project_dir(project_id)
    path = Path(project_dir) / PROJECT_RULES_FILE
    data: list = []
    if path.is_file():
        try:
            loaded = json.loads(read_text(path))
            data = loaded if isinstance(loaded, list) else loaded.get("rules", [])
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            data = []

    entry = {
        "name": clean,
        "body": (body or "").strip(),
        "keys": list(keys or []),
        "priority": int(priority),
        "confirmed": bool(confirmed),
        "enabled": bool(enabled),
        "source": "project",
    }
    data = [item for item in data if item.get("name") != clean] + [entry]
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    _register("project", project_id, clean, body, priority, enabled)
    compile_rules(project_id)
    log_operation(project_id, "rule-upsert", str(path), {"name": clean})
    return next(rule for rule in list_project_rules(project_id) if rule["name"] == clean)


def delete_rule(scope: str, name: str, project_id: int | None = None) -> dict:
    if scope == "global":
        path = rules_root() / f"{name}{RULES_FILE_SUFFIX}"
        if not path.is_file():
            raise NodeNotFoundError(f"全局规则不存在：{name}")
        path.unlink()
    elif scope == "project":
        if not project_id:
            raise InvalidOperationError("删除作品级规则需要 project_id")
        _row, project_dir = get_project_dir(project_id)
        path = Path(project_dir) / PROJECT_RULES_FILE
        if not path.is_file():
            raise NodeNotFoundError("该项目没有作品级规则")
        try:
            data = json.loads(read_text(path))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            data = []
        items = data if isinstance(data, list) else data.get("rules", [])
        remaining = [item for item in items if item.get("name") != name]
        if len(remaining) == len(items):
            raise NodeNotFoundError(f"作品级规则不存在：{name}")
        atomic_write_text(path, json.dumps(remaining, ensure_ascii=False, indent=2) + "\n")
    else:
        raise InvalidOperationError("scope 只能是 global / project")

    with db.get_conn() as conn:
        conn.execute(
            "DELETE FROM rules WHERE scope = ? AND name = ? AND"
            " (project_id IS ? OR project_id = ?)",
            (scope, name, project_id, project_id),
        )
    compile_rules(project_id)
    return {"scope": scope, "name": name, "deleted": True}


def _register(scope: str, project_id: int | None, name: str, body: str,
              priority: int, enabled: bool) -> None:
    with db.get_conn() as conn:
        conn.execute("DELETE FROM rules WHERE scope = ? AND name = ?", (scope, name))
        conn.execute(
            "INSERT INTO rules (scope, project_id, name, body, priority, enabled)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (scope, project_id, name, body, int(priority), 1 if enabled else 0),
        )


# ─────────────────────────── 编译 ───────────────────────────


def compile_rules(project_id: int | None = None) -> dict:
    """把解析后的规则编译进 ``.dsh/skills/workbench-rules/SKILL.md``。

    选择技能路径而非根 AGENTS.md：AGENTS.md 是路由表（≤8KB 预算），
    规则正文放这里可随技能被 dsh 自动发现，且不挤占路由预算。
    """
    digest = rules_digest(project_id, limit_chars=6000)
    target_dir = Path(config.PROJECT_ROOT) / ".dsh" / "skills" / COMPILED_SKILL_NAME
    target_dir.mkdir(parents=True, exist_ok=True)

    body = (
        "# 工作台规则（自动编译，请勿手工编辑）\n\n"
        "本文件由工作台「规则」页编译生成；优先级链：作者确认 > 作品级 > 全局 > 技能内置。\n"
        "覆盖或补充本文件的规则时，请在规则页修改，不要直接改这里。\n\n"
        f"{digest}\n"
    )
    text = (
        "---\n"
        f"name: {COMPILED_SKILL_NAME}\n"
        "description: 工作台编译的写作规则（全局与作品级），写作/审稿时自动生效。\n"
        "---\n\n" + body
    )
    atomic_write_text(target_dir / "SKILL.md", text)

    from .skill_service import _register as register_skill

    register_skill(COMPILED_SKILL_NAME, "工作台编译的写作规则（全局与作品级）",
                   target_dir / "SKILL.md", len(text.encode("utf-8")),
                   source="workbench_custom")
    return {
        "path": str(target_dir / "SKILL.md"),
        "chars": len(text),
        "compiled_from": "project" if project_id else "global",
        "rules": len(resolve_rules(project_id)["effective"]),
    }


__all__ = [
    "COMPILED_SKILL_DIR",
    "compile_rules",
    "delete_rule",
    "list_global_rules",
    "list_project_rules",
    "list_rules",
    "list_skill_rules",
    "resolve_rules",
    "rules_digest",
    "upsert_global_rule",
    "upsert_project_rule",
]