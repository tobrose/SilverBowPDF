"""Unexpected errors get shown, never swallowed (rule 7, app-wide).

An exception raised in a Qt slot -- a menu item, a button, a key -- goes to
sys.excepthook. Python's default prints it to stderr, and the installed app
has no console: the action silently does nothing. (That's exactly how a bug
in Print went unseen until it reached real users.) This hook says what failed,
in a message box, and appends the full traceback to a log that can be sent
to whoever looks after the app.

The log is in roaming AppData, not %TEMP%: on non-persistent desktops,
profile management carries roaming AppData across logons, while %TEMP% goes
at logoff -- usually before anyone asks for the log. Roaming means it's copied
at every logon, so it's kept small (LOG_LIMIT; the oldest entries go).
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import traceback
from pathlib import Path


def _log_path() -> Path:
    appdata = os.environ.get("APPDATA")
    folder = Path(appdata) / "SilverbowPDF" if appdata else Path(tempfile.gettempdir())
    return folder / "SilverbowPDF-errors.log"


LOG = _log_path()
LOG_LIMIT = 256 * 1024  # bytes; past this, the newest half is kept
_showing = False


def _append(entry: str) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    if LOG.exists() and LOG.stat().st_size > LOG_LIMIT:
        tail = LOG.read_bytes()[-(LOG_LIMIT // 2):]
        start = tail.find(b"\n--- ")  # from a whole entry, not mid-traceback
        kept = tail[start + 1:] if start >= 0 else tail
        LOG.write_bytes(b"(older entries dropped)\n" + kept)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(entry)


def report(exc_type, exc, tb) -> None:
    global _showing
    from PySide6.QtWidgets import QApplication, QMessageBox

    from pdfapp import __version__
    details = "".join(traceback.format_exception(exc_type, exc, tb))
    try:
        _append(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')}  SilverbowPDF {__version__}\n"
                f"{details}\n")
        where = f"The details were written to:\n{LOG}"
    except OSError as log_error:
        where = f"(The details couldn't be written to {LOG}: {log_error})"
    if sys.stderr is not None:  # a console, when there is one
        sys.stderr.write(details)
    if _showing or QApplication.instance() is None:
        return  # an error while showing an error: logged, not looped
    _showing = True
    try:
        QMessageBox.critical(
            QApplication.activeWindow(), "SilverbowPDF -- something went wrong",
            f"That didn't work, because of a problem in SilverbowPDF:\n\n"
            f"{exc_type.__name__}: {exc}\n\n{where}\n\n"
            f"Please send that file to whoever looks after SilverbowPDF.")
    finally:
        _showing = False


def install() -> None:
    sys.excepthook = report
