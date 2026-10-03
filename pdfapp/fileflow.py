"""Opening, inserting from, extracting to and saving files."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Optional

from PySide6.QtWidgets import QFileDialog, QInputDialog, QLineEdit, QMessageBox

from .annots import DocState
from .document import DocumentError, PasswordRequired, PdfDocument, SourceInfo
from .pagelist import PageList
from .security import KEEP, REMOVE
from .writer import EncryptionWouldBeLost, SaveError, commit, write_pdf


class FilesMixin:
    """MainWindow: opening documents (with their passwords), inserting pages
    from other files, extracting pages, and saving -- write to a temp file,
    swap it in, reopen."""

    # ---- opening -------------------------------------------------------

    def open_dialog(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Open PDF", "", "PDF documents (*.pdf);;All files (*)"
        )
        if path:
            self.open_path(path)

    def open_path(self, path: str | Path) -> None:
        if not self._confirm_discard():
            return
        document = self._with_password(Path(path), lambda pw: PdfDocument(path, password=pw))
        if document is not None:
            self.view.set_document(document)

    def _with_password(self, path: Path, opener):
        """Call opener(password), prompting until it works or the user gives up.

        Returns opener's result, or None if cancelled or unreadable (already
        reported to the user).
        """
        password: Optional[str] = None
        while True:
            try:
                return opener(password)
            except PasswordRequired:
                prompt = (f"{path.name} is encrypted.\nEnter the password:" if password is None
                          else f"That password didn't work for {path.name}.\nTry again:")
                password, ok = QInputDialog.getText(
                    self, "Password required", prompt, QLineEdit.Password)
                if not ok:
                    return None
            except DocumentError as exc:
                QMessageBox.critical(self, "Could not open file", str(exc))
                return None

    def close_document(self) -> None:
        if self._confirm_discard():
            self.view.set_document(None)

    def _confirm_discard(self) -> bool:
        """True if it's OK to drop the current document's unsaved edits."""
        self.view.commit_text_edit()  # typed text counts as an edit
        if self.history is None or not self.history.dirty:
            return True
        answer = QMessageBox.warning(
            self, "Unsaved changes",
            f"Save changes to {self.view.document.path.name} before closing it?",
            QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel, QMessageBox.Save,
        )
        if answer == QMessageBox.Save:
            return self.save()
        return answer == QMessageBox.Discard

    # ---- insert --------------------------------------------------------

    def _insert_position(self) -> int:
        """After the last selected page, else after the current page."""
        doc = self.view.document
        if doc is None:
            return 0
        return max(self._targets()) + 1

    def insert_dialog(self, at: Optional[int] = None) -> None:
        if self.view.document is None:
            return
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Insert Pages from PDF", str(self.view.document.path.parent),
            "PDF documents (*.pdf)")
        if paths:
            self.insert_files([Path(p) for p in paths],
                              self._insert_position() if at is None else at)

    def insert_files(self, paths: list[Path], at: int) -> bool:
        """Insert every page of each file, in order, starting at position `at`."""
        doc = self.view.document
        if doc is None or not paths:
            return False
        pages = doc.pages
        inserted: list[str] = []
        start = at = max(0, min(doc.page_count, at))
        for path in paths:
            file = self._with_password(path, lambda pw, p=path: doc.add_source(p, pw))
            if file is None:
                continue  # cancelled or unreadable; already reported
            if not doc.access(file).copy:
                # Taking a restricted file's pages is copying them out of it.
                QMessageBox.warning(
                    self, "Can't insert pages",
                    f"The author of {path.name} doesn't allow its pages to be copied, "
                    f"so they can't be inserted into another document.")
                continue
            if doc.form_error(file):
                QMessageBox.warning(
                    self, "Form fields not loaded",
                    f"The form fields in {path.name} couldn't be loaded, so its pages "
                    f"are inserted without them showing.\n\n({doc.form_error(file)})")
            refs = doc.source_refs(file)
            pages = pages.inserted(refs, at)
            at += len(refs)
            inserted.append(path.name)
        if not inserted:
            return False
        count = at - start
        noun = "Page" if count == 1 else f"{count} Pages"
        label = (f"Insert {noun} from {inserted[0]}" if len(inserted) == 1
                 else f"Insert {noun} from {len(inserted)} Files")
        self._edit(pages, label, start, list(range(start, at)))
        return True

    # ---- extract -------------------------------------------------------

    @staticmethod
    def _extract_name(stem: str, indices: list[int]) -> str:
        nums = [i + 1 for i in indices]
        if len(nums) == 1:
            return f"{stem} (page {nums[0]}).pdf"
        if nums == list(range(nums[0], nums[-1] + 1)):
            return f"{stem} (pages {nums[0]}-{nums[-1]}).pdf"
        return f"{stem} ({len(nums)} pages).pdf"

    def extract_pages(self, indices: list[int]) -> bool:
        """Write the given pages, as currently shown, to a new file.

        The open document is not changed. Unsaved rotations and reordering
        are included -- extract what you see.
        """
        doc = self.view.document
        if doc is None or not indices or not self._allowed(self.access.copy,
                                                           "copying pages out of it"):
            return False
        indices = sorted(indices)
        suggested = doc.path.with_name(self._extract_name(doc.path.stem, indices))
        dest = self._ask_save_path(f"Extract {self._describe(indices)}", suggested)
        if dest is None:
            return False
        if self._same_file(dest, doc.path):
            QMessageBox.warning(
                self, "Choose a different file",
                f"{dest.name} is the document you have open. Extracting over it "
                f"would replace the whole document with just these pages.\n\n"
                f"To keep only these pages, delete the others and save instead.",
            )
            return False
        if any(self._same_file(dest, s.path) for s in doc.sources):
            QMessageBox.warning(
                self, "Choose a different file",
                f"{dest.name} is a file you inserted pages from, and it's still in "
                f"use by this document. Choose a different name.",
            )
            return False

        written = self._write(PageList(doc.pages[i] for i in indices), dest, "Extract failed")
        if written is None:
            return False
        try:
            commit(written[0], dest)
        except SaveError as exc:
            QMessageBox.critical(self, "Extract failed", str(exc))
            return False
        self.statusBar().showMessage(
            f"Extracted {self._describe(indices).lower()} to {dest}", 8000)
        return True

    # ---- saving --------------------------------------------------------

    def save(self) -> bool:
        self.view.commit_text_edit()
        doc = self.view.document
        return doc is not None and self._save_to(doc.path)

    def save_as(self) -> bool:
        self.view.commit_text_edit()
        doc = self.view.document
        if doc is None:
            return False
        dest = self._ask_save_path("Save PDF As", doc.path)
        return dest is not None and self._save_to(dest)

    def _ask_save_path(self, title: str, suggested: Path) -> Optional[Path]:
        dest, _ = QFileDialog.getSaveFileName(self, title, str(suggested), "PDF documents (*.pdf)")
        if not dest:
            return None
        dest = Path(dest)
        if dest.suffix.lower() != ".pdf":
            dest = dest.with_name(dest.name + ".pdf")
        return dest

    @staticmethod
    def _same_file(a: Path, b: Path) -> bool:
        try:
            return a.resolve() == b.resolve() or (a.exists() and a.samefile(b))
        except OSError:
            return False

    def _write(self, pages: PageList, dest: Path,
               failure_title: str) -> Optional[tuple[Path, set[int]]]:
        """write_pdf, asking once per password-protected source whether it's OK
        for its pages to come out unprotected. Returns the temp file and the
        sources the user let lose protection, or None if the user declined or
        it failed (already reported)."""
        doc = self.view.document
        # The document's own protection is kept unless File > Password
        # Protection changed it (security.py) -- Save and Save As alike.
        protection = self.history.current.protection if self.history else KEEP
        allowed: set[int] = set()
        while True:
            try:
                return write_pdf(doc.sources, pages, dest, allow_decrypt=allowed,
                                 annots=doc.annots, fields=doc.fields, forms=doc.forms,
                                 ocr=doc.ocr, protection=protection), allowed
            except EncryptionWouldBeLost as exc:
                # Only pages inserted from another protected file, going into
                # an unprotected result, get here.
                if not self._confirm_unprotected_copy(exc.path, dest):
                    return None
                allowed.add(exc.file)
            except Exception as exc:  # nothing on disk has changed
                QMessageBox.critical(self, failure_title,
                                     f"Could not write {dest.name}:\n\n{exc}")
                return None

    def _confirm_unprotected_copy(self, source: Path, dest: Path) -> bool:
        answer = QMessageBox.warning(
            self, "Save pages unprotected?",
            f"Pages you inserted from {source.name} are password-protected in that "
            f"file. In {dest.name} they will NOT be — anyone who gets the file can "
            f"open it.\n\n{source.name} itself stays protected. To protect "
            f"{dest.name} too, use File > Password Protection before saving.",
            QMessageBox.Save | QMessageBox.Cancel, QMessageBox.Cancel,
        )
        return answer == QMessageBox.Save

    def _saved_password(self) -> Optional[str]:
        """The password the file being saved will open with."""
        protection = self.history.current.protection
        return (self.view.document.password if protection is KEEP
                else None if protection == REMOVE else protection)

    def _unchanged(self) -> bool:
        """Nothing to write: the document is exactly the file on disk."""
        return self.history is not None and not self.history.dirty

    def _confirm_breaking_signatures(self, dest: Path) -> bool:
        """Saving rewrites the whole file (qpdf), and a digital signature
        covers exact bytes: every one in the file breaks. Say so first."""
        count = self.view.document.signature_count()
        if not count:
            return True
        what = "a digital signature" if count == 1 else f"{count} digital signatures"
        answer = QMessageBox.warning(
            self, "This document is signed",
            f"This document carries {what}. Saving your changes rewrites the file, so "
            f"in {dest.name} the signature{'s' if count > 1 else ''} will show as broken "
            f"in every PDF reader.\n\nTo keep a valid signed copy, Cancel and use Save As "
            f"under a new name for your changes, leaving the signed file as it is.",
            QMessageBox.Save | QMessageBox.Cancel, QMessageBox.Cancel)
        return answer == QMessageBox.Save

    def _save_to(self, dest: Path) -> bool:
        """Write, swap in, reopen."""
        doc = self.view.document
        if self._unchanged() and doc.signature_count() and not self._same_file(dest, doc.path):
            # Save As of an untouched signed file: copy its bytes, so the copy's
            # signatures stay valid (rewriting would break them for nothing).
            tmp = dest.with_name(f".{dest.stem}.copy.tmp")
            try:
                shutil.copyfile(doc.path, tmp)
            except OSError as exc:
                QMessageBox.critical(self, "Save failed", f"Could not write {dest.name}:\n\n{exc}")
                return False
            return self._install(tmp, dest, doc.password, "Saved")
        if not self._confirm_breaking_signatures(dest):
            return False
        written = self._write(doc.pages, dest, "Save failed")
        if written is None:
            return False
        tmp, _ = written
        return self._install(tmp, dest, self._saved_password(), "Saved")

    def _install(self, tmp: Path, dest: Path, password: Optional[str], verb: str) -> bool:
        """Swap a finished temp file into place and reopen it as the document.
        The saved file is the new baseline, edits baked in; the undo history
        refers to the old files' page numbers, so it starts over."""
        doc = self.view.document
        edits = DocState(doc.pages, doc.annots, doc.fields, doc.forms, doc.ocr)
        # pdfium holds every source open and Windows won't replace an open
        # file, so release them -- but only when one of them is the target.
        sources = doc.sources
        overwriting = any(self._same_file(dest, s.path) for s in sources)
        if overwriting:
            doc.close()
        try:
            commit(tmp, dest)
        except SaveError as exc:
            if overwriting:
                self._reopen(sources, keep_edits=edits)
            QMessageBox.critical(self, f"{verb} failed",
                                 f"{exc}\n\nThe original file is unchanged.")
            return False
        try:
            self._reopen([SourceInfo(dest, password)])
        except Exception as exc:
            self.view.set_document(None)
            QMessageBox.critical(
                self, f"{verb}, but could not reopen",
                f"{dest.name} was written, but reopening it failed:\n\n{exc}",
            )
            return True
        self.statusBar().showMessage(f"{verb} {dest}", 5000)
        return True

    def _reopen(self, sources: list[SourceInfo],
                keep_edits: Optional[DocState] = None) -> None:
        document = PdfDocument(sources[0].path, password=sources[0].password)
        try:
            for source in sources[1:]:
                document.add_source(source.path, source.password)
            if keep_edits is not None:
                document.pages = keep_edits.pages
                document.annots = keep_edits.annots
                document.fields = keep_edits.fields
                document.forms = keep_edits.forms
                document.ocr = keep_edits.ocr
        except Exception:
            document.close()
            raise
        self._keep_history = keep_edits is not None
        try:
            self.view.set_document(document, keep_view=True)
        finally:
            self._keep_history = False
