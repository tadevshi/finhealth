"""phase 2 — recurring rules (PR #5)

Revision ID: 0007_phase2_recurring_rules
Revises: 0006_phase2_merchants_transactions_alter
Create Date: 2026-07-07 12:00:00.000000

The Phase 2 PR #5 change introduces the deterministic
recurring-transaction detector. This migration adds the
schema the detector writes to and the FK the detector
back-fills on historical transactions.

Two changes
-----------

1. A new ``recurring_rules`` table holding the upsert
   output of :class:`app.services.recurring_detection.RecurringDetector`.
   The columns mirror the model's field set: a nullable-FK to
   ``merchants.id`` (``ON DELETE CASCADE`` so a deleted
   merchant drops its rules), the period metadata
   (``period_days``, ``period_label``), the amount bounds
   (``amount_min`` / ``amount_max``), the currency, the
   soft-delete flag, the ``confidence`` score (decision #10),
   the ``last_seen_date`` / ``occurrences`` counters, and the
   ``created_at`` / ``updated_at`` columns from
   :class:`app.models.mixins.TimestampMixin`. Two indexes:

   * ``ix_recurring_rules_merchant_currency_period`` — the
     composite that backs the upsert path's lookup by
     ``(merchant_id, currency, period_days)``. The amount
     bounds are *not* part of the index because the SQL
     ``WHERE`` clause uses all four columns but the index
     covers the three most selective ones; the
     ``amount_min`` / ``amount_max`` are checked in the
     application code after the index hit.

   * ``ix_recurring_rules_is_active`` — the read-side index
     for the ``GET /api/v1/recurring`` filter
     (``is_active=True``).

2. A new nullable column ``transactions.recurring_rule_id``
   (FK to ``recurring_rules.id`` with ``ON DELETE SET NULL``,
   plus a B-tree index) so the detector can back-fill the
   FK on the in-band transactions in a single ``UPDATE``
   after the rule is upserted (design D3).

Why a NEW migration file (and not an extension of 0006)
-------------------------------------------------------

Migration 0006 is a 2-PR coordination point (PR #2 categories
+ PR #4 merchants + aliases, per design decision #1 in the
PR #4 design). PR #5 was always going to be a separate
file — the round-trip test pattern in 0006 is a fixed
upstream contract and adding a third PR to the same
``upgrade()`` body would have grown it past the readable
limit. The linear DAG (``0005`` → ``0006`` → ``0007``) keeps
``alembic current`` and ``alembic upgrade head`` simple and
lets PR #5 be reviewed in isolation.

Why ``op.batch_alter_table`` for the ``transactions`` change
-------------------------------------------------------------

The same reasoning as migration 0006: SQLite cannot
``ALTER TABLE ... ADD COLUMN ... REFERENCES`` with the
foreign-key clause baked in. ``batch_alter_table`` is the
portable abstraction; the resulting DDL is identical on
PostgreSQL and SQLite.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0007_phase2_recurring_rules"
down_revision: str | Sequence[str] | None = "0006_phase2_merchants_transactions_alter"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create ``recurring_rules`` + add ``transactions.recurring_rule_id``.

    The two changes are split into clearly-labelled sections
    so a reviewer can scan the change in one pass. The new
    table is created first (so the FK on ``transactions`` has
    a valid target) and the existing ``transactions`` table is
    altered in a ``batch_alter_table`` block so the
    ``ForeignKeyConstraint`` is preserved through SQLite's
    table-copy-under-the-hood path.
    """
    # --- 1. Create the ``recurring_rules`` table ---------------------
    op.create_table(
        "recurring_rules",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("merchant_id", sa.String(length=36), nullable=False),
        sa.Column("period_days", sa.Integer(), nullable=False),
        sa.Column("period_label", sa.String(length=16), nullable=False),
        sa.Column("amount_min", sa.Numeric(15, 2), nullable=False),
        sa.Column("amount_max", sa.Numeric(15, 2), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("last_seen_date", sa.Date(), nullable=False),
        sa.Column("occurrences", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.PrimaryKeyConstraint("id", name="pk_recurring_rules"),
        sa.ForeignKeyConstraint(
            ["merchant_id"],
            ["merchants.id"],
            name="fk_recurring_rules_merchant_id_merchants",
            ondelete="CASCADE",
        ),
    )
    # Composite index backing the upsert path's
    # ``SELECT ... WHERE (merchant_id, currency, period_days)``.
    # The amount bounds are checked in application code
    # after the index hit; including them in the index would
    # bloat the storage without changing the query plan.
    op.create_index(
        "ix_recurring_rules_merchant_currency_period",
        "recurring_rules",
        ["merchant_id", "currency", "period_days"],
    )
    # Read-side index for the ``is_active=True`` filter on
    # ``GET /api/v1/recurring``.
    op.create_index(
        "ix_recurring_rules_is_active",
        "recurring_rules",
        ["is_active"],
    )

    # --- 2. Add the ``transactions.recurring_rule_id`` FK ----------
    with op.batch_alter_table("transactions") as batch_op:
        batch_op.add_column(
            sa.Column(
                "recurring_rule_id",
                sa.String(length=36),
                nullable=True,
            ),
        )
        # The ``ForeignKeyConstraint`` is added separately so
        # the ``ON DELETE SET NULL`` clause is preserved
        # through the batch alter (Alembic's
        # ``batch_op.create_foreign_key`` is the documented
        # hook for this on SQLite).
        batch_op.create_foreign_key(
            "fk_transactions_recurring_rule_id_recurring_rules",
            "recurring_rules",
            ["recurring_rule_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_index(
            "ix_transactions_recurring_rule_id",
            ["recurring_rule_id"],
        )


def downgrade() -> None:
    """Reverse the upgrade: drop the ``transactions`` FK first, then the new table.

    The order is the inverse of ``upgrade`` so the FK chain
    drops cleanly: the ``transactions.recurring_rule_id`` FK
    is removed *before* the ``recurring_rules`` table itself
    (otherwise SQLite would refuse to drop a table referenced
    by a live FK). The two indexes on the new table are
    dropped *before* the table itself.
    """
    # --- 2 (reverse). Drop the ``transactions`` FK ------------------
    with op.batch_alter_table("transactions") as batch_op:
        batch_op.drop_index("ix_transactions_recurring_rule_id")
        batch_op.drop_constraint(
            "fk_transactions_recurring_rule_id_recurring_rules",
            type_="foreignkey",
        )
        batch_op.drop_column("recurring_rule_id")

    # --- 1 (reverse). Drop the ``recurring_rules`` table -----------
    op.drop_index("ix_recurring_rules_is_active", table_name="recurring_rules")
    op.drop_index("ix_recurring_rules_merchant_currency_period", table_name="recurring_rules")
    op.drop_table("recurring_rules")
