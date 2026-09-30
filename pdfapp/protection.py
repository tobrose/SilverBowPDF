"""File > Password Protection: set, change or remove a document's open
password (security.py). The choice is an edit like any other -- undoable,
applied when the file is saved.

Only someone with full access (the owner, or a file without restrictions)
can change protection; anyone else is offered Unlock instead, so no one can
strip an author's restrictions by re-protecting the file their own way.
"""

from __future__ import annotations

from typing import Optional, Tuple

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
)

from .security import KEEP, REMOVE

SET, REMOVE_CHOICE, UNLOCK = "set", "remove", "unlock"


class ProtectionDialog(QDialog):
    """choice is (SET, password), (REMOVE_CHOICE, None), (UNLOCK, None), or
    None if closed."""

    def __init__(self, parent, name: str, encrypted: bool, needs_password: bool,
                 pending: Optional[str], full_access: bool):
        super().__init__(parent)
        self.setWindowTitle("Password Protection")
        self.choice: Optional[Tuple[str, Optional[str]]] = None
        layout = QVBoxLayout(self)

        if pending is not KEEP and pending != REMOVE:
            state = "A new password to open it will be applied when you save."
        elif pending == REMOVE:
            state = "Its password protection will be removed when you save."
        elif encrypted and needs_password:
            state = "It needs a password to open."
        elif encrypted:
            state = "It opens without a password, but its author has set restrictions."
        else:
            state = "It isn't password protected: anyone can open it."
        status = QLabel(f"<b>{name}</b><br>{state}", self)
        status.setWordWrap(True)
        layout.addWidget(status)

        buttons = QDialogButtonBox(QDialogButtonBox.Close, self)
        buttons.rejected.connect(self.reject)

        if not full_access:
            note = QLabel("Only the document's owner can change its protection. "
                          "Unlock it with the owner password first.", self)
            note.setWordWrap(True)
            layout.addWidget(note)
            unlock = buttons.addButton("Unlock…", QDialogButtonBox.ActionRole)
            unlock.clicked.connect(lambda: self._done(UNLOCK, None))
            layout.addWidget(buttons)
            return

        form = QFormLayout()
        self.first = QLineEdit(self, echoMode=QLineEdit.Password)
        self.second = QLineEdit(self, echoMode=QLineEdit.Password)
        # Will it be protected as things stand (the file's own, or a pending one)?
        new = (encrypted and pending != REMOVE) or pending not in (KEEP, REMOVE)
        form.addRow("New password:" if new else "Password:", self.first)
        form.addRow("Type it again:", self.second)
        layout.addLayout(form)
        warn = QLabel("Anyone with this password can open the document. Keep it safe: "
                      "if it's lost, the document can't be opened, by anyone.", self)
        warn.setWordWrap(True)
        warn.setStyleSheet("color: #5a5a60;")
        layout.addWidget(warn)
        self.error = QLabel("", self)
        self.error.setStyleSheet("color: #b00020;")
        layout.addWidget(self.error)

        set_button = buttons.addButton("Change Password" if new else "Set Password",
                                       QDialogButtonBox.AcceptRole)
        set_button.clicked.connect(self._set)
        if new:
            remove = buttons.addButton("Remove Password", QDialogButtonBox.DestructiveRole)
            remove.clicked.connect(lambda: self._done(REMOVE_CHOICE, None))
        layout.addWidget(buttons)
        self.first.setFocus()

    def _set(self) -> None:
        first, second = self.first.text(), self.second.text()
        if not first:
            self.error.setText("Type a password.")
        elif first != second:
            self.error.setText("The two passwords are different.")
        else:
            self._done(SET, first)

    def _done(self, what: str, value: Optional[str]) -> None:
        self.choice = (what, value)
        self.accept()
