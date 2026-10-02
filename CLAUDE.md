# SilverbowPDF

Desktop PDF viewer and editor. Python 3.11+ / PySide6 / pdfium / pikepdf / pyHanko.

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
                 At most PAGE_CACHE_LIMIT (12) pdfium pages open, LRU: a
                 rendered page keeps its decoded images until closed. Page
                 geometry is cached apart, so transforms never reload one.
  writer.py      The only code that SAVES PDFs (signing appends; see pdfsign). pikepdf. write to temp -> commit (os.replace).
                 prune_dangling() drops bookmarks/links/fields/dests aimed at removed pages.
                 Foreign pages: plain append + _merge_forms, NOT add_pages_from (see rule 6).
  rendering.py   pdfium bitmap -> QImage (one copy, over pdfium's buffer), plus
                 a page-image cache with a 128MB budget: other zoom levels go
                 first, then LRU, never the newest two (both may be on screen).
  pageview.py    Continuous scroll view, zoom, tools, annotation
                 select/move/delete. Emits edit requests; never edits.
                 No view rotation in the UI (removed, user's call); the
                 view_rotation plumbing under it stays, always 0.
  ocrlayer.py    OCR text layer as data (OcrWord, OcrEdits) + the invisible-text
                 content/font and a hand-written one-page PDF for the preview.
                 No libs. Words are text matrices over a glyphless Identity-H
                 font (0.5em x 1em glyphs, ToUnicode identity).
  ocr.py         Windows.Media.Ocr via winrt (lazy import). Renders page
                 content only (no annots/fields), 300 DPI, maps word boxes
                 back through PageTransform.bitmap_to_pdf. Only pages with no
                 text at all (has_text) are recognised. Loads QtCore before
                 winrt ON PURPOSE: winrt ships an older msvcp140 (14.29) and
                 loading it first makes Qt (14.44) crash (access violation).
  security.py    Access (what a password allows, from pdfium's effective
                 permission bits) and the protection values KEEP / REMOVE /
                 "<new open password>". No libs.
  protection.py  File > Password Protection dialog (Qt).
  pdfsign.py     Certificate signatures (pyHanko): sign() appends an
                 INCREMENTAL revision to the original bytes -- the one PDF
                 write that isn't writer.py, because a rewrite breaks every
                 earlier signature; check() validates against Windows' trust.
                 Draws its own appearance (picture + text, counter-rotated).
                 No Qt.
  digitalid.py   DigitalID; self-made .pfx IDs (cryptography: RSA 3072,
                 5 years) and their signer. No Qt.
  wincert.py     Windows certificate store via ctypes: signing IDs (MY),
                 trust (ROOT + TrustedPeople, CA), and a pyHanko Signer that
                 signs through CNG (NCryptSignHash) so smart cards and
                 non-exportable keys work; Windows shows any PIN prompt.
  sigimage.py    Signature picture from a photo/scan (Qt): paper made
                 transparent, trimmed, kept as zlib RGB + alpha planes.
  signui.py      Sign dialog, Create Digital ID, signature details (Qt).
  findbar.py     Ctrl+F bar. Scans pages in 30ms timer slices from the current
                 page (find_text: pdfium search, throwaway text pages so a long
                 document isn't pinned in memory); the view paints the matches.
  printing.py    File > Print. PageRenderer.print_image per sheet (pdfium's
                 FPDF_PRINTING, no form-field tint, unsaved edits included),
                 fitted and centred, turned a quarter when bigger (against
                 the page's own /Rotate so it lands upright), max 300 DPI.
  printpreview.py Print preview, shown before Windows' print dialog (user's
                 choice: Windows 11's dialog can't preview this app and says
                 "This app doesn't support print preview"). Draws sheets with
                 printing.draw_page -- the same code the printer gets -- one
                 at a time, only that one kept. Its page choice is preset in
                 Windows' dialog.
  thumbnails.py  Page sidebar: multi-select, drag-to-reorder, lazy thumbnails.
                 Emits requests only; MainWindow makes the PageList edit.
  mainwindow.py  Menus, toolbar, status bar, drag-and-drop.
  errors.py      sys.excepthook: an exception in any Qt slot is shown in a
                 message box and appended to %TEMP%\SilverbowPDF-errors.log.
                 Without it the console-less app just "does nothing".
packaging/       PyInstaller spec, Inno Setup script, build script. icon.ico is made
                 from icon-source.png by scripts/make_icon.py (also writes
                 pdfapp/icon.png, the runtime window icon); both committed.
                 The AppUserModelID is set in __main__.py AND on the installer's
                 shortcuts -- change one, change both, or pinning splits.
```

Dependency direction is one-way: `transform` <- `pagelist` <- `document` <-
`rendering` <- `pageview`/`thumbnails`/`printing` <- `findbar` <- `mainwindow`, with `writer` depending only on
`pagelist` and used only by `mainwindow`. `ocrlayer` (no libs) sits beside
`annots`; `ocr` sits on `document` and only `mainwindow`/`selftest` use it.
Signing: `digitalid` <- `wincert` <- `pdfsign` (none of them Qt; `pdfsign`
needs only `annots`/`textlayout` and loads `wincert` lazily for trust), with
`sigimage` (Qt) and `signui` on top, used only by `mainwindow`/`selftest`.
Nothing lower imports anything higher.

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
(verified, pikepdf 10). `write_pdf(protection=KEEP)` -- the default -- saves
with `encryption=True`, which makes qpdf copy the file's own encryption:
same method, passwords and restrictions, whichever password opened it
(verified). Protection changes only through an explicit, undoable File >
Password Protection edit (DocState.protection: a new open password, or
REMOVE), and only with full access. Pages inserted from ANOTHER protected
file carry no protection, so if the result is unprotected `write_pdf` raises
`EncryptionWouldBeLost` unless that file is in `allow_decrypt` (user agreed).
The writer re-opens what it wrote and checks the protection is what was
asked for.

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
window. Follow that pattern: isolate, display, don't hide. App-wide, errors.py
shows any exception raised in a slot (0.6.0 shipped File > Print crashing on
`doc.title()` -- a property -- and in the installed app it just did nothing).

**8. Test UI features through their action, not only the engine below.**
Print's engine was tested; the menu handler wasn't, and it was broken.
`tests/test_search.py::test_every_menu_action_runs` triggers every QAction
with dialogs stubbed to cancel and fails on any exception -- keep it
passing, and give a new feature its own test that goes through its action.

## Conventions

- A closed PdfDocument raises DocumentError; never let pdfium see one (it's
  an access violation, not an exception).
- UI smoke tests: QTest.keyClicks aborts the process on non-ASCII text;
  use insertPlainText for those.
- PySide6 enums are always truthy: compare them (`== QPageLayout.Landscape`),
  never `if layout.orientation():`.

- Type hints on public functions. `from __future__ import annotations` at the top.
- Comments explain *why*, especially where something looks wrong but isn't
  (the two-transform HiDPI split, the `.copy()` in `bitmap_to_qimage`, ceil in
  `bitmap_size`). Don't add comments that restate the code.
- No new runtime dependencies without asking. The dependency list is short on
  purpose: PySide6, pypdfium2, pikepdf. Each one is ~50-200MB in the installer.
  Plus the winrt-* packages (Windows OCR bridge, ~3MB; user chose Windows'
  built-in OCR 2026-09-29 -- the engine itself ships with Windows 11).
- Rendering is read-only via pdfium. Document mutation goes through pikepdf.
  Don't mix the two on one code path.

## Roadmap

Built: open (incl. encrypted), continuous scroll, zoom to cursor, fit
width/page, navigation, form-field rendering, drag-and-drop,
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

Annotations stage 3: Typewriter T (no border, breaks only at Enter),
typed in place in a QPlainTextEdit over the page; size/colour presets
(Text Style menu, and the style bar above the editor while typing --
restyling existing text is an edit even if no word changed); the
separate Text Box X tool was removed (user's call), but bordered boxes
already in files still edit and resize; the
typewriter clicks into existing text to edit it; double-click edits any
FreeText (other apps' too, restyled in Helvetica keeping size/colour).
Helvetica/WinAnsi only: non-cp1252 text is refused, never saved as '?'.
Text reads upright on rotated pages (AP /Matrix counter-rotates by the page
rotation at typing time).
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

Stage 5b, creating fields (Form menu): Edit Fields (select, drag to move,
edges to resize, Delete, double-click for properties: name, required,
read-only, multi-line, font size, dropdown options) plus Text Field /
Multi-line / Checkbox / Dropdown tools (drag, or click for a standard size;
then back to Edit Fields with the new field selected; a dropdown opens its
properties). Auto-named "Text 1"... FormEdits (page key -> complete widget
list, like AnnotEdits) in DocState; renaming migrates filled values. The
writer applies structure then values to each source before copying; a
deleted widget becomes a null /Annots placeholder so annotation edits'
indices hold, and nulls are dropped just before prune_dangling (which drops
the deleted fields from /AcroForm). A flat PDF gains an /AcroForm with Helv
and ZaDb. New fields are fillable before saving (current_widgets).
Radio-button creation is not built (filling radios is). Clicking empty page
in Edit Fields returns to Select (user's rule, as everywhere).

Mark bar (user's design): in the Select tool, a text drag or double-clicked
word shows a small Highlight (current colour, or a swatch that becomes the
current colour) / Underline / Strikethrough bar under the selection (a child
of the page column, not a popup -- no focus grab, scrolls with the page, not
clipped by a narrow page). A click elsewhere, Esc, zoom or a tool change
dismisses it. The typewriter's style bar is the same kind of widget.
The Highlight/Underline/Strikethrough tools still mark directly, no bar.

Rotate View (left/right, Ctrl+L/R) was removed (user's call): a turned view
on top of turned pages confused users. Page rotation (sidebar, Pages menu)
is the only rotation.

OCR (Tools > Recognize Text, or right-click pages): Windows' own engine on
the pages with no text (sidebar selection if several, else all). One
undoable edit, OcrEdits in DocState; before saving, document._text reads
those pages from the recognised layer (ocrlayer.text_layer_pdf in pdfium),
so search/select/highlight work at once; the writer draws the same words
invisibly after the page's content (wrapped in q/Q). tests/test_ocr.py
asserts preview text boxes == saved within 0.5pt and pixels unchanged.
Opening a PDF whose first pages have no text shows a status-bar hint.
The self-test runs OCR (says so if the Windows install has no language).

Print (Ctrl+P, toolbar, File menu): our print preview first (sheet by
sheet, page choice), then Windows print dialog -- all, a range,
current page, or the sidebar selection; copies (sent as extra sheets if
the driver can't); progress with Cancel. A page that fails to render gets
its error printed on its sheet and is listed afterwards (rule 7). The
self-test prints to PDF and logs how many printers the app can see.

Find (Ctrl+F; F3 / Shift+F3, Enter / Shift+Enter): bar above the view,
match case / whole words, "3 of 17", all matches tinted, current one
orange and scrolled into view. Page edits rescan quietly (no jump).
Pan tool (hand, sticky until Esc; no letter -- H is Highlight) and
middle-drag pans in every tool, gaps between pages included.

Toolbar / Tools menu layout (user's): Select, Pan, Annotate (drop-down: note,
text markup, typewriter/text box, shapes/pen, stamps, style menus), Form
(drop-down: Edit Fields + field tools). The buttons keep their labels and
name the active tool ("Annotate: Rectangle").

Single-letter tool shortcuts are OFF by default (user's call: their users
are mouse-only). Tools menu toggle (HKCU); administrator policy
HKLM\Software\Policies\SilverbowPDF\SingleKeyShortcuts locks it either way
(settings.py; set by GPO or deployment tooling -- the installer task for it was removed,
user's call: off by default is sufficient). Ctrl shortcuts always on.
Tests pass INI-backed QSettings -- never touch the real registry.

Encryption (user's choices, 2026-09-30): Save and Save As keep a protected
file's protection exactly (rule 5). File > Password Protection sets,
changes or removes an OPEN PASSWORD only (AES-256, owner = user password,
no restrictions) -- an undoable edit applied on save. Authors' restrictions
are HONOURED like Acrobat: without the owner password, what's forbidden is
off (buttons disabled, AND every edit entry point in MainWindow gated via
_allowed -- _edit, _on_annots_edit, _on_fields_edit, _on_forms_edit, OCR,
print, copy, extract, insert), PdfView.access stops the page offering it,
and a yellow bar says what's off with Unlock… (owner password; reopens,
keeps edits and history). Low-res-only printing caps at 150 DPI. Pages
from a restricted file (no copying) can't be inserted elsewhere. Changing
protection needs full access, so nobody strips restrictions by
re-protecting. The self-test saves a protected file and re-protects one.

Signatures (user's choices, 2026-10-02), Sign button / Tools > Sign:
- Sign with a Certificate: drag (or click) a box, pick a digital ID --
  Windows certificate store, a .pfx file, or Create a Digital ID (self-
  signed .pfx in Documents\SilverbowPDF Digital IDs) -- optional picture,
  reason, location, date. Asks where to save (default: over the same
  file), then opens the signed file. Written as an incremental update,
  PAdES, SHA-256. With no unsaved edits the original bytes are signed, so
  earlier signatures stay valid; with edits, those are saved first
  (warned: that breaks old signatures).
- Signature Picture (Fill & Sign, no certificate): a Stamp annotation
  showing the user's picture (remembered, HKCU), placed like a stamp,
  undoable, saved like any annotation. Not a cryptographic signature.
- On open, signatures are checked (pyHanko, no network): green bar if all
  valid and trusted, yellow if valid but not trusted (self-signed IDs),
  red if changed after signing or broken; Details... lists each.
- Saving a signed document (normal save, qpdf rewrite) warns first that
  the signatures will break; an untouched Save As copies the bytes.
- Restricted files: signing follows the annotate permission.
- A file pyHanko can't append to (pdfsign.damaged: typically a broken
  xref table, which pdfium silently rebuilds) is offered a repair first:
  the qpdf rewrite path, as for unsaved edits, then signed.
- Wording (review, 2026-10-02): "Signed by X" only when the certificate is
  verified (SigInfo.verified); otherwise the problem leads and the name is
  "in the name of X" -- anyone can make an ID in any name. Dates are the
  signer's own clock (no timestamp authority), and say so in Details.
- Signed signature fields are locked in Edit Fields (Acrobat does the
  same); empty ones still move. Signed = /V on the widget, or the field's
  name among the checked signatures (a kid's /V is on its parent, which
  pdfium can't reach).
The self-test signs twice (.pfx and Windows CNG) and checks both.

Next: nothing queued -- ask.

Deployment target is shared multi-session desktops: no GPU, non-persistent
profiles, pixels re-encoded over a remote-display protocol. Keep memory and repaints lean
(tests/test_memory.py holds the limits: pages, page images, and no
off-screen renders -- Qt's deferred show of new page widgets once made
every open render the whole document); don't
count on per-user settings surviving logoff.

## Known limits (don't "fix" these without discussion)

- Form pages: fields are tinted light blue (pdfium highlight, as Acrobat/
  Edge do). Draw order is content, fields, annotations -- NOT pdfium's
  native content, annotations, fields -- so ink over a checkbox lies on
  top. Any form page with annotations is therefore always layered
  (annotpdf.overlay_args is the single rule; tests use it too).
- XFA (LiveCycle) forms: pdfium can't show their fields; the status bar
  says so. Converting XFA isn't planned.
- Find doesn't match a phrase broken across two lines (pdfium's text has a
  line break there). Scanned pages need Recognize Text first.
- OCR: pages with ANY text are skipped (a second invisible copy would double
  every search hit); a page that's part scan, part text gets nothing. Skewed
  scans: the engine's text angle is ignored, so words on a tilted line are
  boxed level. Languages are whatever Windows has for the user. Characters
  outside Unicode's BMP are dropped from the layer.

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
- Password Protection sets an open password only; creating restrictions
  (owner password + permissions) isn't offered (user's call). Restrictions
  a file already has are kept on save and honoured.
- Certificate (public-key) security isn't supported: pdfium can't open it.
- Unlock lasts until the document is closed; the file keeps its restrictions.

- Signature checks don't look up revocation (no network on open: opening
  a PDF shouldn't stall on an OCSP server), so a revoked certificate still
  shows as valid. No timestamps (TSA) are added when signing, and no
  long-term validation data (LTV).
- A picture signature moves but doesn't resize (place it again instead).
- Signature appearances are Helvetica/WinAnsi like typed text: a name or
  reason with characters outside cp1252 is left off the appearance (the
  certificate still carries it). Lines that don't fit are dropped.
- Signing always adds a new signature field where the box is drawn: an
  empty signature field already in a form isn't signed by clicking it,
  and creating empty signature fields for others isn't offered.

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
