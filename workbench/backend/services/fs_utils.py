"""文件系统与 Markdown 文档通用工具。

集中实现三件事，供 project / chapter / tree 三个服务共用：

1. **原子写**：先写同目录临时文件，``flush + fsync`` 后 ``os.replace`` 覆盖目标，
   避免半截文件；失败时清理临时文件。
2. **YAML frontmatter 读写**：``---`` 包裹的键值对 + 正文；键为中文（书名/状态/字数…）。
3. **空文件判定**：口径固定为「剔除 YAML frontmatter 后的正文，去除首尾空白长度为 0」。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import uuid
from datetime import datetime
from pathlib import Path

import yaml

from .. import config
from .errors import InvalidNameError, InvalidOperationError

# Windows 文件名非法字符（含控制字符）
ILLEGAL_NAME_CHARS = '\\/:*?"<>|'
_ILLEGAL_CHARS_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')

# Windows 保留设备名（不可作为文件名/目录名）
_RESERVED_DEVICE_NAMES = frozenset(
    ["CON", "PRN", "AUX", "NUL"]
    + [f"COM{i}" for i in range(1, 10)]
    + [f"LPT{i}" for i in range(1, 10)]
)

MAX_NAME_LENGTH = 80

# frontmatter 分隔块：`---\n<yaml>\n---\n`
_FRONTMATTER_RE = re.compile(
    r"^---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|$)", re.DOTALL
)

TRASH_DIR_NAME = "trash"
TRASH_MANIFEST_NAME = "entry.json"
TRASH_PAYLOAD_NAME = "payload"


# ────────────────────────────── 原子写 ──────────────────────────────


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """原子写二进制：临时文件 + ``os.replace``。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        with open(tmp, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def atomic_write_text(path: Path, text: str) -> None:
    """原子写文本（UTF-8，统一 ``\\n`` 行尾）。"""
    atomic_write_bytes(path, (text or "").encode("utf-8"))


def read_text(path: Path) -> str:
    """读取文本（UTF-8）。文件不存在时抛 :class:`FileNotFoundError`。"""
    return Path(path).read_text(encoding="utf-8")


# ─────────────────────────── frontmatter ───────────────────────────


def split_frontmatter(text: str) -> tuple[dict, str]:
    """拆分 frontmatter 与正文，返回 ``(meta, body)``。

    无 frontmatter 时 meta 为空 dict；YAML 解析失败时同样退化为空 dict（不抛错，
    保证「外部手工改坏 frontmatter 也不会导致整章读不出来」）。
    """
    raw_text = text or ""
    match = _FRONTMATTER_RE.match(raw_text)
    if match is None:
        return {}, raw_text

    try:
        data = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        data = None
    if not isinstance(data, dict):
        data = {}
    return data, raw_text[match.end():]


def strip_frontmatter(text: str) -> str:
    """只取正文（剔除 frontmatter）。"""
    return split_frontmatter(text)[1]


def has_frontmatter(text: str) -> bool:
    """文本是否以 frontmatter 块开头。"""
    return _FRONTMATTER_RE.match(text or "") is not None


def compose_document(meta: dict | None, body: str) -> str:
    """拼装 frontmatter + 正文；无 meta 时只输出正文。"""
    parts: list[str] = []
    if meta:
        dumped = yaml.safe_dump(
            dict(meta), allow_unicode=True, sort_keys=False, default_flow_style=False
        ).strip()
        parts.append(f"---\n{dumped}\n---\n")

    text_body = body or ""
    if text_body and parts and not text_body.startswith("\n"):
        parts.append("\n")  # frontmatter 与正文之间保留一个空行
    parts.append(text_body)

    result = "".join(parts)
    if result and not result.endswith("\n"):
        result += "\n"
    return result


def is_empty_document(text: str) -> bool:
    """空文件判定：剔除 frontmatter 后正文去首尾空白长度为 0。"""
    return strip_frontmatter(text).strip() == ""


def is_empty_file(path: Path) -> bool:
    """读盘判定是否为空文件；读不出来（二进制/权限）时按非空处理。"""
    try:
        return is_empty_document(read_text(path))
    except (UnicodeDecodeError, OSError):
        return False


def count_words(text: str) -> int:
    """字数口径：正文中所有非空白字符的数量（中文按字计，标点与西文按字符计）。"""
    return len(re.sub(r"\s+", "", text or ""))


# ─────────────────────────── 名称处理 ───────────────────────────


def sanitize_project_name(name: str) -> str:
    """清洗书名以得到合法的目录名（非法字符替换为 ``_``）。

    处理内容：Windows 非法字符 ``\\ / : * ? " < > |`` 与控制字符 → ``_``；
    去除首尾空白与结尾的点/空格；校验保留设备名与长度上限。
    """
    if name is None or not str(name).strip():
        raise InvalidNameError("书名不能为空")

    cleaned = _ILLEGAL_CHARS_RE.sub("_", str(name)).strip().rstrip(" .")
    if not cleaned:
        raise InvalidNameError("书名去除非法字符后为空，请换一个名称")
    if cleaned.upper() in _RESERVED_DEVICE_NAMES:
        raise InvalidNameError(f"书名不能使用系统保留名：{cleaned}")
    if len(cleaned) > MAX_NAME_LENGTH:
        raise InvalidNameError(f"书名过长（>{MAX_NAME_LENGTH} 字符）：{cleaned}")
    return cleaned


def validate_node_name(name: str) -> str:
    """校验用户输入的文件/文件夹名；含非法字符直接报错（不静默替换）。"""
    if name is None or not str(name).strip():
        raise InvalidNameError("名称不能为空")

    cleaned = str(name).strip()
    if cleaned in {".", ".."}:
        raise InvalidNameError("名称不能是 . 或 ..")
    if _ILLEGAL_CHARS_RE.search(cleaned):
        raise InvalidNameError(
            f"名称含非法字符（{ILLEGAL_NAME_CHARS}）：{cleaned}"
        )
    cleaned = cleaned.rstrip(" .")
    if not cleaned:
        raise InvalidNameError("名称不能以点或空格结尾")
    if cleaned.upper() in _RESERVED_DEVICE_NAMES:
        raise InvalidNameError(f"名称不能使用系统保留名：{cleaned}")
    if len(cleaned) > MAX_NAME_LENGTH:
        raise InvalidNameError(f"名称过长（>{MAX_NAME_LENGTH} 字符）：{cleaned}")
    return cleaned


# ─────────────────────────── 路径解析 ───────────────────────────


def resolve_within(base: Path, rel: str | Path) -> Path:
    """把相对路径解析到 ``base`` 之下，并拦截 ``..`` 越界。"""
    base_path = Path(base).resolve()
    rel_text = str(rel or "").replace("\\", "/").strip("/")
    candidate = base_path if rel_text in ("", ".") else (base_path / rel_text).resolve()
    if candidate != base_path and base_path not in candidate.parents:
        raise InvalidNameError(f"路径越界（不允许访问项目目录之外）：{rel}")
    return candidate


def to_rel(base: Path, path: Path) -> str:
    """把绝对路径转成相对 ``base`` 的 POSIX 风格相对路径。"""
    return Path(path).resolve().relative_to(Path(base).resolve()).as_posix()


# ─────────────────────────── 回收站 ───────────────────────────


def trash_root() -> Path:
    """回收站根目录：``<项目根>/.workbench/trash/``。"""
    return Path(config.RUNTIME_DIR) / TRASH_DIR_NAME


def move_to_trash(project_name: str, project_dir: Path, rel_path: str) -> str:
    """把项目内某个文件/文件夹整体移入回收站，返回回收站条目 id。

    回收站布局（便于恢复时还原原位置）：:

        .workbench/trash/{书名}/{时间戳}/entry.json   # 记录原相对路径
        .workbench/trash/{书名}/{时间戳}/payload      # 被删除的文件或文件夹本体
    """
    source = Path(project_dir) / rel_path
    if not source.exists():
        raise FileNotFoundError(f"待删除对象不存在：{rel_path}")

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    entry_dir = trash_root() / project_name / stamp
    entry_dir.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(entry_dir / TRASH_PAYLOAD_NAME))

    manifest = {
        "project_name": project_name,
        "rel_path": Path(rel_path).as_posix(),
        "kind": "dir" if (entry_dir / TRASH_PAYLOAD_NAME).is_dir() else "file",
        "deleted_at": stamp,
    }
    atomic_write_text(
        entry_dir / TRASH_MANIFEST_NAME,
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
    )
    return f"{project_name}/{stamp}"


def move_project_to_trash(project_name: str) -> str:
    """把**整本书**目录移入回收站，返回条目 id ``{书名}/{时间戳}``。

    与 :func:`move_to_trash` 的区别：这里移动的是 ``projects/{书名}/`` 项目根本身，
    manifest 的 ``kind`` 固定为 ``"project"``、``rel_path`` 为空串，
    供回收站列表区分「整本书」与「项目内文件/文件夹」条目。
    """
    source = Path(config.PROJECTS_DIR) / project_name
    if not source.exists():
        raise FileNotFoundError(f"项目目录不存在：{project_name}")

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    entry_dir = trash_root() / project_name / stamp
    entry_dir.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(entry_dir / TRASH_PAYLOAD_NAME))

    manifest = {
        "project_name": project_name,
        "rel_path": "",
        "kind": "project",
        "deleted_at": stamp,
    }
    atomic_write_text(
        entry_dir / TRASH_MANIFEST_NAME,
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
    )
    return f"{project_name}/{stamp}"


def read_trash_manifest(entry_dir: Path) -> dict | None:
    """读取回收站条目的 entry.json；缺失或损坏时返回 None。"""
    manifest_path = Path(entry_dir) / TRASH_MANIFEST_NAME
    if not manifest_path.is_file():
        return None
    try:
        data = json.loads(read_text(manifest_path))
    except (json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def trash_entry_payload(entry_dir: Path) -> Path:
    """回收站条目中被删除对象的本体路径。"""
    return Path(entry_dir) / TRASH_PAYLOAD_NAME


def resolve_trash_entry(trash_rel: str) -> Path:
    """把回收站相对路径（entry id）解析为绝对路径并校验越界。"""
    return resolve_within(trash_root(), trash_rel)


# ─────────────────────── 按内容分策略的删除 ───────────────────────


def directory_is_all_empty(path: Path) -> bool:
    """目录下（递归）所有文件是否都是空文件。"""
    for child in Path(path).rglob("*"):
        if child.is_file() and not is_empty_file(child):
            return False
    return True


def is_referenced_by_meta(project_dir: Path, rel_path: str) -> bool:
    """该相对路径是否被 ``.meta/`` 下的记录引用（卡片缓存 / 合同等）。

    被引用的空文件不允许物理删除（否则会留下悬空引用），删除策略降级为进回收站。
    """
    meta_dir = Path(project_dir) / ".meta"
    if not meta_dir.is_dir():
        return False
    needle = Path(rel_path).as_posix()
    name = Path(rel_path).name
    for record in meta_dir.rglob("*.json"):
        try:
            text = record.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if needle in text or (name and name in text):
            return True
    return False


def delete_path_by_content(
    project_dir: Path, rel_path: str, project_name: str
) -> dict:
    """按内容分策略删除文件/文件夹。

    - 文件：空文件（剔除 frontmatter 后正文为空）**物理删除**；非空文件进回收站；
    - 空文件若被 ``.meta/`` 记录引用 → 降级为进回收站（避免悬空引用）；
    - 文件夹：其下全部为空则**物理删除**，否则整个文件夹进回收站。

    :return: ``{"rel_path", "type", "action", "trash_rel", "reason"}``，
             action ∈ {deleted, trashed}
    """
    project_dir = Path(project_dir)
    target = resolve_within(project_dir, rel_path)
    if target == project_dir.resolve():
        raise InvalidOperationError("不能删除项目根目录")
    if not target.exists():
        raise FileNotFoundError(f"待删除对象不存在：{rel_path}")

    rel_posix = to_rel(project_dir, target)
    referenced = is_referenced_by_meta(project_dir, rel_posix)

    if target.is_dir():
        if directory_is_all_empty(target) and not referenced:
            shutil.rmtree(target)
            return {"rel_path": rel_posix, "type": "dir", "action": "deleted",
                    "trash_rel": None, "reason": "空文件夹"}
        reason = "被 .meta 记录引用" if referenced else "含非空文件"
        return {"rel_path": rel_posix, "type": "dir", "action": "trashed",
                "trash_rel": move_to_trash(project_name, project_dir, rel_posix),
                "reason": reason}

    if is_empty_file(target) and not referenced:
        target.unlink()
        return {"rel_path": rel_posix, "type": "file", "action": "deleted",
                "trash_rel": None, "reason": "空文件"}
    reason = "被 .meta 记录引用（空文件降级）" if is_empty_file(target) else "含正文内容"
    return {"rel_path": rel_posix, "type": "file", "action": "trashed",
            "trash_rel": move_to_trash(project_name, project_dir, rel_posix),
            "reason": reason}
