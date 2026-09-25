# PDF Workbench

Desktop PDF viewer and editor. Python 3.11+ / PySide6 / pdfium.

Built by a solo developer with no code reviewer and no rollback buddy. Assume
every change ships straight to the only user who matters. Prefer loud failures
over clever recovery.

## Commands

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

python -m pdfapp                  # run
python -m pdfapp file.pdf         # run with a file
python tests/test_transform.py    # tests, no pytest needed
pytest                            # tests, if pytest is installed
```

## Architecture

```
pdfapp/
  transform.py   PageTransform. All coordinate math. LOAD-BEARING.
  document.py    pypdfium2 wrapper. NO Qt imports -- must stay headless-testable.
  rendering.py   pdfium bitmap -> QImage, plus an LRU page cache.
  pageview.py    Continuous scroll view, zoom, view rotation.
  mainwindow.py  Menus, toolbar, status bar, drag-and-drop.
packaging/       PyInstaller spec, Inno Setup script, build script.
```

Dependency direction is one-way: `transform` <- `document` <- `rendering` <-
`pageview` <- `mainwindow`. Nothing lower imports anything higher.

## Hard rules

**1. Never change `transform.py` without running the tests.**

`tests/test_transform.py` validates `PageTransform` point-by-point against
pdfium's own `PdfPosConv` across 11 configurations -- offset CropBoxes,
negative-origin MediaBoxes, and intrinsic `/Rotate` stacked on view rotation.
Three coordinate systems and four rotation values means twelve ways to be
subtly wrong, and every feature after the viewer depends on this being right.
A regression here does not crash; it puts annotations two inches from where
the user clicked. If you touch this file and the tests do not pass, you have
broken the application. Do not adjust the tests to match new behaviour unless
the old behaviour was provably wrong.

**2. `document.py` imports no Qt.** It is the layer that runs in CI and in a
headless sandbox. Keep it that way.

**3. Two transforms on HiDPI, not one.** `PageWidget.render_scale()` is
`scale x devicePixelRatio` and feeds the renderer. `PageWidget.widget_transform()`
is plain `scale` and is the only one valid for mouse input. Using the render
transform for hit-testing produces clicks that land at half or double position
on Retina/scaled displays and nowhere else -- a bug that is invisible on the
dev machine.

**4. `PageRenderer.image()` raises if pdfium's bitmap size disagrees with
`PageTransform`'s prediction.** That check is deliberate. Do not soften it to a
warning or a resize.

**5. Never silently swallow a pdfium exception.** `PageWidget.paintEvent` draws
the error onto the failed page so one corrupt page does not take down the
window. Follow that pattern: isolate, display, don't hide.

## Conventions

- Type hints on public functions. `from __future__ import annotations` at the top.
- Comments explain *why*, especially where something looks wrong but isn't
  (the two-transform HiDPI split, the `.copy()` in `bitmap_to_qimage`, ceil in
  `bitmap_size`). Don't add comments that restate the code.
- No new runtime dependencies without asking. The dependency list is short on
  purpose: PySide6, pypdfium2, pikepdf. Each one is ~50-200MB in the installer.
- Rendering is read-only via pdfium. Document mutation goes through pikepdf.
  Don't mix the two on one code path.

## Roadmap

Built: open (incl. encrypted), continuous scroll, zoom to cursor, fit
width/page, view rotation, navigation, form-field rendering, drag-and-drop,
per-page error isolation.

Next, in order: page operations (rotate/delete/reorder, saved via pikepdf),
merge/split, annotations, form filling, encryption, packaging.

The command/undo stack lands with page operations, not before -- a viewer has
nothing to undo.

## Known limits (don't "fix" these without discussion)

- Rendering is synchronous. Fine for text; a 600 DPI scan at 400% hitches.
  The planned fix is moving `PageRenderer` onto a `QThreadPool`; the interface
  is already shaped so that swap doesn't touch the widgets.
- One widget per page. Cheap to a few thousand pages, then it isn't. The fix
  is a virtualised canvas, deliberately deferred.
- Fit-width uses the widest page in the document, not the current page, so the
  layout doesn't jump when scrolling past a landscape sheet.
- `Format_ARGB32`/`Format_RGB32` assume little-endian. There's an endian-safe
  fallback in `rendering.py`.
