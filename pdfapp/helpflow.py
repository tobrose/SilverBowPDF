"""The Help menu: the guide, Check for Updates, and About."""

from __future__ import annotations

import html
import platform
import sys
from typing import Optional

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QMessageBox,
    QTextBrowser,
    QVBoxLayout,
)

from pdfapp import __version__

from . import errors, updates
from .guide import GUIDE_HTML

APP = "SilverbowPDF"


class GuideDialog(QDialog):
    """The guide in its own window, not modal: it can stay open beside the
    document while the user tries what it describes."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"{APP} Guide")
        self.resize(720, 760)
        layout = QVBoxLayout(self)
        self.browser = QTextBrowser(self)
        self.browser.setOpenExternalLinks(True)
        self.browser.setHtml(GUIDE_HTML)
        layout.addWidget(self.browser)
        buttons = QDialogButtonBox(QDialogButtonBox.Close, self)
        buttons.rejected.connect(self.close)
        layout.addWidget(buttons)


class AboutDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"About {APP}")
        layout = QVBoxLayout(self)
        text = QLabel(about_html(), self)
        text.setTextFormat(Qt.RichText)
        text.setOpenExternalLinks(True)
        text.setTextInteractionFlags(Qt.TextBrowserInteraction)
        text.setWordWrap(True)
        layout.addWidget(text)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok, self)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)
        self.setMinimumWidth(460)


def about_html() -> str:
    """What IT needs when someone reports a problem: the version, what it's
    built on, and where the error log is."""
    import pikepdf
    import pypdfium2
    import PySide6
    pdfium = getattr(pypdfium2, "PYPDFIUM_INFO", "")
    esc = lambda value: html.escape(str(value))  # noqa: E731 -- a path can hold '&'
    return (
        f"<h2>{APP} {__version__}</h2>"
        "<p>View, mark up, fill in, sign and rearrange PDFs.</p>"
        f"<p>Python {esc(platform.python_version())} &middot; Qt (PySide6) "
        f"{esc(PySide6.__version__)} &middot; pikepdf {esc(pikepdf.__version__)}"
        f" &middot; pypdfium2 {esc(pdfium)}</p>"
        f"<p>Error log: <code>{esc(errors.LOG)}</code></p>"
        "<p>Free software under the GNU General Public License, version 3 or later. "
        f"Source and releases: <a href='https://github.com/{updates.REPO}'>"
        f"github.com/{updates.REPO}</a></p>"
    )


class HelpMixin:
    """MainWindow: Help > Guide, Check for Updates, About."""

    _guide: Optional[GuideDialog] = None
    _update_check: Optional[updates.ReleaseCheck] = None

    def show_guide(self) -> None:
        if self._guide is None:
            self._guide = GuideDialog(self)
        self._guide.show()
        self._guide.raise_()
        self._guide.activateWindow()

    def show_about(self) -> None:
        AboutDialog(self).exec()

    def check_for_updates(self) -> None:
        """Ask GitHub for the latest release. The answer arrives later
        (_update_checked); meanwhile the window stays usable."""
        if self._update_check is not None:
            return  # one at a time
        self.act_check_updates.setEnabled(False)
        self.statusBar().showMessage("Checking for updates…")
        check = updates.ReleaseCheck(self, updates.RELEASES_URL)
        check.done.connect(self._update_checked)
        self._update_check = check
        check.start()

    def _update_checked(self, release, error) -> None:
        self._update_check.deleteLater()
        self._update_check = None
        self.act_check_updates.setEnabled(True)
        self.statusBar().clearMessage()
        if error is not None:
            QMessageBox.warning(self, "Check for Updates",
                                f"Couldn't check for a newer version:\n\n{error}\n\n"
                                f"You can look yourself at {updates.RELEASES_PAGE}")
            return
        if not updates.newer_than_running(release):
            QMessageBox.information(self, "Check for Updates",
                                    f"You have the latest version, {APP} {__version__}.")
            return
        answer = QMessageBox.question(
            self, "Check for Updates",
            f"{APP} {release.version} is available (you have {__version__}).\n\n"
            "Open its download page? (On a managed computer, your IT department "
            "may install updates for you.)",
            QMessageBox.Open | QMessageBox.Close, QMessageBox.Open)
        if answer == QMessageBox.Open:
            QDesktopServices.openUrl(QUrl(release.page))
