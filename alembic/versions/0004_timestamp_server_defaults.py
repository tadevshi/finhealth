"""add server_default to timestamp columns

Revision ID: 0004_timestamp_server_defaults
Revises: 0003_statement_error_message
Create Date: 2026-06-22 19:10:00.000000

Phase 1 — bug fix.

The original ingestion migration (``0002_phase1_ingestion``) created
``created_at`` and ``updated_at`` as ``NOT NULL`` columns *without* a
``server_default`` clause. The ORM model had ``server_default=func.now()``
on both columns, but the database schema that Alembic produced did not
carry the same default — so direct inserts (and any INSERT issued by
SQLAlchemy when the column is omitted from the statement, e.g. via
``RETURNING``) hit ``NOT NULL constraint failed: <table>.created_at``.

This migration rewrites the four domain tables to add
``DEFAULT CURRENT_TIMESTAMP`` to both timestamp columns. The changes
are wrapped in ``op.batch_alter_table`` because SQLite cannot
``ALTER COLUMN`` — the batch abstraction recreates the table under
the hood and copies the data across.

Why this is safe on the existing data
--------------------------------------

Every row that exists in the four tables today was inserted through
one of two paths that *do* set the timestamps explicitly:

* the ``0002`` seed (``op.bulk_insert`` of the three banks, with
  ``created_at`` / ``updated_at`` set in Python at migration time);
* the application code, which always passed the columns through
  (until the previous schema mismatch started surfacing as 500s).

The columns are ``NOT NULL`` in the schema, so a missing default at
the DB level is the *only* reason the constraint trips. Backfilling
``DEFAULT CURRENT_TIMESTAMP`` is a metadata-only change from the
data's perspective: the column's existing values are not touched,
and any future INSERT that omits the column will now succeed.

The companion change in :mod:`app.models.mixins` adds
``default=func.now()`` and ``nullable=False`` to ``TimestampMixin``
so the ORM-side metadata and the database-side metadata stay in
lock-step — this is the real defence against the same drift
reappearing in a future migration.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004_timestamp_server_defaults"
down_revision: str | Sequence[str] | None = "0003_statement_error_message"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Tables whose ``created_at`` / ``updated_at`` columns get the new
# server default. Keeping the list in one place makes it trivial to
# add the next domain model that opts into ``TimestampMixin``.
_TABLES: tuple[str, ...] = ("banks", "credit_cards", "statements", "transactions")


def upgrade() -> None:
    """Add ``DEFAULT CURRENT_TIMESTAMP`` to both timestamp columns on every table.

    ``batch_alter_table`` is the portable abstraction: PostgreSQL gets
    a plain ``ALTER COLUMN ... SET DEFAULT``; SQLite gets a
    ``CREATE TABLE ... ; INSERT ... ; DROP ... ;`` round-trip under
    the hood. Either way, the resulting schema is identical.
    """
    for table in _TABLES:
        with op.batch_alter_table(table) as batch_op:
            batch_op.alter_column(
                "created_at",
                server_default=sa.func.now(),
                existing_type=sa.DateTime(timezone=True),
                existing_nullable=False,
            )
            batch_op.alter_column(
                "updated_at",
                server_default=sa.func.now(),
                existing_type=sa.DateTime(timezone=True),
                existing_nullable=False,
            )


def downgrade() -> None:
    """Remove the ``DEFAULT CURRENT_TIMESTAMP`` from both timestamp columns.

    The columns themselves, the data, and the ``NOT NULL`` constraint
    are all preserved — only the *default expression* is dropped.
    A downgrade followed by a fresh application insert will reproduce
    the original ``NOT NULL constraint failed`` failure mode, which
    is the intended behaviour (it keeps downgrade → upgrade
    round-trips honest).
    """
    for table in _TABLES:
        with op.batch_alter_table(table) as batch_op:
            batch_op.alter_column(
                "created_at",
                server_default=None,
                existing_type=sa.DateTime(timezone=True),
                existing_nullable=False,
            )
            batch_op.alter_column(
                "updated_at",
                server_default=None,
                existing_type=sa.DateTime(timezone=True),
                existing_nullable=False,
            )
