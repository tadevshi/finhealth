"""ORM model for the ``recurring_rules`` table.

The Phase 2 PR #5 change introduces a deterministic
recurring-transaction detector. Each detected pattern — a
``(merchant_id, currency, period)`` triple with ≥3 in-band
occurrences inside the 90-day scan window — becomes a
:class:`RecurringRule` row the user can review through
``GET /api/v1/recurring`` and toggle via
``PATCH /api/v1/recurring/{id}``.

Why one table, not a join to ``merchants`` only
----------------------------------------------

The grouping key the detector uses is the *tuple*
``(merchant_id, amount_min, amount_max, currency, period_days)``,
not the merchant alone. Two different subscriptions from the
same merchant — Netflix at $9.99 monthly and a quarterly print
magazine at $24.99 quarterly — produce two rules with the
same ``merchant_id`` but different ``amount_*`` / ``period_days``
columns. Keeping the amount bounds on the rule row (instead of
computing them on the fly) makes the upsert path a single
``SELECT`` against the ``uq_recurring_rules_upsert_key``
UNIQUE constraint and makes the rule self-describing for the
API consumer.

Why a real ``confidence`` column
--------------------------------

Decision #10 mandates an explicit ``confidence REAL`` column
with a value in ``[0.0, 1.0]``. The column carries the detector's
estimate of how trustworthy the pattern is — a high-confidence
rule is one with many in-band occurrences and a tight amount
range; a low-confidence rule is one with three barely-in-band
occurrences. The detector recomputes the value on every upsert
(per the spec, the formula is
``min(1.0, occurrences / 5) * max(0.0, 1.0 - (max - min) / median)``
rounded to four decimal places — see
:func:`app.services.recurring_detection.RecurringDetector._compute_confidence`).

Why a soft-delete ``is_active`` flag
------------------------------------

The user can deactivate a rule (it is wrong / not actually a
subscription) without losing the historical
``recurring_rule_id`` FK on the matched transactions (per
design D in the PR #5 design). A ``DELETE`` would cascade
the FK to ``NULL`` and lose the audit trail. ``is_active``
is filtered on the read side (``GET /api/v1/recurring`` only
returns active rules) and the upsert path ignores it (so a
re-detected pattern always updates the same row).
"""

from __future__ import annotations

import uuid
from datetime import date as date_typ
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, Date, Float, ForeignKey, Integer, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base
from app.models.mixins import TimestampMixin, UUIDMixin, UUIDType

if TYPE_CHECKING:
    from app.models.merchant import Merchant
    from app.models.transaction import Transaction


class RecurringRule(UUIDMixin, TimestampMixin, Base):
    """A detected recurring-transaction pattern for one merchant.

    The row is the result of :class:`app.services.recurring_detection.RecurringDetector`'s
    single-query scan over the last 90 days. The
    ``(merchant_id, amount_min, amount_max, currency, period_days)``
    composite is the *upsert key* — a second detector run on the
    same pattern updates the existing row (increments
    ``occurrences``, recomputes ``confidence``, bumps
    ``last_seen_date``) instead of inserting a new one. The
    composite is enforced at the database layer by the
    ``uq_recurring_rules_upsert_key`` UNIQUE constraint
    (migration 0008) — a concurrent detector run on the same
    pattern is now caught at flush as ``IntegrityError``
    instead of silently inserting a duplicate. The
    ``ix_recurring_rules_merchant_currency_period`` index
    (migration 0007) is kept alongside for read-side queries
    that filter on ``(merchant_id, currency, period_days)``
    alone.

    Attributes
    ----------
    id:
        UUID primary key (from :class:`UUIDMixin`).
    merchant_id:
        FK to :class:`app.models.merchant.Merchant`. Indexed.
        ``ON DELETE CASCADE`` — when the merchant is dropped the
        rules drop with it (the reverse relationship is
        not load-bearing for the user).
    period_days:
        The median interval between consecutive in-band
        occurrences, rounded to the nearest integer. Drives the
        period-classification bucket and is part of the upsert
        key.
    period_label:
        The human-readable bucket name. One of
        ``"weekly"`` (≤10d), ``"biweekly"`` (≤18d),
        ``"monthly"`` (≤45d), ``"quarterly"`` (≤120d), or
        ``"yearly"`` (≤400d). Stored as ``String(16)`` to leave
        headroom for future bucket additions.
    amount_min, amount_max:
        The lower and upper bounds of the in-band amount range
        (the detector's ±15% band around the median). Part of
        the upsert key; ``Numeric(15, 2)`` matches the rest of
        the money columns.
    currency:
        ISO-4217 code. Part of the upsert key; ``String(3)``.
    is_active:
        Soft-delete flag. ``True`` for new rows; flipped to
        ``False`` via ``PATCH /api/v1/recurring/{id}``. The
        read-side endpoint filters on ``is_active=True``; the
        detector's upsert path ignores the flag (so a
        re-detected pattern updates the same row regardless of
        the active state).
    confidence:
        The detector's ``[0.0, 1.0]`` confidence score
        (decision #10). ``Float`` is acceptable here because
        the value is a *score*, not a money amount. Rounded to
        four decimal places before storage (design D1).
    last_seen_date:
        The most-recent in-band date across all detector runs
        on this pattern. The API list orders by this column
        descending so the freshest patterns show first.
    occurrences:
        The cumulative in-band count across all detector runs.
        Bumped on every upsert. The confidence formula
        divides by 5, so 5+ occurrences saturates the
        occurrence factor at 1.0.
    transactions:
        One-to-many relationship to :class:`Transaction`. Backed
        by ``Transaction.recurring_rule_ref``. ``noload`` so the
        default read path does not pay for the relationship the
        v1 API does not expose. Future endpoints that need the
        list must opt in at query time with
        ``selectinload(RecurringRule.transactions)`` —
        ``session.refresh`` does NOT override ``noload`` on
        SQLAlchemy 2.0 and the relationship would stay empty.
    """

    __tablename__ = "recurring_rules"

    __table_args__ = (
        UniqueConstraint(
            "merchant_id",
            "amount_min",
            "amount_max",
            "currency",
            "period_days",
            name="uq_recurring_rules_upsert_key",
        ),
    )

    merchant_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType(),
        ForeignKey("merchants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    period_days: Mapped[int] = mapped_column(Integer, nullable=False)
    period_label: Mapped[str] = mapped_column(String(16), nullable=False)
    amount_min: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    amount_max: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    last_seen_date: Mapped[date_typ] = mapped_column(Date, nullable=False)
    occurrences: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Relationships ---------------------------------------------------------
    # Both relationships use ``lazy="noload"`` so the
    # default read path (``GET /api/v1/recurring``) does
    # not pay for a JOIN on ``merchants`` or a second
    # round-trip for ``transactions`` — neither is in the
    # ``RecurringRuleResponse`` schema. ``noload`` is
    # stricter than ``select``: it ignores ``session.refresh``
    # as well, so callers that need the relationship must
    # opt in at query time with ``selectinload`` /
    # ``joinedload``.
    merchant: Mapped[Merchant] = relationship(lazy="noload")
    transactions: Mapped[list[Transaction]] = relationship(
        back_populates="recurring_rule_ref",
        lazy="noload",
    )


__all__ = ["RecurringRule"]
