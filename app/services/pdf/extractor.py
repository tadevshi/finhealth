"""Extract plain text from a (decrypted) PDF with :mod:`pdfplumber`.

The extraction step is the bridge between the binary PDF world and
the structured LLM extraction that follows. We want a *clean,
linear* text representation:

* one logical "line" per PDF text line (i.e. one per row of the
  bank statement table),
* concatenated across pages in document order, separated by a
  single newline so downstream regexes like
  ``r"ESTADO DE CUENTA INTERNACIONAL"`` see a single string.

The temptation to call :func:`pdfplumber.Page.extract_tables` is
strong — the statements *are* tables, after all — but the CMF
layout is wide enough that ``pdfplumber`` misaligns columns on
~30% of pages, producing rows where the date and the amount live
in different cells than the visible PDF. :func:`Page.extract_text`
returns the underlying character stream in visual order, which
preserves column alignment implicitly. The cost is that the
parser has to know about spacing, but the variant and amount
detectors already do.

Failure modes
-------------

* **Encrypted PDF** — :mod:`pdfplumber` raises
  ``pdfminer.pdfdocument.PDFPasswordIncorrect`` because it does
  not accept a password. We translate this to
  :class:`TextExtractionError`. The pipeline must decrypt first.
* **Corrupted PDF** — :mod:`pdfplumber` raises
  ``pdfminer.pdfdocument.PDFSyntaxError`` or simply returns empty
  pages. We surface both as :class:`TextExtractionError` with a
  clear message.
* **Empty pages** — :func:`Page.extract_text` returns ``None`` for
  a page with no extractable text (e.g. a scanned image). We
  treat ``None`` as the empty string so the joined output stays
  well-formed.
"""

from __future__ import annotations

from pathlib import Path

import pdfplumber
from pdfminer.pdfdocument import PDFPasswordIncorrect
from pdfplumber.utils.exceptions import PdfminerException


class TextExtractionError(Exception):
    """Raised when text cannot be extracted from a PDF.

    The base class covers all extraction failures — encrypted
    input, syntax errors, missing pages. The ``__cause__`` always
    carries the underlying library exception.
    """


def extract_text(pdf_path: Path) -> str:
    """Return the full text content of ``pdf_path``, page by page.

    Pages are joined with a single newline. ``None`` returns from
    :func:`pdfplumber.Page.extract_text` (typical for image-only
    pages) become the empty string so the join is safe and the
    caller never has to defend against ``None``.

    Parameters
    ----------
    pdf_path:
        Path to a *decrypted* PDF. Passing an encrypted PDF
        produces :class:`TextExtractionError` — the caller must
        route the file through :mod:`app.services.pdf.decryptor`
        first.

    Returns
    -------
    str
        Concatenated page text. The string may contain newlines
        but is never ``None`` and never empty unless the document
        has no extractable text at all.

    Raises
    ------
    FileNotFoundError
        If ``pdf_path`` does not exist. Re-raised from the
        underlying ``open`` call (not wrapped) so the caller can
        distinguish a missing file from any other extraction
        failure.
    TextExtractionError
        For any extraction failure (encrypted input, syntax
        error, page iteration error).
    """
    try:
        with pdfplumber.open(pdf_path) as pdf:
            return "\n".join(page.extract_text() or "" for page in pdf.pages)
    except FileNotFoundError:
        # Re-raise so the caller can distinguish a missing file
        # from any other extraction failure (e.g. encrypted).
        raise
    except PdfminerException as exc:
        # ``pdfplumber`` wraps pdfminer errors in a single
        # ``PdfminerException`` class. The underlying cause is
        # preserved in ``__context__`` because pdfplumber raises
        # with ``raise X from None`` semantics — the cause is on
        # the context, not the explicit cause.
        cause = exc.__context__ or exc
        if isinstance(cause, PDFPasswordIncorrect):
            raise TextExtractionError(
                f"PDF {pdf_path} is still password-protected; decrypt it first"
            ) from cause
        raise TextExtractionError(f"Failed to extract text from {pdf_path}: {exc}") from cause
    except Exception as exc:  # pdfplumber raises broad pdfminer exceptions
        # Keep the message but preserve the underlying type so
        # callers can still introspect via ``__cause__``.
        raise TextExtractionError(f"Failed to extract text from {pdf_path}: {exc}") from exc
