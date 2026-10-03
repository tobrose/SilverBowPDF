"""Signing: picture signatures, certificate signing, checking signatures."""

from __future__ import annotations

import getpass
from pathlib import Path
from typing import Optional

from PySide6.QtWidgets import QDialog, QMessageBox

from .document import PdfDocument
from . import pdfsign
from .sigimage import SignatureImageError, load_signature_image
from .signui import (
    CreateIDDialog,
    SignaturesDialog,
    SignDialog,
    choose_picture,
    summary as signature_summary,
)
from .tools import Tool


class SigningMixin:
    """MainWindow: the Sign menu -- signature pictures, certificate signing
    (pdfsign), and the signatures found on opening a document."""

    # ---- signing -------------------------------------------------------

    def _signature_picture(self, ask: bool):
        """The user's signature picture: loaded once, from the remembered
        file; asked for if there's none (and `ask`)."""
        if self._picture is not None:
            return self._picture
        path = self.settings.signature_picture()
        if path and Path(path).exists():
            try:
                self._picture = load_signature_image(path)
                return self._picture
            except SignatureImageError:
                pass  # the file changed into something unusable: ask again
        if not ask:
            return None
        chosen = choose_picture(self, path)
        if chosen is None:
            return None
        path, self._picture = chosen
        self.settings.set_signature_picture(path)
        return self._picture

    def choose_signature_picture(self) -> None:
        chosen = choose_picture(self, self.settings.signature_picture())
        if chosen is not None:
            path, self._picture = chosen
            self.settings.set_signature_picture(path)
            self.view.signature_image = self._picture
            self.statusBar().showMessage("Signature picture set.", 5000)

    def place_signature_picture(self) -> None:
        if self.view.document is None or not self._allowed(self.access.annotate,
                                                            "adding signatures"):
            self.view.set_tool(Tool.SELECT)
            return
        picture = self._signature_picture(ask=True)
        if picture is None:
            self.view.set_tool(Tool.SELECT)
            self._on_tool_changed(self.view.tool)  # un-tick the menu item
            return
        self.view.signature_image = picture
        self.view.set_tool(Tool.SIGNATURE)
        self.statusBar().showMessage(
            "Click where your signature goes, or drag a box to size it.", 8000)

    def start_certificate_signing(self) -> None:
        if self.view.document is None or not self._allowed(self.access.annotate,
                                                            "adding signatures"):
            self.view.set_tool(Tool.SELECT)
            return
        self.view.set_tool(Tool.SIGN)
        self.statusBar().showMessage(
            "Drag a box where the signature goes (or click for a standard size).", 8000)

    def create_digital_id(self) -> None:
        dialog = CreateIDDialog(self, getpass.getuser())
        if dialog.exec() == QDialog.Accepted and dialog.created is not None:
            self.settings.remember_digital_id_file(str(dialog.created.path))
            self.statusBar().showMessage(f"Digital ID saved: {dialog.created.path}", 10000)

    def sign_with_certificate(self, index: int, rect) -> None:
        """Sign where the box was drawn. The signed file is the document's own
        bytes plus a new revision -- so signatures already in it stay valid --
        unless there are unsaved changes, which have to be written first."""
        doc = self.view.document
        if doc is None or self.history is None or not self._allowed(self.access.annotate,
                                                                    "adding signatures"):
            return
        self.view.commit_text_edit()
        self.view.commit_field_edit()
        windows_ids = []
        try:
            from . import wincert
            windows_ids = wincert.list_ids()
        except Exception as exc:  # shown, not hidden: the .pfx route still works
            self.statusBar().showMessage(f"Windows certificates couldn't be listed: {exc}", 10000)
        dialog = SignDialog(self, self.settings, self._signature_picture(ask=False),
                            windows_ids, int(self.winId()))
        if dialog.exec() != QDialog.Accepted:
            return
        try:
            self._sign_accepted(dialog, doc, index, rect)
        finally:
            # The signer holds a Windows certificate (and a smart card's key)
            # open. pdfsign.sign releases it; any earlier way out -- Cancel
            # on the save dialog, a failed write -- has to as well.
            close = getattr(dialog.signer, "close", None)
            if close is not None:
                close()

    def _sign_accepted(self, dialog, doc: PdfDocument, index: int, rect) -> None:
        if dialog.picture_path:
            self._picture = dialog.picture
            self.settings.set_signature_picture(dialog.picture_path)
        dest = self._ask_save_path("Save the Signed Document", doc.path)
        if dest is None:
            return
        unchanged = self._unchanged()
        if unchanged:
            problem = pdfsign.damaged(doc.path, doc.password, index)
            if problem is not None:
                if not self._confirm_repair(problem):
                    return
                unchanged = False  # sign a clean rewrite instead of the file's own bytes
        if not unchanged and not self._confirm_breaking_signatures(dest):
            return
        written = None
        if unchanged:
            base, password = doc.path, doc.password
        else:
            written = self._write(doc.pages, dest, "Signing failed")
            if written is None:
                return
            base, password = written[0], self._saved_password()
        signed = dest.with_name(f".{dest.stem}.signed.tmp")
        try:
            pdfsign.sign(base, password, signed, index, rect, doc.page_rotation(index),
                         dialog.look, dialog.signer)
        except Exception as exc:
            signed.unlink(missing_ok=True)
            QMessageBox.critical(self, "Signing failed", f"The document wasn't signed:\n\n{exc}")
            return
        finally:
            if written is not None:
                written[0].unlink(missing_ok=True)
        self._install(signed, dest, password, "Signed")

    def _confirm_repair(self, problem: str) -> bool:
        answer = QMessageBox.warning(
            self, "Repair before signing?",
            f"{self.view.document.path.name} is slightly damaged inside. It opens and "
            f"shows fine, but a signature can't be added to it as it is.\n\n"
            f"Click OK to repair it -- by saving a clean copy, which changes nothing "
            f"you can see -- and sign that.\n\n(What was found: {problem})",
            QMessageBox.Ok | QMessageBox.Cancel, QMessageBox.Ok)
        return answer == QMessageBox.Ok

    def _check_signatures(self, document: Optional[PdfDocument]) -> None:
        """On opening: check the document's signatures and say what was found."""
        self._signature_infos = []
        self.view.signed_fields = frozenset()
        self.act_signatures.setEnabled(document is not None)
        if document is None or not document.signature_count():
            self.signature_bar.hide()
            return
        try:
            self._signature_infos = pdfsign.check(document.path, document.password)
        except Exception as exc:  # rule 7: say so, don't hide it
            self._show_signature_bar(pdfsign.BROKEN,
                                     f"<b>The signatures couldn't be checked:</b> {exc}")
            return
        self.view.signed_fields = frozenset(i.field for i in self._signature_infos)
        if self._signature_infos:
            self._show_signature_bar(*signature_summary(self._signature_infos))
        else:
            self.signature_bar.hide()

    def _show_signature_bar(self, level: str, text: str) -> None:
        background, border = {pdfsign.OK: ("#e6f4ea", "#9fd3ae"),
                              pdfsign.UNTRUSTED: ("#fff4ce", "#e0c870"),
                              pdfsign.CHANGED: ("#ffe7cc", "#e8b070"),
                              pdfsign.BROKEN: ("#fde7e9", "#e8a0a8")}[level]
        # Explicit colours for the button too (dark mode; see the restriction bar).
        self.signature_bar.setStyleSheet(
            f"#signatures {{ background: {background}; border-bottom: 1px solid {border}; }}"
            "QLabel { color: #202020; }"
            "QPushButton { color: #1e1e1e; background: #ffffff; border: 1px solid #a0a0a8;"
            " border-radius: 3px; padding: 3px 14px; }")
        self.signature_text.setText(text)
        self.signature_bar.show()

    def show_signatures(self) -> None:
        if not self._signature_infos:
            QMessageBox.information(self, "Signatures", "This document has no digital signatures.")
            return
        SignaturesDialog(self, self._signature_infos).exec()
