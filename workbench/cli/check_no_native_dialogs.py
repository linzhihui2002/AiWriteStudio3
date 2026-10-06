#!/usr/bin/env python
"""浏览器弹窗静态自检 —— 阻止原生对话框、凭据误识别与权限提示入口。

工作台前端的交互一律走应用内组件：

- 确认：``components/ConfirmDialog.tsx`` 的 ``useConfirm()``；
- 就地命名：``components/InlineEdit.tsx``；
- 只读 / 长内容：``components/Drawer.tsx``；短表单 / 决策：``components/Modal.tsx``；
- 轻提示与撤销：``state/useToast.tsx`` 的 ``push()``。

禁止出现原生阻塞对话框、新窗口跳转、密码输入框、离页确认，以及直接调用需要
浏览器权限的 API。API Key 使用 ``CredentialInput``，复制使用 ``copyTextToClipboard``。
显式文件上传和下载继续使用 ``input type=file`` 与下载链接。

注：应用内 ``confirm(...)`` / ``prompt(...)``（由 ``useConfirm`` 解构而来的局部名）不在拦截范围，
只拦截全局接收者（window/globalThis/self，包括方括号访问）与裸 ``alert(``，
避免误伤应用内 ``useConfirm()`` 的局部绑定。

用法::

    python workbench/cli/check_no_native_dialogs.py

退出码：0 = 通过；1 = 发现违规。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
FRONTEND_SRC = _PROJECT_ROOT / "workbench" / "frontend" / "src"

SCAN_SUFFIXES = {".ts", ".tsx", ".js", ".jsx", ".html"}
SKIP_DIR_NAMES = {"node_modules", "dist", "build"}

# 此脚本是静态守卫，不能替代浏览器验收，也不追踪别名或动态生成的 API 名称。
_GLOBAL = r"\b(?:window|globalThis|self)\s*"
_NATIVE_METHOD = r"(?:\.\s*(?:alert|confirm|prompt|open)|\[\s*['\"](?:alert|confirm|prompt|open)['\"]\s*\])"
PATTERNS = (
    (re.compile(_GLOBAL + _NATIVE_METHOD + r"\s*\("), "global native dialog/window"),
    (re.compile(r"(?<![.\w])alert\s*\("), "bare alert("),
    (re.compile(r"\btype\s*(?:=|:)\s*\{?\s*['\"]password['\"]"), "password input (browser credential UI)"),
    (re.compile(r"['\"]new-password['\"]"), "new-password (browser credential UI)"),
    (re.compile(r"\b(?:on)?beforeunload\b", re.I), "beforeunload (browser leave confirmation)"),
    (re.compile(r"\bnavigator\s*\.\s*clipboard\s*\.\s*(?:read|readText|write|writeText)\s*\("), "Async Clipboard (browser permission UI)"),
    (re.compile(r"\bnavigator\s*\.\s*permissions\s*\.\s*(?:query|request)\s*\("), "browser permission API"),
    (re.compile(r"\bNotification\s*\.\s*requestPermission\s*\("), "notification permission"),
    (re.compile(r"\bnavigator\s*\.\s*mediaDevices\s*\.\s*(?:getUserMedia|getDisplayMedia)\s*\("), "camera/microphone/screen permission"),
    (re.compile(r"\bnavigator\s*\.\s*geolocation\s*\.\s*(?:getCurrentPosition|watchPosition)\s*\("), "location permission"),
    (re.compile(r"\bnavigator\s*\.\s*credentials\s*\.\s*(?:get|store|create)\s*\("), "credential manager UI"),
    (re.compile(r"\bnavigator\s*\.\s*(?:bluetooth|usb|serial)\s*\.\s*(?:requestDevice|requestPort)\s*\("), "device permission"),
    (re.compile(r"\b(?:showOpenFilePicker|showSaveFilePicker|showDirectoryPicker)\s*\("), "file system permission picker"),
    (re.compile(r"\.\s*reportValidity\s*\("), "native form validation popup"),
)


class Violation:
    def __init__(self, path: Path, lineno: int, reason: str, text: str) -> None:
        self.path = path
        self.lineno = lineno
        self.reason = reason
        self.text = text.strip()

    def __str__(self) -> str:
        rel = self.path.relative_to(_PROJECT_ROOT)
        return f"{rel}:{self.lineno}  [{self.reason}]  {self.text}"


def iter_source_files() -> list[Path]:
    if not FRONTEND_SRC.is_dir():
        return []
    files: list[Path] = []
    for path in FRONTEND_SRC.rglob("*"):
        if not path.is_file() or path.suffix not in SCAN_SUFFIXES:
            continue
        if any(part in SKIP_DIR_NAMES for part in path.parts):
            continue
        files.append(path)
    entry = FRONTEND_SRC.parent / "index.html"
    if entry.is_file():
        files.append(entry)
    return sorted(files)


def strip_comments(text: str) -> str:
    """保留引号内的 URL/转义文本；仅将注释替换为空格，维持行号与匹配位置。"""
    output = list(text)
    index = 0
    quote = ""
    while index < len(text):
        char = text[index]
        if quote:
            if char == "\\":
                index += 2
                continue
            if char == quote:
                quote = ""
            index += 1
            continue
        if char in "'\"`":
            quote = char
            index += 1
            continue
        if text.startswith("//", index):
            end = text.find("\n", index)
            end = len(text) if end == -1 else end
        elif text.startswith("/*", index):
            closing = text.find("*/", index + 2)
            end = len(text) if closing == -1 else closing + 2
        else:
            index += 1
            continue
        for cursor in range(index, end):
            if text[cursor] not in "\r\n":
                output[cursor] = " "
        index = end
    return "".join(output)


def check_file(path: Path) -> list[Violation]:
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []

    code = strip_comments(raw)
    violations: list[Violation] = []
    lines = raw.splitlines()
    seen: set[tuple[int, str]] = set()
    for pattern, reason in PATTERNS:
        for match in pattern.finditer(code):
            lineno = code.count("\n", 0, match.start()) + 1
            if (lineno, reason) not in seen:
                seen.add((lineno, reason))
                violations.append(Violation(path, lineno, reason, lines[lineno - 1]))
    return sorted(violations, key=lambda item: (item.lineno, item.reason))


def main() -> int:
    print("=" * 64)
    print(" 浏览器弹窗自检（frontend/src + index.html 静态扫描）")
    print(f" 扫描根: {FRONTEND_SRC}")
    print("=" * 64)

    files = iter_source_files()
    print(f" 待扫描文件数: {len(files)}")

    all_violations: list[Violation] = []
    for path in files:
        all_violations.extend(check_file(path))

    print("-" * 64)
    if not all_violations:
        print(" PASS：未发现原生对话框、密码输入框或浏览器权限提示入口。")
        print(" 交互请使用应用内组件；密钥用 CredentialInput，复制用 copyTextToClipboard。")
        return 0

    print(f" FAIL：发现 {len(all_violations)} 处违规：")
    for violation in all_violations:
        print(f"   {violation}")
    print("-" * 64)
    print(" 修复指引：确认用 useConfirm()，命名用 InlineEdit，只读/长内容用 Drawer，")
    print(" 短表单用 Modal，提示与撤销用 useToast；密钥用 CredentialInput，复制用 copyTextToClipboard。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
