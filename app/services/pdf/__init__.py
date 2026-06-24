"""PDF ingestion service layer.

The modules in this subpackage form the deterministic half of the
ingestion pipeline. They take a raw, possibly encrypted PDF and
produce a clean UTF-8 text plus a parsed set of monetary amounts
suitable for the LLM extraction step (added in a later work unit).

Module map
----------

* :mod:`app.services.pdf.password_deriver` — derives the per-bank
  PDF password from a user-supplied RUT and the bank's
  ``password_formula``.
* :mod:`app.services.pdf.decryptor` — opens an encrypted PDF with
  :mod:`pikepdf` and writes a decrypted copy to disk.
* :mod:`app.services.pdf.extractor` — pulls plain text out of a
  decrypted PDF using :func:`pdfplumber.Page.extract_text`
  (deliberately not :func:`~pdfplumber.Page.extract_tables`, which
  fails on the wide CMF-mandated layout).
* :mod:`app.services.pdf.variant_detector` — decides whether a
  statement is the ``NACIONAL`` (CLP) or ``INTERNACIONAL`` (USD)
  variant by looking for the bank's section header.
* :mod:`app.services.pdf.amount_parser` — converts a raw
  amount string (``"$ 1.234.567"`` or ``"US$ 1.234,56"``) to a
  :class:`decimal.Decimal` with no floating-point drift.
* :mod:`app.services.pdf.text_truncator` — clips the extracted
  text to a size small local LLMs can handle while keeping the
  transactions section.

Each module exposes a small set of pure functions or a single
class with a few methods. No module here touches the database, the
HTTP layer, or the LLM provider — those integrations are layered
on top in the orchestrator (added in a later work unit).
"""

from app.services.pdf.amount_parser import AmountParseError, parse_amount
from app.services.pdf.decryptor import PDFDecryptError, PDFPasswordError, decrypt_pdf
from app.services.pdf.extractor import TextExtractionError, extract_text
from app.services.pdf.password_deriver import (
    InvalidPasswordFormulaError,
    InvalidRUTError,
    derive_password,
)
from app.services.pdf.text_truncator import DEFAULT_MAX_CHARS, truncate_for_llm
from app.services.pdf.variant_detector import (
    Variant,
    VariantDetectionError,
    detect_variant,
)

__all__ = [
    "DEFAULT_MAX_CHARS",
    "AmountParseError",
    "InvalidPasswordFormulaError",
    "InvalidRUTError",
    "PDFDecryptError",
    "PDFPasswordError",
    "TextExtractionError",
    "Variant",
    "VariantDetectionError",
    "decrypt_pdf",
    "derive_password",
    "detect_variant",
    "extract_text",
    "parse_amount",
    "truncate_for_llm",
]
