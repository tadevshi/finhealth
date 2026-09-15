"""Additive migration: dedicated Card Payments closed-set category.

Revision ID: 0003_card_payments_category
Revises: 0002_statement_source

Upgrade: inserts the 13th closed-set category ("Card Payments",
deterministic UUID ``10000000-0000-0000-0000-000000000013``,
``sort_order`` 13). Payments TO the credit card (statement payment
lines such as "MONTO CANCELADO", "PAGO DE TARJETA") are not spend, so
they get their own category and the dashboard spend distributions
exclude it at query time.

The insert is idempotent: it is skipped when a row with the same id OR
the same name already exists — an operator who pre-created the
category under a different id adopts their row as-is instead of
hitting the unique ``name`` constraint (PostgreSQL allows only one
conflict target per statement, so a single guarded
``INSERT ... SELECT`` covers both keys).

Downgrade: deletes the Card Payments row by its deterministic id
(and name) only. Referencing ``transactions.category_id`` values are
preserved by the FK's ``ON DELETE SET NULL`` clause, so transaction
history survives with a NULL category."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0003_card_payments_category"
down_revision: str | Sequence[str] | None = "0002_statement_source"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Same deterministic id family as the 0001 category seeds
# (``10000000-0000-0000-0000-00000000000{1..13}``).
_CARD_PAYMENTS_ID = "10000000-0000-0000-0000-000000000013"
_CARD_PAYMENTS_NAME = "Card Payments"
_CARD_PAYMENTS_SORT_ORDER = 13

# Guarded insert: skipped when the id or the unique name is taken, so a
# re-run (or an operator pre-seed) never duplicates the category.
_INSERT_CARD_PAYMENTS_SQL = (
    "INSERT INTO categories (id, created_at, updated_at, name, display_name, sort_order) "
    "SELECT :id, now(), now(), :name, :display_name, :sort_order "
    "WHERE NOT EXISTS (SELECT 1 FROM categories WHERE id = :id) "
    "AND NOT EXISTS (SELECT 1 FROM categories WHERE name = :name)"
)

_DELETE_CARD_PAYMENTS_SQL = "DELETE FROM categories WHERE id = :id AND name = :name"


def upgrade() -> None:
    """Insert the Card Payments category (idempotent on id and name)."""
    op.execute(
        sa.text(_INSERT_CARD_PAYMENTS_SQL).bindparams(
            id=_CARD_PAYMENTS_ID,
            name=_CARD_PAYMENTS_NAME,
            display_name=_CARD_PAYMENTS_NAME,
            sort_order=_CARD_PAYMENTS_SORT_ORDER,
        )
    )


def downgrade() -> None:
    """Delete the Card Payments category row by its deterministic id.

    Transactions that referenced the category keep their rows: the
    ``transactions.category_id`` FK is ``ON DELETE SET NULL``, so the
    downgrade only removes the taxonomy entry, never history."""
    op.execute(
        sa.text(_DELETE_CARD_PAYMENTS_SQL).bindparams(
            id=_CARD_PAYMENTS_ID,
            name=_CARD_PAYMENTS_NAME,
        )
    )
