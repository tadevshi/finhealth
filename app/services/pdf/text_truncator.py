"""Truncate extracted PDF text to a size suitable for small LLMs.

Small local models (qwen2.5:1.5b, llama3.2:1b) cannot handle long
prompts reliably. A full Chilean bank statement produces ~18k chars
of Markdown via markitdown, but these models need <5k chars to
generate valid JSON.

This module finds the relevant sections (transaction tables) and
returns a truncated excerpt that keeps the transactions section
(marker-anchored) and falls back to a head slice when no marker
is found.

Strategy
--------

1. If the text is already short enough, return it as-is.
2. Look for section markers (PERÍODO ACTUAL, INFORMACIÓN DE
   TRANSACCIONES, DETALLE) and prefer text starting at the first
   match. Markers within the first ~100 chars are ignored as header
   noise (the CMF statement repeats the period label in the header).
3. If no section markers are found, return the first ``max_chars``
   characters (the cardholder / period info is always at the start
   of the statement).
"""

from __future__ import annotations

from typing import Final

#: Maximum number of characters to send to the LLM. Small local
#: models start producing malformed JSON above this threshold.
DEFAULT_MAX_CHARS: Final = 5000

#: Markers that indicate the start of a transactions section. The
#: first match wins. Order matters: more specific markers come first.
_TRANSACTION_SECTION_MARKERS: Final = (
    "INFORMACIÓN DE TRANSACCIONES",
    "PERÍODO ACTUAL",
    "DETALLE DE OPERACIONES",
    "DETALLE",
    "TRANSACCIONES",
)

#: Markers that indicate the END of the transactions section
#: (boilerplate that we don't need for extraction). Currently
#: reserved for future use — the marker-anchored head slice does
#: not need a tail trim because ``max_chars`` is the hard cap.
_TRANSACTION_END_MARKERS: Final = (
    "INFORMACIÓN DE PAGO",
    "COSTOS POR ATRASO",
    "TOTAL FACTURADO",
    "RESUMEN",
    "PAGO MÍNIMO",
)

#: Markers that appear in the first ~100 chars are treated as
#: header noise (the bank restates the period label in the page
#: header). Anything past this offset is considered a real
#: transactions section anchor.
_HEADER_NOISE_OFFSET: Final = 100


def truncate_for_llm(text: str, max_chars: int = DEFAULT_MAX_CHARS) -> str:
    """Return a truncated excerpt of ``text`` suitable for LLM extraction.

    Parameters
    ----------
    text
        The full Markdown text extracted from the PDF.
    max_chars
        Maximum number of characters to return. Default 5000.

    Returns
    -------
    str
        The truncated text, no longer than ``max_chars``. When a
        transactions section marker is found past the header-noise
        offset, the result starts at that marker; otherwise it
        starts at the beginning of the text (so the cardholder /
        period info is preserved).
    """
    if len(text) <= max_chars:
        return text

    # Find the start of the transactions section. The first marker
    # in priority order wins, but only if it sits past the header
    # noise offset — the period label also appears in the page
    # header so a near-zero match would be a false positive.
    start_idx = 0
    for marker in _TRANSACTION_SECTION_MARKERS:
        idx = text.find(marker)
        if idx > _HEADER_NOISE_OFFSET:
            start_idx = idx
            break

    if start_idx > 0:
        return text[start_idx : start_idx + max_chars]

    return text[:max_chars]


__all__ = ["DEFAULT_MAX_CHARS", "truncate_for_llm"]
