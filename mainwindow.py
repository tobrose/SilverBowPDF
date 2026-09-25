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
    QMessageBox,
    QSpinBox,
    QToolBar,
    QWidget,
)

from .document import DocumentError, PasswordRequired, PdfDocument
from .pageview import FitMode, PdfView

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

        self.view = PdfView(self)
        self.setCentralWidget(self.view)

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
        self.act_open = QAction("&Open\u2026", self, shortcut=QKeySequence.Open,
                                triggered=self.open_dialog)
        self.act_close = QAction("&Close", self, shortcut=QKeySequence.Close,
                                 triggered=self.close_document)
        self.act_quit = QAction("&Quit", self, shortcut=QKeySequence.Quit,
                                triggered=self.close)

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

        self.act_rotate_ccw = QAction("Rotate &Left", self, shortcut=QKeySequence("Ctrl+L"),
                                      triggered=lambda: self.view.rotate_view(-90))
        self.act_rotate_cw = QAction("Rotate &Right", self, shortcut=QKeySequence("Ctrl+R"),
                                     triggered=lambda: self.view.rotate_view(90))

        self.act_next = QAction("Next Page", self, shortcut=QKeySequence("PgDown"),
                                triggered=self.view.next_page)
        self.act_prev = QAction("Previous Page", self, shortcut=QKeySequence("PgUp"),
                                triggered=self.view.previous_page)
        self.act_first = QAction("First Page", self, shortcut=QKeySequence("Ctrl+Home"),
                                 triggered=lambda: self.view.scroll_to_page(0))
        self.act_last = QAction("Last Page", self, shortcut=QKeySequence("Ctrl+End"),
                                triggered=self._goto_last)
        self.act_goto = QAction("&Go to Page\u2026", self, shortcut=QKeySequence("Ctrl+G"),
                                triggered=self._goto_dialog)

    def _build_toolbar(self) -> None:
        bar = QToolBar("Main", self)
        bar.setMovable(False)
        bar.setIconSize(bar.iconSize())
        self.addToolBar(bar)

        bar.addAction(self.act_open)
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
        file_menu.addAction(self.act_close)
        file_menu.addSeparator()
        file_menu.addAction(self.act_quit)

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

        go_menu = self.menuBar().addMenu("&Go")
        for act in (self.act_prev, self.act_next, self.act_first, self.act_last, self.act_goto):
            go_menu.addAction(act)

    def _build_statusbar(self) -> None:
        self.status_left = QLabel("No document")
        self.statusBar().addWidget(self.status_left, 1)
        self.status_right = QLabel("")
        self.statusBar().addPermanentWidget(self.status_right)

    def _set_document_actions_enabled(self, enabled: bool) -> None:
        for act in (self.act_close, self.act_zoom_in, self.act_zoom_out, self.act_zoom_100,
                    self.act_fit_width, self.act_fit_page, self.act_rotate_ccw,
                    self.act_rotate_cw, self.act_next, self.act_prev, self.act_first,
                    self.act_last, self.act_goto):
            act.setEnabled(enabled)
        self.page_spin.setEnabled(enabled)
        self.zoom_box.setEnabled(enabled)

    # ---- opening -------------------------------------------------------

    def open_dialog(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Open PDF", "", "PDF documents (*.pdf);;All files (*)"
        )
        if path:
            self.open_path(path)

    def open_path(self, path: str | Path) -> None:
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
        self.view.set_document(None)

    # ---- signal handlers -----------------------------------------------

    def _on_document_opened(self, document: Optional[PdfDocument]) -> None:
        if document is None:
            self.setWindowTitle(APP_NAME)
            self.page_spin.setRange(1, 1)
            self.page_total.setText(" of 0 ")
            self.status_left.setText("No document")
            self.status_right.setText("")
            self._set_document_actions_enabled(False)
            return

        self.setWindowTitle(f"{document.path.name} \u2014 {APP_NAME}")
        self.page_spin.blockSignals(True)
        self.page_spin.setRange(1, document.page_count)
        self.page_spin.setValue(1)
        self.page_spin.blockSignals(False)
        self.page_total.setText(f" of {document.page_count} ")
        forms = "  \u00b7  has form fields" if document.has_forms else ""
        self.status_left.setText(f"{document.path}{forms}")
        self._set_document_actions_enabled(True)

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
            f"{w / 72:.2f} \u00d7 {h / 72:.2f} in   \u00b7   {self.view.scale * 100:.0f}%"
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
            self, "Go to page", f"Page (1\u2013{doc.page_count}):",
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
        self.view.set_document(None)
        super().closeEvent(event)
