"""Page thumbnail sidebar: selection, drag-to-reorder, lazy rendering.

The sidebar never edits anything itself. Drops and key presses are reported
as signals; MainWindow turns them into PageList edits and calls set_document
again. One source of truth, so the sidebar can't disagree with the document.

Thumbnails show the page as it will be saved -- intrinsic plus edit rotation,
but not the view rotation, which is a display preference of the main view.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from PySide6.QtCore import QItemSelectionModel, QPoint, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QIcon, QImage, QKeySequence, QPainter, QPen, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QListWidget,
    QListWidgetItem,
    QStyledItemDelegate,
    QStyleOptionViewItem,
)

from .document import PdfDocument
from .pagelist import PageRef
from .rendering import PageRenderer

THUMB_W, THUMB_H = 120, 150   # logical px box each thumbnail is fitted into
CACHE_LIMIT = 200             # thumbnails kept; ~70KB each at 1x, ~280KB at 2x
RENDER_BUDGET_S = 0.030       # per timer tick, so scrolling stays smooth


class _Delegate(QStyledItemDelegate):
    """Thumbnail above its page number, centred -- ListMode's default is beside."""

    def initStyleOption(self, option: QStyleOptionViewItem, index) -> None:
        super().initStyleOption(option, index)
        option.decorationPosition = QStyleOptionViewItem.Top
        option.decorationAlignment = Qt.AlignHCenter
        option.displayAlignment = Qt.AlignHCenter

    def sizeHint(self, option, index) -> QSize:
        return QSize(THUMB_W + 24, THUMB_H + 30)


class ThumbnailSidebar(QListWidget):
    page_activated = Signal(int)            # user picked a page to look at
    move_requested = Signal(list, int)      # (selected rows, insert-before row)
    delete_requested = Signal(list)         # selected rows
    context_requested = Signal(list, object)  # (selected rows, global pos)
    files_dropped = Signal(list, int)       # (PDF paths from outside, insert-before row)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.document: Optional[PdfDocument] = None
        self._renderer: Optional[PageRenderer] = None
        self._cache: "OrderedDict[Tuple[PageRef, int], QPixmap]" = OrderedDict()
        self._errors: dict[PageRef, str] = {}

        self.setViewMode(QListWidget.ListMode)
        self.setFlow(QListWidget.TopToBottom)
        self.setIconSize(QSize(THUMB_W, THUMB_H))
        self.setItemDelegate(_Delegate(self))
        self.setUniformItemSizes(True)
        self.setSpacing(4)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        # Reordering is a hand-rolled drag (see "reorder drag" below), not Qt
        # drag-and-drop: on Windows QDrag runs inside OLE's DoDragDrop loop,
        # which swallows wheel events, so you can't scroll mid-drag.
        self.setDragDropMode(QAbstractItemView.NoDragDrop)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        # QListView defaults to scrolling a whole item per step: at 180px
        # thumbnails, edge autoscroll lurches a page at a time and the wheel
        # jumps several pages per notch.
        self.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.verticalScrollBar().setSingleStep(24)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(
            lambda pos: self.context_requested.emit(
                self.selected_rows(), self.viewport().mapToGlobal(pos)))
        self.setMinimumWidth(THUMB_W + 48)

        self.currentRowChanged.connect(self._on_current_row)
        self.verticalScrollBar().valueChanged.connect(self._schedule)

        # Scoped to the sidebar: Delete in the page view must not delete pages.
        delete = QShortcut(QKeySequence.Delete, self, context=Qt.WidgetShortcut)
        delete.activated.connect(self._on_delete_key)

        self._timer = QTimer(self, singleShot=True, interval=0)
        self._timer.timeout.connect(self._render_visible)
        self._following = False

        self._press_pos: Optional[QPoint] = None   # viewport coords of LMB press
        self._press_row = -1
        self._deferred_select = -1   # row to single-select on release if no drag
        self._drag_rows: Optional[List[int]] = None
        self._drag_pos = QPoint()    # last cursor position, viewport coords
        self._drop_before = -1
        self._autoscroll = QTimer(self, interval=16)
        self._autoscroll.timeout.connect(self._autoscroll_tick)
        # Files dragged in from outside (Explorer). This does go through Qt's
        # drag-and-drop -- it has to -- so the wheel is dead during it, but
        # the edge autoscroll below still works.
        self._external_drop = False
        self.setAcceptDrops(True)

    # ---- content -------------------------------------------------------

    def set_document(self, document: Optional[PdfDocument]) -> None:
        """Rebuild the list. Call again after every page edit."""
        if document is not self.document:
            self._cache.clear()
            self._errors.clear()
        self.document = document
        # No caching inside the renderer: its keys are displayed indices,
        # which go stale on every edit. Ours are keyed by PageRef.
        self._renderer = PageRenderer(document, max_pages=0) if document else None

        self.blockSignals(True)
        self.clear()
        if document is not None:
            for i in range(document.page_count):
                item = QListWidgetItem(str(i + 1))
                item.setTextAlignment(Qt.AlignHCenter)
                pixmap = self._cached(i)
                item.setIcon(QIcon(pixmap if pixmap is not None else self._slot(i)))
                self._decorate_error(item, i)
                self.addItem(item)
        self.blockSignals(False)
        self._schedule()

    def selected_rows(self) -> List[int]:
        return sorted(self.row(item) for item in self.selectedItems())

    def select_rows(self, rows: Sequence[int]) -> None:
        rows = [r for r in rows if 0 <= r < self.count()]
        if not rows:
            return
        self.blockSignals(True)
        self.clearSelection()
        self.setCurrentRow(rows[0], QItemSelectionModel.NoUpdate)
        for r in rows:
            self.item(r).setSelected(True)
        self.blockSignals(False)
        self.scrollToItem(self.item(rows[0]))

    def follow_page(self, index: int) -> None:
        """The main view scrolled to `index`. Track it unless the user has
        a multi-page selection going, which scrolling must not destroy."""
        if self._following or len(self.selectedItems()) > 1 or not 0 <= index < self.count():
            return
        self._following = True
        try:
            self.setCurrentRow(index)
            self.scrollToItem(self.item(index))
        finally:
            self._following = False

    # ---- signals -------------------------------------------------------

    def _on_current_row(self, row: int) -> None:
        if row < 0 or self._following:
            return
        # Guard the echo too: near the end of a document the view can't scroll
        # the clicked page to the top, reports a neighbour as current, and
        # follow_page would move the selection off what the user clicked.
        self._following = True
        try:
            self.page_activated.emit(row)
        finally:
            self._following = False

    def _on_delete_key(self) -> None:
        rows = self.selected_rows()
        if rows:
            self.delete_requested.emit(rows)

    # ---- reorder drag --------------------------------------------------
    #
    # Press, move past the drag threshold, release. The mouse is implicitly
    # grabbed while the button is down, so moves keep arriving even outside
    # the sidebar, and the wheel keeps working because we never leave the
    # normal event loop.

    def mousePressEvent(self, event) -> None:
        self._cancel_drag()
        if event.button() == Qt.LeftButton:
            pos = event.position().toPoint()
            index = self.indexAt(pos)
            self._press_pos, self._press_row = pos, index.row() if index.isValid() else -1
            # Plain press on a page that's already part of a multi-selection:
            # the user may be about to drag the whole selection, so don't
            # collapse it to one page until we know it was just a click.
            if (index.isValid() and event.modifiers() == Qt.NoModifier
                    and self.item(index.row()).isSelected() and len(self.selectedItems()) > 1):
                self._deferred_select = index.row()
                self.setFocus()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        pos = event.position().toPoint()
        if self._drag_rows is not None:
            self._drag_pos = pos
            self._update_drop()
            return
        if (event.buttons() & Qt.LeftButton and self._press_pos is not None
                and self._press_row >= 0
                and (pos - self._press_pos).manhattanLength() >= QApplication.startDragDistance()):
            self._start_drag(pos)
            return
        if self._deferred_select >= 0:
            return  # don't let a tiny wobble rubber-band the selection away
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.LeftButton and self._drag_rows is not None:
            rows, before = self._drag_rows, self._drop_before
            self._cancel_drag()
            if before >= 0:
                # Deferred so the rebuild doesn't delete items while Qt is
                # still dispatching this event to them.
                QTimer.singleShot(0, lambda: self.move_requested.emit(rows, before))
            return
        if event.button() == Qt.LeftButton and self._deferred_select >= 0:
            row = self._deferred_select
            self._deferred_select = -1
            self._press_pos = None
            self.clearSelection()
            self.setCurrentRow(row)  # a plain click after all
            return
        self._press_pos = None
        super().mouseReleaseEvent(event)

    def wheelEvent(self, event) -> None:
        # Holding a page and scrolling means carrying it somewhere. Without
        # this, the base view is still drag-selecting from the press and
        # selects every page scrolled past -- a surprise, and one Delete away
        # from losing a lot of pages.
        if (self._drag_rows is None and self._press_row >= 0
                and self._press_pos is not None and event.buttons() & Qt.LeftButton):
            self._start_drag(self._press_pos)
        super().wheelEvent(event)
        if self._drag_rows is not None:
            self._update_drop()  # content moved under a still cursor

    def keyPressEvent(self, event) -> None:
        if self._drag_rows is not None and event.key() == Qt.Key_Escape:
            self._cancel_drag()
            return
        super().keyPressEvent(event)

    def focusOutEvent(self, event) -> None:
        self._cancel_drag()  # alt-tab mid-drag must not leave a stuck drag
        super().focusOutEvent(event)

    def _start_drag(self, pos: QPoint) -> None:
        if not self.item(self._press_row).isSelected():
            self.clearSelection()
            self.setCurrentRow(self._press_row)
        self._deferred_select = -1
        # The press put the base view into drag-selecting; it won't see the
        # release, so clear that or later hovers keep extending the selection.
        self.setState(QAbstractItemView.NoState)
        self._drag_rows = self.selected_rows()
        self._drag_pos = pos
        self.viewport().setCursor(Qt.ClosedHandCursor)
        self._autoscroll.start()
        self._update_drop()

    def _cancel_drag(self) -> None:
        was_dragging = self._drag_rows is not None
        self._drag_rows = None
        self._drop_before = -1
        self._press_pos = None
        self._deferred_select = -1
        self._autoscroll.stop()
        if was_dragging:
            self.viewport().unsetCursor()
            self.viewport().update()

    # ---- files dropped from outside -------------------------------------

    @staticmethod
    def _pdf_paths(event) -> List[Path]:
        mime = event.mimeData()
        if not mime.hasUrls():
            return []
        return [Path(u.toLocalFile()) for u in mime.urls()
                if u.isLocalFile() and u.toLocalFile().lower().endswith(".pdf")]

    def dragEnterEvent(self, event) -> None:
        # With nothing open there's nothing to insert into: ignore, and the
        # main window takes the drop and opens the file instead.
        if self.document is None or not self._pdf_paths(event):
            event.ignore()
            return
        event.acceptProposedAction()
        self._external_drop = True
        self._drag_pos = event.position().toPoint()
        self._autoscroll.start()
        self._update_drop()

    def dragMoveEvent(self, event) -> None:
        if not self._external_drop:
            event.ignore()
            return
        event.acceptProposedAction()
        self._drag_pos = event.position().toPoint()
        self._update_drop()

    def dragLeaveEvent(self, event) -> None:
        self._end_external_drop()

    def dropEvent(self, event) -> None:
        paths = self._pdf_paths(event) if self._external_drop else []
        before = self._drop_before
        self._end_external_drop()
        if not paths:
            event.ignore()
            return
        event.acceptProposedAction()
        # Deferred: inserting may prompt for a password, and a modal dialog
        # inside the OS drag loop leaves Explorer waiting on us.
        QTimer.singleShot(0, lambda: self.files_dropped.emit(paths, before))

    def _end_external_drop(self) -> None:
        self._external_drop = False
        self._drop_before = -1
        self._autoscroll.stop()
        self.viewport().update()

    def _dragging(self) -> bool:
        return self._drag_rows is not None or self._external_drop

    def drop_row_at(self, y: int) -> int:
        """Insert-before row for a cursor at viewport height y: the number of
        pages whose middle is above it. Gaps and past-the-end fall out right."""
        before = 0
        for row in range(self.count()):
            if self.visualItemRect(self.item(row)).center().y() < y:
                before = row + 1
            else:
                break
        return before

    def _update_drop(self) -> None:
        self._drop_before = self.drop_row_at(self._drag_pos.y())
        self.viewport().update()

    def _autoscroll_tick(self) -> None:
        """Scroll while the cursor is near or beyond the top/bottom edge,
        faster the further in. Past the edge counts as full speed."""
        if not self._dragging():
            self._autoscroll.stop()
            return
        edge = min(60, self.viewport().height() // 4)
        y, h = self._drag_pos.y(), self.viewport().height()
        if y < edge:
            depth = edge - y
        elif y > h - edge:
            depth = -(y - (h - edge))
        else:
            return
        speed = max(2, min(40, abs(depth) * 40 // max(1, edge)))
        bar = self.verticalScrollBar()
        before = bar.value()
        bar.setValue(before - speed if depth > 0 else before + speed)
        if bar.value() != before:
            self._update_drop()

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        if not self._dragging() or self._drop_before < 0 or self.count() == 0:
            return
        if self._drop_before < self.count():
            y = self.visualItemRect(self.item(self._drop_before)).top() - self.spacing()
        else:
            y = self.visualItemRect(self.item(self.count() - 1)).bottom() + self.spacing()
        painter = QPainter(self.viewport())
        painter.setPen(QPen(self.palette().highlight().color(), 3))
        painter.drawLine(6, y, self.viewport().width() - 6, y)
        painter.end()

    # ---- lazy rendering ------------------------------------------------

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._schedule()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._schedule()

    def _schedule(self, *_):
        if self.document is not None and self.isVisible():
            self._timer.start()

    def _key(self, index: int) -> Tuple[PageRef, int]:
        return (self.document.pages[index], int(round(self.devicePixelRatioF() * 100)))

    def _cached(self, index: int) -> Optional[QPixmap]:
        pixmap = self._cache.get(self._key(index))
        if pixmap is not None:
            self._cache.move_to_end(self._key(index))
        return pixmap

    def _fit_scale(self, index: int) -> float:
        w, h = self.document.transform(index).display_size_pt
        return min(THUMB_W / w, THUMB_H / h)

    def _slot(self, index: int, image: Optional[QImage] = None, error: bool = False) -> QPixmap:
        """The page drawn into a fixed THUMB_W x THUMB_H slot.

        Bottom-aligned so the page number sits right under every page, not
        floating below a landscape sheet's empty space. With no image it's a
        blank page of the right shape, so nothing reflows as thumbnails fill in.
        """
        dpr = self.devicePixelRatioF()
        slot = QPixmap(round(THUMB_W * dpr), round(THUMB_H * dpr))
        slot.setDevicePixelRatio(dpr)
        slot.fill(Qt.transparent)
        try:
            w, h = self.document.transform(index, self._fit_scale(index)).bitmap_size
        except Exception:
            w, h = THUMB_W, THUMB_H
        rect = QRect((THUMB_W - w) // 2, THUMB_H - h, w, h)

        painter = QPainter(slot)
        if image is not None:
            image.setDevicePixelRatio(dpr)
            painter.drawImage(rect.topLeft(), image)
        else:
            painter.fillRect(rect, QColor("#f4f4f4"))
        if error:
            painter.setPen(QPen(QColor("#b00020"), 3))
            painter.drawRect(rect.adjusted(1, 1, -2, -2))
            painter.drawText(rect, Qt.AlignCenter, "Render\nfailed")
        else:
            painter.setPen(QPen(QColor(0, 0, 0, 70)))
            painter.drawRect(rect.adjusted(0, 0, -1, -1))
        painter.end()
        return slot

    def _render_visible(self) -> None:
        if self.document is None:
            return
        view = self.viewport().rect()
        started = time.perf_counter()
        for row in range(self.count()):
            item = self.item(row)
            if not self.visualItemRect(item).intersects(view):
                continue
            if self._cached(row) is not None or self.document.pages[row] in self._errors:
                continue
            item.setIcon(QIcon(self._render(row)))
            self._decorate_error(item, row)
            if time.perf_counter() - started > RENDER_BUDGET_S:
                self._timer.start()  # more to do; let the UI breathe first
                return

    def _render(self, index: int) -> QPixmap:
        dpr = self.devicePixelRatioF()
        try:
            image = self._renderer.image(index, self._fit_scale(index) * dpr)
        except Exception as exc:  # isolate: one bad page gets a marked thumbnail
            self._errors[self.document.pages[index]] = str(exc)
            return self._slot(index, error=True)
        pixmap = self._slot(index, image)
        self._cache[self._key(index)] = pixmap
        while len(self._cache) > CACHE_LIMIT:
            self._cache.popitem(last=False)
        return pixmap

    def _decorate_error(self, item: QListWidgetItem, index: int) -> None:
        error = self._errors.get(self.document.pages[index])
        if error is not None:
            item.setIcon(QIcon(self._slot(index, error=True)))
            item.setToolTip(f"Page {index + 1} failed to render:\n{error}")
