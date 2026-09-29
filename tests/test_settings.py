"""Settings and administrator policy, against throwaway INI files -- never
the real registry.

Run with pytest, or directly:  python tests/test_settings.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from pdfapp.settings import LETTER_SHORTCUTS, Settings  # noqa: E402

_app = QApplication.instance() or QApplication([])


def _ini(folder, name, **values) -> QSettings:
    s = QSettings(str(Path(folder, name)), QSettings.IniFormat)
    for key, value in values.items():
        s.setValue(key, value)
    s.sync()
    return s


def test_letter_shortcuts_default_off_and_user_choice():
    with tempfile.TemporaryDirectory() as tmp:
        settings = Settings(_ini(tmp, "user.ini"), _ini(tmp, "policy.ini"))
        choice = settings.letter_shortcuts()
        assert not choice.enabled and not choice.locked, "off by default"
        settings.set_letter_shortcuts(True)
        assert settings.letter_shortcuts().enabled
        # persisted: a fresh reader sees it
        again = Settings(_ini(tmp, "user.ini"), _ini(tmp, "policy.ini"))
        assert again.letter_shortcuts().enabled


def test_policy_locks_either_way():
    with tempfile.TemporaryDirectory() as tmp:
        user = _ini(tmp, "user.ini", **{LETTER_SHORTCUTS: 1})
        off = Settings(user, _ini(tmp, "off.ini", **{LETTER_SHORTCUTS: 0}))
        assert off.letter_shortcuts() == off.letter_shortcuts().__class__(False, True), \
            "policy beats the user's own choice"
        try:
            off.set_letter_shortcuts(True)
        except PermissionError:
            pass
        else:
            raise AssertionError("user changed a locked setting")
        on = Settings(_ini(tmp, "user2.ini"), _ini(tmp, "on.ini", **{LETTER_SHORTCUTS: "true"}))
        assert on.letter_shortcuts().enabled and on.letter_shortcuts().locked


def test_main_window_applies_the_setting():
    from pdfapp.mainwindow import MainWindow
    with tempfile.TemporaryDirectory() as tmp:
        w = MainWindow(Settings(_ini(tmp, "user.ini"), _ini(tmp, "policy.ini")))
        assert w.act_tool_highlight.shortcut().isEmpty(), "letters off by default"
        assert w.act_save.shortcut().toString() == "Ctrl+S", "Ctrl shortcuts untouched"
        w.act_letter_shortcuts.trigger()
        assert w.act_tool_highlight.shortcut().toString() == "H"
        assert w.act_tool_select.shortcut().toString() == "V"
        w.act_letter_shortcuts.trigger()
        assert w.act_tool_pen.shortcut().isEmpty()
        w.close()

        locked = MainWindow(Settings(_ini(tmp, "u.ini"), _ini(tmp, "p.ini", **{LETTER_SHORTCUTS: 1})))
        assert locked.act_tool_highlight.shortcut().toString() == "H"
        assert not locked.act_letter_shortcuts.isEnabled()
        assert "administrator" in locked.act_letter_shortcuts.text()
        locked.close()


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"PASS  {fn.__name__}")
        except Exception as exc:
            failed += 1
            print(f"FAIL  {fn.__name__}\n      {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
