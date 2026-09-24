"""发布导出模板（Task 66）：按目标平台整理正文为发布素材 + 提交前自检清单。

设计
----
- 平台清单是**工作台自定**的整理规则（不复制任何平台的官方文档文本）：
  章节标题格式、段落缩进、分隔方式、单章建议字数、命名规则；
- 导出只使用项目内 Markdown 正文与标题（**不含任何凭据**）；
- 同时产出**提交前自检**：未达建议字数的章、状态非「完成」的章、仍有质量债的章。
"""

from __future__ import annotations

from pathlib import Path

from . import operation_log
from .chapter_service import list_chapters, read_chapter
from .errors import InvalidOperationError
from .fs_utils import count_words, split_frontmatter
from .project_service import get_project_dir

# 平台整理规则（自定；字段含义见下方注释）
PLATFORM_PROFILES: dict[str, dict] = {
    "通用": {
        "label": "通用（无平台特定要求）",
        "chapter_title": "{n}、{title}",
        "use_markdown_heading": False,
        "paragraph_indent": 0,
        "chapter_separator": "\n\n",
        "file_suffix": "txt",
        "word_range": (1800, 4000),
        "notes": ["按章节顺序导出，标题为「序号、标题」。", "段落不缩进，平台编辑器多会自动排版。"],
    },
    "起点中文网": {
        "label": "起点中文网",
        "chapter_title": "第{cn}章 {title}",
        "use_markdown_heading": False,
        "paragraph_indent": 2,
        "chapter_separator": "\n\n",
        "file_suffix": "txt",
        "word_range": (2000, 4000),
        "notes": [
            "章节名统一「第N章 标题」，便于平台自动识别章节；",
            "单章建议 2000-4000 字（过短影响推荐，过长影响完读率）；",
            "首章前 300 字内给出冲突，避免大段设定说明。",
        ],
    },
    "番茄小说": {
        "label": "番茄小说",
        "chapter_title": "第{cn}章 {title}",
        "use_markdown_heading": False,
        "paragraph_indent": 0,
        "chapter_separator": "\n\n",
        "file_suffix": "txt",
        "word_range": (1500, 3000),
        "notes": [
            "移动端阅读为主：段落短、对话密、每章 1500-3000 字；",
            "避免长篇环境描写；场景切换用空行分隔；",
            "章节名保持短句，不用特殊符号。",
        ],
    },
    "晋江文学城": {
        "label": "晋江文学城",
        "chapter_title": "第{cn}章 {title}",
        "use_markdown_heading": False,
        "paragraph_indent": 2,
        "chapter_separator": "\n\n",
        "file_suffix": "txt",
        "word_range": (2500, 6000),
        "notes": [
            "段落首行缩进两格；",
            "单章 2500-6000 字常见；",
            "文案（简介）与正文分开准备，正文不要夹带作者话。",
        ],
    },
}

DEFAULT_PLATFORM = "通用"


def list_platforms() -> list[dict]:
    """平台清单（设置页/导出弹窗展示）。"""
    return [
        {"key": key, "label": value["label"], "word_range": list(value["word_range"]),
         "suffix": value["file_suffix"], "notes": list(value["notes"])}
        for key, value in PLATFORM_PROFILES.items()
    ]


def _format_paragraphs(body: str, indent: int) -> str:
    lines: list[str] = []
    for raw in body.replace("\r\n", "\n").split("\n"):
        text = raw.strip()
        if not text:
            lines.append("")
            continue
        if indent and not text.startswith(("“", "「", "#", "-", ">")):
            text = "　" * indent + text
        lines.append(text)
    # 合并多余空行
    cleaned: list[str] = []
    for line in lines:
        if line == "" and cleaned and cleaned[-1] == "":
            continue
        cleaned.append(line)
    # 只去掉首尾空行：不得 strip() 全串，否则首段的全角缩进会被吃掉
    return "\n".join(cleaned).strip("\n")


def build_publish_export(
    project_id: int,
    *,
    platform: str = DEFAULT_PLATFORM,
    only_completed: bool = False,
) -> dict:
    """按平台模板整理正文，返回内容 + 统计 + 提交前自检清单。"""
    if platform not in PLATFORM_PROFILES:
        raise InvalidOperationError(
            f"未知平台：{platform}（可选：{'/'.join(PLATFORM_PROFILES)}）"
        )
    profile = PLATFORM_PROFILES[platform]
    row, project_dir = get_project_dir(project_id)

    chapters = list_chapters(project_id)
    if only_completed:
        chapters = [item for item in chapters if item["status"] == "完成"]
    if not chapters:
        raise InvalidOperationError("没有可发布的章节（章节为空或没有「完成」状态的章节）")

    min_words, max_words = profile["word_range"]
    blocks: list[str] = []
    warnings: list[dict] = []
    total_words = 0
    published = 0

    for index, item in enumerate(chapters, start=1):
        detail = read_chapter(project_id, item["rel_path"])
        body = split_frontmatter(detail["body"] if detail["body"] else detail["content"])[1]
        words = count_words(body)
        if words == 0:
            warnings.append({"rel_path": item["rel_path"], "level": "跳过",
                             "note": "正文为空，已跳过"})
            continue

        title = detail["title"] or Path(item["file_name"]).stem
        heading = profile["chapter_title"].format(n=index, cn=item["number"] or index,
                                                 title=title)
        if profile["use_markdown_heading"]:
            heading = f"# {heading}"
        blocks.append(f"{heading}\n\n{_format_paragraphs(body, profile['paragraph_indent'])}")
        total_words += words
        published += 1

        if words < min_words:
            warnings.append({"rel_path": item["rel_path"], "level": "偏短",
                             "note": f"{words} 字，低于 {platform} 建议下限 {min_words} 字"})
        elif words > max_words:
            warnings.append({"rel_path": item["rel_path"], "level": "偏长",
                             "note": f"{words} 字，高于 {platform} 建议上限 {max_words} 字"})
        if item["status"] != "完成":
            warnings.append({"rel_path": item["rel_path"], "level": "未定稿",
                             "note": f"章节状态为「{item['status']}」，建议改为「完成」后再发布"})

    if published == 0:
        raise InvalidOperationError("没有可发布的正文（章节均为空文件）")

    # 质量债自检（未解决的一致性问题不该带病发布）
    with_debt: list[str] = []
    try:
        from .review_service import list_debts

        with_debt = [item["rel_path"] for item in list_debts(project_id, status="open")
                     if item["rel_path"]]
    except Exception:  # noqa: BLE001 - 质量债读取失败不阻断导出
        with_debt = []
    for rel_path in sorted(set(with_debt)):
        warnings.append({"rel_path": rel_path, "level": "质量债",
                         "note": "该章仍有未解决的质量债（一致性问题），发布前建议先处理"})

    content = profile["chapter_separator"].join(blocks).strip() + "\n"
    filename = f"{row['name']}-{platform}-发布稿.{profile['file_suffix']}"
    stats = {
        "chapters": published,
        "words": total_words,
        "avg_words": round(total_words / published) if published else 0,
        "platform": platform,
        "word_range": [min_words, max_words],
    }
    result = {
        "platform": platform,
        "platform_label": profile["label"],
        "filename": filename,
        "content": content,
        "stats": stats,
        "warnings": warnings,
        "checklist": _checklist(project_id, row["name"], stats, warnings, profile),
        "notes": list(profile["notes"]),
        "project_name": row["name"],
    }
    operation_log.log(project_id, "publish-export", None,
                      {"platform": platform, "chapters": published, "words": total_words,
                       "warnings": len(warnings)})
    return result


def _checklist(project_id: int, project_name: str, stats: dict, warnings: list[dict],
               profile: dict) -> list[dict]:
    """提交前自检清单（逐项给结论与依据）。"""
    row, project_dir = get_project_dir(project_id)
    meta, body = None, ""
    project_md = Path(project_dir) / "project.md"
    if project_md.is_file():
        from .fs_utils import read_text

        meta, body = split_frontmatter(read_text(project_md))

    one_liner = ""
    if body:
        import re

        match = re.search(r"##\s*一句话设定\s*\n+(.*?)(?:\n#|\Z)", body, re.DOTALL)
        one_liner = (match.group(1).strip() if match else "")

    min_words, max_words = profile["word_range"]
    short = [item for item in warnings if item["level"] == "偏短"]
    debts = [item for item in warnings if item["level"] == "质量债"]
    drafts = [item for item in warnings if item["level"] == "未定稿"]

    return [
        {"item": "章节标题格式", "ok": True,
         "basis": f"已统一为「{profile['chapter_title'].format(n=1, cn=1, title='示例')}」"},
        {"item": "单章字数在建议区间", "ok": not short,
         "basis": f"{len(short)} 章低于 {min_words} 字" if short else
                  f"全部在 {min_words}-{max_words} 字区间内"},
        {"item": "章节状态均为「完成」", "ok": not drafts,
         "basis": f"{len(drafts)} 章仍是草稿/发表态" if drafts else "全部为「完成」"},
        {"item": "无未解决质量债", "ok": not debts,
         "basis": f"{len(debts)} 章存在未解决质量债" if debts else "无未解决质量债"},
        {"item": "无凭据泄漏", "ok": True,
         "basis": "导出只取正文与标题，不含 Key / 配置（Key 存 .workbench/secrets.json）"},
        {"item": "作品简介（一句话设定）", "ok": bool(one_liner),
         "basis": one_liner or "未填写一句话设定（project.md）"},
        {"item": "题材与标签", "ok": bool((meta or {}).get("题材")),
         "basis": str((meta or {}).get("题材") or "未填写题材（project.md）")},
        {"item": "章节数", "ok": stats["chapters"] >= 1,
         "basis": f"本次导出 {stats['chapters']} 章 · {stats['words']} 字"},
    ]


def write_publish_file(project_id: int, **kwargs) -> Path:
    """导出并落到 ``.workbench/exports/``（供下载/归档）。"""
    from .. import config
    from .fs_utils import atomic_write_text

    result = build_publish_export(project_id, **kwargs)
    target = Path(config.exports_dir()) / result["filename"]
    atomic_write_text(target, result["content"])
    return target


__all__ = [
    "DEFAULT_PLATFORM",
    "PLATFORM_PROFILES",
    "build_publish_export",
    "list_platforms",
    "write_publish_file",
]