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

Every push to main runs all tests on GitHub (windows-latest, pinned
versions): .github/workflows/tests.yml. A red run means don't ship.

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
                 PdfView = mixins sharing its state: viewdraw.py (shapes,
                 ink, stamps, signature boxes), viewtext.py (text boxes,
                 resizing), viewforms.py (filling, Edit Fields). tools.py:
                 Tool/FitMode and the tool groups; viewbars.py: the in-place
                 editors, mark bar and style bar. New page work goes in the
                 mixin it belongs to, not back into pageview.py.
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
  mainwindow.py  Menus, toolbar, status bar, drag-and-drop, edits and undo.
                 MainWindow = mixins: fileflow.py (open, insert, extract,
                 save), jobs.py (print, OCR), accessflow.py (restrictions,
                 Unlock, Password Protection), signflow.py (Sign menu).
                 Tests swap dialogs with tests/patching.py, which patches a
                 name in every pdfapp module that has it.
                 One tab per document: view, sidebar, find_bar, history,
                 the bars... are properties reading the CURRENT tab, so the
                 code reads as single-document. Tab views' signals reach
                 MainWindow only while their tab is current (_add_tab's
                 guard); per-tab state that must change regardless (a
                 closed document's history) is the tab's own business.
  doctab.py      DocumentTab: one document's view, find bar, notice bars,
                 page sidebar (in the dock's stack) and undo history. Out
                 of sight it releases its rendered and pdfium pages.
  instance.py    One app per user session: a second launch hands its files
                 to the running one over a named pipe and exits. The pipe
                 name holds user + session; UserAccessOption limits who
                 connects; and the newcomer checks the LISTENER is this
                 user in this session before sending (Windows lets anyone
                 listen under a taken pipe name).
  errors.py      sys.excepthook: an exception in any Qt slot is shown in a
                 message box and appended to
                 %APPDATA%\SilverbowPDF\SilverbowPDF-errors.log -- roaming
                 AppData, which profile management keeps (%TEMP% it
                 doesn't); trimmed past 256KB since it roams.
                 Without it the console-less app just "does nothing".
packaging/       PyInstaller spec, Inno Setup script, build script. icon.ico is made
                 from icon-source.png by scripts/make_icon.py (also writes
                 pdfapp/icon.png, the runtime window icon); both committed.
                 The AppUserModelID is set in __main__.py AND on the installer's
                 shortcuts -- change one, change both, or pinning splits.
                 The installer registers a PDF handler (ProgID, OpenWithProgids,
                 Capabilities) but never sets .pdf's default: Windows ignores
                 that, and IT sets it by GPO (docs/PACKAGING.md). Upgrades
                 delete the old _internal first.
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
  built-in OCR 2026-09-29 -- the engine itself ships with Windows 11), and
  pyHanko for signatures (2026-10-02).
- Builds use exact versions: packaging/constraints.txt (build.ps1 installs
  with -c). Upgrading a library is a deliberate commit to that file,
  gated by the tests and the frozen self-test, then a check on the deployment target.
- Rendering is read-only via pdfium. Document mutation goes through pikepdf.
  Don't mix the two on one code path.

## Features and known limits

What's built, and the user's design decisions behind each feature
(tool behaviour, shortcuts, tabs, encryption, signatures, print, OCR...):
**docs/FEATURES.md**. Read the relevant part before changing how an existing
feature behaves -- many behaviours are deliberate user choices ("user's call").

Known limits: **docs/KNOWN-LIMITS.md**. Don't "fix" any of them without
discussion. Next: nothing queued -- ask.

## Deployment target

Shared Windows 11 multi-session desktops: no GPU, non-persistent
profiles, pixels re-encoded over a remote-display protocol. Keep memory and repaints lean
(tests/test_memory.py holds the limits: pages, page images, and no
off-screen renders). profile management keeps HKCU, roaming AppData
and Documents; %TEMP% and local AppData don't. Per-user values can still be
absent (first logon, profile reset): never depend on one being there.
