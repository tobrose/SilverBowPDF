"""Write page edits out to disk with pikepdf.

The only module that SAVES a PDF (pdfsign only appends a signed revision
to one, because rewriting would break signatures). Rendering never goes
through here, and nothing here goes through pdfium -- the two libraries each
see the file on their own terms.

Saving is two steps so the caller can release its own handle in between:

    tmp = write_pdf(sources, pages, dest)            # originals untouched
    <close anything that holds `dest` open -- Windows won't replace it otherwise>
    commit(tmp, dest)                                # atomic swap

If write_pdf raises, nothing on disk changed. If commit raises, the original
is still intact and the temp file is removed.
"""

from __future__ import annotations

import os
import tempfile
import warnings
from contextlib import ExitStack
from pathlib import Path
from typing import Collection, Dict, List, Optional, Protocol, Sequence, Tuple

import pikepdf

from .annotpdf import apply_edits
from .annots import AnnotEdits
from .fields import FieldEdits, FormEdits
from .formpdf import apply_field_edits, apply_form_edits
from .ocrlayer import FONT_DESCRIPTOR, FONT_NAME, GLYPH_WIDTH, TO_UNICODE, OcrEdits, OcrWord
from .ocrlayer import content as ocr_content
from .pagelist import PageList
from .security import KEEP, REMOVE


class SaveError(Exception):
    """Writing failed. The original file is unchanged."""


class EncryptionWouldBeLost(SaveError):
    """Pages from a protected file would come out unprotected.

    The document's own protection is kept (protection=KEEP: qpdf copies it,
    passwords and restrictions unchanged) or replaced or removed only when
    asked (security.py). But pages inserted from ANOTHER protected file carry
    no protection with them, so if the result isn't protected either, the
    caller must opt in for that file, knowingly (allow_decrypt).
    """

    def __init__(self, file: int, path: Path):
        self.file = file
        self.path = path
        super().__init__(
            f"{path.name} is password-protected, and saving would remove the protection."
        )


class Source(Protocol):
    path: Path
    password: Optional[str]


def write_pdf(
    sources: Sequence[Source],
    pages: PageList,
    dest: str | Path,
    allow_decrypt: Collection[int] = (),
    annots: AnnotEdits = AnnotEdits(),
    fields: FieldEdits = FieldEdits(),
    forms: FormEdits = FormEdits(),
    ocr: OcrEdits = OcrEdits(),
    protection: Optional[str] = KEEP,
) -> Path:
    """Build the edited PDF into a temp file beside `dest` and return its path.

    sources[0] is the base document: its catalog -- metadata, bookmarks, form
    definitions -- is what gets written. Pages from other sources are copied
    in, bringing their form fields, the named destinations their links use,
    and their bookmarks (under a folder named after the file).

    protection: security.KEEP (the base document's own protection, if any,
    unchanged), security.REMOVE (none), or a new open password (AES-256).

    allow_decrypt: indices of other encrypted sources whose pages the user
    agreed may come out unprotected. Only asked when the result has no
    protection; otherwise EncryptionWouldBeLost.

    annots: annotation edits per (file, source page); see annots.py.
    fields: filled-in form values per (file, field name); see fields.py.
    forms: form structure per (file, source page): fields added, moved,
        resized, renamed, deleted; see fields.FormEdits.
    ocr: recognised text layers per (file, source page); see ocrlayer.py.
    """
    dest = Path(dest)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{dest.stem}.", suffix=".tmp", dir=dest.parent)
    os.close(fd)
    tmp = Path(tmp_name)
    used = sorted({r.file for r in pages} | {0})
    try:
        with ExitStack() as stack:
            pdfs: Dict[int, pikepdf.Pdf] = {}
            for f in used:
                src = sources[f]
                pdf = stack.enter_context(pikepdf.open(src.path, password=src.password or ""))
                if f == 0:
                    protected = (protection not in (KEEP, REMOVE)
                                 or (protection is KEEP and pdf.is_encrypted))
                elif pdf.is_encrypted and not protected and f not in allow_decrypt:
                    raise EncryptionWouldBeLost(f, Path(src.path))
                highest = max((r.source for r in pages if r.file == f), default=-1)
                if len(pdf.pages) <= highest:
                    raise SaveError(
                        f"{Path(src.path).name} has {len(pdf.pages)} pages on disk but "
                        f"the edit refers to page {highest + 1}. Was the file changed "
                        f"by another program?"
                    )
                # Structure, then values, before anything is copied: field
                # names are the source's own until _merge_forms renames
                # clashes, and values may belong to fields added here.
                for (ff, source_page), widgets in forms.items():
                    if ff == f and source_page < len(pdf.pages):
                        apply_form_edits(pdf, source_page, widgets)
                apply_field_edits(pdf, fields.for_file(f))
                pdfs[f] = pdf
            base = pdfs[0]

            # Every page object the result will contain, keyed by (file, page).
            #
            # They must be the SAME objects throughout, because bookmarks,
            # links and form fields point at them by object number. Slice
            # assignment, insert and reverse all silently copy a page that's
            # already in the tree (checked, pikepdf 10), leaving every
            # reference aimed at a stale original. Emptying the tree and
            # appending the same Page objects back keeps their identity. qpdf
            # flattens the page tree on removal, pushing inherited /Resources,
            # /MediaBox, /CropBox and /Rotate down onto each page, so nothing
            # is lost when their old parent node goes away.
            placed: Dict[Tuple[int, int], pikepdf.Page] = {
                (0, r.source): base.pages[r.source] for r in pages if r.file == 0
            }
            while len(base.pages):
                del base.pages[-1]

            # Foreign pages. Plain append, not add_pages_from: the latter copies
            # form fields page by page, so a field with widgets on two copied
            # pages becomes two unrelated fields that no longer share a value
            # (checked, pikepdf 10). qpdf's own copier remembers everything it
            # has copied from a given file, so appending all of a file's pages
            # keeps shared fields shared -- and turns references to pages it
            # didn't copy into null instead of dragging those pages along.
            foreign_origin: Dict[Tuple[int, int], Tuple[int, Tuple[int, int]]] = {}
            with warnings.catch_warnings():
                # "widgets not reachable from /AcroForm": _merge_forms fixes that.
                warnings.simplefilter("ignore", pikepdf.PageCopyWarning)
                for f in used:
                    if f == 0:
                        continue
                    for r in pages:
                        if r.file != f:
                            continue
                        original = pdfs[f].pages[r.source]
                        base.pages.append(original)
                        page = base.pages[-1]
                        placed[(f, r.source)] = page
                        foreign_origin[page.obj.objgen] = (f, original.obj.objgen)
                    copied = [placed[(f, r.source)] for r in pages if r.file == f]
                    _merge_forms(base, pdfs[f], copied)
                    _resolve_named_links(base, pdfs[f], f, copied, foreign_origin)
            while len(base.pages):
                del base.pages[-1]

            expected = [placed[(r.file, r.source)] for r in pages]
            for page in expected:
                base.pages.append(page)
            if [p.obj.objgen for p in base.pages] != [p.obj.objgen for p in expected]:
                raise SaveError("page reorder did not preserve page identity")
            ocr_font = None
            for page, ref in zip(base.pages, pages):
                if ref.rotation:
                    page.rotate(ref.rotation, relative=True)
                # Before prune_dangling: edits address /Annots by index, and
                # pruning removes entries.
                edits = annots.get((ref.file, ref.source))
                if edits is not None:
                    apply_edits(base, page.obj, edits)
                words = ocr.get((ref.file, ref.source))
                if words:
                    if ocr_font is None:
                        ocr_font = _ocr_font(base)
                    _add_text_layer(base, page, words, ocr_font)

            _import_outlines(base, pdfs, sources, foreign_origin)

            # Deleted form fields left null placeholders so annotation
            # edits' indices held; now they can go.
            for page in base.pages:
                annots = page.obj.get("/Annots")
                if isinstance(annots, pikepdf.Array) and any(a is None for a in annots):
                    page.obj.Annots = pikepdf.Array([a for a in annots if a is not None])

            # Always, not just after deletes: with every page kept it finds
            # nothing, and one code path is one thing to get right.
            prune_dangling(base)

            if protection is KEEP:
                # True = qpdf copies the file's own encryption: same method,
                # passwords and restrictions, whichever password opened it
                # (checked, pikepdf 10). pikepdf's default would strip it.
                encryption = True if base.is_encrypted else None
                check_password = sources[0].password or ""
            elif protection == REMOVE:
                encryption, check_password = False, ""
            else:
                # An open password only (user's design): no restrictions, and
                # the same password as owner so whoever opens it can change it.
                encryption = pikepdf.Encryption(
                    user=protection, owner=protection, R=6,
                    allow=pikepdf.Permissions(modify_assembly=True))
                check_password = protection
            base.save(tmp, encryption=encryption)

        # Reopen and count before letting the caller replace anything -- and
        # confirm the protection is what was asked for, not just assumed.
        with pikepdf.open(tmp, password=check_password) as check:
            if len(check.pages) != len(pages):
                raise SaveError(
                    f"wrote {len(check.pages)} pages, expected {len(pages)}"
                )
            if check.is_encrypted != protected:
                raise SaveError("the saved file's password protection is not what was asked for")
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return tmp


def _merge_forms(base: pikepdf.Pdf, src: pikepdf.Pdf, copied: List[pikepdf.Page]) -> None:
    """Register form fields that arrived on copied pages with base's AcroForm.

    A top-level field whose name is already taken is renamed "name+1",
    "name+2"... -- same-named fields in one document share a value, which
    would silently tie together two unrelated forms.
    """
    tops: List[pikepdf.Dictionary] = []
    seen = set()
    for page in copied:
        for annot in page.obj.get("/Annots") or []:
            if not isinstance(annot, pikepdf.Dictionary) \
                    or annot.get("/Subtype") != pikepdf.Name.Widget:
                continue
            top, hops = annot, 0
            while isinstance(top.get("/Parent"), pikepdf.Dictionary) and hops < 64:
                top, hops = top.Parent, hops + 1
            if top.objgen not in seen:
                seen.add(top.objgen)
                tops.append(top)
    if not tops:
        return

    form = base.Root.get("/AcroForm")
    if not isinstance(form, pikepdf.Dictionary):
        form = base.Root.AcroForm = base.make_indirect(pikepdf.Dictionary(Fields=pikepdf.Array()))
    if "/Fields" not in form:
        form.Fields = pikepdf.Array()

    taken = {str(f.get("/T", "")) for f in form.Fields if isinstance(f, pikepdf.Dictionary)}
    for top in tops:
        name = str(top.get("/T", ""))
        if name and name in taken:
            n = 1
            while f"{name}+{n}" in taken:
                n += 1
            name = f"{name}+{n}"
            top.T = pikepdf.String(name)
        taken.add(name)
        form.Fields.append(top)

    # Fonts and defaults the copied fields' appearances refer to by name.
    src_form = src.Root.get("/AcroForm")
    if isinstance(src_form, pikepdf.Dictionary):
        if src_form.get("/NeedAppearances"):
            form.NeedAppearances = True
        if "/DA" in src_form and "/DA" not in form:
            form.DA = src_form.DA
        src_fonts = src_form.get("/DR", {}).get("/Font") if "/DR" in src_form else None
        if isinstance(src_fonts, pikepdf.Dictionary):
            if "/DR" not in form:
                form.DR = pikepdf.Dictionary()
            if "/Font" not in form.DR:
                form.DR.Font = pikepdf.Dictionary()
            for key, font in src_fonts.items():
                if key not in form.DR.Font:
                    form.DR.Font[key] = base.copy_foreign(font)


def _resolve_named_links(
    base: pikepdf.Pdf,
    src: pikepdf.Pdf,
    f: int,
    copied: List[pikepdf.Page],
    foreign_origin: Dict[Tuple[int, int], Tuple[int, Tuple[int, int]]],
) -> None:
    """Rewrite links on copied pages that jump by *name* into explicit targets.

    The name means something in the source file's name table, not base's --
    left alone it would resolve against base's destinations, possibly to a
    same-named one on the wrong page. Names that resolve to a copied page
    become explicit; anything else is nulled, and prune_dangling drops it.
    """
    named = _named_destinations(src)
    to_copy = {origin: base_objgen for base_objgen, (ff, origin) in foreign_origin.items()
               if ff == f}
    by_objgen = {p.obj.objgen: p.obj for p in copied}

    def explicit(dest):
        if not isinstance(dest, (pikepdf.Name, pikepdf.String)):
            return dest
        target = named.get(str(dest))
        if isinstance(target, pikepdf.Dictionary) and "/D" in target:
            target = target.D
        if isinstance(target, pikepdf.Array) and len(target) > 0 \
                and isinstance(target[0], pikepdf.Dictionary):
            page = by_objgen.get(to_copy.get(target[0].objgen))
            if page is not None:
                return pikepdf.Array([page] + [base.copy_foreign(target[i])
                                               for i in range(1, len(target))])
        return pikepdf.Array([None])

    for page in copied:
        for annot in page.obj.get("/Annots") or []:
            if not isinstance(annot, pikepdf.Dictionary) \
                    or annot.get("/Subtype") != pikepdf.Name.Link:
                continue
            if "/Dest" in annot:
                annot.Dest = explicit(annot.Dest)
            action = annot.get("/A")
            if isinstance(action, pikepdf.Dictionary) and action.get("/S") == pikepdf.Name.GoTo:
                action.D = explicit(action.D)


def _import_outlines(
    base: pikepdf.Pdf,
    pdfs: Dict[int, pikepdf.Pdf],
    sources: Sequence[Source],
    foreign_origin: Dict[Tuple[int, int], Tuple[int, int]],
) -> None:
    """Bring each inserted file's bookmarks across, under a folder per file.

    Only bookmarks that land on a copied page (or are folders holding such
    bookmarks) come along; their destinations are rewritten to the copies.
    Anything that doesn't resolve to a copied page is dropped rather than
    copied, since copying would drag the uncopied page into the file.
    """
    # (file, original page objgen) -> copied page object in base
    copied: Dict[Tuple[int, Tuple[int, int]], pikepdf.Object] = {}
    for page in base.pages:
        origin = foreign_origin.get(page.obj.objgen)
        if origin is not None:
            copied[origin] = page.obj

    for f, pdf in pdfs.items():
        if f == 0 or "/Outlines" not in pdf.Root:
            continue
        named = _named_destinations(pdf)

        def convert(item) -> Optional[pikepdf.OutlineItem]:
            children = [c for c in (convert(ch) for ch in item.children) if c is not None]
            dest = item.destination
            if dest is None and isinstance(item.action, pikepdf.Dictionary) \
                    and item.action.get("/S") == pikepdf.Name.GoTo:
                dest = item.action.get("/D")
            if isinstance(dest, (pikepdf.Name, pikepdf.String)):
                dest = named.get(str(dest))
            if isinstance(dest, pikepdf.Dictionary) and "/D" in dest:
                dest = dest.D
            new_dest = None
            if isinstance(dest, pikepdf.Array) and len(dest) > 0 \
                    and isinstance(dest[0], pikepdf.Dictionary) and dest[0].is_indirect:
                target = copied.get((f, dest[0].objgen))
                if target is not None:
                    new_dest = pikepdf.Array([target] + [dest[i] for i in range(1, len(dest))])
            if new_dest is None and not children:
                return None
            new = pikepdf.OutlineItem(str(item.title))
            if new_dest is not None:
                new.destination = new_dest
            new.children.extend(children)
            return new

        with pdf.open_outline() as source_outline:
            imported = [c for c in (convert(i) for i in source_outline.root) if c is not None]
        if not imported:
            continue
        first_page = next(
            (page for page in base.pages
             if foreign_origin.get(page.obj.objgen, (None,))[0] == f), None)
        folder = pikepdf.OutlineItem(Path(sources[f].path).name,
                                     first_page.index if first_page is not None else 0)
        folder.children.extend(imported)
        with base.open_outline() as outline:
            outline.root.append(folder)


def _add_text_layer(pdf: pikepdf.Pdf, page: pikepdf.Page, words: Sequence[OcrWord],
                    font: pikepdf.Object) -> None:
    """Draw recognised words invisibly over the page (ocrlayer.py), after
    its own content. That content is wrapped in q/Q first: a scan's stream
    that leaves its transformation matrix changed would otherwise move every
    word away from its ink."""
    name = page.add_resource(font, pikepdf.Name.Font, prefix="OCR")
    page.contents_add(pdf.make_stream(b"q\n"), prepend=True)
    page.contents_add(pdf.make_stream(b"\nQ\n" + ocr_content(words, str(name).lstrip("/"))))


def _ocr_font(pdf: pikepdf.Pdf) -> pikepdf.Object:
    """The glyphless text-layer font (ocrlayer.py); one per saved file."""
    N = pikepdf.Name
    descriptor = pikepdf.Dictionary({
        k: (N(v) if isinstance(v, str) else pikepdf.Array(v) if isinstance(v, list) else v)
        for k, v in FONT_DESCRIPTOR.items()})
    cid_font = pikepdf.Dictionary(
        Type=N.Font, Subtype=N.CIDFontType2, BaseFont=N("/" + FONT_NAME),
        CIDSystemInfo=pikepdf.Dictionary(Registry=pikepdf.String("Adobe"),
                                         Ordering=pikepdf.String("Identity"), Supplement=0),
        FontDescriptor=pdf.make_indirect(descriptor), DW=GLYPH_WIDTH, CIDToGIDMap=N.Identity)
    font = pdf.make_indirect(pikepdf.Dictionary(
        Type=N.Font, Subtype=N.Type0, BaseFont=N("/" + FONT_NAME), Encoding=N("/Identity-H"),
        DescendantFonts=pikepdf.Array([pdf.make_indirect(cid_font)]),
        ToUnicode=pdf.make_stream(TO_UNICODE)))
    return font


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
            if page is None:
                # qpdf nulls references to pages it didn't copy from another
                # file, so a null target is a link to a page left behind.
                return False
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
