"""What a protected document allows, Unlock, and Password Protection."""

from __future__ import annotations

from dataclasses import replace
from typing import Optional

from PySide6.QtWidgets import QDialog, QInputDialog, QLineEdit, QMessageBox

from .document import DocumentError, PasswordRequired, PdfDocument, SourceInfo
from .protection import SET, UNLOCK, ProtectionDialog
from .security import FULL_ACCESS, KEEP, REMOVE, Access


class AccessMixin:
    """MainWindow: the author's restrictions (security.py) -- the gate on every
    edit -- the owner-password Unlock, and File > Password Protection."""

    # ---- restrictions set by a document's author (security.py) ---------

    @property
    def access(self) -> Access:
        doc = self.view.document
        return doc.access() if doc is not None else FULL_ACCESS

    def _allowed(self, allowed: bool, what: str) -> bool:
        """The gate on every edit entry point. Greyed-out buttons are the
        visible half; this holds for requests no button stops (a drag, a
        key, a right-click menu)."""
        if not allowed:
            self.statusBar().showMessage(
                f"This document's author doesn't allow {what}. "
                "Unlock it with the owner password to change that.", 10000)
        return allowed

    def _apply_access(self) -> None:
        """Turn off what the document's restrictions forbid, and say so in
        the bar above the pages. Only ever disables: what's on otherwise is
        decided by whoever called this (document open, editing ended)."""
        a = self.access
        self.view.access = a
        off = []
        if not a.annotate:
            off += [self.tool_actions[t] for t in self.annotate_tools | self.sign_tools] \
                + [self.act_delete_annot]
        if not a.modify:
            off += [self.tool_actions[t] for t in self.form_tools] + [self.act_ocr]
        if not a.assemble:
            off += [self.act_page_rot_ccw, self.act_page_rot_cw, self.act_page_up,
                    self.act_page_down, self.act_page_delete, self.act_page_insert]
        if not a.copy:
            off += [self.act_page_extract, self.act_copy]
        if not a.print:
            off.append(self.act_print)
        for act in off:
            act.setEnabled(False)
        # Every item in these two needs a document. (Sign keeps going without
        # one: choosing a picture or making a digital ID doesn't.)
        has_document = self.view.document is not None
        self.annotate_button.setEnabled(a.annotate and has_document)
        self.form_button.setEnabled(a.modify and has_document)
        for act in (self.act_signatures,):  # needs a document, not permission
            act.setEnabled(self.view.document is not None)
        self.current.show_restrictions(a.restricted() if self.view.document is not None else [])

    def unlock(self, password: Optional[str] = None) -> bool:
        """Reopen with the owner password (asked for if not given), which
        lifts the restrictions. The edits so far and their undo history
        carry over. True if unlocked."""
        doc = self.view.document
        if doc is None or self.history is None:
            return False
        if password is None:
            password, ok = QInputDialog.getText(
                self, "Unlock", f"Owner password for {doc.path.name}:", QLineEdit.Password)
            if not ok or not password:
                return False
        try:
            with PdfDocument(doc.path, password=password) as trial:
                full = trial.access().full
        except (PasswordRequired, DocumentError):
            full = False
        if not full:
            QMessageBox.warning(self, "Unlock", "That isn't the owner password for this document.")
            return False
        sources = doc.sources
        sources[0] = SourceInfo(sources[0].path, password)
        self._reopen(sources, keep_edits=self.history.current)
        self.statusBar().showMessage("Unlocked: the author's restrictions are lifted "
                                     "for this session. Saving keeps them in the file.", 10000)
        return True

    def password_protection(self) -> None:
        doc = self.view.document
        if doc is None or self.history is None:
            return
        needs_password = False
        if doc.is_encrypted:
            try:  # does it open with no password (restrictions only)?
                PdfDocument(doc.path).close()
            except PasswordRequired:
                needs_password = True
            except DocumentError:
                pass
        dialog = ProtectionDialog(self, doc.path.name, doc.is_encrypted, needs_password,
                                  self.history.current.protection, self.access.full)
        if dialog.exec() == QDialog.Accepted and dialog.choice is not None:
            self.set_protection(*dialog.choice)

    def set_protection(self, what: str, password: Optional[str] = None) -> None:
        """Apply a Password Protection choice as an undoable edit."""
        if what == UNLOCK:
            self.unlock()
            return
        if not self.access.full:  # the dialog doesn't offer it; this holds regardless
            self._allowed(False, "changing its password protection")
            return
        doc, current = self.view.document, self.history.current
        protected_now = (doc.is_encrypted and current.protection is KEEP) or \
            current.protection not in (KEEP, REMOVE)
        if what == SET:
            label = "Change Password" if protected_now else "Add Password"
            value = password
            note = "The password will be applied when you save."
        else:
            label = "Remove Password"
            # Removing a password that was only pending just drops it.
            value = REMOVE if doc.is_encrypted else KEEP
            note = "The password protection will be removed when you save."
        state = replace(current, protection=value)
        if state == current:
            return
        self.history.push(state, label)
        self._refresh_edit_state()
        self.statusBar().showMessage(f"{note} (Ctrl+Z undoes it.)", 10000)
