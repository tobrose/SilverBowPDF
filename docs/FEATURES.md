# Features built so far (with the user's design decisions)

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

Help menu (user's request, 2026-10-31): SilverbowPDF Guide (F1; guide.py,
HTML in code so it works offline; a non-modal window -- tests/test_help.py
checks every key combination it names is really bound), Check for Updates
(updates.py: on request only, never at start; reads where GitHub's
/releases/latest page redirects -- not the REST API, whose 60 requests an
hour per address a shared host's users would exhaust together; plain vX.Y.Z
tags only; Qt networking for the system proxy; every outcome reported; the
repo was made public 2026-10-31 so it works without signing in), About (version,
libraries, error-log path). The self-test checks Qt's TLS is bundled.

Stamps resize (user's request, 2026-10-31): Select tool, drag an edge or
corner of any stamp or signature picture; proportions kept, the opposite
edge/corner fixed, dragging past it stops at the smallest size
(viewtext._resized_stamp). Grips shrink with small boxes so the middle half
always moves (a stamp is ~9px tall at 25%); a zero-size box isn't resizable. Ours are redrawn in the new
box; other apps' stamps keep their appearance and get the new /Rect
(viewers map the appearance onto it).

The page-number box sizes itself to the page count (the Windows 11 style's
side-by-side arrows cut off a second digit at a fixed 70px). File > Close
answers Ctrl+W as well as Ctrl+F4 (a standard key given as shortcut= kept
only the first).

Tabs (user's call, 2026-10-02, instead of a copy of the app per PDF):
every document opens in its own tab -- File > Open, drag-and-drop, the
command line, and a second launch from Explorer/email, which hands over
to the running app (instance.py) and exits. An empty window shows no tab
strip; its one empty tab is reused by the next open. Opening a file
that's already open switches to its tab. Close (Ctrl+W, the tab's X)
asks to save that tab; the last tab closes to the empty start page.
Closing the window asks for each unsaved tab in turn, shown as it asks.
Ctrl+Tab / Ctrl+Shift+Tab (and Ctrl+PgDn / PgUp) switch. Unsaved tabs
carry a dot. Style choices (colours, sizes, stamp, signature picture)
follow from tab to tab. A tab out of sight releases its memory.
A file handed over while a dialog is open waits until it closes.

Toolbar / Tools menu layout (user's): Select, Pan, Annotate (drop-down: note,
text markup, typewriter/text box, shapes/pen, stamps, style menus), Form
(drop-down: Edit Fields + field tools). The buttons keep their labels and
name the active tool ("Annotate: Rectangle").

Single-letter tool shortcuts are OFF by default (user's call: their users
are mouse-only). Tools menu toggle (HKCU); administrator policy
HKLM\Software\Policies\SilverbowPDF\SingleKeyShortcuts locks it either way
(settings.py; set by GPO or deployment tooling -- the installer task for
it was removed, user's call: off by default is sufficient). Ctrl shortcuts
always on.
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
