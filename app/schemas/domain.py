"""Pydantic request/response schemas for the domain models.

Each model gets a ``Create`` schema (the data the client supplies on
``POST``) and a ``Response`` schema (the shape returned by ``GET``
endpoints). The split lets us evolve the public response shape — by
adding computed fields, aliases, or hiding internal columns —
without breaking input validation.

Money columns are typed as :class:`decimal.Decimal` and the field
is bounded by ``Decimal`` constraints on the Pydantic side. Floating
point is rejected at the boundary, which is the right place to
enforce "no float for money" — by the time a row hits the ORM the
type is already correct.
"""

from __future__ import annotations

import uuid
from datetime import date as date_typ
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.models.statement import StatementStatus

# ---------------------------------------------------------------------------
# Bank
# ---------------------------------------------------------------------------


class BankCreate(BaseModel):
    """Payload for creating a :class:`app.models.Bank` row."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        min_length=1,
        max_length=50,
        description="Short stable identifier (e.g. 'santander').",
    )
    display_name: str = Field(
        min_length=1,
        max_length=100,
        description="Human-readable name shown in the UI.",
    )
    password_formula: str = Field(
        min_length=1,
        max_length=50,
        description="Token describing how the bank encrypts statement PDFs.",
    )
    is_active: bool = Field(
        default=True,
        description="Whether the bank accepts new card registrations.",
    )


class BankResponse(BaseModel):
    """Shape returned when reading a :class:`app.models.Bank` row."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    display_name: str
    password_formula: str
    is_active: bool
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# CreditCard
# ---------------------------------------------------------------------------


class CreditCardCreate(BaseModel):
    """Payload for creating a :class:`app.models.CreditCard` row."""

    model_config = ConfigDict(extra="forbid")

    bank_id: uuid.UUID = Field(description="UUID of the issuing bank.")
    card_number_masked: str = Field(
        min_length=1,
        max_length=25,
        description="Masked card number, e.g. 'XXXX XXXX XXXX 0951'.",
    )
    cardholder: str = Field(
        min_length=1,
        max_length=100,
        description="Printed cardholder name.",
    )
    currency: str = Field(
        min_length=3,
        max_length=3,
        description="ISO-4217 currency code, e.g. 'CLP'.",
    )
    is_active: bool = Field(
        default=True,
        description="Whether the card accepts new uploads.",
    )


class CreditCardResponse(BaseModel):
    """Shape returned when reading a :class:`app.models.CreditCard` row."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    bank_id: uuid.UUID
    card_number_masked: str
    cardholder: str
    currency: str
    is_active: bool
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# Statement
# ---------------------------------------------------------------------------


class StatementCreate(BaseModel):
    """Payload for creating a :class:`app.models.Statement` row."""

    model_config = ConfigDict(extra="forbid")

    credit_card_id: uuid.UUID = Field(description="UUID of the credit card.")
    period_start: date_typ = Field(description="First day of the billing period.")
    period_end: date_typ = Field(description="Last day of the billing period (inclusive).")
    statement_date: date_typ = Field(description="Date the bank issued the statement.")
    file_path: str = Field(
        min_length=1,
        max_length=512,
        description="Path to the stored PDF, relative to PDF_UPLOAD_DIR.",
    )
    file_hash: str = Field(
        min_length=64,
        max_length=64,
        description="SHA-256 of the file contents, lowercase hex.",
    )


class StatementResponse(BaseModel):
    """Shape returned when reading a :class:`app.models.Statement` row.

    The statement is returned *with* its transactions so a
    single ``GET`` is enough to render a statement detail page
    on the client. The relationship is eagerly loaded by the
    endpoint (``selectinload`` for ``Statement.transactions``)
    so the Pydantic model can access the list without an extra
    round-trip.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    credit_card_id: uuid.UUID
    period_start: date_typ
    period_end: date_typ
    statement_date: date_typ
    file_path: str
    file_hash: str
    status: StatementStatus
    error_message: str | None
    transactions: list[TransactionResponse] = Field(
        default_factory=list,
        description="All transactions extracted from this statement.",
    )
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# Transaction
# ---------------------------------------------------------------------------


class TransactionCreate(BaseModel):
    """Payload for creating a :class:`app.models.Transaction` row."""

    model_config = ConfigDict(extra="forbid")

    statement_id: uuid.UUID = Field(description="UUID of the parent statement.")
    date: date_typ = Field(description="Posting date on the statement.")
    description: str = Field(
        min_length=1,
        max_length=500,
        description="Line-item description as it appears on the statement.",
    )
    amount: Decimal = Field(
        max_digits=15,
        decimal_places=2,
        description="Signed monetary value. Positive for charges.",
    )
    currency: str = Field(
        min_length=3,
        max_length=3,
        description="ISO-4217 currency code, e.g. 'CLP'.",
    )
    category: str | None = Field(
        default=None,
        max_length=50,
        description="Optional manual category. None until the user tags the row.",
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
    installment_value: Decimal | None = Field(
        default=None,
        max_digits=15,
        decimal_places=2,
        description="Per-installment value. None for one-off charges.",
    )
    raw_json: dict[str, object] | list[object] | None = Field(
        default=None,
        description="Verbatim LLM extraction output. Preserved for re-derivation.",
    )


class TransactionResponse(BaseModel):
    """Shape returned when reading a :class:`app.models.Transaction` row."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    statement_id: uuid.UUID
    date: date_typ
    description: str
    amount: Decimal
    currency: str
    category: str | None
    category_id: uuid.UUID | None
    low_confidence: bool
    installment_number: int | None
    installment_total: int | None
    installment_value: Decimal | None
    raw_json: dict[str, object] | list[object] | None
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# Category
# ---------------------------------------------------------------------------


class CategoryResponse(BaseModel):
    """Shape returned when reading a :class:`app.models.Category` row.

    The 12 closed-set Y-NAB categories are listed by
    ``GET /api/v1/categories`` ordered by ``sort_order``. The
    response carries the stable identifier (``name``), the
    human-readable label (``display_name``), the position in
    the display order (``sort_order``), and the timestamp
    columns. The endpoint does not filter — every row is
    returned, in ``sort_order`` ascending.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    display_name: str
    sort_order: int
    created_at: datetime
    updated_at: datetime


class CategoryRenameRequest(BaseModel):
    """Body of the ``POST /api/v1/categories/{id}`` rename endpoint.

    Both fields are optional per design decision #6 — the UI can
    rename the short identifier, the human-readable label, or
    both in one call. The endpoint enforces "at least one field
    supplied" at the handler level; a body with neither field is
    rejected with 422 there.
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(
        default=None,
        min_length=1,
        max_length=50,
        description="New short stable identifier. Must not collide with another row.",
    )
    display_name: str | None = Field(
        default=None,
        min_length=1,
        max_length=100,
        description="New human-readable label shown in the UI.",
    )


# ---------------------------------------------------------------------------
# Merchant
# ---------------------------------------------------------------------------


class MerchantResponse(BaseModel):
    """Shape returned when reading a :class:`app.models.merchant.Merchant` row.

    Mirrors :class:`CategoryResponse`: the closed set of canonical
    merchants is returned by ``GET /api/v1/merchants`` ordered by
    ``name`` ascending so the UI can render a ``<select>`` in one
    round-trip. The ``default_category_id`` carries the FK to
    :class:`Category` for the ``KNOWN_MERCHANT_PATTERNS`` mapping;
    ``None`` for merchants that did not match a known pattern
    (the user can re-tag them by hand).
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    default_category_id: uuid.UUID | None
    is_active: bool
    created_at: datetime
    updated_at: datetime


class MerchantAliasResponse(BaseModel):
    """Shape returned when reading a :class:`app.models.merchant.MerchantAlias` row.

    Returned by ``POST /api/v1/merchants/{id}/aliases``. The
    ``alias_text`` is the raw description the user submitted
    (preserved verbatim for audit); the ``normalized`` form is
    the :func:`app.services.merchants.normalize` result and is
    the lookup key the alias table is indexed on.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    merchant_id: uuid.UUID
    alias_text: str
    normalized: str
    source: str
    confidence: float | None
    created_at: datetime
    updated_at: datetime


class MerchantAliasCreate(BaseModel):
    """Body of the ``POST /api/v1/merchants/{id}/aliases`` endpoint.

    The body is a single ``alias_text`` field — the canonical
    ``normalized`` form is computed server-side via
    :func:`app.services.merchants.normalize` and the ``source``
    is stamped to ``"user"`` on the resulting row. The
    ``alias_text`` is bounded to ``min_length=1`` (no blank
    aliases) and ``max_length=200`` (matches the column width
    in migration 0006).
    """

    model_config = ConfigDict(extra="forbid")

    alias_text: str = Field(
        min_length=1,
        max_length=200,
        description=(
            "Raw bank description the user wants to bind to this merchant. "
            "Preserved verbatim on the row; the canonical form is computed "
            "server-side."
        ),
    )


# ---------------------------------------------------------------------------
# RecurringRule
# ---------------------------------------------------------------------------


class RecurringRuleResponse(BaseModel):
    """Shape returned when reading a :class:`app.models.recurring_rule.RecurringRule` row.

    Returned by ``GET /api/v1/recurring`` (filtered to
    ``is_active=True`` and ordered by ``last_seen_date``
    descending) and by ``PATCH /api/v1/recurring/{id}``. The
    ``confidence`` is a 0.0-1.0 score (decision #10) computed
    by the detector from occurrence count and amount
    consistency; ``period_label`` is the human-readable bucket
    name (``weekly`` / ``biweekly`` / ``monthly`` / ``quarterly``
    / ``yearly``) and ``period_days`` is the median interval
    between consecutive in-band postings. The ``amount_min``
    and ``amount_max`` define the in-band range the detector
    matched against.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    merchant_id: uuid.UUID
    period_label: str
    period_days: int
    amount_min: Decimal
    amount_max: Decimal
    currency: str
    is_active: bool
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Detector's 0.0-1.0 confidence score (decision #10).",
    )
    last_seen_date: date_typ
    occurrences: int
    created_at: datetime
    updated_at: datetime


class RecurringRuleUpdate(BaseModel):
    """Body of the ``PATCH /api/v1/recurring/{id}`` endpoint.

    The body carries a single ``is_active`` flag — the only
    field the user can edit. Deactivating a rule preserves the
    ``recurring_rule_id`` FK on existing transactions
    (per design D) so the historical audit trail survives.
    The endpoint returns 200 with the updated rule, or 404
    if the UUID does not exist.
    """

    model_config = ConfigDict(extra="forbid")

    is_active: bool = Field(
        description=(
            "Whether the rule is active. The detector's upsert path "
            "ignores this flag, so a re-detected pattern always updates "
            "the same row regardless of the active state."
        ),
    )
