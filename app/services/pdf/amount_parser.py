"""Parse Chilean bank statement amount strings to :class:`Decimal`.

The two statement variants use two different *visual* amount
formats that the LLM layer will see in the extracted text:

============ ================================= ===========
Variant      Example                         Decimal value
============ ================================= ===========
``CLP``      ``$ 1.234.567``                 ``1234567``
``CLP``      ``$ -100.000``                  ``-100000``
``USD``      ``US$ 1.234,56``                ``1234.56``
``USD``      ``US$ -1.234,56``               ``-1234.56``
``USD``      ``US$ 236,86``                  ``236.86``
============ ================================= ===========

Two format rules drive the parser:

* **CLP** has *no decimal part* — Chilean pesos are not
  sub-divided. Dots are thousand separators and must group digits
  in threes (e.g. ``1.234.567``). A malformed input like
  ``1.2.3.4`` is rejected.
* **USD** is two-decimal. The *comma* is the decimal separator
  and the *dot* is the thousand separator — the European/Latin
  American convention that :class:`decimal.Decimal` does not
  understand by default. The decimal part is required.

We never use :class:`float` for money. ``Decimal("0.10")`` is
exactly ``0.10``; ``float("0.10")`` is
``0.1000000000000000055511151231257827021181583404541015625``.
That difference compounds across thousands of transactions and
surfaces as a one-cent drift on the user's monthly rollup.

Error model
-----------

The function is strict: any input that is not parseable raises
:class:`AmountParseError`. The orchestrator can catch that and
flag the LLM response as malformed, which is the right place for
a "garbage in, garbage out" failure to live — not in a silent
``0.0`` default that would corrupt the user's totals.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Final

#: Literal currency marker for the international (USD) variant.
#: Must be replaced *before* the bare ``"$"`` marker.
_USD_MARKER: str = "US$"

#: Literal currency marker for the national (CLP) variant.
_CLP_MARKER: str = "$"

#: CLP body pattern: one or more digits, optionally followed by
#: groups of exactly three digits separated by a dot. Matches
#: ``"4250"``, ``"1"``, ``"1.234"``, ``"1.234.567"``. Rejects
#: ``"1.2.3.4"`` because the intermediate groups are not three
#: digits — the *position* of the dots must match the Chilean
#: thousand-separator convention.
_CLP_BODY_PATTERN: Final = re.compile(r"\A\d+(?:\.\d{3})*\Z")

#: USD body pattern: one or more digits, optionally followed by
#: groups of three digits separated by a dot, then an optional
#: ``,xx`` two-digit decimal part. The decimal part is required
#: by the banks' statement layout, but we accept a missing one
#: defensively because some tests round-trip without it.
_USD_BODY_PATTERN: Final = re.compile(r"\A\d+(?:\.\d{3})*(?:,\d{1,2})?\Z")


class AmountParseError(ValueError):
    """Raised when an amount string cannot be parsed.

    The error message always quotes the offending input so
    debugging does not require re-running the pipeline. Sensitive
    data does not flow through this function (the LLM is the
    one that supplies the raw string), so quoting is safe.
    """


def parse_amount(text: str, currency: str) -> Decimal:
    """Return the :class:`Decimal` value encoded in ``text``.

    Parameters
    ----------
    text:
        The raw amount string as it appears on the statement.
        Whitespace and surrounding currency markers are stripped
        automatically.
    currency:
        ISO-4217 code (``"CLP"`` or ``"USD"``). Case-insensitive.
        The currency determines the decimal-separator convention;
        CLP has no decimal part.

    Returns
    -------
    Decimal
        The parsed value. Negative inputs yield a negative
        :class:`Decimal`. ``Decimal("1234.56")`` is the
        canonical string form (no float-induced junk digits).

    Raises
    ------
    AmountParseError
        If ``text`` is empty, contains unsupported characters,
        uses the wrong decimal convention for ``currency``, or
        cannot be converted by :class:`decimal.Decimal`.
    """
    if text is None:
        # Defensive: callers occasionally pass ``None`` from
        # ``.get()`` lookups. We treat it as an empty input.
        raise AmountParseError("Amount string is None")

    cur = currency.upper()
    if cur not in {"CLP", "USD"}:
        raise AmountParseError(f"Unsupported currency {currency!r}. Expected 'CLP' or 'USD'.")

    # 1. Strip currency markers. Order matters: ``"US$"`` must be
    #    replaced before the bare ``"$"`` so we do not leave a
    #    dangling ``"US"`` prefix on the value.
    cleaned = text.strip()
    cleaned = cleaned.replace(_USD_MARKER, "", 1)
    cleaned = cleaned.replace(_CLP_MARKER, "", 1)
    cleaned = cleaned.strip()
    if not cleaned:
        raise AmountParseError(f"Amount string is empty after stripping markers: {text!r}")

    # 2. Detect a leading minus. The sign may sit *after* the
    #    currency marker (``"$ -100.000"``) so it is checked
    #    here, not in the raw input. An accounting-style
    #    parenthesised form like ``"(1.234,56)"`` is *not*
    #    supported by any of the banks we ingest, so we do not
    #    special-case it; if it ever appears the parser will
    #    fail and the LLM layer will retry.
    negative = False
    if cleaned.startswith("-"):
        negative = True
        cleaned = cleaned[1:].strip()
    elif cleaned.startswith("+"):
        cleaned = cleaned[1:].strip()

    # 3. Validate the body shape BEFORE normalising, so a
    #    malformed string like ``"1.2.3.4"`` is rejected with
    #    a clear error instead of being silently coerced to
    #    ``1234``.
    pattern = _CLP_BODY_PATTERN if cur == "CLP" else _USD_BODY_PATTERN
    if not pattern.match(cleaned):
        raise AmountParseError(f"Amount body {cleaned!r} does not match the {cur} format")

    # 4. Normalise to a ``Decimal``-compatible string.
    if cur == "CLP":
        # No decimal part: drop *both* dots and commas. A stray
        # comma is treated as a thousand separator (defensive —
        # we have not seen one in the wild, but the cost of
        # stripping it is zero).
        normalised = cleaned.replace(".", "").replace(",", "")
    else:  # USD
        # Two-decimal: drop thousand dots, then convert the
        # decimal comma to a dot.
        normalised = cleaned.replace(".", "").replace(",", ".")

    if not normalised:
        raise AmountParseError(f"Amount string has no digits: {text!r}")

    # 5. Convert. ``InvalidOperation`` catches things like
    #    ``"abc"`` that survived the ``replace`` calls.
    try:
        value = Decimal(normalised)
    except InvalidOperation as exc:
        raise AmountParseError(f"Cannot parse amount {text!r}: {exc}") from exc

    return -value if negative else value
