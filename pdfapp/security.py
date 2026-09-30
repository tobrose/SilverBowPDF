"""What a protected PDF lets this user do, and the protection to save with.
No libs.

A PDF's author can restrict what people without the owner password may do
(print, copy, edit...). SilverbowPDF honours that, as Acrobat does (user's
call): restricted features are off, and the owner password unlocks them.
Access is read from pdfium's *effective* permissions for the password the
file was opened with -- all set when that was the owner password.

Protection to save (DocState.protection), set from File > Password
Protection:
    KEEP        the file's own protection, unchanged -- passwords, strength,
                restrictions (qpdf copies it; no password needed to do so)
    REMOVE      no protection at all (only offered with full access)
    "<pw>"      a new open password, AES-256, no restrictions
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

KEEP: Optional[str] = None
REMOVE = ""

# PDF permission bits (ISO 32000 table 22), as pdfium returns them.
_PRINT, _MODIFY, _COPY, _ANNOTATE = 1 << 2, 1 << 3, 1 << 4, 1 << 5
_FILL, _ASSEMBLE, _PRINT_HIGH = 1 << 8, 1 << 10, 1 << 11
_ALL = _PRINT | _MODIFY | _COPY | _ANNOTATE | _FILL | _ASSEMBLE | _PRINT_HIGH


@dataclass(frozen=True)
class Access:
    encrypted: bool = False
    print: bool = True
    print_high: bool = True   # without it, printing is low resolution
    copy: bool = True         # copy text; extract pages
    annotate: bool = True     # comments, marks, typed text, shapes, stamps
    fill_forms: bool = True
    modify: bool = True       # page content and form structure: OCR, field authoring
    assemble: bool = True     # insert, delete, rotate, move pages

    @property
    def full(self) -> bool:
        """Everything allowed: the owner, or a file with no restrictions.
        Only then may its protection be changed or removed."""
        return all((self.print, self.print_high, self.copy, self.annotate,
                    self.fill_forms, self.modify, self.assemble))

    def restricted(self) -> List[str]:
        """What's off, in words for the user; empty if nothing is."""
        out = []
        if not (self.annotate and self.modify and self.assemble):
            if not (self.annotate or self.modify or self.assemble):
                out.append("editing")
            else:
                if not self.annotate:
                    out.append("adding comments and marks")
                if not self.assemble:
                    out.append("changing pages")
                if not self.modify:
                    out.append("changing content and form fields")
        if not self.fill_forms:
            out.append("filling in forms")
        if not self.print:
            out.append("printing")
        elif not self.print_high:
            out.append("high-quality printing")
        if not self.copy:
            out.append("copying text")
        return out


FULL_ACCESS = Access()


def access_from_bits(revision: int, bits: int) -> Access:
    """revision: the security handler's (-1 if not encrypted); bits: the
    effective permission bits pdfium reports for the password used."""
    if revision < 0:
        return FULL_ACCESS
    bits &= 0xFFFFFFFF
    has = lambda b: bool(bits & b)  # noqa: E731
    if revision == 2:
        # Revision 2 predates the finer bits: they follow the coarse ones.
        return Access(True, has(_PRINT), has(_PRINT), has(_COPY), has(_ANNOTATE),
                      has(_ANNOTATE), has(_MODIFY), has(_MODIFY))
    return Access(True, has(_PRINT), has(_PRINT) and has(_PRINT_HIGH), has(_COPY),
                  has(_ANNOTATE), has(_FILL) or has(_ANNOTATE), has(_MODIFY),
                  has(_ASSEMBLE) or has(_MODIFY))
