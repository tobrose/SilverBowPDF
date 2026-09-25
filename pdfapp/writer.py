"""Write page edits out to disk with pikepdf.

The only module that produces a PDF. Rendering never goes through here, and
nothing here goes through pdfium -- the two libraries each see the file on
their own terms.

Saving is two steps so the caller can release its own handle in between:

    tmp = write_pdf(source, password, pages, dest)   # original untouched
    <close anything that holds `dest` open -- Windows won't replace it otherwise>
    commit(tmp, dest)                                # atomic swap

If write_pdf raises, nothing on disk changed. If commit raises, the original
is still intact and the temp file is removed.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Optional

import pikepdf

from .pagelist import PageList


class SaveError(Exception):
    """Writing failed. The original file is unchanged."""


class EncryptionWouldBeLost(SaveError):
    """The source is encrypted and saving would silently strip that.

    pikepdf's default save drops encryption (checked against pikepdf 10: a
    file opened with its user password saves back out with no password at
    all). Re-encrypting properly needs the owner password and permissions,
    which is the encryption milestone. Until then the caller must opt in to
    writing an unprotected copy, knowingly.
    """


def write_pdf(
    source: str | Path,
    password: Optional[str],
    pages: PageList,
    dest: str | Path,
    allow_decrypt: bool = False,
) -> Path:
    """Build the edited PDF into a temp file beside `dest` and return its path."""
    source, dest = Path(source), Path(dest)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{dest.stem}.", suffix=".tmp", dir=dest.parent)
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        with pikepdf.open(source, password=password or "") as pdf:
            if pdf.is_encrypted and not allow_decrypt:
                raise EncryptionWouldBeLost(
                    f"{source.name} is password-protected, and saving would remove "
                    f"the protection."
                )
            if len(pdf.pages) <= max(r.source for r in pages):
                raise SaveError(
                    f"{source.name} has {len(pdf.pages)} pages on disk but the edit "
                    f"refers to page {max(r.source for r in pages) + 1}. Was the file "
                    f"changed by another program?"
                )

            # Reorder in place rather than copying into a fresh Pdf, so the
            # document catalog -- outlines, form definitions, metadata -- comes
            # along.
            #
            # The pages must stay the SAME objects, because bookmarks, links
            # and form fields point at them by object number. Slice assignment,
            # insert and reverse all silently copy a page that's already in
            # the tree (checked, pikepdf 10), leaving every reference aimed at
            # a stale original. Emptying the tree first and appending the same
            # Page objects back keeps their identity. qpdf flattens the page
            # tree on removal, pushing inherited /Resources, /MediaBox,
            # /CropBox and /Rotate down onto each page, so nothing is lost
            # when their old parent node goes away.
            new_pages = [pdf.pages[r.source] for r in pages]
            while len(pdf.pages):
                del pdf.pages[-1]
            for page in new_pages:
                pdf.pages.append(page)
            if [p.obj.objgen for p in pdf.pages] != [p.obj.objgen for p in new_pages]:
                raise SaveError("page reorder did not preserve page identity")
            for page, ref in zip(pdf.pages, pages):
                if ref.rotation:
                    page.rotate(ref.rotation, relative=True)

            # Always, not just after deletes: with every page kept it finds
            # nothing, and one code path is one thing to get right.
            prune_dangling(pdf)

            pdf.save(tmp)

        # Reopen and count before letting the caller replace anything.
        with pikepdf.open(tmp) as check:
            if len(check.pages) != len(pages):
                raise SaveError(
                    f"wrote {len(check.pages)} pages, expected {len(pages)}"
                )
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return tmp


def prune_dangling(pdf: pikepdf.Pdf) -> None:
    """Drop document-level references to pages no longer in the page tree.

    Removing a page from the tree doesn't remove it from the file: qpdf
    writes every object still referenced, so a bookmark, named destination,
    link or form field pointing at a deleted page keeps that page -- content,
    images, fonts -- alive as invisible dead weight, and clicking the bookmark
    goes nowhere. Extracting 2 pages of 300 would carry all 300.

    Only references that provably point at a removed page are touched.
    Anything this can't resolve (remote GoTo, JavaScript, odd structures) is
    left exactly as it was.

    Not handled: the tagged-PDF structure tree and /PageLabels. Both are
    index- or structure-based and rewriting them wrong is worse than leaving
    them; revisit if a real file needs it.
    """
    kept = {page.obj.objgen for page in pdf.pages}
    named = _named_destinations(pdf)

    def target_alive(dest) -> Optional[bool]:
        """True/False for a destination pointing at a kept/removed page,
        None when it can't be determined."""
        if dest is None:
            return None
        if isinstance(dest, (pikepdf.Name, pikepdf.String)):
            dest = named.get(str(dest))
            if dest is None:
                return None
        if isinstance(dest, pikepdf.Dictionary) and "/D" in dest:
            dest = dest.D
        if isinstance(dest, pikepdf.Array) and len(dest) > 0:
            page = dest[0]
            if isinstance(page, pikepdf.Dictionary) and page.is_indirect:
                return page.objgen in kept
        return None

    def action_alive(action) -> Optional[bool]:
        if isinstance(action, pikepdf.Dictionary) and action.get("/S") == pikepdf.Name.GoTo:
            return target_alive(action.get("/D"))
        return None

    def points_at_removed(obj) -> bool:
        return (target_alive(obj.get("/Dest")) is False
                or action_alive(obj.get("/A")) is False)

    # Bookmarks: drop dead leaves via the outline API, which relinks the tree.
    if "/Outlines" in pdf.Root:
        def prune(items):
            survivors = []
            for item in items:
                item.children[:] = prune(item.children)
                dead = (target_alive(item.destination) is False
                        or action_alive(item.action) is False)
                if not dead or item.children:
                    survivors.append(item)
            return survivors

        with pdf.open_outline() as outline:
            outline.root[:] = prune(outline.root)

        # A dead bookmark with live children stays as a plain folder. Setting
        # item.destination = None doesn't remove /Dest (checked, pikepdf 10),
        # so strip it at the dictionary level.
        seen = set()

        def strip(node):
            while isinstance(node, pikepdf.Dictionary) and node.objgen not in seen:
                seen.add(node.objgen)
                if points_at_removed(node):
                    for key in ("/Dest", "/A"):
                        if key in node:
                            del node[key]
                strip(node.get("/First"))
                node = node.get("/Next")

        if "/Outlines" in pdf.Root:
            strip(pdf.Root.Outlines.get("/First"))

    # Named destinations.
    if "/Dests" in pdf.Root:
        dests = pdf.Root.Dests
        for key in [k for k in dests.keys() if target_alive(dests[k]) is False]:
            del dests[key]
    if "/Names" in pdf.Root and "/Dests" in pdf.Root.Names:
        tree = pikepdf.NameTree(pdf.Root.Names.Dests)
        for key in [k for k, v in tree.items() if target_alive(v) is False]:
            del tree[key]

    # Internal links on the pages that remain.
    for page in pdf.pages:
        annots = page.obj.get("/Annots")
        if not isinstance(annots, pikepdf.Array):
            continue
        keep = [a for a in annots
                if not (isinstance(a, pikepdf.Dictionary)
                        and a.get("/Subtype") == pikepdf.Name.Link
                        and points_at_removed(a))]
        if len(keep) != len(annots):
            page.obj.Annots = pikepdf.Array(keep)

    # Form fields whose widgets were all on removed pages.
    acroform = pdf.Root.get("/AcroForm")
    if isinstance(acroform, pikepdf.Dictionary) and "/Fields" in acroform:
        on_kept_pages = {
            a.objgen for page in pdf.pages
            for a in (page.obj.get("/Annots") or [])
            if isinstance(a, pikepdf.Dictionary) and a.is_indirect
        }

        def field_alive(field) -> bool:
            if not isinstance(field, pikepdf.Dictionary):
                return True
            kids = field.get("/Kids")
            if isinstance(kids, pikepdf.Array):
                alive = [k for k in kids if field_alive(k)]
                if len(alive) != len(kids):
                    field.Kids = pikepdf.Array(alive)
                return bool(alive)
            if field.get("/Subtype") == pikepdf.Name.Widget:
                return field.objgen in on_kept_pages
            return True  # not a widget, can't place it: leave it

        acroform.Fields = pikepdf.Array([f for f in acroform.Fields if field_alive(f)])

    # "Open at page N", when N is gone.
    open_action = pdf.Root.get("/OpenAction")
    if open_action is not None and (target_alive(open_action) is False
                                    or action_alive(open_action) is False):
        del pdf.Root["/OpenAction"]


def _named_destinations(pdf: pikepdf.Pdf) -> dict:
    """Name -> destination, from both the PDF 1.1 /Dests dict and the
    PDF 1.2+ /Names /Dests name tree."""
    named: dict = {}
    if "/Dests" in pdf.Root:
        for key, value in pdf.Root.Dests.items():
            named[str(key)] = value
            named[str(key).lstrip("/")] = value
    if "/Names" in pdf.Root and "/Dests" in pdf.Root.Names:
        for key, value in pikepdf.NameTree(pdf.Root.Names.Dests).items():
            named[str(key)] = value
    return named


def commit(tmp: str | Path, dest: str | Path) -> None:
    """Atomically move the temp file over `dest`."""
    tmp, dest = Path(tmp), Path(dest)
    try:
        os.replace(tmp, dest)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise SaveError(
            f"could not replace {dest.name}: {exc.strerror or exc}. "
            f"Is it open in another program?"
        ) from exc
