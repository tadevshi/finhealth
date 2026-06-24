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
2. If a ``variant`` is provided and the text contains BOTH
   variants (some PDFs bundle NACIONAL + INTERNACIONAL in one
   file), trim the text to the section that matches the variant
   and drop the other section entirely. This prevents the small
   LLM from reading transactions from the wrong currency section.
3. Look for section markers (PERÍODO ACTUAL, INFORMACIÓN DE
   TRANSACCIONES, DETALLE) and prefer text starting at the first
   match. Markers within the first ~100 chars are ignored as header
   noise (the CMF statement repeats the period label in the header).
4. If no section markers are found, return the first ``max_chars``
   characters (the cardholder / period info is always at the start
   of the statement).
"""

from __future__ import annotations

from typing import Final, Literal

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

#: Markers that start the INTERNACIONAL (USD) section of a statement.
#: Used to strip the INTERNACIONAL section when the variant we want
#: is NACIONAL (so the LLM doesn't get confused by USD transactions).
_INTERNACIONAL_SECTION_START: Final = (
    "ESTADO DE CUENTA INTERNACIONAL",
    "INTERNACIONAL",
)

#: Markers that start the NACIONAL (CLP) section of a statement.
#: Used to strip the NACIONAL section when the variant we want is
#: INTERNACIONAL.
_NACIONAL_SECTION_START: Final = (
    "ESTADO DE CUENTA NACIONAL",
)


def _strip_other_variant(
    text: str, variant: Literal["NACIONAL", "INTERNACIONAL"]
) -> str:
    """Drop the OTHER variant's section from ``text``.

    Some Chilean bank statements bundle NACIONAL (CLP) and
    INTERNACIONAL (USD) sections in a single PDF. When the
    detector picks one variant but the LLM receives both, it
    sometimes returns transactions from the wrong currency
    section (e.g. USD rows when we asked for NACIONAL/CLP).
    This helper trims the text so only the chosen variant's
    section remains.

    Returns
    -------
    str
        ``text`` unchanged if no other-variant section is found,
        or ``text`` with the other-variant section removed.
    """
    if variant == "NACIONAL":
        # Drop everything from the start of the INTERNACIONAL section
        # to the end of the document.
        for marker in _INTERNACIONAL_SECTION_START:
            idx = text.find(marker)
            if idx > _HEADER_NOISE_OFFSET:
                return text[:idx]
    else:  # INTERNACIONAL
        # Drop everything from the start of the document up to the
        # INTERNACIONAL section. The NACIONAL section is at the
        # start, so we keep text from the INTERNACIONAL marker onward.
        # We still want the header info (cardholder, card number)
        # from the NACIONAL section, so look for a header marker
        # at the start of the document instead.
        # The header (cardholder, card number, period) is in the
        # first ~500 chars of every CMF statement. Keep that, then
        # jump to the INTERNACIONAL section.
        for marker in _INTERNACIONAL_SECTION_START:
            idx = text.find(marker)
            if idx > _HEADER_NOISE_OFFSET:
                # Keep the header (first ~500 chars) + INTERNACIONAL section
                header = text[:500]
                return header + text[idx:]
    return text


def truncate_for_llm(
    text: str,
    max_chars: int = DEFAULT_MAX_CHARS,
    variant: Literal["NACIONAL", "INTERNACIONAL"] | None = None,
) -> str:
    """Return a truncated excerpt of ``text`` suitable for LLM extraction.

    Parameters
    ----------
    text
        The full Markdown text extracted from the PDF.
    max_chars
        Maximum number of characters to return. Default 5000.
    variant
        The detected statement variant (``"NACIONAL"`` for CLP,
        ``"INTERNACIONAL"`` for USD). When provided and the text
        contains BOTH variants, only the matching section is
        kept. When ``None``, the first transactions section is
        used regardless of currency.

    Returns
    -------
    str
        The truncated text, no longer than ``max_chars``. When a
        transactions section marker is found past the header-noise
        offset, the result starts at that marker; otherwise it
        starts at the beginning of the text (so the cardholder /
        period info is preserved).
    """
    if variant is not None:
        text = _strip_other_variant(text, variant)

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
