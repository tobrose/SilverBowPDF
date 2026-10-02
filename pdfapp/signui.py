"""Signing dialogs: choose a digital ID and how the signature looks; make a
digital ID; see the signatures a document carries. Qt; the work is done in
digitalid / wincert / pdfsign.
"""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path
from typing import List, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from . import digitalid
from .annots import SignatureImage
from .digitalid import PFX, DigitalID, DigitalIDError
from .pdfsign import BROKEN, CHANGED, OK, REASONS, UNTRUSTED, Look, SigInfo
from .sigimage import SignatureImageError, load_signature_image, to_qimage

_BROWSE, _CREATE = "browse", "create"
LEVEL_COLOURS = {OK: "#1e7b34", UNTRUSTED: "#8a6100", CHANGED: "#b35c00", BROKEN: "#b00020"}
LEVEL_WORDS = {OK: "Valid", UNTRUSTED: "Signer not verified", CHANGED: "Changed after signing",
               BROKEN: "Broken"}


def default_id_folder() -> Path:
    return Path(os.path.expanduser("~")) / "Documents" / "SilverbowPDF Digital IDs"


def choose_picture(parent, start: str = "") -> Optional[tuple]:
    """Ask for a signature picture. (path, SignatureImage), or None."""
    path, _ = QFileDialog.getOpenFileName(
        parent, "Choose your signature picture", start or str(Path.home()),
        "Images (*.png *.jpg *.jpeg *.bmp)")
    if not path:
        return None
    try:
        return path, load_signature_image(path)
    except SignatureImageError as exc:
        from PySide6.QtWidgets import QMessageBox
        QMessageBox.warning(parent, "Signature picture", str(exc))
        return None


class CreateIDDialog(QDialog):
    """Acrobat's "New digital ID": a self-signed certificate in a
    password-protected .pfx. On accept, `created` is the DigitalID."""

    def __init__(self, parent, default_name: str = ""):
        super().__init__(parent)
        self.setWindowTitle("Create a Digital ID")
        self.setMinimumWidth(480)
        self.created: Optional[DigitalID] = None
        layout = QVBoxLayout(self)
        intro = QLabel("A digital ID you make yourself. People you send signed documents to "
                       "will see your signature as valid, but your identity as not verified, "
                       "unless they choose to trust it. For a verified identity, ask IT for "
                       "a certificate.", self)
        intro.setWordWrap(True)
        layout.addWidget(intro)
        form = QFormLayout()
        self.name = QLineEdit(default_name, self)
        self.email = QLineEdit(self)
        self.organization = QLineEdit(self)
        self.password = QLineEdit(self, echoMode=QLineEdit.Password)
        self.again = QLineEdit(self, echoMode=QLineEdit.Password)
        self.where = QLineEdit(self)
        browse = QPushButton("Browse…", self, autoDefault=False)
        browse.clicked.connect(self._browse)
        where = QHBoxLayout()
        where.addWidget(self.where, 1)
        where.addWidget(browse)
        holder = QWidget(self)
        holder.setLayout(where)
        form.addRow("Your name:", self.name)
        form.addRow("Email (optional):", self.email)
        form.addRow("Organization (optional):", self.organization)
        form.addRow("Password:", self.password)
        form.addRow("Type it again:", self.again)
        form.addRow("Save the ID in:", holder)
        layout.addLayout(form)
        note = QLabel("Keep the file and its password safe: anyone with both can sign as you, "
                      "and a lost password can't be recovered.", self)
        note.setWordWrap(True)
        note.setStyleSheet("color: #808088;")
        layout.addWidget(note)
        self.error = QLabel("", self)
        self.error.setStyleSheet("color: #b00020;")
        layout.addWidget(self.error)
        buttons = QDialogButtonBox(QDialogButtonBox.Cancel, self)
        create = buttons.addButton("Create", QDialogButtonBox.AcceptRole)
        create.clicked.connect(self._create)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.name.textChanged.connect(self._suggest_path)
        self._suggest_path()

    def _suggest_path(self) -> None:
        stem = "".join(c for c in self.name.text().strip() if c not in '\\/:*?"<>|') or "My ID"
        self.where.setText(str(default_id_folder() / f"{stem}.pfx"))

    def _browse(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Save the digital ID", self.where.text(),
                                              "Digital ID (*.pfx)")
        if path:
            self.where.setText(path if path.lower().endswith(".pfx") else path + ".pfx")

    def _create(self) -> None:
        if not self.name.text().strip():
            self.error.setText("Type your name.")
        elif not self.password.text():
            self.error.setText("Choose a password.")
        elif self.password.text() != self.again.text():
            self.error.setText("The two passwords are different.")
        elif Path(self.where.text()).exists():
            self.error.setText("There's already a file there; choose another name.")
        else:
            try:
                self.created = digitalid.create_pfx_id(
                    Path(self.where.text()), self.name.text(), self.password.text(),
                    self.email.text(), self.organization.text())
            except (DigitalIDError, OSError) as exc:
                self.error.setText(str(exc))
                return
            self.accept()


class SignDialog(QDialog):
    """Which digital ID, and how the signature looks. On accept: `signer`
    (a pyHanko signer, ready), `identity`, `look`, and `picture_path` if a
    picture was newly chosen here."""

    def __init__(self, parent, settings, picture: Optional[SignatureImage],
                 windows_ids: List[DigitalID], hwnd: int = 0):
        super().__init__(parent)
        self.setWindowTitle("Sign with a Certificate")
        self.settings, self.picture, self.hwnd = settings, picture, hwnd
        self.signer = self.identity = self.look = None
        self.picture_path: Optional[str] = None
        layout = QVBoxLayout(self)
        form = QFormLayout()

        self.ids = QComboBox(self)
        for ident in windows_ids:
            self.ids.addItem(ident.label(), ident)
        for path in settings.digital_id_files():
            if Path(path).exists():
                self.ids.addItem(f"{Path(path).stem} — {Path(path).name}",
                                 DigitalID(PFX, Path(path).stem, path=Path(path)))
        self.ids.addItem("Use a digital ID file (.pfx)…", _BROWSE)
        self.ids.addItem("Create a new digital ID…", _CREATE)
        self.ids.activated.connect(self._id_chosen)
        form.addRow("Digital ID:", self.ids)
        self.password = QLineEdit(self, echoMode=QLineEdit.Password)
        form.addRow("Password:", self.password)

        self.use_picture = QCheckBox("Include my signature picture", self)
        self.preview = QLabel(self)
        self.preview.setFixedHeight(48)
        # On paper, as it will be: dark ink would vanish on a dark theme.
        self.preview.setStyleSheet("background: white; color: #555; padding: 2px;")
        change = QPushButton("Choose Picture…", self, autoDefault=False)
        change.clicked.connect(self._choose_picture)
        row = QHBoxLayout()
        row.addWidget(self.use_picture)
        row.addWidget(self.preview, 1)
        row.addWidget(change)
        holder = QWidget(self)
        holder.setLayout(row)
        form.addRow("Appearance:", holder)
        self.reason = QComboBox(self, editable=True)
        self.reason.addItems(("",) + REASONS)
        self.reason.setCurrentText(settings.signing_reason())
        form.addRow("Reason (optional):", self.reason)
        self.location = QLineEdit(self)
        form.addRow("Location (optional):", self.location)
        self.show_date = QCheckBox("Show the date and time", self, checked=True)
        form.addRow("", self.show_date)
        layout.addLayout(form)
        self.error = QLabel("", self)
        self.error.setStyleSheet("color: #b00020;")
        self.error.setWordWrap(True)
        layout.addWidget(self.error)
        buttons = QDialogButtonBox(QDialogButtonBox.Cancel, self)
        self.sign_button = buttons.addButton("Sign…", QDialogButtonBox.AcceptRole)
        self.sign_button.clicked.connect(self._sign)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._show_picture()
        # Only a user's pick opens a file dialog: with no IDs yet, the first
        # item is "Use a digital ID file…", and opening on it would be rude.
        self._sync_password()

    def _current(self):
        return self.ids.currentData()

    def _id_chosen(self, _index: int) -> None:
        data = self._current()
        if data == _BROWSE:
            path, _ = QFileDialog.getOpenFileName(self, "Choose your digital ID file",
                                                  str(default_id_folder()),
                                                  "Digital ID (*.pfx *.p12)")
            self._add_pfx(path)
        elif data == _CREATE:
            dialog = CreateIDDialog(self)
            if dialog.exec() == QDialog.Accepted and dialog.created is not None:
                self._add_pfx(str(dialog.created.path))
                self.password.setText(dialog.password.text())
            else:
                self.ids.setCurrentIndex(0)
        self._sync_password()

    def _sync_password(self) -> None:
        data = self._current()
        is_file = isinstance(data, DigitalID) and data.kind == PFX
        self.password.setEnabled(is_file)
        self.password.setPlaceholderText(
            "the digital ID file's password" if is_file
            else "not needed (Windows asks for a PIN if the certificate has one)")

    def _add_pfx(self, path: str) -> None:
        if not path:
            self.ids.setCurrentIndex(0)
            return
        for i in range(self.ids.count()):  # already listed: just pick it
            data = self.ids.itemData(i)
            if isinstance(data, DigitalID) and data.path and str(data.path).lower() == path.lower():
                self.ids.setCurrentIndex(i)
                return
        ident = DigitalID(PFX, Path(path).stem, path=Path(path))
        self.ids.insertItem(0, f"{Path(path).stem} — {Path(path).name}", ident)
        self.ids.setCurrentIndex(0)

    def _show_picture(self) -> None:
        if self.picture is None:
            self.preview.setText("(no picture chosen)")
            self.use_picture.setChecked(False)
            self.use_picture.setEnabled(False)
            return
        pix = QPixmap.fromImage(to_qimage(self.picture))
        self.preview.setPixmap(pix.scaledToHeight(44, Qt.SmoothTransformation))
        self.use_picture.setEnabled(True)
        self.use_picture.setChecked(True)

    def _choose_picture(self) -> None:
        chosen = choose_picture(self, self.settings.signature_picture())
        if chosen is not None:
            self.picture_path, self.picture = chosen
            self._show_picture()

    def _sign(self) -> None:
        ident = self._current()
        if not isinstance(ident, DigitalID):
            self.error.setText("Choose a digital ID.")
            return
        try:
            if ident.kind == PFX:
                self.signer = digitalid.pfx_signer(ident.path, self.password.text())
            else:
                from . import wincert
                self.signer = wincert.windows_signer(ident, self.hwnd)
        except DigitalIDError as exc:
            self.error.setText(str(exc))
            return
        if ident.kind == PFX:
            self.settings.remember_digital_id_file(str(ident.path))
        name = digitalid.common_name(self.signer.signing_cert)
        self.identity = ident
        self.settings.set_signing_reason(self.reason.currentText().strip())
        self.look = Look(name=name, when=dt.datetime.now().astimezone(),
                         reason=self.reason.currentText().strip(),
                         location=self.location.text().strip(),
                         image=self.picture if self.use_picture.isChecked() else None,
                         show_date=self.show_date.isChecked())
        self.accept()


def summary(infos: List[SigInfo]) -> tuple:
    """(worst level, rich text) for the bar above the pages."""
    order = [OK, UNTRUSTED, CHANGED, BROKEN]
    worst = max((i.level for i in infos), key=order.index)
    if len(infos) == 1:
        i = infos[0]
        when = f" on {i.when:%Y-%m-%d %H:%M}" if i.when else ""
        return worst, f"<b>Signed by {_esc(i.signer)}{when}.</b> {_esc(i.message)}"
    words = {OK: "valid", UNTRUSTED: "signer not verified", CHANGED: "changed after signing",
             BROKEN: "broken"}
    counts = [f"{sum(1 for i in infos if i.level == lv)} {words[lv]}" for lv in order
              if any(i.level == lv for i in infos)]
    return worst, f"<b>{len(infos)} signatures:</b> " + ", ".join(counts) + "."


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class SignaturesDialog(QDialog):
    def __init__(self, parent, infos: List[SigInfo]):
        super().__init__(parent)
        self.setWindowTitle("Signatures in This Document")
        self.setMinimumWidth(440)
        layout = QVBoxLayout(self)
        for i in infos:
            when = f"{i.when:%Y-%m-%d %H:%M:%S %z}" if i.when else "time not given"
            reason = f"<br>Reason: {_esc(i.reason)}" if i.reason else ""
            issuer = f"<br>Certificate issued by: {_esc(i.issuer)}" if i.issuer else ""
            label = QLabel(
                f"<b>{_esc(i.signer)}</b> &nbsp; <span style='color:{LEVEL_COLOURS[i.level]}'>"
                f"<b>{LEVEL_WORDS[i.level]}</b></span><br>{_esc(i.message)}<br>"
                f"Signed: {when}{reason}{issuer}<br><span style='color:#808088'>Field: "
                f"{_esc(i.field)}</span>", self)
            label.setWordWrap(True)
            label.setTextFormat(Qt.RichText)
            layout.addWidget(label)
        note = QLabel("Revocation isn't checked. A self-signed ID shows as verified once its "
                      "certificate is in Windows' Trusted People or Trusted Root store.", self)
        note.setWordWrap(True)
        note.setStyleSheet("color: #808088;")
        layout.addWidget(note)
        buttons = QDialogButtonBox(QDialogButtonBox.Close, self)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
