"""Swap names in the app's modules for one test: stand-ins for dialogs that
would block (Windows' print dialog, file pickers, message boxes).

A name is replaced in EVERY loaded pdfapp module that has it, not just
mainwindow: MainWindow is assembled from mixins in their own modules
(signflow, fileflow, printflow...), each importing its own QMessageBox,
and a stand-in missing from one of them means a real dialog blocks the
test run. A name no module has is an error -- a typo would otherwise
leave the real dialog in place.
"""

from __future__ import annotations

import sys
from contextlib import contextmanager


def _app_modules():
    import pdfapp.mainwindow  # noqa: F401  -- loads every module MainWindow uses
    return [m for name, m in list(sys.modules.items())
            if m is not None and (name == "pdfapp" or name.startswith("pdfapp."))]


@contextmanager
def patched(**names):
    saved = []
    try:
        for name, value in names.items():
            holders = [m for m in _app_modules() if name in vars(m)]
            if not holders:
                raise AttributeError(f"no pdfapp module has {name!r} to patch")
            for module in holders:
                saved.append((module, name, vars(module)[name]))
                setattr(module, name, value)
        yield
    finally:
        for module, name, value in reversed(saved):
            setattr(module, name, value)
