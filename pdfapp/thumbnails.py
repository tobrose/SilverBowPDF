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
from typing import List, Optional, Sequence, Tuple

from PySide6.QtCore import QItemSelectionModel, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QIcon, QImage, QKeySequence, QPainter, QPen, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
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
        self.setDragDropMode(QAbstractItemView.InternalMove)
        self.setDefaultDropAction(Qt.MoveAction)
        self.setDropIndicatorShown(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
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

    def dropEvent(self, event) -> None:
        if event.source() is not self:
            event.ignore()
            return
        rows = self.selected_rows()
        target = self.indexAt(event.position().toPoint())
        if not target.isValid():
            before = self.count()
        else:
            rect = self.visualRect(target)
            below = event.position().y() > rect.center().y()
            before = target.row() + (1 if below else 0)

        # IgnoreAction, not Move: on a Move, Qt's drag code removes the
        # "moved" rows itself afterwards, fighting the rebuild below.
        event.setDropAction(Qt.IgnoreAction)
        event.accept()
        if rows:
            # Deferred: the rebuild deletes items the drag machinery is still
            # holding until this handler returns.
            QTimer.singleShot(0, lambda: self.move_requested.emit(rows, before))

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
