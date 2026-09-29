"""SilverbowPDF: desktop PDF viewer and editor."""

from pathlib import Path as _Path

# The one place the version is set: pyproject.toml and the installer
# (via build.ps1) read it from here.
__version__ = "0.2.0"

# Window/taskbar icon. Made by scripts/make_icon.py; the frozen build ships
# it beside this file (pdfapp.spec datas), so the same path works there.
ICON_FILE = str(_Path(__file__).with_name("icon.png"))
