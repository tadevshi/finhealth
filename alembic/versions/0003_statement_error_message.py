"""add statement error_message

Revision ID: 0003_statement_error_message
Revises: 0002_phase1_ingestion
Create Date: 2026-06-21 21:30:00.000000

Phase 1 — Work Unit 4 (Ingestion orchestrator).

This migration adds a single nullable ``error_message`` column to
the ``statements`` table. The orchestrator populates it when a
statement's ingestion pipeline raises (decryption failure, LLM
extraction error, amount parse error, etc.) so the operator can
inspect the failure without grepping application logs.

The column is intentionally ``Text`` (not ``String(N)``): a
traceback-style error message is unbounded in length, and the
orchestrator caps the stored value at 500 characters before
writing — see :func:`app.services.ingestion._truncate_error`.
A bound ``String`` would silently truncate at the database
boundary, hiding the error from the operator.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003_statement_error_message"
down_revision: str | Sequence[str] | None = "0002_phase1_ingestion"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the ``error_message`` column to ``statements``.

    The column is nullable with no default so existing rows
    remain untouched (their ``error_message`` is implicitly
    ``NULL``). The DDL is wrapped in ``batch_alter_table`` for
    SQLite compatibility — SQLite does not support
    ``ALTER TABLE ... ADD COLUMN`` for some column types without
    the batch abstraction, and using it here keeps the
    migration portable if the project ever moves to PostgreSQL.
    """
    with op.batch_alter_table("statements") as batch_op:
        batch_op.add_column(sa.Column("error_message", sa.Text(), nullable=True))


def downgrade() -> None:
    """Remove the ``error_message`` column from ``statements``.

    Reverses the upgrade. Existing data in the column is lost
    — the application contract is that ``error_message`` is a
    debugging aid, not authoritative data.
    """
    with op.batch_alter_table("statements") as batch_op:
        batch_op.drop_column("error_message")
