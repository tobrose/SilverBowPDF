"""Entry point: ``python -m pdfapp [file.pdf]`` and the frozen SilverbowPDF.exe."""

from __future__ import annotations

import sys

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

# Absolute, not relative: PyInstaller runs this file as a top-level script,
# where ``from .mainwindow`` has no parent package and fails at launch.
from pdfapp import ICON_FILE, errors, instance
from pdfapp.mainwindow import APP_NAME, MainWindow

APP_USER_MODEL_ID = "SilverbowPDF.SilverbowPDF"  # also in packaging/installer.iss


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv if argv is None else argv
    if "--self-test" in argv:
        from pdfapp.selftest import run

        rest = argv[argv.index("--self-test") + 1:]
        return run(rest[0] if rest else None)

    if sys.platform == "win32":
        # Run from source, Windows files the window under python.exe and the
        # taskbar shows Python's icon. An explicit ID makes it our own app;
        # the installer's shortcuts carry the same ID so a pinned shortcut and
        # the running window share one taskbar button.
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)

    app = QApplication(argv)
    app.setApplicationName(APP_NAME)
    app.setWindowIcon(QIcon(ICON_FILE))
    errors.install()  # an error in a menu action is shown, not lost

    # Qt strips its own options (-style etc.) from app.arguments(); what's
    # left after the program name is ours.
    files = app.arguments()[1:]

    # Already running in this session? It opens the files in tabs; this
    # copy ends before it costs a window's worth of memory (instance.py).
    if instance.send_to_running(files):
        return 0
    server = instance.InstanceServer()

    window = MainWindow()
    server.files_requested.connect(window.open_paths)
    window.show()
    for path in files:
        window.open_path(path)

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
