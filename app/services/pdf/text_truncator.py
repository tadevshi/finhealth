"""Truncate extracted PDF text to a size suitable for small LLMs.

Small local models (qwen2.5:1.5b, llama3.2:1b) cannot handle long
prompts reliably. A full Chilean bank statement produces ~18k chars
of Markdown via markitdown, but these models need <5k chars to
generate valid JSON.

This module exposes two helpers that hand the right slice of the
PDF text to the LLM:

* :func:`truncate_for_llm` — returns a single ``max_chars``-long
  excerpt, anchored to the transactions section marker. Useful
  for callers that only need a single window.
* :func:`chunk_for_llm` — splits the full document into
  overlapping windows of ``max_chars`` chars. The orchestrator
  calls the LLM once per chunk and merges the responses, so a
  18k-char statement produces every transaction instead of
  the first 27% of them.

Strategy for :func:`truncate_for_llm`
------------------------------------

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

Strategy for :func:`chunk_for_llm`
---------------------------------

1. Apply the same variant-aware stripping as above so chunks
   never include the other variant's section.
2. If the text fits in a single chunk, return it as a one-element
   list — the common case for short test inputs and the fast path
   in production.
3. Otherwise, walk the text with a sliding window of ``max_chars``
   and ``overlap_chars`` between consecutive windows. At each
   window boundary, try to snap to a newline so we never split a
   table row mid-line (transactions are rendered as pipe-delimited
   Markdown rows; a mid-row split would confuse the LLM).
4. Deduplication of transactions that straddle a window boundary
   is the caller's responsibility — the same row will appear in
   the overlap region of two adjacent chunks.
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

#: Default overlap (in chars) between consecutive chunks. ~200 chars
#: covers a full transaction row even for the verbose Santander
#: layout, and small enough that we don't double the LLM cost on
#: a typical statement.
DEFAULT_CHUNK_OVERLAP_CHARS: Final = 200


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


def _find_transactions_section_start(text: str) -> int:
    """Return the index of the first transactions section marker.

    The first marker in priority order wins, but only if it sits
    past the header-noise offset — the period label also appears
    in the page header so a near-zero match would be a false
    positive. Returns ``0`` when no marker qualifies, so the
    caller can fall back to the head slice.
    """
    for marker in _TRANSACTION_SECTION_MARKERS:
        idx = text.find(marker)
        if idx > _HEADER_NOISE_OFFSET:
            return idx
    return 0


def _align_to_newline(text: str, position: int) -> int:
    """Snap ``position`` backward to the previous newline in ``text``.

    Used to avoid splitting a row of the transactions table
    mid-line when chunking. Returns ``position`` unchanged when
    no newline exists at or before ``position`` (the chunk is
    already aligned or the text has no newlines).

    Snapping **backward** keeps each chunk's length at most
    ``max_chars`` — any text we drop is picked up by the next
    chunk because its start position is past the snap point.
    """
    newline = text.rfind("\n", 0, position)
    if newline == -1:
        return position
    return newline + 1  # include the newline in the previous chunk


def truncate_for_llm(
    text: str,
    max_chars: int = DEFAULT_MAX_CHARS,
    variant: Literal["NACIONAL", "INTERNACIONAL"] | None = None,
) -> str:
    """Return a truncated excerpt of ``text`` suitable for LLM extraction.

    The slice is **marker-anchored**: when a transactions section
    marker is found past the header-noise offset, the result
    starts at that marker (the header / period info is dropped
    because the LLM does not need it to extract rows). When no
    marker is found, the result is the head slice (the cardholder
    / period info is always at the start of the statement).

    This is a thin wrapper that returns the first chunk produced
    by :func:`chunk_for_llm` after applying the marker-anchored
    trimming. Useful for callers that only need a single window;
    the production pipeline uses :func:`chunk_for_llm` so the
    full statement is processed.

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

    # Fast path: short text is returned as-is.
    if len(text) <= max_chars:
        return text

    # Find the start of the transactions section. The first marker
    # in priority order wins, but only if it sits past the header
    # noise offset — the period label also appears in the page
    # header so a near-zero match would be a false positive.
    start_idx = _find_transactions_section_start(text)

    if start_idx > 0:
        return text[start_idx : start_idx + max_chars]

    return text[:max_chars]


def chunk_for_llm(
    text: str,
    max_chars: int = DEFAULT_MAX_CHARS,
    variant: Literal["NACIONAL", "INTERNACIONAL"] | None = None,
    overlap_chars: int = DEFAULT_CHUNK_OVERLAP_CHARS,
) -> list[str]:
    """Split ``text`` into overlapping chunks for LLM extraction.

    The whole document is split (no marker-anchoring) so the
    header / metadata at the top of the document lands in the
    first chunk and the transactions table spans the rest.
    Chunks overlap by ``overlap_chars`` so a transaction that
    straddles a window boundary is still present in at least
    one chunk in full. The consumer is responsible for
    deduplicating transactions across chunks.

    Parameters
    ----------
    text
        The full Markdown text extracted from the PDF.
    max_chars
        Maximum number of characters per chunk. Default 5000.
        Each chunk is at most this long (the last chunk may be
        shorter).
    variant
        The detected statement variant (``"NACIONAL"`` for CLP,
        ``"INTERNACIONAL"`` for USD). When provided and the text
        contains BOTH variants, the matching section is kept
        before chunking. When ``None``, the text is chunked as-is.
    overlap_chars
        Number of characters of overlap between consecutive
        chunks. The default (200) is large enough to cover a
        full transaction row in the verbose Santander layout.
        Set to 0 to disable overlap (transactions at chunk
        boundaries will be lost).

    Returns
    -------
    list[str]
        Non-empty list of text chunks. Length is ``ceil(len(text)
        / (max_chars - overlap_chars))`` for the long-text path
        and ``1`` for the short-text fast path.
    """
    if variant is not None:
        text = _strip_other_variant(text, variant)

    text_len = len(text)
    if text_len <= max_chars:
        return [text]

    # Minimum chunk length to bother snapping. When a row is
    # longer than ``max_chars`` the snap would leave a tiny
    # chunk (one or two chars of the next row's tail) and
    # force ``start`` to advance by one char at a time —
    # turning the loop into a quadratic scan of the document.
    # The 50% threshold is the smallest that still gives a
    # useful newline-aligned split for real statements (rows
    # are typically 80-200 chars, ``max_chars`` is 5000+).
    min_aligned_length = max_chars // 2

    chunks: list[str] = []
    start = 0
    while start < text_len:
        end = min(start + max_chars, text_len)

        # When we are not at the end of the document, snap
        # the chunk boundary back to the most recent newline
        # so we never split a Markdown table row mid-line.
        # For an input with no newlines (``"x" * N``) the
        # snap is a no-op and the chunk ends exactly at
        # ``max_chars``. When the row is longer than
        # ``max_chars`` the snap is skipped (see
        # ``min_aligned_length``).
        if end < text_len:
            aligned = _align_to_newline(text, end)
            if aligned - start >= min_aligned_length:
                end = aligned

        chunks.append(text[start:end])

        if end >= text_len:
            break

        # Slide the window. The next chunk starts ``overlap_chars``
        # before the end of the current one, so any row that
        # straddled the boundary is fully present in the next
        # chunk. The ``start + 1`` floor guarantees forward
        # progress even if ``overlap_chars >= max_chars``.
        start = max(end - overlap_chars, start + 1)

    return chunks


__all__ = [
    "DEFAULT_CHUNK_OVERLAP_CHARS",
    "DEFAULT_MAX_CHARS",
    "chunk_for_llm",
    "truncate_for_llm",
]
