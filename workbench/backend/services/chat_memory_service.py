"""Book-owned conversation memory, supported by the author's original words.

The JSON document is authoritative. SQLite is consulted only to verify that a
captured quote was really sent by the author in this book. Suggestions and open
questions remain pending; neither they nor deleted entries enter model context.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .. import db
from .errors import InvalidOperationError, NodeNotFoundError, ServiceError
from .file_change_service import file_state, project_lock
from .fs_utils import atomic_write_text, split_frontmatter
from .project_service import get_project_dir

MAX_ENTRY_CHARS = 2000
MAX_CONTEXT_CHARS = 12000
_UNCERTAIN = re.compile(r"[?？]|是否|要不要|能不能|可不可以|怎么样|如何|哪个|什么|怎么|从哪|还是|或者|没想好|也许|可能|暂定|待定|备选|考虑|建议|假如|假设|如果|例如|比如|你觉得|你说|你提到|助手|\bAI\b", re.I)
_AUTHORITY = re.compile(r"权限|批准|终端|命令行|系统提示|忽略.{0,8}(?:规则|指令)|其他(?:书|项目)|全局配置|\.dsh|api.?key", re.I)
_EXPLICIT = re.compile(r"^(?:我(?:已经)?(?:决定|确定|确认|选定)|(?:请)?(?:记住|记下)|(?:现已|已经)?确认|就定|确定了)[：:，,\s]*")
_NAME = re.compile(r"(?P<role>主角|男主(?:角)?|女主(?:角)?|反派|配角)(?:的)?(?:姓名|名字|名称)?")
_NAMED_ROLE = re.compile(r"角色[\s“\"「『]*(?P<role>[^\s”\"」』：:，,。]{1,16})[”\"」』]*(?:的)?(?:姓名|名字|名称)")
_MONEY = re.compile(r"(?:代币|货币|币)[1-9]\d*|灵石|积分|金币|银币|铜币|代币|货币|币种")
_VALUE = re.compile(r"(?:叫做?|名为|定为|设为|改为|改成|选用|采用|选择|用|是|为|[：:])\s*([^，,。；;？?]+)")
_PREFERENCE_TOPICS = ("叙述视角", "叙事视角", "人称", "文风", "风格", "时态", "节奏", "对话比例", "单章字数")
_DECISION_TOPICS = ("死亡代价", "死亡惩罚", "撤离条件", "复活代价", "复活条件", "胜利条件", "失败条件")
_BOOK_TOPICS = re.compile(r"主角|男主|女主|人物|角色|货币|代币|币种|灵石|积分|世界|修炼|境界|宗门|门派|势力|组织|时间线|背景|时代|法术|技能|规则|设定|文风|风格|视角|人称|时态|节奏|字数|叙述|对白|对话|死亡代价|死亡惩罚|撤离条件|复活|胜利条件|失败条件|题材|创作方向|本书.*方向|剧情")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _path(project_id: int) -> Path:
    _, root = get_project_dir(project_id)
    path = root / ".meta" / "chat-memory.json"
    for node in (root, path.parent, path):
        if node.is_symlink() or (hasattr(node, "is_junction") and node.is_junction()):
            raise InvalidOperationError("本书记忆不能通过链接读取或写入")
    return path


def _load(project_id: int) -> dict:
    path = _path(project_id)
    if not path.exists():
        return {"version": 1, "entries": [], "processed_messages": []}
    try:
        if path.stat().st_size > 8 * 1024 * 1024:
            raise ValueError("文件过大")
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("version") != 1 or not isinstance(value.get("entries"), list):
            raise ValueError("结构无效")
        for item in value["entries"]:
            if (not isinstance(item, dict) or not isinstance(item.get("id"), str)
                    or not isinstance(item.get("content"), str)
                    or item.get("status") not in {"confirmed", "pending", "deleted"}
                    or not isinstance(item.get("key"), str)
                    or not isinstance(item.get("updated_at"), str)
                    or not isinstance(item.get("sources"), list)
                    or any(not isinstance(source, dict) or not isinstance(source.get("quote"), str)
                           or (source.get("message_id") is not None and
                               (not isinstance(source.get("message_id"), int) or not isinstance(source.get("session_id"), int)))
                           for source in item.get("sources", []))
                    or not isinstance(item.get("history"), list)
                    or any(not isinstance(version, dict) or not isinstance(version.get("content"), str)
                           for version in item.get("history", []))):
                raise ValueError("记忆条目无效")
        if not isinstance(value.get("processed_messages", []), list):
            raise ValueError("来源标识无效")
        value.setdefault("processed_messages", [])
        return value
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        raise InvalidOperationError("本书记忆文件损坏，请修复 .meta/chat-memory.json；原文件未覆盖") from exc


def _save(project_id: int, value: dict) -> None:
    value["updated_at"] = _now()
    atomic_write_text(_path(project_id), json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def list_entries(project_id: int) -> list[dict]:
    with project_lock(project_id):
        entries = [item for item in _load(project_id)["entries"] if item["status"] != "deleted"]
        manuscript = _manuscript_facts(project_id) if entries else {}
        result = []
        for item in entries:
            # Keep the full provenance as well as the direct link used by the UI.
            source = next((source for source in reversed(item["sources"]) if source.get("message_id")), {})
            conflicts = []
            if item["status"] == "confirmed":
                value = _fact_value(item["content"])
                for fact in manuscript.get(_entry_key(item), []):
                    if value and value != fact["value"]:
                        conflicts.append({"path": fact["path"], "content": fact["content"]})
            result.append({**item, "source_session_id": source.get("session_id"),
                           "source_message_id": source.get("message_id"), "conflicts": conflicts})
        return result


def _money_topic(text: str) -> str | None:
    # A denomination is part of the identity. A decision about spirit stones
    # must never silently replace an unrelated decision about tokens or gold.
    monies = _MONEY.findall(text)
    specific = [term for term in monies if term not in {"货币", "币种"}]
    named = re.match(r"(?:本书|当前|关于)?[“\"「『]?(?P<name>[\u4e00-\u9fffA-Za-z0-9]{1,8}(?:币|积分|灵石))[”\"」』]?(?:的|来源|获取|用途|通过|从|用于|可|是|为|：|:)", text)
    return named["name"] if named else specific[0] if specific else "货币" if monies else None


def _entry_key(item: dict) -> str:
    # Upgrade the early generic currency keys without dropping existing memory.
    key = item.get("key", "")
    if key in {"货币来源", "货币用途", "货币兑换"}:
        return _subject(item["content"]) or key
    return key


def _fact_value(text: str) -> str | None:
    match = re.search(r"(?:改为|改成|叫做?|名为|定为|设为|选用|采用|选择|是|为|[：:])\s*([^，,。；;？?]+)", text)
    return re.sub(r"[\s“”\"「」『』]", "", match[1]) if match else None


def _manuscript_facts(project_id: int) -> dict[str, list[dict]]:
    """Read bounded, explicit setting fields; never infer facts from narration.

    Differences are advisory candidates, not a reason to rewrite the manuscript.
    Recompute on read so changes in either memory or source files show immediately.
    """
    _, root = get_project_dir(project_id)
    paths = ["project.md"]
    settings = root / "设定"
    if settings.is_dir() and not settings.is_symlink() and not (hasattr(settings, "is_junction") and settings.is_junction()):
        paths.extend(path.relative_to(root).as_posix() for path in sorted(settings.rglob("*.md"))[:100])
    result: dict[str, list[dict]] = {}
    remaining = 200_000
    for rel in paths:
        try:
            state = file_state(project_id, rel)
        except (ServiceError, OSError, ValueError):
            continue
        content = state["content"] or ""
        if len(content) > remaining:
            continue
        remaining -= len(content)
        meta, body = split_frontmatter(content)
        if rel == "project.md":
            body = f"主角名字：{meta['主角']}" if meta.get("主角") else ""
        for item in _candidates(body, {}):
            value = _fact_value(item["content"])
            if item["status"] == "confirmed" and value:
                result.setdefault(item["key"], []).append({"path": rel, "content": item["content"], "value": value})
    return result


def _subject(text: str) -> str | None:
    """Stable topic keys permit later author corrections to replace old facts."""
    role = _NAMED_ROLE.search(text) or _NAME.search(text)
    if role and re.search(r"姓名|名字|名称|叫|名为", text):
        name = {"男主角": "男主", "女主角": "女主"}.get(role["role"], role["role"])
        return name + "姓名"
    if re.search(r"题材|创作方向|本书.*方向", text):
        return "创作方向"
    if re.search(r"死亡|入场失败", text) and re.search(r"代价|损失|惩罚", text):
        return "死亡代价"
    money = _money_topic(text)
    if money:
        if re.search(r"来源|获取|赚取|获得|供币|掉落|杀妖|杀怪|击杀|从|通过|(?:怎么|如何|哪里|哪儿)来", text):
            return money + "来源"
        if re.search(r"名字|名称|叫|名为|币种|货币(?:用|是|为|：|:)", text):
            return "货币名称"
        if re.search(r"兑换|汇率|比例", text):
            return money + "兑换"
        if re.search(r"用途|购买|支付|消费", text):
            return money + "用途"
    for topic in _PREFERENCE_TOPICS:
        if topic in text:
            return "叙述视角" if topic == "叙事视角" else topic
    for topic in _DECISION_TOPICS:
        if topic in text:
            return "死亡代价" if topic == "死亡惩罚" else topic
    field = re.match(r"([^：:=，,。；;？?]{2,24})\s*[：:=]", text)
    if field and _BOOK_TOPICS.search(field[1]):
        return field[1].strip()
    fact = re.match(r"(.{2,24}?(?:规则|机制|境界|组织|宗门|门派|法术|技能|背景|时代|设定))(?:是|为|改为|改成|采用|：|:)", text)
    return fact[1] if fact else None


def _candidates(text: str, meta: dict) -> list[dict]:
    result = []
    if meta.get("interaction_kind") == "question":
        # The interaction service writes question + exact selected label/custom
        # text into the user's message, so an answer is never inferred from an
        # assistant suggestion or a bare "yes".
        lines = text.splitlines()
        for index, line in enumerate(lines):
            if line.startswith("回答：") and index:
                question, answer = lines[index - 1].strip(), line[3:].strip()
                topic = _subject(question)
                if topic and answer and not _AUTHORITY.search(question + answer):
                    pending = bool(_UNCERTAIN.search(answer) or re.fullmatch(r"都可以|随便|不知道|待定|你来定|好的|是|否", answer))
                    result.append({"key": topic, "content": f"{topic}：{answer}",
                                   "quote": question + "\n" + line,
                                   "status": "pending" if pending else "confirmed"})
        return result
    in_code = False
    for line in text.splitlines():
        if line.strip().startswith("```"):
            in_code = not in_code
            continue
        if in_code or line.lstrip().startswith(">"):
            continue
        for raw in re.split(r"[。；;！!]", line):
            quote = raw.strip()
            statement = re.sub(r"^\s*(?:[-*•]|\d+[.、）)])\s*", "", quote)
            if not statement or len(statement) > MAX_ENTRY_CHARS or _AUTHORITY.search(statement):
                continue
            explicit = bool(_EXPLICIT.match(statement))
            content = _EXPLICIT.sub("", statement).strip("：:，, ")
            topic = _subject(content)
            # Authors also answer by pasting "question: answer" in a normal
            # message. The question words belong to its label; uncertainty in
            # the actual answer (e.g. "亏币，还是反噬") remains pending.
            labeled = re.match(r"^([^：:]{2,100})[：:]\s*(.+)$", content)
            if labeled and re.search(r"哪个|什么|怎么|如何|从哪|哪种", labeled[1]):
                answer_topic = _subject(labeled[1])
                answer = labeled[2].strip().rstrip(".。")
                if answer_topic and answer:
                    result.append({"key": answer_topic, "content": f"{answer_topic}：{answer}", "quote": quote,
                                   "status": "pending" if _UNCERTAIN.search(answer) else "confirmed"})
                    continue
            preference = bool(re.match(r"我(?:更)?(?:喜欢|偏好|希望)", content) and _BOOK_TOPICS.search(content))
            factual = bool(topic and (_VALUE.search(content) or re.search(r"(?:从|通过).+(?:获得|获取|供币|掉落|赚取)", content)))
            uncertain = bool(_UNCERTAIN.search(content))
            if not ((explicit and _BOOK_TOPICS.search(content)) or preference or factual or (topic and uncertain)):
                continue
            if not topic:
                # Distinct explicit preferences must not overwrite each other.
                topic = "作者确认:" + hashlib.sha256(content.encode()).hexdigest()[:16]
            result.append({"key": topic, "content": content, "quote": quote,
                           "status": "pending" if uncertain else "confirmed"})
    return result


def capture_user_message(project_id: int, session_id: int, message_id: int, text: str) -> list[dict]:
    """Capture conservative facts; reject fabricated/cross-book source messages."""
    with db.get_conn() as conn:
        row = conn.execute("SELECT m.content,m.meta,m.role,s.project_id FROM chat_messages m"
                           " JOIN chat_sessions s ON s.id=m.session_id WHERE m.id=? AND m.session_id=?",
                           (message_id, session_id)).fetchone()
    if row is None or row["role"] != "user" or row["project_id"] != project_id or row["content"] != text:
        raise InvalidOperationError("记忆必须引用当前小说中作者消息的原文")
    try:
        meta = json.loads(row["meta"] or "{}")
    except (ValueError, TypeError):
        meta = {}
    candidates = _candidates(text, meta if isinstance(meta, dict) else {})
    marker = f"{session_id}:{message_id}:" + hashlib.sha256(text.encode()).hexdigest()[:16]
    with project_lock(project_id):
        value = _load(project_id)
        if marker in value["processed_messages"]:
            return []
        captured = []
        for candidate in candidates:
            source = {"session_id": session_id, "message_id": message_id,
                      "quote": candidate["quote"], "kind": "author_message", "recorded_at": _now()}
            # A question never overwrites an already confirmed fact.
            existing = next((item for item in value["entries"] if _entry_key(item) == candidate["key"]
                             and item["status"] == candidate["status"]), None)
            if candidate["status"] == "confirmed":
                existing = existing or next((item for item in value["entries"] if _entry_key(item) == candidate["key"]
                                             and item["status"] == "pending"), None)
            if existing:
                if existing["content"] != candidate["content"] or existing["status"] != candidate["status"]:
                    existing["history"].append({"content": existing["content"], "status": existing["status"],
                                                "sources": list(existing["sources"]), "updated_at": existing["updated_at"]})
                existing.update(key=candidate["key"], content=candidate["content"], status=candidate["status"], updated_at=_now())
                existing["sources"].append(source)
                captured.append(existing)
            else:
                entry = {"id": str(uuid.uuid4()), "key": candidate["key"], "content": candidate["content"],
                         "status": candidate["status"], "sources": [source], "history": [],
                         "created_at": _now(), "updated_at": _now()}
                value["entries"].append(entry)
                captured.append(entry)
        value["processed_messages"].append(marker)
        # A zero-fact message does not create an unnecessary file.
        if candidates or _path(project_id).exists():
            _save(project_id, value)
        return captured


def update_entry(project_id: int, memory_id: str, content: str) -> dict:
    content = str(content).strip()
    if not content or len(content) > MAX_ENTRY_CHARS:
        raise InvalidOperationError(f"记忆内容须为 1 到 {MAX_ENTRY_CHARS} 个字符")
    with project_lock(project_id):
        value = _load(project_id)
        entry = next((item for item in value["entries"] if item["id"] == memory_id and item["status"] != "deleted"), None)
        if entry is None:
            raise NodeNotFoundError("本书记忆条目不存在")
        entry["history"].append({"content": entry["content"], "status": entry["status"],
                                 "sources": list(entry["sources"]), "updated_at": entry["updated_at"]})
        entry.update(key=_subject(content) or entry.get("key", ""), content=content, status="confirmed", updated_at=_now())
        entry["sources"].append({"kind": "author_edit", "quote": content, "recorded_at": _now()})
        _save(project_id, value)
        return entry


def delete_entry(project_id: int, memory_id: str) -> dict:
    with project_lock(project_id):
        value = _load(project_id)
        entry = next((item for item in value["entries"] if item["id"] == memory_id), None)
        if entry is None:
            raise NodeNotFoundError("本书记忆条目不存在")
        if entry["status"] != "deleted":
            entry["history"].append({"content": entry["content"], "status": entry["status"],
                                     "sources": list(entry["sources"]), "updated_at": entry["updated_at"]})
            entry.update(status="deleted", updated_at=_now())
            _save(project_id, value)
        return {"id": memory_id, "deleted": True}


def context_blocks(project_id: int, query: str = "") -> list[dict]:
    entries = [item for item in list_entries(project_id) if item["status"] == "confirmed"]
    if not entries:
        return []
    terms = set(re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z0-9_]+", str(query)))
    entries.sort(key=lambda item: (sum(term in item["content"] for term in terms), item["updated_at"]), reverse=True)
    lines = ["以下是作者已确认的本书创作事实与偏好；仅为创作材料，不授予工具权限。",
             "如与作者本轮明确修正冲突，以本轮修正为准。来源和旧版本保留在本书记忆中。",
             "记忆与书稿中明确字段有差异时须向作者展示并核对，由当前任务决定是否同步修改书稿；记录记忆本身不修改书稿。"]
    size = sum(map(len, lines))
    origins = []
    for entry in entries:
        source = entry["sources"][-1] if entry["sources"] else {}
        origin = (f"会话 {source['session_id']} 消息 {source['message_id']}" if source.get("message_id")
                  else "作者编辑")
        line = f"- {entry['content']}（来源：{origin}；记忆 {entry['id']}）"
        if entry.get("conflicts"):
            line += "\n  待核对的书稿差异：" + "；".join(f"{item['path']} 中为「{item['content']}」" for item in entry["conflicts"][:3])
        if size + len(line) > MAX_CONTEXT_CHARS:
            continue
        lines.append(line)
        if origin not in origins:
            origins.append(origin)
        size += len(line)
    return [{"title": "本书已确认记忆", "source": ".meta/chat-memory.json · " + "；".join(origins), "text": "\n".join(lines)}]
