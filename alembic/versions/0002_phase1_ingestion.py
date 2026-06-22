"""phase1 ingestion

Revision ID: 0002_phase1_ingestion
Revises: 0001_initial
Create Date: 2026-06-21 19:50:00.000000

Phase 1 — domain foundation.

This migration creates the four core domain tables introduced by the
Phase 1 ingestion pipeline, then seeds the three known Chilean banks.
The schema and seed are kept together on purpose: a fresh checkout
running ``alembic upgrade head`` ends up with a working database in
one step, which is what the dev environment, tests, and the demo
data all assume.

Schema overview
---------------

* ``banks`` — issuing financial institutions. ``name`` is the short
  stable identifier; ``password_formula`` is the token the ingestion
  pipeline reads to know how to decrypt a given bank's PDF.
* ``credit_cards`` — a card belongs to exactly one bank. We store
  the masked PAN only; the full card number is never persisted.
* ``statements`` — a monthly PDF statement for a card.
  ``(credit_card_id, file_hash)`` is unique so re-uploading the same
  file for the same card is a no-op.
* ``transactions`` — line items extracted from a statement. ``amount``
  and ``installment_value`` are ``Numeric(15, 2)``; never ``Float``
  for money. ``raw_json`` carries the verbatim LLM extraction
  output for re-derivation.

Cascade rules
-------------

Every foreign key uses ``ON DELETE CASCADE`` so deleting a bank
removes its cards, which removes their statements, which removes
their transactions. The application layer does the same
``cascade="all, delete-orphan"`` walk via SQLAlchemy relationships,
keeping the two stories in lock-step.

Indexes
-------

The migration adds explicit indexes on every foreign key column
(``banks.id`` is the primary key, so it does not need one) and on
``transactions.date`` so monthly roll-up queries are cheap. The
unique constraint on ``(credit_card_id, file_hash)`` automatically
builds a composite index that supports idempotent uploads.

Seed data
---------

Three banks are inserted with their production
``password_formula`` values. The seed uses ``op.bulk_insert`` so it
runs inside the same transaction as the table creates and either
both succeed or both roll back.
"""

from collections.abc import Sequence
from datetime import datetime, timezone

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002_phase1_ingestion"
down_revision: str | Sequence[str] | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the four domain tables and seed the known banks.

    All DDL runs in batch mode (``render_as_batch=True`` in
    ``alembic/env.py``) so SQLite's limitations around ``ALTER
    TABLE`` do not bite if this migration is later extended.
    """
    # Banks -----------------------------------------------------------------
    op.create_table(
        "banks",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("name", sa.String(length=50), nullable=False),
        sa.Column("display_name", sa.String(length=100), nullable=False),
        sa.Column("password_formula", sa.String(length=50), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_banks"),
        sa.UniqueConstraint("name", name="uq_banks_name"),
    )
    op.create_index("ix_banks_name", "banks", ["name"], unique=True)

    # Credit cards ----------------------------------------------------------
    op.create_table(
        "credit_cards",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("bank_id", sa.String(length=36), nullable=False),
        sa.Column("card_number_masked", sa.String(length=25), nullable=False),
        sa.Column("cardholder", sa.String(length=100), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(
            ["bank_id"],
            ["banks.id"],
            name="fk_credit_cards_bank_id_banks",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_credit_cards"),
    )
    op.create_index("ix_credit_cards_bank_id", "credit_cards", ["bank_id"])

    # Statements ------------------------------------------------------------
    op.create_table(
        "statements",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("credit_card_id", sa.String(length=36), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("statement_date", sa.Date(), nullable=False),
        sa.Column("file_path", sa.String(length=512), nullable=False),
        sa.Column("file_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.ForeignKeyConstraint(
            ["credit_card_id"],
            ["credit_cards.id"],
            name="fk_statements_credit_card_id_credit_cards",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_statements"),
        sa.UniqueConstraint(
            "credit_card_id",
            "file_hash",
            name="uq_statements_credit_card_id_file_hash",
        ),
    )
    op.create_index("ix_statements_credit_card_id", "statements", ["credit_card_id"])
    op.create_index("ix_statements_file_hash", "statements", ["file_hash"])
    op.create_index("ix_statements_status", "statements", ["status"])

    # Transactions ----------------------------------------------------------
    op.create_table(
        "transactions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("statement_id", sa.String(length=36), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=False),
        sa.Column("amount", sa.Numeric(precision=15, scale=2), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("category", sa.String(length=50), nullable=True),
        sa.Column("installment_number", sa.Integer(), nullable=True),
        sa.Column("installment_total", sa.Integer(), nullable=True),
        sa.Column("installment_value", sa.Numeric(precision=15, scale=2), nullable=True),
        sa.Column("raw_json", sa.JSON(), nullable=True),
        sa.ForeignKeyConstraint(
            ["statement_id"],
            ["statements.id"],
            name="fk_transactions_statement_id_statements",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_transactions"),
    )
    op.create_index("ix_transactions_statement_id", "transactions", ["statement_id"])
    op.create_index("ix_transactions_date", "transactions", ["date"])

    # Seed banks ------------------------------------------------------------
    # Three Chilean banks with their production ``password_formula``
    # values. Timestamps are stamped in Python at migration time so
    # the bulk insert stays self-contained — no SQL function
    # expressions need to be embedded in the row dicts.
    now = datetime.now(timezone.utc)
    banks_table = sa.table(
        "banks",
        sa.column("id", sa.String),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
        sa.column("name", sa.String),
        sa.column("display_name", sa.String),
        sa.column("password_formula", sa.String),
        sa.column("is_active", sa.Boolean),
    )
    op.bulk_insert(
        banks_table,
        [
            {
                "id": "00000000-0000-0000-0000-000000000001",
                "created_at": now,
                "updated_at": now,
                "name": "santander",
                "display_name": "Banco Santander",
                "password_formula": "rut_sin_dv",
                "is_active": True,
            },
            {
                "id": "00000000-0000-0000-0000-000000000002",
                "created_at": now,
                "updated_at": now,
                "name": "itau",
                "display_name": "Itaú",
                "password_formula": "rut_sin_dv",
                "is_active": True,
            },
            {
                "id": "00000000-0000-0000-0000-000000000003",
                "created_at": now,
                "updated_at": now,
                "name": "banco_de_chile",
                "display_name": "Banco de Chile",
                "password_formula": "rut_ultimos_4",
                "is_active": True,
            },
        ],
    )


def downgrade() -> None:
    """Reverse the migration: drop tables, then seed disappears with them.

    Tables are dropped in the reverse of the create order so the
    foreign-key constraints do not need explicit ``CASCADE``
    handling — by the time we drop ``credit_cards`` the
    ``statements`` and ``transactions`` tables are already gone.
    """
    op.drop_index("ix_transactions_date", table_name="transactions")
    op.drop_index("ix_transactions_statement_id", table_name="transactions")
    op.drop_table("transactions")
    op.drop_index("ix_statements_status", table_name="statements")
    op.drop_index("ix_statements_file_hash", table_name="statements")
    op.drop_index("ix_statements_credit_card_id", table_name="statements")
    op.drop_table("statements")
    op.drop_index("ix_credit_cards_bank_id", table_name="credit_cards")
    op.drop_table("credit_cards")
    op.drop_index("ix_banks_name", table_name="banks")
    op.drop_table("banks")
