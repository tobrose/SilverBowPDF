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
            # along. Page objects are collected before the slice assignment
            # because it rewrites the list they index into.
            new_pages = [pdf.pages[r.source] for r in pages]
            pdf.pages[:] = new_pages
            for page, ref in zip(pdf.pages, pages):
                if ref.rotation:
                    page.rotate(ref.rotation, relative=True)

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
