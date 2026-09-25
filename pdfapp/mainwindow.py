"""Main window: chrome around the view."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QSpinBox,
    QToolBar,
    QWidget,
)

from .document import DocumentError, PasswordRequired, PdfDocument
from .pagelist import History, PageList
from .pageview import FitMode, PdfView
from .writer import EncryptionWouldBeLost, SaveError, commit, write_pdf

APP_NAME = "PDF Workbench"

ZOOM_PRESETS = [
    ("Fit width", "fit-width"),
    ("Fit page", "fit-page"),
    ("50%", 0.50),
    ("75%", 0.75),
    ("100%", 1.00),
    ("125%", 1.25),
    ("150%", 1.50),
    ("200%", 2.00),
    ("400%", 4.00),
]


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1100, 860)
        self.setAcceptDrops(True)

        self.history: Optional[History[PageList]] = None
        # Set while reopening the same file with edits intact (a failed save),
        # so document_opened doesn't wipe the undo history.
        self._keep_history = False

        self.view = PdfView(self)
        self.setCentralWidget(self.view)
        self.view.setContextMenuPolicy(Qt.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self._page_context_menu)

        self._building_ui = True
        self._build_actions()
        self._build_toolbar()
        self._build_menus()
        self._build_statusbar()
        self._building_ui = False

        self.view.page_changed.connect(self._on_page_changed)
        self.view.zoom_changed.connect(self._on_zoom_changed)
        self.view.document_opened.connect(self._on_document_opened)

        self._set_document_actions_enabled(False)

    # ---- construction --------------------------------------------------

    def _build_actions(self) -> None:
        self.act_open = QAction("&Open…", self, shortcut=QKeySequence.Open,
                                triggered=self.open_dialog)
        self.act_save = QAction("&Save", self, shortcut=QKeySequence.Save,
                                triggered=self.save)
        self.act_save_as = QAction("Save &As…", self, shortcut=QKeySequence("Ctrl+Shift+S"),
                                   triggered=self.save_as)
        self.act_close = QAction("&Close", self, shortcut=QKeySequence.Close,
                                 triggered=self.close_document)
        self.act_quit = QAction("&Quit", self, shortcut=QKeySequence.Quit,
                                triggered=self.close)

        self.act_undo = QAction("&Undo", self, shortcut=QKeySequence.Undo, triggered=self.undo)
        self.act_redo = QAction("&Redo", self, triggered=self.redo)
        self.act_redo.setShortcuts([QKeySequence.Redo, QKeySequence("Ctrl+Shift+Z")])

        self.act_zoom_in = QAction("Zoom &In", self, shortcut=QKeySequence.ZoomIn,
                                   triggered=self.view.zoom_in)
        # Ctrl+= as well, because half the world's keyboards make Ctrl++ awkward.
        self.act_zoom_in.setShortcuts([QKeySequence.ZoomIn, QKeySequence("Ctrl+=")])
        self.act_zoom_out = QAction("Zoom &Out", self, shortcut=QKeySequence.ZoomOut,
                                    triggered=self.view.zoom_out)
        self.act_zoom_100 = QAction("Actual Size", self, shortcut=QKeySequence("Ctrl+0"),
                                    triggered=lambda: self.view.set_scale(1.0, FitMode.FIXED))
        self.act_fit_width = QAction("Fit &Width", self, shortcut=QKeySequence("Ctrl+1"),
                                     triggered=self.view.fit_width)
        self.act_fit_page = QAction("Fit &Page", self, shortcut=QKeySequence("Ctrl+2"),
                                    triggered=self.view.fit_page)

        # View rotation is display-only and never saved; page rotation below is
        # an edit. Both exist, so the labels have to say which is which.
        self.act_rotate_ccw = QAction("Rotate View &Left", self, shortcut=QKeySequence("Ctrl+L"),
                                      triggered=lambda: self.view.rotate_view(-90))
        self.act_rotate_cw = QAction("Rotate View &Right", self, shortcut=QKeySequence("Ctrl+R"),
                                     triggered=lambda: self.view.rotate_view(90))

        cur = lambda: self.view.current_page  # noqa: E731
        self.act_page_rot_ccw = QAction("Rotate Page Left", self,
                                        shortcut=QKeySequence("Ctrl+Shift+L"),
                                        triggered=lambda: self.rotate_page(cur(), -90))
        self.act_page_rot_cw = QAction("Rotate Page Right", self,
                                       shortcut=QKeySequence("Ctrl+Shift+R"),
                                       triggered=lambda: self.rotate_page(cur(), 90))
        self.act_page_up = QAction("Move Page &Earlier", self, shortcut=QKeySequence("Alt+Up"),
                                   triggered=lambda: self.move_page(cur(), -1))
        self.act_page_down = QAction("Move Page &Later", self, shortcut=QKeySequence("Alt+Down"),
                                     triggered=lambda: self.move_page(cur(), +1))
        self.act_page_delete = QAction("&Delete Page", self, shortcut=QKeySequence("Ctrl+Shift+D"),
                                       triggered=lambda: self.delete_page(cur()))

        self.act_next = QAction("Next Page", self, shortcut=QKeySequence("PgDown"),
                                triggered=self.view.next_page)
        self.act_prev = QAction("Previous Page", self, shortcut=QKeySequence("PgUp"),
                                triggered=self.view.previous_page)
        self.act_first = QAction("First Page", self, shortcut=QKeySequence("Ctrl+Home"),
                                 triggered=lambda: self.view.scroll_to_page(0))
        self.act_last = QAction("Last Page", self, shortcut=QKeySequence("Ctrl+End"),
                                triggered=self._goto_last)
        self.act_goto = QAction("&Go to Page…", self, shortcut=QKeySequence("Ctrl+G"),
                                triggered=self._goto_dialog)

    def _build_toolbar(self) -> None:
        bar = QToolBar("Main", self)
        bar.setMovable(False)
        bar.setIconSize(bar.iconSize())
        self.addToolBar(bar)

        bar.addAction(self.act_open)
        bar.addAction(self.act_save)
        bar.addSeparator()
        bar.addAction(self.act_undo)
        bar.addAction(self.act_redo)
        bar.addSeparator()

        bar.addWidget(QLabel(" Page "))
        self.page_spin = QSpinBox(self)
        self.page_spin.setRange(1, 1)
        self.page_spin.setFixedWidth(70)
        self.page_spin.valueChanged.connect(self._on_page_spin)
        bar.addWidget(self.page_spin)
        self.page_total = QLabel(" of 0 ")
        bar.addWidget(self.page_total)

        bar.addSeparator()
        bar.addAction(self.act_zoom_out)
        self.zoom_box = QComboBox(self)
        self.zoom_box.setEditable(True)
        self.zoom_box.setFixedWidth(110)
        self.zoom_box.lineEdit().setAlignment(Qt.AlignCenter)
        for label, _ in ZOOM_PRESETS:
            self.zoom_box.addItem(label)
        self.zoom_box.activated.connect(self._on_zoom_preset)
        self.zoom_box.lineEdit().returnPressed.connect(self._on_zoom_typed)
        bar.addWidget(self.zoom_box)
        bar.addAction(self.act_zoom_in)

        bar.addSeparator()
        bar.addAction(self.act_rotate_ccw)
        bar.addAction(self.act_rotate_cw)

    def _build_menus(self) -> None:
        file_menu = self.menuBar().addMenu("&File")
        file_menu.addAction(self.act_open)
        file_menu.addAction(self.act_save)
        file_menu.addAction(self.act_save_as)
        file_menu.addAction(self.act_close)
        file_menu.addSeparator()
        file_menu.addAction(self.act_quit)

        edit_menu = self.menuBar().addMenu("&Edit")
        edit_menu.addAction(self.act_undo)
        edit_menu.addAction(self.act_redo)

        view_menu = self.menuBar().addMenu("&View")
        view_menu.addAction(self.act_zoom_in)
        view_menu.addAction(self.act_zoom_out)
        view_menu.addAction(self.act_zoom_100)
        view_menu.addSeparator()
        view_menu.addAction(self.act_fit_width)
        view_menu.addAction(self.act_fit_page)
        view_menu.addSeparator()
        view_menu.addAction(self.act_rotate_ccw)
        view_menu.addAction(self.act_rotate_cw)

        pages_menu = self.menuBar().addMenu("&Pages")
        pages_menu.addAction(self.act_page_rot_ccw)
        pages_menu.addAction(self.act_page_rot_cw)
        pages_menu.addSeparator()
        pages_menu.addAction(self.act_page_up)
        pages_menu.addAction(self.act_page_down)
        pages_menu.addSeparator()
        pages_menu.addAction(self.act_page_delete)

        go_menu = self.menuBar().addMenu("&Go")
        for act in (self.act_prev, self.act_next, self.act_first, self.act_last, self.act_goto):
            go_menu.addAction(act)

    def _build_statusbar(self) -> None:
        self.status_left = QLabel("No document")
        self.statusBar().addWidget(self.status_left, 1)
        self.status_right = QLabel("")
        self.statusBar().addPermanentWidget(self.status_right)

    def _set_document_actions_enabled(self, enabled: bool) -> None:
        for act in (self.act_close, self.act_save_as, self.act_zoom_in, self.act_zoom_out,
                    self.act_zoom_100, self.act_fit_width, self.act_fit_page,
                    self.act_rotate_ccw, self.act_rotate_cw, self.act_page_rot_ccw,
                    self.act_page_rot_cw, self.act_page_up, self.act_page_down,
                    self.act_page_delete, self.act_next, self.act_prev, self.act_first,
                    self.act_last, self.act_goto):
            act.setEnabled(enabled)
        self.page_spin.setEnabled(enabled)
        self.zoom_box.setEnabled(enabled)
        self._refresh_edit_state()

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
        path = Path(path)
        password: Optional[str] = None
        while True:
            try:
                document = PdfDocument(path, password=password)
                break
            except PasswordRequired:
                password, ok = QInputDialog.getText(
                    self,
                    "Password required",
                    f"{path.name} is encrypted.\nEnter the password:",
                    QLineEdit.Password,
                )
                if not ok:
                    return
            except DocumentError as exc:
                QMessageBox.critical(self, "Could not open file", str(exc))
                return

        self.view.set_document(document)

    def close_document(self) -> None:
        if self._confirm_discard():
            self.view.set_document(None)

    def _confirm_discard(self) -> bool:
        """True if it's OK to drop the current document's unsaved edits."""
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

    # ---- page edits ----------------------------------------------------

    def _edit(self, pages: PageList, label: str, current: int) -> None:
        if self.history is None or pages == self.history.current:
            return
        self.history.push(pages, label)
        self._show_pages(pages, current)

    def _show_pages(self, pages: PageList, current: int) -> None:
        self.view.document.pages = pages
        self.view.pages_changed(max(0, min(len(pages) - 1, current)))
        self._refresh_page_count()
        self._refresh_edit_state()

    def rotate_page(self, index: int, delta: int) -> None:
        side = "Right" if delta > 0 else "Left"
        self._edit(self.view.document.pages.rotated([index], delta),
                   f"Rotate Page {index + 1} {side}", index)

    def delete_page(self, index: int) -> None:
        try:
            pages = self.view.document.pages.deleted([index])
        except ValueError as exc:
            QMessageBox.information(self, "Can't delete page", f"{str(exc).capitalize()}.")
            return
        self._edit(pages, f"Delete Page {index + 1}", index)

    def move_page(self, index: int, step: int) -> None:
        to = index + step
        if not 0 <= to < self.view.document.page_count:
            return
        self._edit(self.view.document.pages.moved([index], to), f"Move Page {index + 1}", to)

    def undo(self) -> None:
        if self.history is not None and self.history.can_undo():
            self._show_pages(self.history.undo(), self.view.current_page)

    def redo(self) -> None:
        if self.history is not None and self.history.can_redo():
            self._show_pages(self.history.redo(), self.view.current_page)

    def _refresh_edit_state(self) -> None:
        h = self.history
        self.setWindowModified(h is not None and h.dirty)
        self.act_save.setEnabled(h is not None and h.dirty)
        self.act_undo.setEnabled(h is not None and h.can_undo())
        self.act_redo.setEnabled(h is not None and h.can_redo())
        self.act_undo.setText(f"&Undo {h.undo_label()}" if h and h.can_undo() else "&Undo")
        self.act_redo.setText(f"&Redo {h.redo_label()}" if h and h.can_redo() else "&Redo")

    def _page_context_menu(self, pos) -> None:
        if self.view.document is None:
            return
        index = self.view.page_at(pos)
        if index is None:
            return
        n = self.view.document.page_count
        menu = QMenu(self)
        menu.addAction(f"Rotate Page {index + 1} Left", lambda: self.rotate_page(index, -90))
        menu.addAction(f"Rotate Page {index + 1} Right", lambda: self.rotate_page(index, 90))
        menu.addSeparator()
        menu.addAction("Move Earlier", lambda: self.move_page(index, -1)).setEnabled(index > 0)
        menu.addAction("Move Later", lambda: self.move_page(index, +1)).setEnabled(index < n - 1)
        menu.addSeparator()
        menu.addAction(f"Delete Page {index + 1}",
                       lambda: self.delete_page(index)).setEnabled(n > 1)
        menu.exec(self.view.viewport().mapToGlobal(pos))

    # ---- saving --------------------------------------------------------

    def save(self) -> bool:
        doc = self.view.document
        if doc is None:
            return False
        try:
            return self._save_to(doc.path, allow_decrypt=False)
        except EncryptionWouldBeLost:
            QMessageBox.warning(
                self, "Can't save over a protected file",
                f"{doc.path.name} is password-protected, and saving changes would "
                f"remove the password. {APP_NAME} can't re-apply encryption yet.\n\n"
                f"Use Save As to write an unprotected copy under a different name.",
            )
            return False

    def save_as(self) -> bool:
        doc = self.view.document
        if doc is None:
            return False
        dest, _ = QFileDialog.getSaveFileName(
            self, "Save PDF As", str(doc.path), "PDF documents (*.pdf)"
        )
        if not dest:
            return False
        dest = Path(dest)
        if dest.suffix.lower() != ".pdf":
            dest = dest.with_name(dest.name + ".pdf")
        try:
            return self._save_to(dest, allow_decrypt=False)
        except EncryptionWouldBeLost:
            if self._same_file(dest, doc.path):
                return self.save()  # explains; never overwrite the protected original
            answer = QMessageBox.warning(
                self, "Save unprotected copy?",
                f"{doc.path.name} is password-protected. The copy saved as "
                f"{dest.name} will NOT have a password — anyone who gets the "
                f"file can open it.\n\nThe original stays protected.",
                QMessageBox.Save | QMessageBox.Cancel, QMessageBox.Cancel,
            )
            if answer != QMessageBox.Save:
                return False
            return self._save_to(dest, allow_decrypt=True)

    @staticmethod
    def _same_file(a: Path, b: Path) -> bool:
        try:
            return a.resolve() == b.resolve() or (a.exists() and a.samefile(b))
        except OSError:
            return False

    def _save_to(self, dest: Path, allow_decrypt: bool) -> bool:
        """Write, swap in, reopen. Lets EncryptionWouldBeLost through to the caller."""
        doc = self.view.document
        pages = doc.pages
        try:
            tmp = write_pdf(doc.path, doc.password, pages, dest, allow_decrypt=allow_decrypt)
        except EncryptionWouldBeLost:
            raise
        except Exception as exc:  # nothing on disk has changed
            QMessageBox.critical(self, "Save failed", f"Could not save {dest.name}:\n\n{exc}")
            return False

        # pdfium holds its file open and Windows won't replace an open file,
        # so release it -- but only when it's the file being overwritten.
        overwriting = self._same_file(dest, doc.path)
        if overwriting:
            doc.close()
        try:
            commit(tmp, dest)
        except SaveError as exc:
            if overwriting:
                self._reopen(doc.path, doc.password, keep_edits=pages)
            QMessageBox.critical(self, "Save failed", f"{exc}\n\nThe original file is unchanged.")
            return False

        # The saved file is the new baseline, with the edits baked in. The undo
        # history refers to the old file's page numbers, so it starts over.
        try:
            self._reopen(dest, None if allow_decrypt else doc.password)
        except Exception as exc:
            self.view.set_document(None)
            QMessageBox.critical(
                self, "Saved, but could not reopen",
                f"{dest.name} was saved, but reopening it failed:\n\n{exc}",
            )
            return True
        self.statusBar().showMessage(f"Saved {dest}", 5000)
        return True

    def _reopen(self, path: Path, password: Optional[str],
                keep_edits: Optional[PageList] = None) -> None:
        document = PdfDocument(path, password=password)
        if keep_edits is not None:
            document.pages = keep_edits
        self._keep_history = keep_edits is not None
        try:
            self.view.set_document(document, keep_view=True)
        finally:
            self._keep_history = False

    # ---- signal handlers -----------------------------------------------

    def _on_document_opened(self, document: Optional[PdfDocument]) -> None:
        if document is None:
            self.history = None
            self.setWindowTitle(APP_NAME)
            self.page_spin.setRange(1, 1)
            self.page_total.setText(" of 0 ")
            self.status_left.setText("No document")
            self.status_right.setText("")
            self._set_document_actions_enabled(False)
            return

        if not self._keep_history:
            self.history = History(document.pages)
        # [*] is where Qt draws the unsaved-changes marker (setWindowModified).
        self.setWindowTitle(f"{document.path.name}[*] — {APP_NAME}")
        self._refresh_page_count()
        forms = "  ·  has form fields" if document.has_forms else ""
        self.status_left.setText(f"{document.path}{forms}")
        self._set_document_actions_enabled(True)

    def _refresh_page_count(self) -> None:
        doc = self.view.document
        if doc is None:
            return
        self.page_spin.blockSignals(True)
        self.page_spin.setRange(1, doc.page_count)
        self.page_spin.setValue(self.view.current_page + 1)
        self.page_spin.blockSignals(False)
        self.page_total.setText(f" of {doc.page_count} ")

    def _on_page_changed(self, index: int) -> None:
        self.page_spin.blockSignals(True)
        self.page_spin.setValue(index + 1)
        self.page_spin.blockSignals(False)
        self._refresh_status_right()

    def _on_zoom_changed(self, scale: float) -> None:
        mode = self.view.fit_mode
        if mode is FitMode.WIDTH:
            text = "Fit width"
        elif mode is FitMode.PAGE:
            text = "Fit page"
        else:
            text = f"{scale * 100:.0f}%"
        self.zoom_box.blockSignals(True)
        self.zoom_box.setEditText(text)
        self.zoom_box.blockSignals(False)
        self._refresh_status_right()

    def _refresh_status_right(self) -> None:
        doc = self.view.document
        if doc is None:
            self.status_right.setText("")
            return
        t = doc.transform(self.view.current_page, view_rotation=self.view.rotation)
        w, h = t.display_size_pt
        self.status_right.setText(
            f"{w / 72:.2f} × {h / 72:.2f} in   ·   {self.view.scale * 100:.0f}%"
        )

    def _on_page_spin(self, value: int) -> None:
        if not self._building_ui:
            self.view.scroll_to_page(value - 1)

    def _on_zoom_preset(self, index: int) -> None:
        _, value = ZOOM_PRESETS[index]
        if value == "fit-width":
            self.view.fit_width()
        elif value == "fit-page":
            self.view.fit_page()
        else:
            self.view.set_scale(float(value), FitMode.FIXED)

    def _on_zoom_typed(self) -> None:
        text = self.zoom_box.currentText().strip().rstrip("%").strip()
        try:
            self.view.set_scale(float(text) / 100.0, FitMode.FIXED)
        except ValueError:
            self._on_zoom_changed(self.view.scale)  # snap back

    def _goto_last(self) -> None:
        if self.view.document is not None:
            self.view.scroll_to_page(self.view.document.page_count - 1)

    def _goto_dialog(self) -> None:
        doc = self.view.document
        if doc is None:
            return
        page, ok = QInputDialog.getInt(
            self, "Go to page", f"Page (1–{doc.page_count}):",
            self.view.current_page + 1, 1, doc.page_count,
        )
        if ok:
            self.view.scroll_to_page(page - 1)

    # ---- drag and drop --------------------------------------------------

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                if url.isLocalFile() and url.toLocalFile().lower().endswith(".pdf"):
                    event.acceptProposedAction()
                    return
        event.ignore()

    def dropEvent(self, event) -> None:
        for url in event.mimeData().urls():
            if url.isLocalFile() and url.toLocalFile().lower().endswith(".pdf"):
                self.open_path(url.toLocalFile())
                event.acceptProposedAction()
                return

    def closeEvent(self, event) -> None:
        if not self._confirm_discard():
            event.ignore()
            return
        self.view.set_document(None)
        super().closeEvent(event)
