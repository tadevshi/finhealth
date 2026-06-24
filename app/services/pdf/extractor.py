"""Extract structured Markdown from a (decrypted) PDF with :mod:`markitdown`.

The extraction step is the bridge between the binary PDF world and
the structured LLM extraction that follows. We previously used
:func:`pdfplumber.Page.extract_text` here, which returns the
character stream in visual order. That works for the deterministic
*variant detector* and *amount parser* (they only need anchored
substrings), but it produces a flat, dense wall of text that small
LLMs (e.g. ``llama3.2:3b``) struggle to parse and tend to time out
on: the model has to re-infer the table structure from arbitrary
whitespace and column alignment that mean nothing in plain text.

Microsoft's :mod:`markitdown` solves this by converting the PDF
into **structured Markdown**:

* Tables become pipe-delimited ``| col | col |`` rows.
* Multi-column layouts are resolved into a single reading order.
* Headings, lists, and emphasis are preserved when the underlying
  PDF exposes them.

LLMs are trained on Markdown, so the model spends its context on
*content* rather than re-deriving layout. Empirically (see the
benchmark in the PR description) this drops extraction wall time
on the same real statement PDF from >120s with ``llama3.2:3b`` to
under 60s, and improves JSON output accuracy on multi-column
statements.

Architecture
------------

``markitdown`` itself uses ``pdfplumber`` for pages that contain
form-style content (tables, aligned columns) and ``pdfminer.six``
as a fallback for plain prose pages. We still depend on
``pdfplumber`` because the variant detector and the tests in
``tests/test_pdf_services.py`` inspect the raw character stream;
we do not need to keep two implementations of the extraction step.

Failure modes
-------------

* **Encrypted PDF** — :mod:`markitdown` re-raises
  ``pdfminer.pdfdocument.PDFPasswordIncorrect`` because it does
  not accept a password. We translate this to
  :class:`TextExtractionError`. The pipeline must decrypt first.
* **Corrupted PDF** — :mod:`markitdown` raises
  ``pdfminer.pdfdocument.PDFSyntaxError`` or a generic
  :class:`markitdown.FileConversionException`. Both are surfaced as
  :class:`TextExtractionError` with a clear message.
* **Missing dependency** — :mod:`markitdown` raises
  :class:`markitdown.MissingDependencyException` if the
  ``[all]`` extra was not installed at install time. We translate
  this to :class:`TextExtractionError` so the caller does not have
  to know about markitdown's internal exception hierarchy.
"""

from __future__ import annotations

from pathlib import Path

from markitdown import FileConversionException, MarkItDown, MissingDependencyException


class TextExtractionError(Exception):
    """Raised when text cannot be extracted from a PDF.

    The base class covers all extraction failures — encrypted
    input, syntax errors, missing markitdown extras, missing pages.
    The ``__cause__`` always carries the underlying library
    exception.
    """


def extract_text(pdf_path: Path) -> str:
    """Return the full Markdown content of ``pdf_path``.

    The returned string is the markitdown ``text_content`` — i.e.
    the Markdown rendering of every page concatenated in document
    order, with form-style pages rendered as aligned Markdown
    tables and prose pages rendered as plain text paragraphs.

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
        Concatenated Markdown text. The string may contain
        newlines and pipe-delimited table rows, but is never
        ``None`` and never empty unless the document has no
        extractable content at all.

    Raises
    ------
    FileNotFoundError
        If ``pdf_path`` does not exist. Re-raised from the
        underlying ``open`` call (not wrapped) so the caller can
        distinguish a missing file from any other extraction
        failure.
    TextExtractionError
        For any extraction failure (encrypted input, syntax
        error, missing markitdown extras, page iteration error).
    """
    md = MarkItDown()
    try:
        result = md.convert(str(pdf_path))
    except FileNotFoundError:
        # Re-raise so the caller can distinguish a missing file
        # from any other extraction failure (e.g. encrypted).
        raise
    except MissingDependencyException as exc:
        # ``markitdown[all]`` was not installed. This is a
        # deployment bug, not a user error, but we surface it as
        # :class:`TextExtractionError` so the orchestrator does
        # not have to know about markitdown's internal exception
        # hierarchy.
        raise TextExtractionError(
            f"markitdown is missing a required dependency to convert {pdf_path}: {exc}"
        ) from exc
    except FileConversionException as exc:
        # ``markitdown`` wraps every PDF failure (encrypted input,
        # syntax error, empty stream) into a single
        # :class:`FileConversionException`. Unfortunately, it does
        # **not** preserve the original exception on ``__cause__``
        # — the underlying ``pdfminer`` exception is only reachable
        # by parsing the message string. The text of the message
        # is stable across markitdown versions (it comes from the
        # ``PdfConverter.convert`` source), so we duck-type on the
        # marker token.
        message = str(exc)
        if "PDFPasswordIncorrect" in message:
            raise TextExtractionError(
                f"PDF {pdf_path} is still password-protected; decrypt it first"
            ) from exc
        raise TextExtractionError(f"Failed to extract text from {pdf_path}: {exc}") from exc
    except Exception as exc:  # pragma: no cover - defensive net
        # Keep the message but preserve the underlying type so
        # callers can still introspect via ``__cause__``.
        raise TextExtractionError(f"Failed to extract text from {pdf_path}: {exc}") from exc

    text = result.text_content
    # ``markitdown`` is contractually required to return ``str``,
    # but the type annotation is ``str | None`` to mirror the
    # underlying ``pdfplumber.Page.extract_text`` signature.
    # Normalise to ``str`` so downstream code never has to defend
    # against ``None``.
    return text if text is not None else ""
