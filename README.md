# PDF Workbench

A desktop PDF viewer and (eventually) editor. Python + PySide6 + pdfium.

## Run it

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m pdfapp                # or: python -m pdfapp somefile.pdf
```

## Test it

```bash
python tests/test_transform.py   # no pytest needed
# or
pytest tests/
```

The transform tests need `pikepdf` and `pypdfium2` but not PySide6, so they run
on a headless box.

## Shortcuts

| | |
|---|---|
| `Ctrl+O` / `Ctrl+W` | Open / close |
| `Ctrl++` `Ctrl+-` `Ctrl+0` | Zoom in / out / 100% |
| `Ctrl+1` `Ctrl+2` | Fit width / fit page |
| `Ctrl+L` `Ctrl+R` | Rotate view left / right |
| `PgUp` `PgDn` `Ctrl+Home` `Ctrl+End` `Ctrl+G` | Navigation |
| `Ctrl` + scroll | Zoom at cursor |

Drag a PDF onto the window to open it.

## Layout

```
pdfapp/
  transform.py   PageTransform -- all coordinate math. Start here.
  document.py    pypdfium2 wrapper. No Qt imports, testable headless.
  rendering.py   pdfium bitmap -> QImage, plus an LRU page cache.
  pageview.py    Continuous scrolling view, zoom, view rotation.
  mainwindow.py  Menus, toolbar, status bar, drag-and-drop.
tests/
  test_transform.py
```

`transform.py` is the load-bearing file. Three coordinate systems (PDF user
space, bitmap space, widget space) and four rotation values means twelve ways
to be subtly wrong, and every feature after the viewer — annotations, text
overlays, form filling — depends on it being right. It is validated point by
point against pdfium's own `PdfPosConv`, including offset CropBoxes and
combined intrinsic-plus-view rotation, so a regression fails a test instead of
putting a highlight two inches from where the user clicked.

## Status

Working: open (including encrypted, with a password prompt), continuous scroll,
zoom to cursor, fit width/page, view rotation, page navigation, form-field
rendering, drag-and-drop, per-page error isolation.

Not built yet, in intended order: page operations (rotate/delete/reorder,
saved via pikepdf), merge/split, annotations, form filling, encryption,
packaging.

## Known limits

- **Rendering is synchronous.** Fine for text pages; a 600 DPI scan at 400%
  will hitch the UI for a beat. Next structural change is moving `PageRenderer`
  onto a `QThreadPool` — the interface is shaped so that swap doesn't touch the
  widgets.
- **One widget per page.** Cheap up to a few thousand pages, then it isn't.
  The fix is a single virtualised canvas, not worth it yet.
- **Fit-width uses the widest page in the document,** not the current page, so
  the layout doesn't jump when you scroll past a landscape sheet. Costs some
  screen space in mixed-orientation files.
- **`Format_ARGB32` / `Format_RGB32` assume a little-endian host.** True on
  every desktop that matters; there is an endian-safe fallback in
  `rendering.py` anyway.
