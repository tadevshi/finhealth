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
    from app.models.category import Category
    from app.models.merchant import Merchant
    from app.models.recurring_rule import RecurringRule
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
    category_id:
        Optional FK to :class:`app.models.category.Category`. Set
        by the ingestion layer when the LLM-emitted ``category``
        string resolves to one of the seeded closed-set names.
        ``NULL`` for rows tagged with an off-set label (e.g. a
        free-form string from a PATCH) or untagged rows. Backed
        by a B-tree index for ``WHERE category_id = ?`` filters.
    low_confidence:
        ``True`` when the row's category is uncertain: a miss
        against the seeded closed set, a free-form string from
        the legacy PATCH, or a row the LLM could not tag at all.
        Always ``False`` for rows tagged with a known
        :class:`Category`.
        The same Boolean is reused as the unified
        "I can't confidently tag this row" signal for both the
        category miss *and* the merchant-miss case (Phase 2
        PR #4). A newly-auto-created merchant (no
        :data:`app.services.merchants.KNOWN_MERCHANT_PATTERNS`
        hit) flips the flag to ``True`` even when the category
        resolved cleanly.
    merchant_id:
        Optional FK to :class:`app.models.merchant.Merchant`.
        Set by the ingestion layer when the
        :mod:`app.services.merchants` normalizer resolves the
        bank description to a canonical merchant (auto-created
        on miss, looked up on hit). ``NULL`` for rows the
        normalizer did not bind to any merchant (defensive —
        every row is supposed to resolve, but the FK is
        nullable so a transient failure in the normalizer does
        not block ingestion).
    installment_number, installment_total, installment_value:
        Installment plan data. ``None`` when the charge is a one-off.
        When set, ``1 <= installment_number <= installment_total``.
    recurring_rule_id:
        Optional FK to
        :class:`app.models.recurring_rule.RecurringRule`. Set by
        :class:`app.services.recurring_detection.RecurringDetector`
        when the row matches a detected pattern (the in-band
        amount range on the same merchant + currency). The
        detector preserves the FK on deactivation (per design
        D) so historical audit links survive. ``NULL`` for
        one-off charges and for rows the detector has not yet
        processed.
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
    category_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType(),
        ForeignKey("categories.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    low_confidence: Mapped[bool] = mapped_column(default=False, nullable=False)
    merchant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType(),
        ForeignKey("merchants.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    installment_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    installment_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    installment_value: Mapped[Decimal | None] = mapped_column(
        Numeric(15, 2),
        nullable=True,
    )
    recurring_rule_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType(),
        ForeignKey("recurring_rules.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    raw_json: Mapped[dict[str, object] | list[object] | None] = mapped_column(
        JSON,
        nullable=True,
    )

    # Relationships ---------------------------------------------------------
    statement: Mapped[Statement] = relationship(back_populates="transactions", lazy="joined")
    category_ref: Mapped[Category | None] = relationship(
        back_populates="transactions",
        lazy="joined",
    )
    merchant_ref: Mapped[Merchant | None] = relationship(
        back_populates="merchant_transactions",
        lazy="joined",
    )
    recurring_rule_ref: Mapped[RecurringRule | None] = relationship(
        back_populates="transactions",
        lazy="joined",
    )
