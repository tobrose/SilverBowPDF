"""Main window: chrome around the view."""

from __future__ import annotations

import getpass
from dataclasses import replace
from typing import Optional

from PySide6.QtCore import Qt, QTimer
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
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QSpinBox,
    QStackedWidget,
    QTabWidget,
    QToolBar,
    QToolButton,
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
from .document import PdfDocument
from .doctab import DocumentTab
from .pagelist import History, PageList
from .fields import COMBO, FLAG_MULTILINE, FLAG_READONLY, FLAG_REQUIRED, TEXT
from .pageview import FitMode, PdfView, Tool
from .settings import Settings
from .accessflow import AccessMixin
from .fileflow import FilesMixin
from .jobs import JobsMixin
from .signflow import SigningMixin

APP_NAME = "SilverbowPDF"

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


class MainWindow(FilesMixin, JobsMixin, AccessMixin, SigningMixin, QMainWindow):
    def __init__(self, settings: Optional[Settings] = None):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1100, 860)
        self.setAcceptDrops(True)

        self.settings = settings if settings is not None else Settings()
        # Set while reopening the same file with edits intact (a failed save),
        # so document_opened doesn't wipe the undo history.
        self._keep_history = False
        self._picture = None  # the user's signature picture, once loaded
        self._shown_tab: Optional[DocumentTab] = None

        # One tab per open document (user's call, 2026-10-02). Everything
        # per document lives in its DocumentTab; the menus and toolbars here
        # act on the current one through view, sidebar, history...
        self.tabs = QTabWidget(self)
        self.tabs.setDocumentMode(True)
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(True)
        self.tabs.setElideMode(Qt.ElideMiddle)
        self.tabs.tabCloseRequested.connect(self.close_tab)
        self.tabs.currentChanged.connect(self._on_tab_changed)
        self.setCentralWidget(self.tabs)

        # Each tab's page sidebar, stacked: the dock shows the current one's.
        self.sidebar_stack = QStackedWidget(self)
        self.sidebar_dock = QDockWidget("Pages", self)
        self.sidebar_dock.setObjectName("pages_dock")
        self.sidebar_dock.setWidget(self.sidebar_stack)
        self.sidebar_dock.setFeatures(QDockWidget.DockWidgetClosable)
        self.addDockWidget(Qt.LeftDockWidgetArea, self.sidebar_dock)

        self._building_ui = True
        self._build_actions()
        self._build_toolbar()
        self._build_menus()
        self._build_statusbar()
        self._building_ui = False

        self._add_tab()  # the first, empty: "Open a PDF to get started"
        self._apply_letter_shortcuts()

    # ---- tabs ----------------------------------------------------------

    @property
    def current(self) -> DocumentTab:
        return self.tabs.currentWidget()

    # The current tab's parts, under the names MainWindow has always used.
    view = property(lambda self: self.current.view)
    find_bar = property(lambda self: self.current.find_bar)
    sidebar = property(lambda self: self.current.sidebar)
    signature_bar = property(lambda self: self.current.signature_bar)
    signature_text = property(lambda self: self.current.signature_text)
    restriction_bar = property(lambda self: self.current.restriction_bar)
    restriction_text = property(lambda self: self.current.restriction_text)

    @property
    def history(self) -> Optional[History[DocState]]:
        return self.current.history

    @history.setter
    def history(self, history: Optional[History[DocState]]) -> None:
        self.current.history = history

    @property
    def _signature_infos(self) -> list:
        return self.current.signature_infos

    @_signature_infos.setter
    def _signature_infos(self, infos: list) -> None:
        self.current.signature_infos = infos

    def tab_list(self) -> list:
        return [self.tabs.widget(i) for i in range(self.tabs.count())]

    def _add_tab(self) -> DocumentTab:
        """A new, empty tab, made current."""
        tab = DocumentTab(self)

        def on(signal, slot):
            # Only the tab on screen speaks to the window: a background tab's
            # view can still emit (a resize refits it), and must not move the
            # page counter or the sidebar of the tab being looked at.
            signal.connect(lambda *args: slot(*args) if self.current is tab else None)

        v = tab.view
        on(v.customContextMenuRequested, self._page_context_menu)
        on(v.annots_edit, self._on_annots_edit)
        on(v.fields_edit, self._on_fields_edit)
        on(v.forms_edit, self._on_forms_edit)
        on(v.field_properties, self._edit_field_properties)
        on(v.annot_open, self._on_annot_open)
        on(v.note_requested, self._on_note_requested)
        on(v.page_changed, self._on_page_changed)
        on(v.zoom_changed, self._on_zoom_changed)
        on(v.document_opened, self._on_document_opened)
        on(v.signature_box, self.sign_with_certificate)
        on(v.tool_changed, self._on_tool_changed)
        on(v.editing_changed, self._on_editing_changed)
        on(v.message, lambda text: self.statusBar().showMessage(text, 8000))
        on(v.text_selection_changed,
           lambda has: self.act_copy.setEnabled(has and self.access.copy))
        on(v.text_style_changed, self._show_text_style)
        on(v.highlight_color_changed, lambda c: self.act_tool_highlight.setIcon(self._swatch(c)))
        on(tab.sidebar.move_requested, self._on_drop)
        on(tab.sidebar.delete_requested, self.delete_pages)
        on(tab.sidebar.context_requested, self._pages_menu)
        on(tab.sidebar.files_dropped, self.insert_files)
        tab.unlock_button.clicked.connect(lambda: self.unlock())
        tab.details_button.clicked.connect(lambda: self.show_signatures())

        if self._shown_tab is not None:
            self._carry_style(self._shown_tab.view, v)
        self.sidebar_stack.addWidget(tab.sidebar)
        self.tabs.setCurrentIndex(self.tabs.addTab(tab, tab.title()))
        return tab

    @staticmethod
    def _carry_style(old: PdfView, new: PdfView) -> None:
        """Colours, sizes, the stamp and the signature picture are the user's
        choices, not a document's: they follow from tab to tab."""
        for name in ("highlight_color", "text_size", "text_color", "shape_color",
                     "shape_width", "stamp", "signature_image"):
            setattr(new, name, getattr(old, name))

    def _remove_tab(self, tab: DocumentTab) -> None:
        tab.view.set_document(None)  # closes the document (and its file handles)
        if self._shown_tab is tab:
            self._shown_tab = None
        self.tabs.removeTab(self.tabs.indexOf(tab))
        self.sidebar_stack.removeWidget(tab.sidebar)
        tab.sidebar.deleteLater()
        tab.deleteLater()

    def close_tab(self, index: int) -> bool:
        """A tab's close button: that tab, with the usual save prompt."""
        self.tabs.setCurrentIndex(index)
        return self.close_document()

    def _on_tab_changed(self, index: int) -> None:
        tab = self.tabs.widget(index)
        previous, self._shown_tab = self._shown_tab, tab
        if tab is None:
            return
        if previous is not None and previous is not tab:
            self._carry_style(previous.view, tab.view)
            previous.release_memory()
        self.sidebar_stack.setCurrentWidget(tab.sidebar)
        self._sync_chrome()

    def _update_tabs(self) -> None:
        for tab in self.tab_list():
            i = self.tabs.indexOf(tab)
            self.tabs.setTabText(i, tab.title())
            self.tabs.setTabToolTip(i, str(tab.document.path) if tab.document else "")
        # No tab strip for an empty window: just the "open a PDF" page.
        self.tabs.tabBar().setVisible(self.tabs.count() > 1 or self.current.document is not None)

    def next_tab(self, step: int = 1) -> None:
        if self.tabs.count() > 1:
            self.tabs.setCurrentIndex((self.tabs.currentIndex() + step) % self.tabs.count())

    def open_paths(self, paths: list) -> None:
        """Files handed over by a second launch (instance.py): each in its own
        tab, then the window comes forward. Waits while a dialog is open --
        a print or OCR job in progress mustn't find the tab switched under it."""
        if QApplication.activeModalWidget() is not None:
            QTimer.singleShot(500, lambda: self.open_paths(paths))
            return
        for path in paths:
            self.open_path(path)
        if self.isMinimized():
            self.showNormal()
        self.raise_()
        self.activateWindow()

    # ---- construction --------------------------------------------------

    def _build_actions(self) -> None:
        self.act_open = QAction("&Open…", self, shortcut=QKeySequence.Open,
                                triggered=self.open_dialog)
        self.act_save = QAction("&Save", self, shortcut=QKeySequence.Save,
                                triggered=self.save)
        self.act_save_as = QAction("Save &As…", self, shortcut=QKeySequence("Ctrl+Shift+S"),
                                   triggered=self.save_as)
        self.act_print = QAction("&Print…", self, shortcut=QKeySequence.Print,
                                 triggered=self.print_dialog)
        self.act_protect = QAction("Password &Protection…", self,
                                   triggered=self.password_protection)
        self.act_close = QAction("&Close", self, shortcut=QKeySequence.Close,
                                 triggered=self.close_document)
        self.act_quit = QAction("&Quit", self, shortcut=QKeySequence.Quit,
                                triggered=self.close)

        self.act_undo = QAction("&Undo", self, shortcut=QKeySequence.Undo, triggered=self.undo)
        self.act_redo = QAction("&Redo", self, triggered=self.redo)
        self.act_redo.setShortcuts([QKeySequence.Redo, QKeySequence("Ctrl+Shift+Z")])

        self.act_zoom_in = QAction("Zoom &In", self, shortcut=QKeySequence.ZoomIn,
                                   triggered=lambda: self.view.zoom_in())
        # Ctrl+= as well, because half the world's keyboards make Ctrl++ awkward.
        self.act_zoom_in.setShortcuts([QKeySequence.ZoomIn, QKeySequence("Ctrl+=")])
        self.act_zoom_out = QAction("Zoom &Out", self, shortcut=QKeySequence.ZoomOut,
                                    triggered=lambda: self.view.zoom_out())
        self.act_zoom_100 = QAction("Actual Size", self, shortcut=QKeySequence("Ctrl+0"),
                                    triggered=lambda: self.view.set_scale(1.0, FitMode.FIXED))
        self.act_fit_width = QAction("Fit &Width", self, shortcut=QKeySequence("Ctrl+1"),
                                     triggered=lambda: self.view.fit_width())
        self.act_fit_page = QAction("Fit &Page", self, shortcut=QKeySequence("Ctrl+2"),
                                    triggered=lambda: self.view.fit_page())

        # No "rotate view": it was removed (user's call) because a turned view
        # on top of turned pages confused people. Rotation is a page edit only.
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
        # No letter for Pan: H is Highlight. Middle-drag pans in any tool.
        self.act_tool_pan = QAction("Pan", self, checkable=True,
                                    toolTip="Pan: drag the page to scroll (Esc to stop)",
                                    triggered=lambda: self.view.set_tool(Tool.PAN))
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
        # No Text Box tool (removed, user's call): the typewriter covers it.
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
        # Signing (Sign button). These two are tools, but each checks first:
        # the picture tool needs a picture, both need permission to annotate.
        self.act_sign_cert = QAction("Sign with Certificate…", self, checkable=True,
                                     triggered=lambda: self.start_certificate_signing())
        self.act_sign_picture = QAction("Place My Signature Picture", self, checkable=True,
                                        triggered=lambda: self.place_signature_picture())
        self.act_choose_picture = QAction("Choose Signature Picture…", self,
                                          triggered=lambda: self.choose_signature_picture())
        self.act_create_id = QAction("Create a Digital ID…", self,
                                     triggered=lambda: self.create_digital_id())
        self.act_signatures = QAction("Signatures in This Document…", self,
                                      triggered=lambda: self.show_signatures())
        self.tool_actions = {
            Tool.SELECT: self.act_tool_select, Tool.PAN: self.act_tool_pan,
            Tool.NOTE: self.act_tool_note,
            Tool.HIGHLIGHT: self.act_tool_highlight, Tool.UNDERLINE: self.act_tool_underline,
            Tool.STRIKEOUT: self.act_tool_strikeout, Tool.TYPEWRITER: self.act_tool_typewriter,
            Tool.RECT: self.act_tool_rect,
            Tool.ELLIPSE: self.act_tool_ellipse, Tool.LINE: self.act_tool_line,
            Tool.ARROW: self.act_tool_arrow, Tool.PEN: self.act_tool_pen,
            Tool.STAMP: self.act_tool_stamp, Tool.FORM: self.act_tool_form,
            Tool.FIELD_TEXT: self.act_field_text, Tool.FIELD_MULTILINE: self.act_field_multiline,
            Tool.FIELD_CHECKBOX: self.act_field_checkbox, Tool.FIELD_COMBO: self.act_field_combo,
            Tool.SIGN: self.act_sign_cert, Tool.SIGNATURE: self.act_sign_picture,
        }
        # Everything that marks up the page, grouped as the toolbar shows it.
        self.annotate_menu = QMenu("Annotate", self)
        self.annotate_menu.addAction(self.act_tool_note)
        self.annotate_menu.addSeparator()
        for act in (self.act_tool_highlight, self.act_tool_underline, self.act_tool_strikeout):
            self.annotate_menu.addAction(act)
        self.annotate_menu.addSeparator()
        self.annotate_menu.addAction(self.act_tool_typewriter)
        self.annotate_menu.addSeparator()
        for act in (self.act_tool_rect, self.act_tool_ellipse, self.act_tool_line,
                    self.act_tool_arrow, self.act_tool_pen):
            self.annotate_menu.addAction(act)
        self.annotate_menu.addSeparator()
        self.annotate_tools = {Tool.NOTE, Tool.HIGHLIGHT, Tool.UNDERLINE, Tool.STRIKEOUT,
                               Tool.TYPEWRITER, Tool.RECT, Tool.ELLIPSE,
                               Tool.LINE, Tool.ARROW, Tool.PEN, Tool.STAMP}
        self.form_tools = {Tool.FORM, Tool.FIELD_TEXT, Tool.FIELD_MULTILINE,
                           Tool.FIELD_CHECKBOX, Tool.FIELD_COMBO}

        self.form_menu = QMenu("Form", self)
        self.form_menu.addAction(self.act_tool_form)
        self.form_menu.addSeparator()
        for act in (self.act_field_text, self.act_field_multiline, self.act_field_checkbox,
                    self.act_field_combo):
            self.form_menu.addAction(act)

        self.sign_menu = QMenu("Sign", self)
        self.sign_menu.addAction(self.act_sign_cert)
        self.sign_menu.addAction(self.act_sign_picture)
        self.sign_menu.addSeparator()
        self.sign_menu.addAction(self.act_choose_picture)
        self.sign_menu.addAction(self.act_create_id)
        self.sign_menu.addSeparator()
        self.sign_menu.addAction(self.act_signatures)
        self.sign_tools = {Tool.SIGN, Tool.SIGNATURE}
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

        # Single-letter tool shortcuts: off unless turned on (Settings).
        self.letter_actions = {act: act.shortcut() for act in self.tool_actions.values()
                               if act.shortcut().count() == 1
                               and len(act.shortcut().toString()) == 1}
        self.act_ocr = QAction("&Recognize Text (OCR)…", self,
                               toolTip="Make scanned pages searchable and selectable",
                               triggered=lambda: self.recognize_text())
        self.act_letter_shortcuts = QAction("Single-Letter Tool Shortcuts", self, checkable=True,
                                            triggered=self._toggle_letter_shortcuts)

        self.act_copy = QAction("&Copy", self, shortcut=QKeySequence.Copy, triggered=self.copy_text)
        self.act_copy.setEnabled(False)
        self.act_select_all = QAction("Select &All", self, shortcut=QKeySequence.SelectAll,
                                      triggered=self.select_all)
        self.act_find = QAction("&Find…", self, shortcut=QKeySequence.Find,
                                triggered=lambda: self.find_bar.open())
        self.act_find_next = QAction("Find &Next", self, shortcut=QKeySequence("F3"),
                                     triggered=lambda: self.find_bar.find_next())
        self.act_find_prev = QAction("Find Pre&vious", self, shortcut=QKeySequence("Shift+F3"),
                                     triggered=lambda: self.find_bar.find_previous())
        self.act_delete_annot = QAction("Delete Annotation", self,
                                        triggered=lambda: self.view.delete_selected_annot())

        self.act_next = QAction("Next Page", self, shortcut=QKeySequence("PgDown"),
                                triggered=lambda: self.view.next_page())
        self.act_prev = QAction("Previous Page", self, shortcut=QKeySequence("PgUp"),
                                triggered=lambda: self.view.previous_page())
        self.act_first = QAction("First Page", self, shortcut=QKeySequence("Ctrl+Home"),
                                 triggered=lambda: self.view.scroll_to_page(0))
        self.act_last = QAction("Last Page", self, shortcut=QKeySequence("Ctrl+End"),
                                triggered=self._goto_last)
        self.act_goto = QAction("&Go to Page…", self, shortcut=QKeySequence("Ctrl+G"),
                                triggered=self._goto_dialog)
        # Tabs: Ctrl+Tab as in browsers, and Ctrl+PgDn/PgUp (PgDn alone is
        # the next page).
        self.act_next_tab = QAction("Next Tab", self, triggered=lambda: self.next_tab(1))
        self.act_next_tab.setShortcuts([QKeySequence("Ctrl+Tab"), QKeySequence("Ctrl+PgDown")])
        self.act_prev_tab = QAction("Previous Tab", self, triggered=lambda: self.next_tab(-1))
        self.act_prev_tab.setShortcuts([QKeySequence("Ctrl+Shift+Tab"),
                                        QKeySequence("Ctrl+PgUp")])

    def _build_toolbar(self) -> None:
        bar = QToolBar("Main", self)
        bar.setMovable(False)
        bar.setIconSize(bar.iconSize())
        self.addToolBar(bar)

        bar.addAction(self.act_open)
        bar.addAction(self.act_save)
        bar.addAction(self.act_print)
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

        tools = QToolBar("Tools", self)
        tools.setMovable(False)
        self.addToolBar(tools)
        tools.addAction(self.act_tool_select)
        tools.addAction(self.act_tool_pan)
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
        self.sign_button = QToolButton(self, text="Sign")
        self.sign_button.setMenu(self.sign_menu)
        self.sign_button.setPopupMode(QToolButton.InstantPopup)
        tools.addWidget(self.sign_button)

    def _build_menus(self) -> None:
        file_menu = self.menuBar().addMenu("&File")
        file_menu.addAction(self.act_open)
        file_menu.addAction(self.act_save)
        file_menu.addAction(self.act_save_as)
        file_menu.addAction(self.act_protect)
        file_menu.addSeparator()
        file_menu.addAction(self.act_print)
        file_menu.addSeparator()
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
        edit_menu.addAction(self.act_find)
        edit_menu.addAction(self.act_find_next)
        edit_menu.addAction(self.act_find_prev)
        edit_menu.addSeparator()
        edit_menu.addAction(self.act_delete_annot)

        tools_menu = self.menuBar().addMenu("&Tools")
        tools_menu.addAction(self.act_tool_select)
        tools_menu.addAction(self.act_tool_pan)
        tools_menu.addMenu(self.annotate_menu)
        tools_menu.addMenu(self.form_menu)
        tools_menu.addMenu(self.sign_menu)
        tools_menu.addSeparator()
        tools_menu.addAction(self.act_ocr)
        tools_menu.addSeparator()
        tools_menu.addAction(self.act_letter_shortcuts)

        view_menu = self.menuBar().addMenu("&View")
        view_menu.addAction(self.act_zoom_in)
        view_menu.addAction(self.act_zoom_out)
        view_menu.addAction(self.act_zoom_100)
        view_menu.addSeparator()
        view_menu.addAction(self.act_fit_width)
        view_menu.addAction(self.act_fit_page)
        view_menu.addSeparator()
        toggle = self.sidebar_dock.toggleViewAction()
        toggle.setText("Page &Thumbnails")
        toggle.setShortcut(QKeySequence("F4"))
        view_menu.addAction(toggle)
        view_menu.addSeparator()
        view_menu.addAction(self.act_next_tab)
        view_menu.addAction(self.act_prev_tab)

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
        for act in (self.act_close, self.act_save_as, self.act_protect, self.act_print,
                    self.act_ocr, self.act_zoom_in,
                    self.act_zoom_out,
                    self.act_zoom_100, self.act_fit_width, self.act_fit_page,
                    self.act_page_rot_ccw,
                    self.act_page_rot_cw, self.act_page_up, self.act_page_down,
                    self.act_page_delete, self.act_page_extract, self.act_page_insert,
                    *self.tool_actions.values(), self.act_delete_annot, self.act_select_all,
                    self.act_find, self.act_find_next, self.act_find_prev,
                    self.act_next,
                    self.act_prev, self.act_first,
                    self.act_last, self.act_goto):
            act.setEnabled(enabled)
        self.page_spin.setEnabled(enabled)
        self.zoom_box.setEnabled(enabled)
        self._refresh_edit_state()
        self._apply_access()

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

    # ---- page edits (continued) ----------------------------------------

    def _edit(self, pages: PageList, label: str, current: int, select: list[int]) -> None:
        if self.history is None or not self._allowed(self.access.assemble, "changing its pages"):
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
        text_changed = state.ocr != doc.ocr
        doc.pages = state.pages
        doc.annots = state.annots
        doc.fields = state.fields
        doc.forms = state.forms
        doc.ocr = state.ocr
        if text_changed:
            self.view.text_changed()
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
        if self.history is None or not self._allowed(self.access.annotate,
                                                     "adding or changing comments and marks"):
            return
        current = self.history.current
        state = replace(current, annots=current.annots.with_page(key, annots))
        if state == current:
            return
        self.history.push(state, label)
        self._show_state(state, self.view.current_page)

    def _on_fields_edit(self, key, value: str, label: str) -> None:
        if self.history is None or not self._allowed(self.access.fill_forms,
                                                     "filling in its forms"):
            return
        current = self.history.current
        state = replace(current, fields=current.fields.with_value(key, value))
        if state == current:
            return
        self.history.push(state, label)
        self._show_state(state, self.view.current_page)

    def _on_forms_edit(self, key, widgets, label: str, rename=None) -> None:
        if self.history is None or not self._allowed(self.access.modify,
                                                     "changing its form fields"):
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

    def _apply_letter_shortcuts(self) -> None:
        """Give the tool actions their letter keys, or take them away --
        which also hides the letters from the menus."""
        choice = self.settings.letter_shortcuts()
        for act, keys in self.letter_actions.items():
            act.setShortcut(keys if choice.enabled else QKeySequence())
        self.act_letter_shortcuts.setChecked(choice.enabled)
        self.act_letter_shortcuts.setEnabled(not choice.locked)
        self.act_letter_shortcuts.setText(
            "Single-Letter Tool Shortcuts" + (" (set by administrator)" if choice.locked else ""))

    def _toggle_letter_shortcuts(self, enabled: bool) -> None:
        try:
            self.settings.set_letter_shortcuts(enabled)
        except PermissionError:
            pass  # locked by policy; the action is disabled anyway
        self._apply_letter_shortcuts()
        if enabled:
            self.statusBar().showMessage(
                "Letter shortcuts on: V Select, N Note, H Highlight, U Underline, "
                "S Strikethrough, T Typewriter, R E L A P shapes and pen", 10000)

    def _on_tool_changed(self, tool: Tool) -> None:
        act = self.tool_actions[tool]
        act.setChecked(True)
        name = act.text().replace("&", "")
        self.annotate_button.setText(f"Annotate: {name}" if tool in self.annotate_tools
                                     else "Annotate")
        self.form_button.setText(f"Form: {name}" if tool in self.form_tools else "Form")
        self.sign_button.setText({Tool.SIGN: "Sign: Certificate",
                                  Tool.SIGNATURE: "Sign: Picture"}.get(tool, "Sign"))

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
        self._show_text_style()
        if self.view.tool is not Tool.TYPEWRITER:
            self.view.set_tool(Tool.TYPEWRITER)

    def _show_text_style(self) -> None:
        """Menus follow the current text style, however it was picked (menu
        or the style bar over the typewriter)."""
        self.act_tool_typewriter.setIcon(self._swatch(self.view.text_color))
        for act in self.text_size_actions.actions():
            act.setChecked(act.text() == f"{self.view.text_size:g} pt")

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
            self._apply_access()

    def _set_highlight_color(self, color) -> None:
        self.view.highlight_color = color
        self.act_tool_highlight.setIcon(self._swatch(color))
        self.view.set_tool(Tool.HIGHLIGHT)

    # ---- text ----------------------------------------------------------

    def copy_text(self) -> None:
        text = self.view.selected_text()
        if text and self._allowed(self.access.copy, "copying its text"):
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
        self._update_tabs()  # the unsaved-changes dot on the tab

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
        menu.addAction(f"Recognize Text on {what}", lambda: self.recognize_text(indices))
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

    # ---- signal handlers -----------------------------------------------

    def _on_document_opened(self, document: Optional[PdfDocument]) -> None:
        """The current tab opened (or closed) a document: set the tab up, then
        the window's chrome to match."""
        self.sidebar.set_document(document)
        self.view.set_tool(Tool.SELECT)
        if document is None:
            self.history = None
            self._check_signatures(None)
            self._sync_chrome()
            return
        if not self._keep_history:
            self.history = History(DocState(document.pages))
        self._check_signatures(document)
        self._sync_chrome()
        if not any(document.has_text(i) for i in range(min(3, document.page_count))):
            self.statusBar().showMessage(
                "This PDF looks scanned (its pages are pictures, with no text). "
                "Tools > Recognize Text (OCR) makes it searchable.", 15000)

    def _sync_chrome(self) -> None:
        """Title, page counter, zoom box, status bar, tool buttons and which
        actions are on -- all from the current tab. On opening a document and
        on switching tabs."""
        document = self.view.document
        self._set_document_actions_enabled(document is not None)
        self._on_tool_changed(self.view.tool)
        self._show_text_style()
        self.act_tool_highlight.setIcon(self._swatch(self.view.highlight_color))
        self.act_copy.setEnabled(bool(self.view.selected_text()) and self.access.copy)
        self._update_tabs()
        if document is None:
            self.setWindowTitle(APP_NAME)
            self.setWindowModified(False)
            self.page_spin.setRange(1, 1)
            self.page_total.setText(" of 0 ")
            self.status_left.setText("No document")
            self.status_right.setText("")
            return
        # [*] is where Qt draws the unsaved-changes marker (setWindowModified).
        self.setWindowTitle(f"{document.path.name}[*] — {APP_NAME}")
        self._refresh_page_count()
        self._on_zoom_changed(self.view.scale)
        forms = "  ·  has form fields" if document.has_forms else ""
        if document.is_xfa:
            forms = ("  ·  XFA form: its fields can't be shown or filled here "
                     "(same as Chrome/Edge) -- open it in Acrobat")
        if document.form_error():
            forms = f"  ·  its form fields couldn't be loaded ({document.form_error()})"
        self.status_left.setText(f"{document.path}{forms}")

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
        pdfs = [url.toLocalFile() for url in event.mimeData().urls()
                if url.isLocalFile() and url.toLocalFile().lower().endswith(".pdf")]
        if pdfs:
            event.acceptProposedAction()
            for path in pdfs:  # each in its own tab
                self.open_path(path)

    def closeEvent(self, event) -> None:
        # Every tab with unsaved changes asks, shown as it asks; Cancel on
        # any keeps the window open with that tab in front.
        for tab in self.tab_list():
            if tab.dirty or tab.view.is_editing():
                self.tabs.setCurrentWidget(tab)
                if not self._confirm_discard():
                    event.ignore()
                    return
        for tab in self.tab_list():
            tab.view.set_document(None)
        super().closeEvent(event)
