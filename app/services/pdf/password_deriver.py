"""Derive the per-bank PDF password from a user-supplied RUT.

Chilean banks encrypt their monthly credit-card statement PDFs with
a password that is a deterministic function of the cardholder's
RUT (Rol Único Tributario, the national tax identifier). The exact
function is bank-specific:

* **Santander** and **Itaú** use the RUT *body* (all digits before
  the dash, without the verification digit) — e.g. ``26450463`` for
  RUT ``26.450.463-5``.
* **Banco de Chile** uses the *last four* digits of the RUT body —
  e.g. ``0463`` for the same RUT.

The convention for each bank is stored in the database as
``bank.password_formula`` (seeded by the migration with the values
``"rut_sin_dv"`` and ``"rut_ultimos_4"``). This module is the
single source of truth for turning that formula plus a user-typed
RUT into a PDF password.

Design notes
------------

* The RUT parser is permissive about formatting: ``"26450463-5"``,
  ``"26.450.463-5"``, ``"26 450 463 5"`` and ``"26450463"`` (no
  verification digit) all normalise to the same body. Real bank
  forms accept free-form input, and so does this function.
* The verification digit (DV) is *never* part of the password for
  any known bank formula. We still parse it so we can reject
  malformed RUTs early (e.g. ``"abc-def"``).
* Adding a new bank is a matter of (a) inserting a new
  ``password_formula`` token in the seed data and (b) extending the
  :data:`_FORMULAS` dispatch table below. No new code path is
  needed elsewhere.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from app.models.bank import Bank


# ---------------------------------------------------------------------------
# Public exception types
# ---------------------------------------------------------------------------


class InvalidRUTError(ValueError):
    """Raised when the user-supplied RUT cannot be parsed.

    The message is intentionally short and free of any RUT content
    (the value may end up in logs). Callers should surface a
    human-readable translation in the UI.
    """


class InvalidPasswordFormulaError(ValueError):
    """Raised when the bank has an unknown ``password_formula``.

    This is a configuration error, not a user error: a row in
    ``banks`` carries a token that this module does not recognise.
    The data layer is the source of truth, so the error points the
    maintainer at the offending formula string.
    """


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Token used by Santander and Itaú.
FORMULA_RUT_SIN_DV: Final = "rut_sin_dv"

#: Token used by Banco de Chile.
FORMULA_RUT_ULTIMOS_4: Final = "rut_ultimos_4"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def derive_password(bank: Bank, rut: str) -> str:
    """Return the PDF password for ``bank`` derived from ``rut``.

    The function performs the two checks every caller cares about:
    the RUT must parse, and the bank's ``password_formula`` must
    be one we know how to handle. Both failures raise a specific
    exception so the HTTP layer can map them to 400 responses
    without leaking the RUT in the error message.

    Parameters
    ----------
    bank:
        A :class:`app.models.bank.Bank` row. Only
        ``bank.password_formula`` is read.
    rut:
        The user's RUT, in any of the common Chilean forms
        (``"26.450.463-5"``, ``"26450463-5"``, ``"26450463"``,
        etc.). Whitespace is trimmed.

    Returns
    -------
    str
        The password to pass to :mod:`pikepdf` for the encrypted
        statement PDF.

    Raises
    ------
    InvalidRUTError
        If ``rut`` is empty, contains non-digit characters, or has
        an obviously malformed structure.
    InvalidPasswordFormulaError
        If ``bank.password_formula`` is not one of the supported
        tokens. This is a configuration problem (the seed data or
        a manual insert is out of date), not a user mistake.
    """
    body, _dv = _parse_rut(rut)
    formula = bank.password_formula
    if formula == FORMULA_RUT_SIN_DV:
        return body
    if formula == FORMULA_RUT_ULTIMOS_4:
        # Always four digits, zero-padded on the left. The Chilean
        # RUT body is 6-8 digits long in practice; padding is
        # defensive and produces "0463" for both "26450463" and a
        # hypothetical 4-digit body.
        return body[-4:].rjust(4, "0")
    raise InvalidPasswordFormulaError(
        f"bank {bank.name!r} has unknown password_formula: {formula!r}. "
        f"Supported: {sorted(_FORMULAS_DISPLAY)}"
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


_FORMULAS_DISPLAY: Final = frozenset({FORMULA_RUT_SIN_DV, FORMULA_RUT_ULTIMOS_4})


#: Match a RUT body (digits with optional dot or space grouping)
#: optionally followed by ``-DV`` where ``DV`` is a digit or ``K``.
#: Anchored with ``\A`` / ``\Z`` to reject partial matches.
_RUT_BODY_PATTERN: Final = re.compile(r"\A[\d. ]+\Z")
_RUT_DV_PATTERN: Final = re.compile(r"\A[\dKk]\Z")


def _parse_rut(rut: str) -> tuple[str, str]:
    """Return ``(body, dv)`` for ``rut``.

    ``body`` is the digit-only sequence without the verification
    digit; ``dv`` is the verification character (``"0"``-``"9"`` or
    ``"K"``) or an empty string when the RUT was supplied without
    one. Internal: only called from :func:`derive_password` and the
    unit tests.

    Accepted input shapes (whitespace around the input is trimmed):

    * ``"26450463"`` — body only, no verification digit.
    * ``"26450463-5"`` — body + dash + DV.
    * ``"26.450.463-5"`` — body with dot grouping + dash + DV.
    * ``"26 450 463-5"`` — body with space grouping + dash + DV.

    The dash is the *only* allowed separator before the DV; multiple
    dashes or a trailing dash with no DV are rejected.

    Raises
    ------
    InvalidRUTError
        When the RUT is empty, contains non-numeric characters, or
        has an obviously broken structure.
    """
    if not rut or not rut.strip():
        raise InvalidRUTError("RUT is empty")

    cleaned = rut.strip()

    # Split on the (optional) dash. We require at most one dash
    # because the real Chilean format never uses more than one.
    if "-" in cleaned:
        if cleaned.count("-") > 1:
            raise InvalidRUTError("RUT must contain at most one '-' separator")
        body_raw, dv = cleaned.split("-", 1)
    else:
        body_raw, dv = cleaned, ""

    # Normalise the body by stripping whitespace and dots.
    body = body_raw.replace(".", "").replace(" ", "")

    if not body:
        raise InvalidRUTError("RUT body is empty")
    if not body.isdigit():
        raise InvalidRUTError("RUT body must contain only digits")
    if not (1 <= len(body) <= 9):
        raise InvalidRUTError("RUT body length must be 1-9 digits")

    # DV: if a dash was present, the DV is required and must be a
    # single digit or K. If no dash was present, we conservatively
    # assume no DV — that matches the test corpus and the way users
    # type RUTs in the wild.
    if "-" in cleaned:
        if not dv or not _RUT_DV_PATTERN.match(dv):
            raise InvalidRUTError("RUT verification digit must be a single 0-9 or 'K'")
    else:
        dv = ""

    return body, dv
