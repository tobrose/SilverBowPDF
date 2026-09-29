"""Main window: chrome around the view."""

from __future__ import annotations

import getpass
from dataclasses import replace
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QActionGroup, QColor, QIcon, QKeySequence, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QPlainTextEdit,
    QDockWidget,
    QFileDialog,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QSpinBox,
    QToolBar,
    QToolButton,
    QWidget,
)

from .annots import (
    HIGHLIGHT_COLORS,
    SHAPE_COLORS,
    SHAPE_WIDTHS,
    STAMPS,
    TEXT_COLORS,
    TEXT_SIZES,
    Annot,
    DocState,
    describe,
    new_note,
)
from .document import DocumentError, PasswordRequired, PdfDocument, SourceInfo
from .pagelist import History, PageList
from .fields import (
    COMBO,
    FLAG_MULTILINE,
    FLAG_READONLY,
    FLAG_REQUIRED,
    TEXT,
)
from .pageview import FitMode, PdfView, Tool
from .thumbnails import ThumbnailSidebar
from .writer import EncryptionWouldBeLost, SaveError, commit, write_pdf

APP_NAME = "ButterPDF"

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

        self.history: Optional[History[DocState]] = None
        # Set while reopening the same file with edits intact (a failed save),
        # so document_opened doesn't wipe the undo history.
        self._keep_history = False

        self.view = PdfView(self)
        self.setCentralWidget(self.view)
        self.view.setContextMenuPolicy(Qt.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self._page_context_menu)
        self.view.annots_edit.connect(self._on_annots_edit)
        self.view.fields_edit.connect(self._on_fields_edit)
        self.view.forms_edit.connect(self._on_forms_edit)
        self.view.field_properties.connect(self._edit_field_properties)
        self.view.annot_open.connect(self._on_annot_open)
        self.view.note_requested.connect(self._on_note_requested)

        self.sidebar = ThumbnailSidebar(self)
        self.sidebar_dock = QDockWidget("Pages", self)
        self.sidebar_dock.setObjectName("pages_dock")
        self.sidebar_dock.setWidget(self.sidebar)
        self.sidebar_dock.setFeatures(QDockWidget.DockWidgetClosable)
        self.addDockWidget(Qt.LeftDockWidgetArea, self.sidebar_dock)
        self.sidebar.page_activated.connect(self.view.scroll_to_page)
        self.sidebar.move_requested.connect(self._on_drop)
        self.sidebar.delete_requested.connect(self.delete_pages)
        self.sidebar.context_requested.connect(self._pages_menu)
        self.sidebar.files_dropped.connect(self.insert_files)

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

        # These act on the pages selected in the sidebar -- see _targets().
        sel = lambda: self._targets()  # noqa: E731
        self.act_page_rot_ccw = QAction("Rotate Pages Left", self,
                                        shortcut=QKeySequence("Ctrl+Shift+L"),
                                        triggered=lambda: self.rotate_pages(sel(), -90))
        self.act_page_rot_cw = QAction("Rotate Pages Right", self,
                                       shortcut=QKeySequence("Ctrl+Shift+R"),
                                       triggered=lambda: self.rotate_pages(sel(), 90))
        self.act_page_up = QAction("Move Pages &Earlier", self, shortcut=QKeySequence("Alt+Up"),
                                   triggered=lambda: self.move_pages_by(sel(), -1))
        self.act_page_down = QAction("Move Pages &Later", self, shortcut=QKeySequence("Alt+Down"),
                                     triggered=lambda: self.move_pages_by(sel(), +1))
        self.act_page_delete = QAction("&Delete Pages", self,
                                       shortcut=QKeySequence("Ctrl+Shift+D"),
                                       triggered=lambda: self.delete_pages(sel()))
        self.act_page_insert = QAction("&Insert Pages from File…", self,
                                       shortcut=QKeySequence("Ctrl+Shift+I"),
                                       triggered=lambda: self.insert_dialog())
        self.act_page_extract = QAction("E&xtract Pages…", self,
                                        shortcut=QKeySequence("Ctrl+Shift+E"),
                                        triggered=lambda: self.extract_pages(sel()))

        # Tools. Single-letter shortcuts, as in every editor; text fields
        # still get their letters (Qt gives focused editors first refusal).
        self.tool_group = QActionGroup(self)
        self.act_tool_select = QAction("Select", self, shortcut=QKeySequence("V"),
                                       checkable=True, checked=True,
                                       triggered=lambda: self.view.set_tool(Tool.SELECT))
        self.act_tool_note = QAction("Note", self, shortcut=QKeySequence("N"), checkable=True,
                                     triggered=lambda: self.view.set_tool(Tool.NOTE))
        self.act_tool_highlight = QAction("Highlight", self, shortcut=QKeySequence("H"),
                                          checkable=True,
                                          triggered=lambda: self.view.set_tool(Tool.HIGHLIGHT))
        self.act_tool_underline = QAction("Underline", self, shortcut=QKeySequence("U"),
                                          checkable=True,
                                          triggered=lambda: self.view.set_tool(Tool.UNDERLINE))
        self.act_tool_strikeout = QAction("Strikethrough", self, shortcut=QKeySequence("S"),
                                          checkable=True,
                                          triggered=lambda: self.view.set_tool(Tool.STRIKEOUT))
        self.act_tool_typewriter = QAction("Typewriter", self, shortcut=QKeySequence("T"),
                                           checkable=True,
                                           triggered=lambda: self.view.set_tool(Tool.TYPEWRITER))
        self.act_tool_textbox = QAction("Text Box", self, shortcut=QKeySequence("X"),
                                        checkable=True,
                                        triggered=lambda: self.view.set_tool(Tool.TEXTBOX))
        def tool(text, key, which):
            act = QAction(text, self, checkable=True,
                          triggered=lambda: self.view.set_tool(which))
            if key:
                act.setShortcut(QKeySequence(key))
            return act

        self.act_tool_rect = tool("Rectangle", "R", Tool.RECT)
        self.act_tool_ellipse = tool("Ellipse", "E", Tool.ELLIPSE)
        self.act_tool_line = tool("Line", "L", Tool.LINE)
        self.act_tool_arrow = tool("Arrow", "A", Tool.ARROW)
        self.act_tool_pen = tool("Pen", "P", Tool.PEN)
        self.act_tool_stamp = tool("Stamp", None, Tool.STAMP)
        self.act_tool_form = tool("Edit Fields", None, Tool.FORM)
        self.act_field_text = tool("Text Field", None, Tool.FIELD_TEXT)
        self.act_field_multiline = tool("Multi-line Text Field", None, Tool.FIELD_MULTILINE)
        self.act_field_checkbox = tool("Checkbox", None, Tool.FIELD_CHECKBOX)
        self.act_field_combo = tool("Dropdown", None, Tool.FIELD_COMBO)
        self.tool_actions = {
            Tool.SELECT: self.act_tool_select, Tool.NOTE: self.act_tool_note,
            Tool.HIGHLIGHT: self.act_tool_highlight, Tool.UNDERLINE: self.act_tool_underline,
            Tool.STRIKEOUT: self.act_tool_strikeout, Tool.TYPEWRITER: self.act_tool_typewriter,
            Tool.TEXTBOX: self.act_tool_textbox, Tool.RECT: self.act_tool_rect,
            Tool.ELLIPSE: self.act_tool_ellipse, Tool.LINE: self.act_tool_line,
            Tool.ARROW: self.act_tool_arrow, Tool.PEN: self.act_tool_pen,
            Tool.STAMP: self.act_tool_stamp, Tool.FORM: self.act_tool_form,
            Tool.FIELD_TEXT: self.act_field_text, Tool.FIELD_MULTILINE: self.act_field_multiline,
            Tool.FIELD_CHECKBOX: self.act_field_checkbox, Tool.FIELD_COMBO: self.act_field_combo,
        }
        # Everything that marks up the page, grouped as the toolbar shows it.
        self.annotate_menu = QMenu("Annotate", self)
        self.annotate_menu.addAction(self.act_tool_note)
        self.annotate_menu.addSeparator()
        for act in (self.act_tool_highlight, self.act_tool_underline, self.act_tool_strikeout):
            self.annotate_menu.addAction(act)
        self.annotate_menu.addSeparator()
        for act in (self.act_tool_typewriter, self.act_tool_textbox):
            self.annotate_menu.addAction(act)
        self.annotate_menu.addSeparator()
        for act in (self.act_tool_rect, self.act_tool_ellipse, self.act_tool_line,
                    self.act_tool_arrow, self.act_tool_pen):
            self.annotate_menu.addAction(act)
        self.annotate_menu.addSeparator()
        self.annotate_tools = {Tool.NOTE, Tool.HIGHLIGHT, Tool.UNDERLINE, Tool.STRIKEOUT,
                               Tool.TYPEWRITER, Tool.TEXTBOX, Tool.RECT, Tool.ELLIPSE,
                               Tool.LINE, Tool.ARROW, Tool.PEN, Tool.STAMP}
        self.form_tools = {Tool.FORM, Tool.FIELD_TEXT, Tool.FIELD_MULTILINE,
                           Tool.FIELD_CHECKBOX, Tool.FIELD_COMBO}

        self.form_menu = QMenu("Form", self)
        self.form_menu.addAction(self.act_tool_form)
        self.form_menu.addSeparator()
        for act in (self.act_field_text, self.act_field_multiline, self.act_field_checkbox,
                    self.act_field_combo):
            self.form_menu.addAction(act)
        for act in self.tool_actions.values():
            self.tool_group.addAction(act)

        self.highlight_menu = QMenu("Highlight Colour", self)
        for name, color in HIGHLIGHT_COLORS.items():
            act = self.highlight_menu.addAction(self._swatch(color), name)
            act.triggered.connect(lambda _=False, c=color: self._set_highlight_color(c))

        self.text_menu = QMenu("Text Style", self)
        self.text_size_actions = QActionGroup(self)
        for size in TEXT_SIZES:
            act = self.text_menu.addAction(f"{size} pt")
            act.setCheckable(True)
            act.setChecked(size == 12)
            self.text_size_actions.addAction(act)
            act.triggered.connect(lambda _=False, sz=size: self._set_text_style(size=sz))
        self.text_menu.addSeparator()
        for name, color in TEXT_COLORS.items():
            act = self.text_menu.addAction(self._swatch(color), name)
            act.triggered.connect(lambda _=False, c=color: self._set_text_style(color=c))

        self.shape_menu = QMenu("Shape Style", self)
        for name, color in SHAPE_COLORS.items():
            act = self.shape_menu.addAction(self._swatch(color), name)
            act.triggered.connect(lambda _=False, c=color: self._set_shape_style(color=c))
        self.shape_menu.addSeparator()
        widths = QActionGroup(self)
        for width in SHAPE_WIDTHS:
            act = self.shape_menu.addAction(f"{width} pt line")
            act.setCheckable(True)
            act.setChecked(width == 2)
            widths.addAction(act)
            act.triggered.connect(lambda _=False, w=width: self._set_shape_style(width=w))

        self.stamp_menu = QMenu("Stamps", self)
        for stamp in STAMPS:
            label, color, dated, _ = stamp
            text = label.title() + ("  (name & date)" if dated else "")
            act = self.stamp_menu.addAction(self._swatch(color), text)
            act.triggered.connect(lambda _=False, st=stamp: self._pick_stamp(st))

        self.annotate_menu.addMenu(self.stamp_menu)
        self.annotate_menu.addSeparator()
        for menu in (self.highlight_menu, self.text_menu, self.shape_menu):
            self.annotate_menu.addMenu(menu)

        self.act_copy = QAction("&Copy", self, shortcut=QKeySequence.Copy, triggered=self.copy_text)
        self.act_copy.setEnabled(False)
        self.act_select_all = QAction("Select &All", self, shortcut=QKeySequence.SelectAll,
                                      triggered=self.select_all)
        self.act_delete_annot = QAction("Delete Annotation", self,
                                        triggered=self.view.delete_selected_annot)

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

        tools = QToolBar("Tools", self)
        tools.setMovable(False)
        self.addToolBar(tools)
        tools.addAction(self.act_tool_select)
        # Two drop-down buttons hold everything else (user's layout). Their
        # labels stay put -- "Annotate", "Form" -- and name the active tool
        # while one of theirs is in use (see _on_tool_changed).
        self.annotate_button = QToolButton(self, text="Annotate")
        self.annotate_button.setMenu(self.annotate_menu)
        self.annotate_button.setPopupMode(QToolButton.InstantPopup)
        tools.addWidget(self.annotate_button)
        self.form_button = QToolButton(self, text="Form")
        self.form_button.setMenu(self.form_menu)
        self.form_button.setPopupMode(QToolButton.InstantPopup)
        tools.addWidget(self.form_button)
        self.view.tool_changed.connect(self._on_tool_changed)
        self.view.editing_changed.connect(self._on_editing_changed)
        self.view.message.connect(lambda text: self.statusBar().showMessage(text, 8000))
        self.view.text_selection_changed.connect(self.act_copy.setEnabled)

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
        edit_menu.addSeparator()
        edit_menu.addAction(self.act_copy)
        edit_menu.addAction(self.act_select_all)
        edit_menu.addSeparator()
        edit_menu.addAction(self.act_delete_annot)

        tools_menu = self.menuBar().addMenu("&Tools")
        tools_menu.addAction(self.act_tool_select)
        tools_menu.addMenu(self.annotate_menu)
        tools_menu.addMenu(self.form_menu)

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
        view_menu.addSeparator()
        toggle = self.sidebar_dock.toggleViewAction()
        toggle.setText("Page &Thumbnails")
        toggle.setShortcut(QKeySequence("F4"))
        view_menu.addAction(toggle)

        pages_menu = self.menuBar().addMenu("&Pages")
        pages_menu.addAction(self.act_page_rot_ccw)
        pages_menu.addAction(self.act_page_rot_cw)
        pages_menu.addSeparator()
        pages_menu.addAction(self.act_page_up)
        pages_menu.addAction(self.act_page_down)
        pages_menu.addSeparator()
        pages_menu.addAction(self.act_page_insert)
        pages_menu.addAction(self.act_page_extract)
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
                    self.act_page_delete, self.act_page_extract, self.act_page_insert,
                    *self.tool_actions.values(), self.act_delete_annot, self.act_select_all,
                    self.act_next,
                    self.act_prev, self.act_first,
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

    # ---- page edits ----------------------------------------------------

    def _targets(self) -> list[int]:
        """Pages an edit applies to: the sidebar selection, else the current page.

        With the sidebar open the selection is visible, so the user can see
        what a shortcut will hit. Without it, the current page is the only
        sensible target.
        """
        if self.sidebar_dock.isVisible():
            rows = self.sidebar.selected_rows()
            if rows:
                return rows
        return [self.view.current_page]

    @staticmethod
    def _describe(indices: list[int]) -> str:
        return f"Page {indices[0] + 1}" if len(indices) == 1 else f"{len(indices)} Pages"

    def _edit(self, pages: PageList, label: str, current: int, select: list[int]) -> None:
        if self.history is None:
            return
        state = replace(self.history.current, pages=pages)
        if state == self.history.current:
            return
        self.history.push(state, label)
        self._show_state(state, current, select)

    def _show_state(self, state: DocState, current: int,
                    select: Optional[list[int]] = None) -> None:
        doc = self.view.document
        pages_changed = state.pages != doc.pages
        doc.pages = state.pages
        doc.annots = state.annots
        doc.fields = state.fields
        doc.forms = state.forms
        if pages_changed:
            current = max(0, min(len(state.pages) - 1, current))
            self.sidebar.set_document(doc)
            self.view.pages_changed(current)
            self.sidebar.select_rows(select if select else [current])
            self._refresh_page_count()
        else:
            # Annotations only: no relayout, keep the sidebar selection.
            rows = self.sidebar.selected_rows()
            self.sidebar.set_document(doc)
            self.sidebar.select_rows(rows or [self.view.current_page])
            self.view.annots_changed()
        self._refresh_edit_state()

    # ---- annotation edits ----------------------------------------------

    def _on_annots_edit(self, key, annots, label: str) -> None:
        if self.history is None:
            return
        current = self.history.current
        state = replace(current, annots=current.annots.with_page(key, annots))
        if state == current:
            return
        self.history.push(state, label)
        self._show_state(state, self.view.current_page)

    def _on_fields_edit(self, key, value: str, label: str) -> None:
        if self.history is None:
            return
        current = self.history.current
        state = replace(current, fields=current.fields.with_value(key, value))
        if state == current:
            return
        self.history.push(state, label)
        self._show_state(state, self.view.current_page)

    def _on_forms_edit(self, key, widgets, label: str, rename=None) -> None:
        if self.history is None:
            return
        current = self.history.current
        fields = current.fields
        if rename is not None:
            fields = fields.renamed(key[0], *rename)
        state = replace(current, forms=current.forms.with_page(key, widgets), fields=fields)
        if state == current:
            return
        self.history.push(state, label)
        self._show_state(state, self.view.current_page)

    def _edit_field_properties(self, index: int, widget) -> None:
        """Name, required / read-only, multi-line, font size, dropdown options."""
        doc = self.view.document
        dialog = QDialog(self)
        dialog.setWindowTitle(f"Field Properties \u2014 {widget.short_name()}")
        form = QFormLayout(dialog)
        name = QLineEdit(widget.short_name())
        form.addRow("Name:", name)
        required = QCheckBox("Required")
        required.setChecked(bool(widget.flags & FLAG_REQUIRED))
        readonly = QCheckBox("Read-only")
        readonly.setChecked(bool(widget.flags & FLAG_READONLY))
        form.addRow("", required)
        form.addRow("", readonly)
        multiline = QCheckBox("Multiple lines")
        multiline.setChecked(widget.multiline)
        size = QComboBox()
        size.addItems(["Auto", "8", "9", "10", "11", "12", "14", "16", "18"])
        size.setCurrentText(f"{widget.font_size:g}" if widget.font_size else "Auto")
        options = QPlainTextEdit("\n".join(widget.options))
        if widget.kind == TEXT:
            form.addRow("", multiline)
        if widget.kind in (TEXT, COMBO):
            form.addRow("Font size:", size)
        if widget.kind == COMBO:
            options.setPlaceholderText("One option per line")
            form.addRow("Options:", options)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        taken = self.view.taken_field_names(doc.pages[index].file) - {widget.name}
        while True:
            if dialog.exec() != QDialog.Accepted:
                return
            new_name = name.text().strip()
            full = (widget.name.rsplit(".", 1)[0] + "." if "." in widget.name else "") + new_name
            if not new_name or "." in new_name:
                QMessageBox.warning(self, "Field name", "Give the field a name without dots.")
            elif full in taken:
                QMessageBox.warning(self, "Field name", f"There is already a field called "
                                                        f"\u201c{new_name}\u201d.")
            elif widget.kind == COMBO and not options.toPlainText().strip():
                QMessageBox.warning(self, "Options", "A dropdown needs at least one option.")
            else:
                break
        flags = widget.flags & ~(FLAG_REQUIRED | FLAG_READONLY | FLAG_MULTILINE)
        flags |= FLAG_REQUIRED if required.isChecked() else 0
        flags |= FLAG_READONLY if readonly.isChecked() else 0
        if widget.kind == TEXT and multiline.isChecked():
            flags |= FLAG_MULTILINE
        changed = replace(widget.renamed(new_name), flags=flags, changed=True,
                          font_size=0.0 if size.currentText() == "Auto" else float(size.currentText()),
                          options=tuple(o.strip() for o in options.toPlainText().splitlines()
                                        if o.strip()) if widget.kind == COMBO else widget.options)
        if changed == replace(widget, changed=True):
            return
        widgets = tuple(changed if w.ident == widget.ident else w for w in doc.current_widgets(index))
        rename = (widget.name, changed.name) if changed.name != widget.name else None
        self._on_forms_edit(doc.page_key(index), widgets, f"Edit {changed.short_name()}", rename)

    def _on_tool_changed(self, tool: Tool) -> None:
        act = self.tool_actions[tool]
        act.setChecked(True)
        name = act.text().replace("&", "")
        self.annotate_button.setText(f"Annotate: {name}" if tool in self.annotate_tools
                                     else "Annotate")
        self.form_button.setText(f"Form: {name}" if tool in self.form_tools else "Form")

    @staticmethod
    def _swatch(color) -> QIcon:
        pixmap = QPixmap(14, 14)
        pixmap.fill(QColor.fromRgbF(*color))
        return QIcon(pixmap)

    def _set_text_style(self, size=None, color=None) -> None:
        if size is not None:
            self.view.text_size = float(size)
        if color is not None:
            self.view.text_color = color
            self.act_tool_typewriter.setIcon(self._swatch(color))
        if self.view.tool not in (Tool.TYPEWRITER, Tool.TEXTBOX):
            self.view.set_tool(Tool.TYPEWRITER)

    def _set_shape_style(self, color=None, width=None) -> None:
        if color is not None:
            self.view.shape_color = color
            self.act_tool_rect.setIcon(self._swatch(color))
        if width is not None:
            self.view.shape_width = float(width)
        if self.view.tool not in (Tool.RECT, Tool.ELLIPSE, Tool.LINE, Tool.ARROW, Tool.PEN):
            self.view.set_tool(Tool.RECT)

    def _pick_stamp(self, stamp) -> None:
        self.view.stamp = stamp
        self.view.set_tool(Tool.STAMP)
        self.statusBar().showMessage(f"Click on the page to place {stamp[0].title()}", 5000)

    def _on_editing_changed(self, editing: bool) -> None:
        # While typing into a text box, letters must reach the editor: the
        # single-key tool shortcuts (T, H, S...) and Ctrl+Z/A/C belong to it.
        for act in (*self.tool_actions.values(), self.act_undo, self.act_redo,
                    self.act_select_all, self.act_copy, self.act_delete_annot):
            act.setEnabled(not editing)
        if not editing:
            self._refresh_edit_state()
            self.act_copy.setEnabled(bool(self.view.selected_text()))

    def _set_highlight_color(self, color) -> None:
        self.view.highlight_color = color
        self.act_tool_highlight.setIcon(self._swatch(color))
        self.view.set_tool(Tool.HIGHLIGHT)

    # ---- text ----------------------------------------------------------

    def copy_text(self) -> None:
        text = self.view.selected_text()
        if text:
            QApplication.clipboard().setText(text)
            self.statusBar().showMessage(f"Copied {len(text)} characters", 3000)

    def select_all(self) -> None:
        # Ctrl+A means "all pages" in the sidebar and "all text on this
        # page" in the page view; one shortcut, routed by focus.
        if self.sidebar.hasFocus():
            self.sidebar.selectAll()
        else:
            self.view.select_all_text()

    def _on_note_requested(self, index: int, x: float, y: float) -> None:
        text, ok = QInputDialog.getMultiLineText(self, "Add Note", "Note:")
        self.view.set_tool(Tool.SELECT)
        if not ok:
            return
        doc = self.view.document
        # The click marks the icon's top-left corner, as in Acrobat.
        note = new_note(x, y, text, getpass.getuser())
        self._on_annots_edit(doc.page_key(index), doc.page_annots(index) + (note,), "Add Note")
        self.view.select_annot(index, note.ident)

    def _on_annot_open(self, index: int, annot: Annot) -> None:
        doc = self.view.document
        text, ok = QInputDialog.getMultiLineText(
            self, f"Edit {describe(annot.kind)}", "Comment:", annot.contents)
        if not ok or text == annot.contents:
            return
        annots = tuple(a.with_contents(text) if a.ident == annot.ident else a
                       for a in doc.page_annots(index))
        self._on_annots_edit(doc.page_key(index), annots, f"Edit {describe(annot.kind)}")

    def rotate_pages(self, indices: list[int], delta: int) -> None:
        if self.view.document is None or not indices:
            return
        side = "Right" if delta > 0 else "Left"
        self._edit(self.view.document.pages.rotated(indices, delta),
                   f"Rotate {self._describe(indices)} {side}", indices[0], indices)

    def delete_pages(self, indices: list[int]) -> None:
        if self.view.document is None or not indices:
            return
        try:
            pages = self.view.document.pages.deleted(indices)
        except ValueError as exc:
            QMessageBox.information(self, "Can't delete pages", f"{str(exc).capitalize()}.")
            return
        first = min(indices)
        self._edit(pages, f"Delete {self._describe(indices)}", first,
                   [min(first, len(pages) - 1)])

    def move_pages_by(self, indices: list[int], step: int) -> None:
        """Alt+Up / Alt+Down: shift the selection one place, as a block."""
        doc = self.view.document
        if doc is None or not indices:
            return
        to = min(indices) + step
        if to < 0 or to + len(indices) > doc.page_count:
            return
        self._move(indices, to)

    def _on_drop(self, indices: list[int], before: int) -> None:
        doc = self.view.document
        if doc is None or not indices:
            return
        self._move(indices, doc.pages.insert_position(indices, before))

    def _move(self, indices: list[int], to: int) -> None:
        pages = self.view.document.pages.moved(indices, to)
        landed = list(range(to, to + len(indices)))
        self._edit(pages, f"Move {self._describe(indices)}", to, landed)

    def undo(self) -> None:
        if self.history is not None and self.history.can_undo():
            self._show_state(self.history.undo(), self.view.current_page)

    def redo(self) -> None:
        if self.history is not None and self.history.can_redo():
            self._show_state(self.history.redo(), self.view.current_page)

    def _refresh_edit_state(self) -> None:
        h = self.history
        self.setWindowModified(h is not None and h.dirty)
        self.act_save.setEnabled(h is not None and h.dirty)
        self.act_undo.setEnabled(h is not None and h.can_undo())
        self.act_redo.setEnabled(h is not None and h.can_redo())
        self.act_undo.setText(f"&Undo {h.undo_label()}" if h and h.can_undo() else "&Undo")
        self.act_redo.setText(f"&Redo {h.redo_label()}" if h and h.can_redo() else "&Redo")

    def _page_context_menu(self, pos) -> None:
        """Right-click in the page view: acts on exactly the page clicked."""
        if self.view.document is None:
            return
        index = self.view.page_at(pos)
        if index is not None:
            self._pages_menu([index], self.view.viewport().mapToGlobal(pos), text_actions=True)

    def _pages_menu(self, indices: list[int], global_pos, text_actions: bool = False) -> None:
        doc = self.view.document
        if doc is None or not indices:
            return
        what = self._describe(indices)
        n = doc.page_count
        menu = QMenu(self)
        selection = self.view.text_selection()
        if text_actions and selection is not None and selection[0] == indices[0]:
            menu.addAction(self.act_copy)
            for kind, label in (("Highlight", "Highlight"), ("Underline", "Underline"),
                                ("StrikeOut", "Strikethrough")):
                menu.addAction(label, lambda k=kind: self.view.mark_selection(k))
            menu.addSeparator()
        menu.addAction(f"Rotate {what} Left", lambda: self.rotate_pages(indices, -90))
        menu.addAction(f"Rotate {what} Right", lambda: self.rotate_pages(indices, 90))
        menu.addSeparator()
        menu.addAction("Move Earlier",
                       lambda: self.move_pages_by(indices, -1)).setEnabled(min(indices) > 0)
        menu.addAction("Move Later", lambda: self.move_pages_by(indices, +1)).setEnabled(
            min(indices) + len(indices) < n)
        menu.addSeparator()
        menu.addAction("Insert Pages from File…",
                       lambda: self.insert_dialog(max(indices) + 1))
        menu.addAction(f"Extract {what}…", lambda: self.extract_pages(indices))
        menu.addSeparator()
        menu.addAction(f"Delete {what}",
                       lambda: self.delete_pages(indices)).setEnabled(len(indices) < n)
        menu.exec(global_pos)

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
        if doc is None or not indices:
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
        allowed: set[int] = set()
        while True:
            try:
                return write_pdf(doc.sources, pages, dest, allow_decrypt=allowed,
                                 annots=doc.annots, fields=doc.fields, forms=doc.forms), allowed
            except EncryptionWouldBeLost as exc:
                if exc.file == 0 and self._same_file(dest, doc.path):
                    QMessageBox.warning(
                        self, "Can't save over a protected file",
                        f"{doc.path.name} is password-protected, and saving changes "
                        f"would remove the password. {APP_NAME} can't re-apply "
                        f"encryption yet.\n\nUse Save As to write an unprotected copy "
                        f"under a different name.",
                    )
                    return None
                if not self._confirm_unprotected_copy(exc.path, dest, inserted=exc.file != 0):
                    return None
                allowed.add(exc.file)
            except Exception as exc:  # nothing on disk has changed
                QMessageBox.critical(self, failure_title,
                                     f"Could not write {dest.name}:\n\n{exc}")
                return None

    def _confirm_unprotected_copy(self, source: Path, dest: Path, inserted: bool) -> bool:
        what = (f"Pages you inserted from {source.name} are password-protected in that "
                f"file. In {dest.name} they will NOT be" if inserted else
                f"{source.name} is password-protected. The copy saved as {dest.name} "
                f"will NOT have a password")
        answer = QMessageBox.warning(
            self, "Save unprotected copy?",
            f"{what} — anyone who gets the file can open it.\n\n"
            f"{source.name} itself stays protected.",
            QMessageBox.Save | QMessageBox.Cancel, QMessageBox.Cancel,
        )
        return answer == QMessageBox.Save

    def _save_to(self, dest: Path) -> bool:
        """Write, swap in, reopen."""
        doc = self.view.document
        pages, annots, fields, forms = doc.pages, doc.annots, doc.fields, doc.forms
        written = self._write(pages, dest, "Save failed")
        if written is None:
            return False
        tmp, allowed = written

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
                self._reopen(sources, keep_edits=DocState(pages, annots, fields, forms))
            QMessageBox.critical(self, "Save failed", f"{exc}\n\nThe original file is unchanged.")
            return False

        # The saved file is the new baseline, with the edits baked in. The undo
        # history refers to the old files' page numbers, so it starts over.
        password = None if 0 in allowed else doc.password
        try:
            self._reopen([SourceInfo(dest, password)])
        except Exception as exc:
            self.view.set_document(None)
            QMessageBox.critical(
                self, "Saved, but could not reopen",
                f"{dest.name} was saved, but reopening it failed:\n\n{exc}",
            )
            return True
        self.statusBar().showMessage(f"Saved {dest}", 5000)
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
        except Exception:
            document.close()
            raise
        self._keep_history = keep_edits is not None
        try:
            self.view.set_document(document, keep_view=True)
        finally:
            self._keep_history = False

    # ---- signal handlers -----------------------------------------------

    def _on_document_opened(self, document: Optional[PdfDocument]) -> None:
        self.sidebar.set_document(document)
        self.view.set_tool(Tool.SELECT)
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
            self.history = History(DocState(document.pages))
        # [*] is where Qt draws the unsaved-changes marker (setWindowModified).
        self.setWindowTitle(f"{document.path.name}[*] — {APP_NAME}")
        self._refresh_page_count()
        forms = "  ·  has form fields" if document.has_forms else ""
        if document.is_xfa:
            forms = ("  ·  XFA form: its fields can't be shown or filled here "
                     "(same as Chrome/Edge) -- open it in Acrobat")
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
        self.sidebar.follow_page(index)
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
