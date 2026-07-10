"""phase 2 — recurring rules UNIQUE constraint (PR #7)

Revision ID: 0008_phase2_recurring_rules_unique
Revises: 0007_phase2_recurring_rules
Create Date: 2026-07-10 12:00:00.000000

The detector's upsert path at
``app/services/recurring_detection.py`` SELECTs by the
composite key ``(merchant_id, amount_min, amount_max,
currency, period_days)`` then INSERTs on miss. Without a
DB-level unique constraint, two concurrent detector runs
on the same pattern can both miss the existing row, both
INSERT, and create duplicates. The 0007 composite index
``ix_recurring_rules_merchant_currency_period`` is
3-column and non-unique — it covers the read-side
``WHERE`` but does not enforce the upsert key at the
storage layer.

This migration closes the gap with a UNIQUE constraint on
the 5-column upsert key, named
``uq_recurring_rules_upsert_key`` for predictable error
messages and migration scripts. The 3-column read-side
index from 0007 is kept alongside — it still serves the
``GET /api/v1/recurring`` filter path where a unique
5-column index would be wider than needed.

Why a dedup step in ``upgrade()``
---------------------------------

If a database already contains duplicate rows (which
shouldn't happen in production, but the upgrade is
defensive), ``op.create_unique_constraint`` would fail
with a constraint-violation error. The dedup step runs
first and is a no-op on a clean DB:

* For each duplicate group (same 5-tuple), keep the row
  with the highest ``confidence``.
* Tie-break: ``max(last_seen_date)`` (the freshest
  seen-date wins).
* Final tie-break: lex-smallest ``id`` (UUID, for
  determinism).
* The keeper's ``last_seen_date`` is bumped to
  ``max(last_seen_date)`` across the group so the
  surviving row reflects the most recent data the
  detector saw.
* The losers are deleted; their historical
  ``transactions.recurring_rule_id`` FKs are cleared
  (``ON DELETE SET NULL`` from migration 0007).

This matches the detector's "freshest data wins" intent:
when a pattern re-fires the existing rule is updated, not
duplicated.

The dedup and the constraint create run inside the same
``upgrade()`` transaction Alembic manages for the whole
migration, so a failure on either step rolls back both —
no half-migrated state can leak to the live schema. On a
clean DB the dedup matches zero rows (the ``HAVING COUNT(*)
> 1`` filter is empty) and the constraint create is the
only effective change.

Why a UNIQUE constraint, not an ``INSERT ... ON CONFLICT`` rewrite
------------------------------------------------------------------

The spec deliberately keeps the SELECT-then-INSERT upsert
path unchanged. The constraint surfaces the concurrent
race as ``IntegrityError`` at flush, which the
application layer's existing race-guard pattern (per
``app/services/merchants.py``) already handles. Rewriting
the upsert to ``INSERT ... ON CONFLICT`` would be a
larger change with no behavioural upside — the user-
visible behaviour is the same: one rule per upsert key.

Why ``op.batch_alter_table`` for the UNIQUE constraint
-------------------------------------------------------

SQLite cannot ``ALTER TABLE ... ADD CONSTRAINT`` — the
constraint must be defined at table-create time. The
``batch_alter_table`` block is Alembic's portable
abstraction: on PostgreSQL it emits a plain
``ALTER TABLE ... ADD CONSTRAINT``, on SQLite it uses a
copy-and-move strategy that recreates the table with the
constraint baked in. The resulting schema is identical
on both dialects. The same pattern is used in migration
0006 for the ``transactions`` foreign-key add.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0008_phase2_recurring_rules_unique"
down_revision: str | Sequence[str] | None = "0007_phase2_recurring_rules"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# ---------------------------------------------------------------------------
# Table reference — keep the migration self-contained (no model import)
# ---------------------------------------------------------------------------
#
# The existing 0007 migration declares columns inline via
# ``sa.create_table``; this migration does the same. ``sa.table``
# is the Core construct for referring to an existing table
# without the ORM model, so the dedup SQL can use proper column
# references and Alembic doesn't need to import ``app.models``.

_recurring_rules = sa.table(
    "recurring_rules",
    sa.column("id"),
    sa.column("merchant_id"),
    sa.column("amount_min"),
    sa.column("amount_max"),
    sa.column("currency"),
    sa.column("period_days"),
    sa.column("confidence"),
    sa.column("last_seen_date"),
)


def upgrade() -> None:
    """Deduplicate existing rows, then add the UNIQUE constraint.

    The two steps are split into clearly-labelled sections so
    a reviewer can scan the change in one pass:

    1. **Dedup step** — defensive. Should be a no-op on a
       clean DB. Uses a CTE that ranks rows within each
       duplicate group and computes ``max(last_seen_date)``
       per group. The keeper (rank 1) is updated to the
       group's max seen-date; the losers (rank > 1) are
       deleted. The dedup and the constraint create run
       in the same Alembic-managed transaction, so a
       failure on either step rolls back both — no
       half-migrated state.
    2. **Create the UNIQUE constraint** on the 5-column
       upsert key. The constraint name
       ``uq_recurring_rules_upsert_key`` matches the model's
       ``__table_args__`` declaration.
    """
    # --- 1. Dedup step ------------------------------------------------
    #
    # CTE ``ranked``: rank rows within each duplicate group
    # (PARTITION BY the 5-tuple). The keeper is ``rn=1`` (max
    # confidence, then max last_seen_date, then min id for
    # determinism). Also computes ``group_max_last_seen`` per
    # group for the keeper-update step.
    #
    # Subquery ``dupes``: groups with COUNT(*) > 1. The UPDATE
    # is restricted to these groups so on a clean DB the
    # UPDATE matches zero rows.
    ranked_cte = sa.select(
        _recurring_rules.c.id.label("id"),
        sa.func.row_number()
        .over(
            partition_by=(
                _recurring_rules.c.merchant_id,
                _recurring_rules.c.amount_min,
                _recurring_rules.c.amount_max,
                _recurring_rules.c.currency,
                _recurring_rules.c.period_days,
            ),
            order_by=(
                _recurring_rules.c.confidence.desc(),
                _recurring_rules.c.last_seen_date.desc(),
                _recurring_rules.c.id.asc(),
            ),
        )
        .label("rn"),
        sa.func.max(_recurring_rules.c.last_seen_date)
        .over(
            partition_by=(
                _recurring_rules.c.merchant_id,
                _recurring_rules.c.amount_min,
                _recurring_rules.c.amount_max,
                _recurring_rules.c.currency,
                _recurring_rules.c.period_days,
            ),
        )
        .label("group_max_last_seen"),
    ).cte("ranked")

    dupes_subq = (
        sa.select(
            _recurring_rules.c.merchant_id,
            _recurring_rules.c.amount_min,
            _recurring_rules.c.amount_max,
            _recurring_rules.c.currency,
            _recurring_rules.c.period_days,
        )
        .group_by(
            _recurring_rules.c.merchant_id,
            _recurring_rules.c.amount_min,
            _recurring_rules.c.amount_max,
            _recurring_rules.c.currency,
            _recurring_rules.c.period_days,
        )
        .having(sa.func.count() > 1)
        .subquery()
    )

    # 1a. Bump the keeper's last_seen_date to the group's
    #     max. Only targets keepers (rn=1) of duplicate
    #     groups; on a clean DB both filters match zero
    #     rows. The whole ``upgrade()`` runs in a single
    #     transaction managed by Alembic, so a failure
    #     here rolls back the dedup AND the constraint
    #     create together — no half-migrated state.
    op.execute(
        sa.update(_recurring_rules)
        .where(
            sa.and_(
                _recurring_rules.c.id == ranked_cte.c.id,
                ranked_cte.c.rn == 1,
                sa.tuple_(
                    _recurring_rules.c.merchant_id,
                    _recurring_rules.c.amount_min,
                    _recurring_rules.c.amount_max,
                    _recurring_rules.c.currency,
                    _recurring_rules.c.period_days,
                ).in_(
                    sa.select(
                        dupes_subq.c.merchant_id,
                        dupes_subq.c.amount_min,
                        dupes_subq.c.amount_max,
                        dupes_subq.c.currency,
                        dupes_subq.c.period_days,
                    )
                ),
            )
        )
        .values(last_seen_date=ranked_cte.c.group_max_last_seen)
    )

    # 1b. Delete the losers (rn > 1). On a clean DB the
    #     inner SELECT is empty so the DELETE matches
    #     zero rows.
    op.execute(
        sa.delete(_recurring_rules).where(
            _recurring_rules.c.id.in_(sa.select(ranked_cte.c.id).where(ranked_cte.c.rn > 1))
        )
    )

    # --- 2. Add the UNIQUE constraint -------------------------------
    #
    # ``op.create_unique_constraint`` is not supported on
    # SQLite (the dialect cannot ``ALTER TABLE ... ADD
    # CONSTRAINT``). ``op.batch_alter_table`` is the
    # portable abstraction: on SQLite it uses a
    # copy-and-move strategy that recreates the table
    # with the constraint, on PostgreSQL it emits a
    # regular ``ALTER TABLE``. The constraint name
    # ``uq_recurring_rules_upsert_key`` matches the
    # model's ``__table_args__`` declaration.
    with op.batch_alter_table("recurring_rules") as batch_op:
        batch_op.create_unique_constraint(
            "uq_recurring_rules_upsert_key",
            ["merchant_id", "amount_min", "amount_max", "currency", "period_days"],
        )


def downgrade() -> None:
    """Drop the UNIQUE constraint.

    The dedup step is not reversed — the rows that were
    deleted are not re-inserted. Downgrading after a
    dedup that removed real rows is data-destructive, but
    the dedup is documented as defensive (should be a
    no-op on a clean DB), so a downgrade on a clean DB is
    reversible modulo the dedup step.
    """
    with op.batch_alter_table("recurring_rules") as batch_op:
        batch_op.drop_constraint(
            "uq_recurring_rules_upsert_key",
            type_="unique",
        )
