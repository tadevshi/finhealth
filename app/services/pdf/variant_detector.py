"""Detect the statement variant (NACIONAL vs INTERNACIONAL).

The three Chilean banks we support all follow the CMF-mandated
state-of-account template, but the template has two
incompatible variants:

* ``NACIONAL`` — local currency, amounts in CLP, presented as
  ``"$ 1.234.567"``.
* ``INTERNACIONAL`` — US dollars, amounts in USD, presented as
  ``"US$ 1.234,56"``.

The variant is structural, not visual: the row layout, the
column count, and the subtotal letters all differ between the
two. The LLM extractor in WU 3 needs the variant up front because
the prompt template, the few-shot examples, and the amount
formatter are all different.

Detection is deterministic and cheap, which is the whole point of
doing it in code (rather than asking the LLM). The bank's section
title — *the first big header after the per-page divider* — is
the most reliable signal:

* Banco de Chile and Itaú use the literal headings
  ``"ESTADO DE CUENTA NACIONAL"`` and
  ``"ESTADO DE CUENTA INTERNACIONAL"`` respectively.
* Santander writes ``"ESTADO DE CUENTA EN MONEDA NACIONAL"`` for
  the CLP variant (the ``"EN MONEDA"`` filler is the
  differentiator) and
  ``"ESTADO DE CUENTA INTERNACIONAL"`` for the USD variant.

Some banks — Banco de Chile in particular — *append* an
INTERNACIONAL section after the NACIONAL one in the same PDF.
In that case both anchors are present in the text; the variant
is the *first* anchor that appears, because the bank treats the
CLP section as primary and the USD section as a supplement. We
implement this rule by finding the position of every anchor and
returning the one that appears earliest. ``-1`` (not found) is
treated as "after the end" so it never wins against a found
anchor.

The function never silently returns a default. If the input does
not look like a CMF state-of-account, the caller almost certainly
fed it the wrong file and should fail loudly — that is the
project's "raise specific exceptions" rule at work.
"""

from __future__ import annotations

from typing import Final, Literal

Variant = Literal["NACIONAL", "INTERNACIONAL"]


class VariantDetectionError(ValueError):
    """Raised when the variant cannot be determined from ``text``.

    The error message includes a hint about which anchors were
    looked for, so the operator can see at a glance whether the
    text is from a bank we do not support yet or is simply
    malformed.
    """


#: Anchor for the international (USD) variant header.
_ANCHOR_INTERNACIONAL: Final = "ESTADO DE CUENTA INTERNACIONAL"

#: Primary anchor for the national (CLP) variant header. Banco
#: de Chile and Itaú use this literal form.
_ANCHOR_NACIONAL: Final = "ESTADO DE CUENTA NACIONAL"

#: Secondary anchor for the national (CLP) variant header.
#: Santander writes ``"ESTADO DE CUENTA EN MONEDA NACIONAL"``
#: (the ``"EN MONEDA"`` filler is the differentiator). Matching
#: the *full* prefix keeps the rule specific to the section
#: header — a body-text mention like
#: ``"Pago en MONEDA NACIONAL por servicios"`` does not match.
_ANCHOR_NACIONAL_SANTANDER: Final = "ESTADO DE CUENTA EN MONEDA NACIONAL"


def detect_variant(text: str) -> Variant:
    """Return the statement variant encoded in ``text``.

    The function is case-insensitive (the CMF template uses
    upper-case section headers but a misspelled heading should
    not flip the result). Empty or whitespace-only input is
    rejected with :class:`VariantDetectionError` — there is
    nothing to detect against.

    Detection rule
    --------------

    * If neither anchor is found, raise :class:`VariantDetectionError`.
    * If both anchors are found, return the one whose first
      occurrence is *earliest* in the text.
    * If only one anchor is found, return it.

    The "earliest occurrence wins" rule lets a NACIONAL
    statement that *also* includes an INTERNACIONAL supplement
    (Banco de Chile bundles both in one PDF) classify as
    NACIONAL — which matches the bank's own treatment: the CLP
    section is the primary one.

    Parameters
    ----------
    text:
        The full extracted text of the statement, as produced by
        :func:`app.services.pdf.extractor.extract_text`.

    Returns
    -------
    Variant
        The string ``"NACIONAL"`` or ``"INTERNACIONAL"``.

    Raises
    ------
    VariantDetectionError
        If neither anchor is found in ``text``. Likely causes:
        the PDF was not a CMF state-of-account, the text
        extraction silently dropped the header (rare; only
        happens on heavily damaged PDFs), or the bank uses a
        new template we do not yet recognise.
    """
    if not text or not text.strip():
        raise VariantDetectionError("Cannot detect variant from empty text")

    upper = text.upper()

    # Position of the first occurrence of each anchor, or -1
    # if the anchor is absent. The first-occurrence-wins rule
    # is implemented as "the smallest non-negative position
    # determines the variant".
    int_pos = upper.find(_ANCHOR_INTERNACIONAL)
    nac_pos = _first_occurrence(upper, _ANCHOR_NACIONAL, _ANCHOR_NACIONAL_SANTANDER)

    if int_pos < 0 and nac_pos < 0:
        raise VariantDetectionError(
            "Could not detect statement variant. Expected one of the headers: "
            f"{_ANCHOR_INTERNACIONAL!r}, {_ANCHOR_NACIONAL!r}, or "
            f"{_ANCHOR_NACIONAL_SANTANDER!r} (Santander form)."
        )

    # If only one side has a match, that one wins. If both
    # match, the one that appears *first* in the document wins.
    if int_pos < 0:
        return "NACIONAL"
    if nac_pos < 0:
        return "INTERNACIONAL"
    return "NACIONAL" if nac_pos < int_pos else "INTERNACIONAL"


def _first_occurrence(haystack: str, *needles: str) -> int:
    """Return the earliest position of any ``needle`` in ``haystack``.

    Returns ``-1`` if no needle is present. The function avoids
    the obvious ``min(n.find(...) for n in needles)`` because a
    missing needle (``.find`` returning ``-1``) would then beat a
    present needle (returning ``-1`` from a non-missing match).
    Filtering missing needles first makes the intent explicit.
    """
    positions = [haystack.find(n) for n in needles if n in haystack]
    return min(positions) if positions else -1
