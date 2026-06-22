"""ORM model for the ``transactions`` table.

A :class:`Transaction` is a single line-item extracted from a
:class:`Statement`. Monetary values are stored as ``Numeric(15, 2)``
— 15 total digits with 2 decimal places — which is the smallest
column type that fits any realistic personal-finance amount
(``9_999_999_999_999.99``). We never use ``FLOAT`` for money:
floating-point cannot represent ``0.10`` exactly, and rounding
errors compound across thousands of rows.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import JSON, Date, ForeignKey, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base
from app.models.mixins import TimestampMixin, UUIDMixin, UUIDType

if TYPE_CHECKING:
    from app.models.statement import Statement


class Transaction(UUIDMixin, TimestampMixin, Base):
    """A single line-item extracted from a :class:`Statement`.

    ``amount`` is signed: positive for charges, negative for payments
    or refunds (or whichever convention the LLM extraction produces
    — the ingestion layer is responsible for normalising signs in
    reporting queries).

    Attributes
    ----------
    id:
        UUID primary key.
    statement_id:
        Foreign key to :class:`Statement`. Indexed.
    date:
        Transaction posting date as it appears on the statement.
    description:
        Human-readable description from the bank. Free-form text;
        length is bounded so we do not store entire page dumps.
    amount:
        Signed monetary value. ``Numeric(15, 2)`` keeps the precision
        exactly as written, with no floating-point drift.
    currency:
        ISO-4217 code (``"CLP"``, ``"USD"``). Carried per-row because
        some users hold cards in different currencies.
    category:
        Optional manual category. ``None`` until the user tags the
        row; the LLM never assigns categories.
    installment_number, installment_total, installment_value:
        Installment plan data. ``None`` when the charge is a one-off.
        When set, ``1 <= installment_number <= installment_total``.
    raw_json:
        Verbatim LLM extraction output for this row. Preserved so we
        can re-derive the row if the parser changes — debugging gold.
    statement:
        Many-to-one relationship to :class:`Statement`.
    """

    __tablename__ = "transactions"

    statement_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType(),
        ForeignKey("statements.id", ondelete="CASCADE"),
        index=True,
    )
    date: Mapped[date] = mapped_column(Date, index=True)
    description: Mapped[str] = mapped_column(String(500))
    amount: Mapped[Decimal] = mapped_column(Numeric(15, 2))
    currency: Mapped[str] = mapped_column(String(3))
    category: Mapped[str | None] = mapped_column(String(50), nullable=True)
    installment_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    installment_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    installment_value: Mapped[Decimal | None] = mapped_column(
        Numeric(15, 2),
        nullable=True,
    )
    raw_json: Mapped[dict[str, object] | list[object] | None] = mapped_column(
        JSON,
        nullable=True,
    )

    # Relationships ---------------------------------------------------------
    statement: Mapped[Statement] = relationship(back_populates="transactions", lazy="joined")
