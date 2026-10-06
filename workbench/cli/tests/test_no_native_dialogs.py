"""Offline regression checks for the browser-popup static guard."""
from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

_SOURCE = Path(__file__).resolve().parents[1] / "check_no_native_dialogs.py"
_SPEC = importlib.util.spec_from_file_location("native_dialog_guard", _SOURCE)
assert _SPEC and _SPEC.loader
guard = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(guard)


class NativeDialogGuardTests(unittest.TestCase):
    def scan(self, text: str):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.tsx"
            path.write_text(text, encoding="utf-8")
            return guard.check_file(path)

    def test_url_and_comments_preserve_executable_code_and_line_numbers(self):
        source = '/* window.alert("comment")\n */\nconst url = "https://example.test/v1"; window.alert(url) // comment\n'
        violations = self.scan(source)
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0].lineno, 3)
        self.assertEqual(len(guard.strip_comments(source)), len(source))

    def test_application_confirm_and_explicit_file_selection_are_allowed(self):
        source = "const [confirm] = useConfirm(); await confirm({ title: '删除' });\n<input type=\"file\" />\ndocument.execCommand('copy')\n"
        self.assertEqual(self.scan(source), [])

    def test_native_receivers_and_multiline_calls_are_blocked(self):
        for source in ['window.confirm("x")', "globalThis['prompt']('x')", 'self.\nopen("x")', 'alert("x")']:
            with self.subTest(source=source):
                self.assertTrue(self.scan(source))

    def test_browser_generated_dialog_entry_points_are_blocked(self):
        sources = [
            '<input type="password" />', "<input type={'password'} />", "const autocomplete = 'new-password'",
            "window.addEventListener('beforeunload', handler)", "navigator.clipboard.writeText('中文')",
            "window.onbeforeunload = handler", "<div onBeforeUnload={handler} />",
            "navigator.permissions.query({ name: 'clipboard-write' })", 'Notification.requestPermission()',
            'navigator.mediaDevices.getUserMedia({ audio: true })', 'navigator.mediaDevices.getDisplayMedia()',
            'navigator.geolocation.getCurrentPosition(done)', 'navigator.credentials.store(credentials)',
            'navigator.bluetooth.requestDevice(options)', 'showDirectoryPicker()', 'input.reportValidity()',
        ]
        for source in sources:
            with self.subTest(source=source):
                self.assertTrue(self.scan(source))

    def test_commented_examples_do_not_fail(self):
        self.assertEqual(self.scan('// type="password"\n/* navigator.clipboard.writeText("x") */\nconst url = "https://example.test/v1"\n'), [])


if __name__ == "__main__":
    unittest.main()
