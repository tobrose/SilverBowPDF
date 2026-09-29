"""Entry point: ``python -m pdfapp [file.pdf]`` and the frozen SilverbowPDF.exe."""

from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

# Absolute, not relative: PyInstaller runs this file as a top-level script,
# where ``from .mainwindow`` has no parent package and fails at launch.
from pdfapp.mainwindow import APP_NAME, MainWindow


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv if argv is None else argv
    if "--self-test" in argv:
        from pdfapp.selftest import run

        rest = argv[argv.index("--self-test") + 1:]
        return run(rest[0] if rest else None)

    app = QApplication(argv)
    app.setApplicationName(APP_NAME)

    window = MainWindow()
    window.show()

    # Qt strips its own options (-style etc.) from app.arguments(); what's
    # left after the program name is ours.
    files = app.arguments()[1:]
    if files:
        window.open_path(files[0])

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
