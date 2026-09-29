# ButterPDF

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
python tests/test_pages.py
pytest                            # tests, if pytest is installed
```

## Architecture

```
pdfapp/
  transform.py   PageTransform. All coordinate math. LOAD-BEARING.
  pagelist.py    PageList (page edits as data) + History (snapshot undo). No Qt/pdfium/pikepdf.
                 PageRef = (file, page, rotation). File 0 is the opened document.
  annots.py      Annotation edits as data (Annot, AnnotEdits, DocState). No libs.
  annotpdf.py    Annotation edits -> PDF (pikepdf): apply_edits, new-annot APs,
                 overlay_pdf for the live preview.
  textlayout.py  Helvetica metrics, wrapping, DA parsing for text boxes. No libs.
  fields.py      Form field model (Widget, FieldEdits by (file, full name)). No libs.
  formpdf.py     Field values -> PDF (pikepdf): /V, /AS, text-field APs, widget
                 flattening for the preview. Never touches /NeedAppearances.
  document.py    pypdfium2 wrapper. NO Qt imports -- must stay headless-testable.
                 Takes *displayed* indices; maps them through its PageList to a
                 page of one of its sources. Sources are only ever added.
                 Text layer (char_at/nearest_char/word_at/text_in/text_rects)
                 is pdfium's; char indices are pdfium's, ranges inclusive.
  writer.py      The only code that writes PDFs. pikepdf. write to temp -> commit (os.replace).
                 prune_dangling() drops bookmarks/links/fields/dests aimed at removed pages.
                 Foreign pages: plain append + _merge_forms, NOT add_pages_from (see rule 6).
  rendering.py   pdfium bitmap -> QImage, plus an LRU page cache.
  pageview.py    Continuous scroll view, zoom, view rotation, tools, annotation
                 select/move/delete. Emits edit requests; never edits.
  thumbnails.py  Page sidebar: multi-select, drag-to-reorder, lazy thumbnails.
                 Emits requests only; MainWindow makes the PageList edit.
  mainwindow.py  Menus, toolbar, status bar, drag-and-drop.
packaging/       PyInstaller spec, Inno Setup script, build script.
```

Dependency direction is one-way: `transform` <- `pagelist` <- `document` <-
`rendering` <- `pageview`/`thumbnails` <- `mainwindow`, with `writer` depending only on
`pagelist` and used only by `mainwindow`. Nothing lower imports anything higher.

Page edits never mutate anything until save: pdfium keeps rendering the
original file through the PageList. `tests/test_pages.py` asserts the preview
and the saved file are pixel-identical -- keep that test passing.

Annotation edits work the same way (DocState = pages + AnnotEdits, one undo
history). A page with annotation edits is drawn in pdfium's own three passes:
content without annotations, then a throwaway one-page PDF holding the page's
annotations with edits applied (built by the same `apply_edits` the writer
uses), then form fields. This is the one place pikepdf output feeds pdfium --
deliberately, and only for a document that is never saved. Never draw
annotation content with Qt; Qt draws selection chrome only.
`tests/test_annots.py` asserts preview == saved, pixel for pixel.

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

**5. Never silently drop encryption.** pikepdf's default save strips it
(verified, pikepdf 10), and pages inserted from a protected file carry no
protection into another document. `write_pdf` raises `EncryptionWouldBeLost`
(naming the source file) unless that file's index is in `allow_decrypt` after
the user explicitly agreed, and a protected original is never overwritten with
an unprotected file.

**6. Never reorder pages with pikepdf's slice assignment, insert or reverse.**
They silently COPY a page that's already in the tree, so every bookmark, link
and form field keeps pointing at the stale original, which then rides along in
the file as dead weight. `write_pdf` empties the tree and appends the same Page
objects back, and raises if object identity changed. `tests/test_pages.py`
checks that no removed page object survives in a saved file.

Likewise, don't copy pages in with `Pdf.add_pages_from`: it copies form fields
page by page, so a field with widgets on two inserted pages becomes two
unrelated fields. Appending all of one file's pages lets qpdf's copier keep
them shared; `_merge_forms` then registers them with /AcroForm.

**7. Never silently swallow a pdfium exception.** `PageWidget.paintEvent` draws
the error onto the failed page so one corrupt page does not take down the
window. Follow that pattern: isolate, display, don't hide.

## Conventions

- A closed PdfDocument raises DocumentError; never let pdfium see one (it's
  an access violation, not an exception).
- UI smoke tests: QTest.keyClicks aborts the process on non-ASCII text;
  use insertPlainText for those.

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
per-page error isolation. Page operations (rotate, delete, reorder) on
the sidebar selection, with undo/redo, Save / Save As, and an unsaved-changes
prompt. Thumbnail sidebar with multi-select and drag-to-reorder. Extract
pages to a new file (split). Insert pages from other PDFs (merge), from a
menu or by dropping files on the sidebar, carrying their form fields and
bookmarks (in a folder named after the file).

Annotations stage 1: tool modes (Select V, Note N, Esc), select / move /
delete / edit-comment for existing annotations of any type (links, widgets,
popups and hidden ones pass through untouched), sticky notes with their own
appearance streams. Packaging done; installer verified where it's deployed.

Annotations stage 2: text selection (drag, double-click word, Ctrl+A,
Ctrl+C), Highlight H / Underline U / Strikethrough S tools (sticky until Esc),
highlight colour presets, mark-selection from the right-click menu. Text
markup is anchored: click selects it, dragging inside selects the text under
it, it is never moved. Highlights use a Multiply-blend AP so text stays
readable (tested). One layer per markup kind (user's rule): new marks cut
back older marks of the same kind on the same text (`lay_markup`) -- split,
or removed with the comment carried over -- so highlights never stack and
darken. Existing file markups get reshaped (new /QuadPoints + regenerated AP).

Annotations stage 3: Typewriter T (no border, breaks only at Enter) and
Text Box X (bordered, wraps; click = 200pt wide, drag = chosen width),
typed in place in a QPlainTextEdit over the page; size/colour presets; the
typewriter clicks into existing text to edit it; double-click edits any
FreeText (other apps' too, restyled in Helvetica keeping size/colour).
Helvetica/WinAnsi only: non-cp1252 text is refused, never saved as '?'.
Text reads upright on rotated pages (AP /Matrix counter-rotates by the page
rotation at typing time); typing is blocked while the *view* is rotated.
Single-key tool shortcuts are disabled while the editor is open. Finishing
(click away, Esc, Ctrl+Enter) also returns to the Select tool, and the
click-away does nothing else (user's request: no accidental new boxes).
Resize (Select tool): edges/corners of text boxes show resize cursors;
width re-wraps, height is clamped so text is never cut off. `fixed_width`
(wraps) is separate from `boxed` (border): resizing typewriter text gives
it a width. Only while the text reads upright on screen.

Annotations stage 4: Rectangle R / Ellipse E (Shift = square/circle),
Line L / Arrow A (Shift = 45 degree snap) -- return to Select once drawn,
new shape selected (user's preference); Pen P (smoothed ink, one
annotation per stroke) stays active until Esc; Stamps (menu:
Approved/Reviewed/Received carry author + time; Draft/Confidential/
Rejected plain) place once then return to Select, upright on rotated pages
like text. Shape style menu: colour + 1-8pt width. Lines and ink hit-test
along the stroke. Rectangles/ellipses resize at any rotation, file ones
redrawn in their own /C, width and /IC. Author = Windows login name.

Stage 5a, form filling (Select tool): click a text field to type in place
(Enter / Esc finish; Tab / Shift+Tab next / previous text field; multi-line
fields take Enter as a newline), click check boxes / radios to toggle,
combo boxes pop a menu, read-only fields say so. Values are FieldEdits in
DocState, applied by the writer to each source BEFORE page copying (names
still original). Button values are on-state names (/AS); /Opt export
values are mapped back by formpdf. Preview: the overlay gets a first page
of the page's widgets (values applied by the same apply_field_edits,
flattened) that pdfium's form engine draws in place of the file's fields,
with /NeedAppearances set as the saved file will have it (pdfium redraws
fields when it's set -- measured). Verified on the user's real form.

Next, in order (user chose all of these): shapes, ink, stamps;
authoring fillable form fields; form filling; encryption; OCR for scanned
pages (user confirmed it's required; engine not chosen -- weigh machine-image
size/CPU/Windows Server support and ask before adding a dependency).

Deployment target is shared multi-session desktops: no GPU, non-persistent
profiles, pixels re-encoded over a remote-display protocol. Keep memory and repaints lean; don't
count on per-user settings surviving logoff.

## Known limits (don't "fix" these without discussion)

- Form pages: fields are tinted light blue (pdfium highlight, as Acrobat/
  Edge do). Draw order is content, fields, annotations -- NOT pdfium's
  native content, annotations, fields -- so ink over a checkbox lies on
  top. Any form page with annotations is therefore always layered
  (annotpdf.overlay_args is the single rule; tests use it too).
- XFA (LiveCycle) forms: pdfium can't show their fields; the status bar
  says so. Converting XFA isn't planned.

- Save resets undo history. The saved file becomes the new source, and old
  PageList snapshots index into the previous file.
- Every inserted file stays open (pdfium handle) until the document is
  closed or saved, so Windows won't let other programs replace it meanwhile.
- The mouse wheel doesn't scroll the sidebar while dragging files in from
  Explorer (OS drag loop); edge autoscroll does. Reordering pages inside the
  sidebar is unaffected -- it's a hand-rolled drag.
- Removing pages doesn't rewrite the tagged-PDF structure tree or
  /PageLabels. Both are index/structure based; rewriting them wrong is worse
  than leaving them.
- Encrypted files: Save is refused, Save As writes an unprotected copy after
  an explicit warning. Real re-encryption is the encryption milestone.

- Rendering is synchronous. Fine for text; a 600 DPI scan at 400% hitches.
  The planned fix is moving `PageRenderer` onto a `QThreadPool`; the interface
  is already shaped so that swap doesn't touch the widgets.
- Thumbnails render lazily (visible rows only, ~30ms per tick) and cache
  up to 200 by PageRef, so reorders and deletes never re-render.
- One widget per page. Cheap to a few thousand pages, then it isn't. The fix
  is a virtualised canvas, deliberately deferred.
- Fit-width uses the widest page in the document, not the current page, so the
  layout doesn't jump when scrolling past a landscape sheet.
- `Format_ARGB32`/`Format_RGB32` assume little-endian. There's an endian-safe
  fallback in `rendering.py`.
