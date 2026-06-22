"""Pydantic models for the LLM extraction contract.

The LLM is asked to return JSON, but the model behind the JSON
is *ours* — not the model's. Defining it explicitly with
Pydantic gives us three things at the boundary:

1. **Validation.** A response that is missing a field, has the
   wrong type, or contains an unparseable amount raises
   :class:`pydantic.ValidationError` *before* the orchestrator
   ever sees it. The retry layer catches that error and re-asks
   the LLM, so a single bad response does not poison the whole
   ingestion.
2. **Coercion.** Money comes off the LLM as a string with the
   original visual formatting (``"$ 1.234.567"``,
   ``"US$ 1.234,56"``). We accept it as a raw string and
   delegate parsing to :mod:`app.services.pdf.amount_parser`,
   which knows about Chilean conventions. Storing the raw
   string on the schema keeps the model's output verbatim for
   re-derivation; the orchestrator coerces to ``Decimal`` when
   it persists.
3. **Documentation.** The model is a single source of truth
   that we can paste into the prompt and into the API docs.

Error model
-----------

:class:`LLMExtractionError` is the single error type the LLM
layer raises upward. It covers every failure the layer can see
(network errors, JSON parse errors, schema validation, retries
exhausted). The orchestrator catches it and turns it into a
``status=failed`` row on the statement — the right place for a
"garbage in" failure to live, because the file is still on
disk and can be re-ingested once the LLM is back.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Error
# ---------------------------------------------------------------------------


class LLMExtractionError(Exception):
    """Raised when the LLM layer cannot produce a valid extraction.

    Covers every failure the layer can see: network errors,
    HTTP 4xx/5xx responses, malformed JSON, schema validation
    failures, and "retries exhausted". The original cause (a
    :class:`httpx.HTTPError`, a :class:`json.JSONDecodeError`,
    a :class:`pydantic.ValidationError`) is preserved on
    ``__cause__`` for logging.

    Parameters
    ----------
    message:
        Human-readable error message.
    retryable:
        ``True`` when the failure is transient (network blip,
        rate limit, malformed JSON) and a retry might succeed.
        ``False`` when the failure is permanent (HTTP 4xx other
        than 429, schema validation that will not change on
        retry). Defaults to ``False`` because the safe default
        is to fail fast.
    """

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


# ---------------------------------------------------------------------------
# Extraction output
# ---------------------------------------------------------------------------

#: Allowed currency values. The CMF template is restricted to
#: CLP (NACIONAL) and USD (INTERNACIONAL). Anything else is
#: almost certainly an LLM hallucination and must be rejected
#: at the boundary so it cannot reach the database.
_CURRENCY_VALUES: Final = ("CLP", "USD")


class TransactionExtraction(BaseModel):
    """One transaction extracted by the LLM.

    Mirrors a single row of the CMF state-of-account. The
    ``amount`` is kept as a raw string — the LLM emits the
    original visual form (``"$ 1.234.567"`` or
    ``"US$ 1.234,56"``) and the orchestrator coerces it to
    :class:`~decimal.Decimal` via
    :func:`app.services.pdf.amount_parser.parse_amount`.

    Installment fields are optional: only present when the
    statement shows a ``NN/NN`` marker on a given row. The LLM
    is told explicitly *not* to invent them on cash
    transactions.
    """

    model_config = ConfigDict(extra="forbid")

    date: str = Field(
        min_length=1,
        max_length=10,
        description=(
            "Posting date on the statement. Accepts DD/MM/YY or "
            "DD/MM/YYYY — the orchestrator normalises to ISO."
        ),
    )
    description: str = Field(
        min_length=1,
        max_length=500,
        description="Line-item description as it appears on the statement.",
    )
    amount: str = Field(
        min_length=1,
        max_length=64,
        description=(
            "Raw amount text, including the currency marker. "
            "CLP: '$ 1.234.567'. USD: 'US$ 1.234,56'."
        ),
    )
    currency: str = Field(
        min_length=3,
        max_length=3,
        description="ISO-4217 code: 'CLP' for NACIONAL, 'USD' for INTERNACIONAL.",
    )
    category: str | None = Field(
        default=None,
        max_length=50,
        description=(
            "LLM's best-guess category (e.g. 'Restaurants', "
            "'Transport'). Optional — the user can override."
        ),
    )
    installment_number: int | None = Field(
        default=None,
        ge=1,
        description="Current installment number (1-indexed).",
    )
    installment_total: int | None = Field(
        default=None,
        ge=1,
        description="Total number of installments in the plan.",
    )
    installment_value: str | None = Field(
        default=None,
        max_length=64,
        description=(
            "Per-installment value as raw text (same format as "
            "``amount``). None for one-off charges."
        ),
    )

    def currency_is_valid(self) -> bool:
        """Return ``True`` when ``currency`` is a supported CMF code.

        Pydantic only enforces the *length* of the string, not
        its value. The orchestrator calls this before parsing
        the amount so a hallucinated currency (``"EUR"``)
        fails fast with a clear error rather than producing a
        silent :class:`AmountParseError` later.
        """
        return self.currency in _CURRENCY_VALUES


class ExtractionResponse(BaseModel):
    """The full LLM extraction envelope.

    The model is deliberately a single object with a
    ``transactions`` list — never a bare list — so the LLM has
    a stable shape to target, and so we can add metadata
    (``confidence``, ``notes``) without changing the contract.

    ``confidence`` is the LLM's self-reported certainty on a
    0-1 scale. The orchestrator does not use it to gate
    persistence (that would couple correctness to the model's
    calibration), but it is persisted on the statement for
    later review.
    """

    model_config = ConfigDict(extra="forbid")

    transactions: list[TransactionExtraction] = Field(
        min_length=0,
        description="All transactions extracted from the statement text.",
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="LLM's self-reported confidence in the extraction (0-1).",
    )
    notes: str | None = Field(
        default=None,
        max_length=2000,
        description=(
            "Optional reasoning. Useful for debugging bad "
            "extractions — the operator can read why the LLM "
            "made the calls it did."
        ),
    )


def empty_response(confidence: float = 0.0) -> ExtractionResponse:
    """Build an empty :class:`ExtractionResponse`.

    Used when the LLM is asked to extract from a statement with
    no transactions (e.g. a $0 period). Returning an empty
    model is a *success* — the absence of charges is valid
    data. An error here would be a false positive.
    """
    return ExtractionResponse(transactions=[], confidence=confidence, notes=None)


def parse_amount_safe(value: str, currency: str) -> Decimal:
    """Parse an LLM-emitted amount string to :class:`Decimal`.

    Centralises the choice of parser. The LLM layer cannot
    import :mod:`app.services.pdf.amount_parser` directly
    without creating a circular dependency (the parser does
    not need the LLM), so the parsing happens at the
    orchestrator. This helper is here for completeness and
    future tests; the production parser is the one in
    :mod:`app.services.pdf.amount_parser`.

    Raises
    ------
    LLMExtractionError
        If the value cannot be parsed for the given currency.
    """
    # Local import to avoid a circular dependency: amount_parser
    # is in the PDF subpackage, which the LLM subpackage
    # should not depend on at module load time.
    from app.services.pdf.amount_parser import AmountParseError, parse_amount

    try:
        return parse_amount(value, currency)
    except AmountParseError as exc:
        raise LLMExtractionError(
            f"LLM emitted an unparseable amount {value!r} for {currency}: {exc}"
        ) from exc
