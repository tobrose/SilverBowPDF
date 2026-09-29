"""User settings and administrator policy.

Two places, the usual Windows split:

  HKCU\\Software\\ButterPDF             the user's own choices (Tools menu)
  HKLM\\Software\\Policies\\ButterPDF   set by IT (GPO, deployment tooling, or the
                                       installer's task); wins over the user,
                                       and the menu shows it's locked

Policy matters on a non-persistent desktop: a user's HKCU choices may
not survive logoff unless profile management roams them, but HKLM is in
the machine image.

Settings objects are passed in, so tests use throwaway INI files and never
touch the real registry.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from PySide6.QtCore import QSettings

USER_KEY = "ButterPDF"
POLICY_PATH = r"HKEY_LOCAL_MACHINE\Software\Policies\ButterPDF"

LETTER_SHORTCUTS = "SingleKeyShortcuts"  # 1 on, 0 off (default: off)


def user_settings() -> QSettings:
    return QSettings(QSettings.NativeFormat, QSettings.UserScope, USER_KEY, USER_KEY)


def policy_settings() -> QSettings:
    # Read-only in practice: writing HKLM needs admin, and the app never tries.
    return QSettings(POLICY_PATH, QSettings.NativeFormat)


def _as_bool(value) -> Optional[bool]:
    """QSettings hands back ints, strings or bools depending on the backend
    (registry DWORD, INI text...). None when unset or unreadable."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    return None


@dataclass(frozen=True)
class Choice:
    enabled: bool
    locked: bool  # set by policy; the user can't change it


class Settings:
    def __init__(self, user: Optional[QSettings] = None, policy: Optional[QSettings] = None):
        self._user = user if user is not None else user_settings()
        self._policy = policy if policy is not None else policy_settings()

    def letter_shortcuts(self) -> Choice:
        """Single-letter tool shortcuts (V, H, T...). Off unless turned on:
        a stray keypress silently switching a mouse user into the pen is
        worse than a keyboard user switching them on once (user's call).
        Ctrl shortcuts are never affected: nobody presses Ctrl+S by accident."""
        forced = _as_bool(self._policy.value(LETTER_SHORTCUTS))
        if forced is not None:
            return Choice(forced, locked=True)
        mine = _as_bool(self._user.value(LETTER_SHORTCUTS))
        return Choice(False if mine is None else mine, locked=False)

    def set_letter_shortcuts(self, enabled: bool) -> None:
        if self.letter_shortcuts().locked:
            raise PermissionError("letter shortcuts are set by administrator policy")
        self._user.setValue(LETTER_SHORTCUTS, 1 if enabled else 0)
        self._user.sync()
