"""phase 2 — categories columns on transactions (PR #2) + merchants + aliases (PR #4)

Revision ID: 0006_phase2_merchants_transactions_alter
Revises: 0005_phase2_categories
Create Date: 2026-06-29 10:30:00.000000

This migration is the single-file coordination point for the
``phase-2-classification`` SDD change. It hosts *two* PRs'
worth of schema changes in one file (per design decision #1 in
the PR #2 design):

* **PR #2 portion** — the closed-set category support on the
  ``transactions`` table: ``category_id`` (nullable FK to
  ``categories.id`` with ``ON DELETE SET NULL``),
  ``low_confidence`` (``Boolean NOT NULL DEFAULT 0``), and the
  B-tree index on ``category_id`` (per design decision #8 in
  the PR #2 design).
* **PR #4 portion** — the merchant canonicalisation support:
  the ``merchants`` table, the ``merchant_aliases`` table, and
  ``transactions.merchant_id`` (nullable FK to ``merchants.id``
  with ``ON DELETE SET NULL`` + B-tree index). The two new
  tables follow the same ``UUIDMixin`` + ``TimestampMixin``
  pattern as ``categories`` (UUID primary key stored as
  ``CHAR(36)`` for debuggability, timezone-aware
  ``created_at`` / ``updated_at`` with
  ``server_default=func.now()``).

The docstring labels which lines belong to which PR. The
downgrade body inverts the upgrade in reverse order: the PR #4
additions are dropped *first* (so the FK from
``transactions.merchant_id`` and the two new tables come down
cleanly), then the PR #2 additions (the original index, FK,
and columns).

Why a single file
-----------------

Splitting the two PRs into separate migrations would force
PR #4 to depend on PR #2's migration *and* introduce a
linear-history branch in the migration DAG. The single-file
approach keeps the DAG linear and lets the round-trip test
exercise both PRs as one upgrade/downgrade cycle. The
``openspec/changes/phase-2-pr4-merchants-and-aliases/design.md``
docstring cross-references this file as the extension point
for PR #4.

Why ``op.batch_alter_table`` for the ``transactions`` changes
-------------------------------------------------------------

``transactions`` already exists (created in migration 0002)
and SQLite cannot ``ALTER TABLE ... ADD CONSTRAINT`` directly
or ``ALTER TABLE ... ADD COLUMN ... REFERENCES`` with the
foreign-key clause baked in. ``batch_alter_table`` is the
portable abstraction: on PostgreSQL it issues a plain
``ALTER TABLE``; on SQLite it copies the table under the
hood. Either way the resulting schema is identical and the
downgrade reverses both the column and the constraint. The
two new ``merchants`` / ``merchant_aliases`` tables are
created with a plain ``op.create_table`` because they do not
yet exist — the batch abstraction is only needed when
*altering* an existing table.
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
    """Add the PR #2 closed-set columns *and* the PR #4 merchant schema.

    The two PRs share the migration file (per the file-level
    docstring); the body is split into two clearly-labelled
    sections so a reviewer can scan the change in one pass.

    PR #2 portion
    -------------

    Add ``category_id``, ``low_confidence``, and the index on
    ``transactions`` in a single ``batch_alter_table`` block
    so SQLite's table-copy-under-the-hood path runs once, not
    three times. The foreign-key clause is encoded directly on
    the column so Alembic generates a ``ForeignKeyConstraint``
    entry in the table's DDL — matching the pattern used in
    migration 0002 for ``transactions.statement_id``.

    PR #4 portion
    -------------

    Create ``merchants`` (with a nullable FK to ``categories``
    for the ``default_category_id`` column the
    ``KNOWN_MERCHANT_PATTERNS`` dict populates) and
    ``merchant_aliases`` (with a CASCADE FK to ``merchants``,
    a UNIQUE ``alias_text``, and a non-unique index on
    ``normalized`` for the per-row lookup in
    :func:`app.services.merchants.normalize`). Then add the
    ``transactions.merchant_id`` column with a ``SET NULL``
    FK to ``merchants`` and a B-tree index so the
    ``WHERE merchant_id = ?`` filter in any future endpoint
    has a clean query plan.
    """
    # --- PR #2 portion ------------------------------------------------
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

    # --- PR #4 portion ------------------------------------------------
    # The two new tables are created with a plain
    # ``op.create_table`` because they do not exist yet — the
    # batch abstraction is only required when *altering* an
    # existing table. The columns mirror the
    # ``Category`` model: ``CHAR(36)`` UUID PK, timezone-aware
    # ``created_at`` / ``updated_at`` with
    # ``server_default=func.now()``, ``name`` UNIQUE on
    # ``merchants`` and ``alias_text`` UNIQUE on
    # ``merchant_aliases``.
    op.create_table(
        "merchants",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("default_category_id", sa.String(length=36), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.PrimaryKeyConstraint("id", name="pk_merchants"),
        sa.UniqueConstraint("name", name="uq_merchants_name"),
        sa.ForeignKeyConstraint(
            ["default_category_id"],
            ["categories.id"],
            name="fk_merchants_default_category_id_categories",
            ondelete="SET NULL",
        ),
    )
    op.create_index("ix_merchants_name", "merchants", ["name"], unique=True)

    op.create_table(
        "merchant_aliases",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("merchant_id", sa.String(length=36), nullable=False),
        sa.Column("alias_text", sa.String(length=200), nullable=False),
        sa.Column("normalized", sa.String(length=200), nullable=False),
        sa.Column(
            "source",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'auto'"),
        ),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_merchant_aliases"),
        sa.UniqueConstraint("alias_text", name="uq_merchant_aliases_alias_text"),
        sa.ForeignKeyConstraint(
            ["merchant_id"],
            ["merchants.id"],
            name="fk_merchant_aliases_merchant_id_merchants",
            ondelete="CASCADE",
        ),
    )
    op.create_index("ix_merchant_aliases_merchant_id", "merchant_aliases", ["merchant_id"])
    op.create_index("ix_merchant_aliases_normalized", "merchant_aliases", ["normalized"])

    with op.batch_alter_table("transactions") as batch_op:
        batch_op.add_column(
            sa.Column(
                "merchant_id",
                sa.String(length=36),
                nullable=True,
            ),
        )
        batch_op.create_foreign_key(
            "fk_transactions_merchant_id_merchants",
            "merchants",
            ["merchant_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_index(
            "ix_transactions_merchant_id",
            ["merchant_id"],
        )


def downgrade() -> None:
    """Reverse the upgrade: drop PR #4 additions first, then PR #2.

    The order is the inverse of ``upgrade`` so the FK chain
    drops cleanly: the ``transactions.merchant_id`` FK is
    removed *before* the ``merchants`` table itself (otherwise
    SQLite would refuse to drop a table referenced by a live
    FK), and the ``merchant_aliases`` table is dropped *before*
    ``merchants`` (the CASCADE FK on
    ``merchant_aliases.merchant_id`` would otherwise block the
    drop). The PR #2 portion of the downgrade is identical to
    the original PR #2-only version.
    """
    # --- PR #4 portion (reverse) --------------------------------------
    with op.batch_alter_table("transactions") as batch_op:
        batch_op.drop_index("ix_transactions_merchant_id")
        batch_op.drop_constraint("fk_transactions_merchant_id_merchants", type_="foreignkey")
        batch_op.drop_column("merchant_id")

    op.drop_index("ix_merchant_aliases_normalized", table_name="merchant_aliases")
    op.drop_index("ix_merchant_aliases_merchant_id", table_name="merchant_aliases")
    op.drop_table("merchant_aliases")

    op.drop_index("ix_merchants_name", table_name="merchants")
    op.drop_table("merchants")

    # --- PR #2 portion (reverse) --------------------------------------
    with op.batch_alter_table("transactions") as batch_op:
        batch_op.drop_index("ix_transactions_category_id")
        batch_op.drop_constraint("fk_transactions_category_id_categories", type_="foreignkey")
        batch_op.drop_column("low_confidence")
        batch_op.drop_column("category_id")
