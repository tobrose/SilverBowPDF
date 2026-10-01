"""Unexpected errors get shown, never swallowed (rule 7, app-wide).

An exception raised in a Qt slot -- a menu item, a button, a key -- goes to
sys.excepthook. Python's default prints it to stderr, and the installed app
has no console: the action silently does nothing. (That's exactly how a bug
in Print went unseen until it reached real users.) This hook says what failed,
in a message box, and appends the full traceback to a log in %TEMP% that
can be sent to whoever looks after the app.
"""

from __future__ import annotations

import sys
import tempfile
import time
import traceback
from pathlib import Path

LOG = Path(tempfile.gettempdir()) / "SilverbowPDF-errors.log"
_showing = False


def report(exc_type, exc, tb) -> None:
    global _showing
    from PySide6.QtWidgets import QApplication, QMessageBox

    from pdfapp import __version__
    details = "".join(traceback.format_exception(exc_type, exc, tb))
    try:
        with LOG.open("a", encoding="utf-8") as f:
            f.write(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')}  SilverbowPDF {__version__}\n"
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
