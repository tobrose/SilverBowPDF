# SilverbowPDF

A desktop PDF viewer and editor for Windows. Python + PySide6, with pdfium
for rendering and pikepdf (qpdf) for saving.

- **Pages:** rotate, reorder, delete, insert from other PDFs, extract to a new file.
- **Markup:** highlight / underline / strikethrough, typewriter text, notes,
  shapes, freehand pen, stamps.
- **Forms:** fill in fields, and create text fields, checkboxes and dropdowns.
- **Scans:** make scanned pages searchable with Windows' built-in OCR.
- **Find, print** (with preview), **password protection** (AES-256), and
  respect for the restrictions a PDF's author set.
- Built to be deployed: a single-click installer with silent install, an
  administrator policy, and a `--self-test` for checking a build on the
  machine it's installed on (see [docs/PACKAGING.md](docs/PACKAGING.md)).

Everything is undoable until you save, and what you see is what's saved:
the tests compare the on-screen preview with the saved file, pixel for pixel.

## Run it

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
python -m pdfapp                # or: python -m pdfapp somefile.pdf
```

## Test it

```bash
python tests/test_transform.py   # no pytest needed
python tests/test_pages.py
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
| `F4` | Show / hide page thumbnails |
| `Ctrl+Shift+L` `Ctrl+Shift+R` | Rotate selected pages left / right (saved) |
| `Alt+Up` `Alt+Down` | Move selected pages earlier / later |
| `Ctrl+Shift+D`, or `Delete` in the sidebar | Delete selected pages |
| `Ctrl+Shift+E` | Extract selected pages to a new PDF |
| `Ctrl+Shift+I`, or drop PDFs on the sidebar | Insert pages from other PDFs after the selection |
| Drag thumbnails | Reorder pages (Ctrl/Shift+click to select several) |
| `Ctrl+Z` `Ctrl+Y` | Undo / redo |
| `Ctrl+S` `Ctrl+Shift+S` | Save / save as |
| File > Password Protection | Add, change or remove a password to open the document (AES-256), applied on save. Protected files stay protected when saved |
| `Ctrl+P` | Print preview, then print what's on screen (edits included, no field tint): all, a range, the current page or the sidebar selection |
| Right-click a page | Page operations for that page |
| Tools > Single-Letter Tool Shortcuts | Turn on the letter keys below (off by default) |
| `V` / `N` / `Esc` | Select tool / Note tool / back to Select |
| `H` / `U` / `S` | Highlight / Underline / Strikethrough tools -- drag across text |
| Drag on text, double-click a word | Select text; a bar below it offers Highlight (current colour or a swatch) / Underline / Strikethrough. `Ctrl+C` copies, `Ctrl+A` selects the page |
| Right-click selected text | Copy, highlight, underline or strike it through |
| `T` | Typewriter: click and type; the bar above picks colour and size. `Esc` or click away to finish (back to Select) |
| Double-click a text box | Edit its text in place |
| Drag a text box's edge or corner | Resize it; the text re-wraps |
| `R` / `E` / `L` / `A` / `P` | Rectangle / Ellipse / Line / Arrow / Pen -- drag to draw; `Shift` squares or snaps to 45 degrees |
| Stamp button | Pick a stamp, click to place it |
| Click a form field | Type into it (`Tab` for the next field), tick a box, or pick from a list |
| Annotate button | Every markup tool: notes, highlight/underline/strike, typewriter, shapes, pen, stamps, styles |
| Form button | Edit Fields (move, resize, delete, double-click for properties; click empty page to finish) or draw a new text field, checkbox or dropdown |
| Click, drag, `Delete` | Select, move, delete an annotation |
| Double-click an annotation | Edit its comment |
| `PgUp` `PgDn` `Ctrl+Home` `Ctrl+End` `Ctrl+G` | Navigation |
| `Ctrl` + scroll | Zoom at cursor |
| Tools > Recognize Text (OCR) | Make scanned pages searchable and selectable (Windows' own OCR); save to keep it |
| `Ctrl+F` | Find text; `Enter` / `F3` next, `Shift+Enter` / `Shift+F3` previous, `Esc` closes |
| Pan button, or drag with the middle mouse button | Drag the page to scroll; `Esc` back to Select |

Drag a PDF onto the window to open it.

## Layout

```
pdfapp/
  transform.py     PageTransform -- all coordinate math. Start here.
  pagelist.py      Page edits as data, plus snapshot undo.
  annots.py        Annotation edits as data; annotpdf.py writes them (pikepdf).
  fields.py        Form fields as data; formpdf.py writes them (pikepdf).
  ocrlayer.py      The invisible text layer OCR adds; ocr.py runs Windows OCR.
  security.py      What a protected PDF allows; protection.py is its dialog.
  document.py      pypdfium2 wrapper. No Qt imports, testable headless.
  writer.py        Saves edits with pikepdf: temp file, verify, atomic replace.
  rendering.py     pdfium bitmap -> QImage, plus an LRU page cache.
  pageview.py      Continuous scrolling view, zoom, tools, text selection.
  findbar.py       Ctrl+F find bar.
  printing.py      Printing; printpreview.py is the preview before it.
  thumbnails.py    Page sidebar: select, drag to reorder, lazy thumbnails.
  mainwindow.py    Menus, toolbar, status bar, drag-and-drop.
  errors.py        Unexpected errors are shown and logged, never lost.
  selftest.py      --self-test: proves an installed build works.
packaging/         PyInstaller spec, Inno Setup installer script, build.ps1.
tests/             One file per area; run them all before a build (build.ps1 does).
```

CLAUDE.md holds the design rules and the reasons behind them.

`transform.py` is the load-bearing file. Three coordinate systems (PDF user
space, bitmap space, widget space) and four rotation values means twelve ways
to be subtly wrong, and every feature after the viewer — annotations, text
overlays, form filling — depends on it being right. It is validated point by
point against pdfium's own `PdfPosConv`, including offset CropBoxes and
combined intrinsic-plus-view rotation, so a regression fails a test instead of
putting a highlight two inches from where the user clicked.

## Requirements

Windows 10 or 11 (OCR uses Windows' own engine; the rest is cross-platform
Python, but only Windows is built and tested). Python 3.11+ to run from source.

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

More in CLAUDE.md under "Known limits".

## License

SilverbowPDF is free software: you can redistribute it and/or modify it under
the terms of the GNU General Public License, version 3 or (at your option) any
later version. See [LICENSE](LICENSE).

It uses PySide6 / Qt (LGPL v3), pypdfium2 and PDFium (Apache 2.0 / BSD),
pikepdf (MPL 2.0) with qpdf (Apache 2.0), and the winrt bridge (MIT), each
under its own license. The installer bundles them.
