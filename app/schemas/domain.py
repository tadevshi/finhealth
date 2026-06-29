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
    response omits ``is_active`` because the GET endpoint only
    returns active rows; the field is reserved on the model for
    a future soft-disable use case.
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
