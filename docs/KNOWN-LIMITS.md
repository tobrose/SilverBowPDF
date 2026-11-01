# Known limits (don't "fix" these without discussion)

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
- Tabs aren't restored after the app closes (no session restore), and a
  tab can't be dragged out into a window of its own.
- Two copies started at the same instant both run (each listens on the
  pipe); later launches go to either. Harmless, just two windows.

- Signature checks don't look up revocation (no network on open: opening
  a PDF shouldn't stall on an OCSP server), so a revoked certificate still
  shows as valid. No timestamps (TSA) are added when signing, and no
  long-term validation data (LTV).
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
