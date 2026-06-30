"""phase 2 category columns on transactions (PR #2 portion)

Revision ID: 0006_phase2_merchants_transactions_alter
Revises: 0005_phase2_categories
Create Date: 2026-06-29 10:30:00.000000

This migration is the PR #2 portion of the 0006 family. PR #4 of the
phase-2-classification SDD change will add 'merchant_id' to this same
migration. Do not edit the downgrade here without coordinating with
the PR #4 spec.

Phase 2 — categories foundation (PR #2 of the phase-2-classification
SDD change).

This migration is the *PR #2 portion* of the 0006 family. It
extends the ``transactions`` table with the two columns the
category-closed-set needs at the database level:

* ``category_id`` — nullable FK to ``categories.id`` with
  ``ON DELETE SET NULL``. The ingestion layer resolves the
  LLM-emitted ``category`` string against the seeded closed
  set and stamps the matching UUID here; a miss leaves the
  column ``NULL`` and stamps ``low_confidence = True`` so the
  row is still recoverable for the user.
* ``low_confidence`` — ``Boolean NOT NULL DEFAULT 0``. New
  rows default to ``False``; legacy rows backfill to
  ``False`` because the migration is additive and every
  pre-existing row was tagged (or untagged) before the
  closed-set existed, so the conservative default is
  "trust what the user said".

It also creates the B-tree index on ``category_id`` (per design
decision #8) so the ``GET /api/v1/transactions?category_id=...``
filter introduced in PR #3 has a clean query plan.

Extension point for PR #4
------------------------

The follow-up PR #4 of the phase-2-classification SDD change
extends this *same* migration file (single-file 0006, per
design decision #1) to add ``merchants``, ``merchant_aliases``,
and ``Transaction.merchant_id``. The downgrade body therefore
must remain idempotent against the PR #4 additions: it drops
the PR #2 columns/indexes here and the PR #4 additions in the
PR #4 commit on the same file.

A reviewer reading the upgrade body sees the PR #2 scope (the
two columns + the index). A reviewer reading the diff *after*
PR #4 merges sees the PR #4 additions layered on top.

Why ``op.batch_alter_table``
----------------------------

``transactions`` already exists (created in migration 0002)
and SQLite cannot ``ALTER TABLE ... ADD CONSTRAINT`` directly
or ``ALTER TABLE ... ADD COLUMN ... REFERENCES`` with the
foreign-key clause baked in. ``batch_alter_table`` is the
portable abstraction: on PostgreSQL it issues a plain
``ALTER TABLE``; on SQLite it copies the table under the
hood. Either way the resulting schema is identical and the
downgrade reverses both the column and the constraint.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0006_phase2_merchants_transactions_alter"
down_revision: str | Sequence[str] | None = "0005_phase2_categories"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add ``category_id``, ``low_confidence``, and the index on ``transactions``.

    The two columns and the index are added in a single
    ``batch_alter_table`` block so SQLite's
    table-copy-under-the-hood path runs once, not three times.
    The foreign-key clause is encoded directly on the column so
    Alembic generates a ``ForeignKeyConstraint`` entry in the
    table's DDL — matching the pattern used in migration 0002
    for ``transactions.statement_id``.
    """
    with op.batch_alter_table("transactions") as batch_op:
        batch_op.add_column(
            sa.Column(
                "category_id",
                sa.String(length=36),
                nullable=True,
            ),
        )
        batch_op.add_column(
            sa.Column(
                "low_confidence",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("0"),
            ),
        )
        # The ``ForeignKeyConstraint`` is added separately so the
        # ``ON DELETE SET NULL`` clause is preserved through the
        # batch alter (Alembic's ``batch_op.create_foreign_key``
        # is the documented hook for this on SQLite).
        batch_op.create_foreign_key(
            "fk_transactions_category_id_categories",
            "categories",
            ["category_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_index(
            "ix_transactions_category_id",
            ["category_id"],
        )


def downgrade() -> None:
    """Drop the index, the FK, and the two columns added in PR #2.

    The order is the inverse of ``upgrade``: the index is
    dropped first (it depends on the column), then the FK, then
    the columns themselves. The ``batch_alter_table`` context
    is required on SQLite because ``ALTER TABLE ... DROP
    COLUMN`` is not directly supported — the batch abstraction
    recreates the table under the hood.
    """
    with op.batch_alter_table("transactions") as batch_op:
        batch_op.drop_index("ix_transactions_category_id")
        batch_op.drop_constraint("fk_transactions_category_id_categories", type_="foreignkey")
        batch_op.drop_column("low_confidence")
        batch_op.drop_column("category_id")
