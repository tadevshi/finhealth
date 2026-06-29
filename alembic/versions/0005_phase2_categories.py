"""phase 2 categories table + seed of the 12 Y-NAB taxonomy

Revision ID: 0005_phase2_categories
Revises: 0004_timestamp_server_defaults
Create Date: 2026-06-29 10:00:00.000000

Phase 2 — categories foundation (PR #2 of the phase-2-classification
SDD change).

This migration introduces the closed-set 12-category Y-NAB taxonomy
the application will use from here on. The taxonomy is *flat* (no
parent/child hierarchy) and is seeded at migration time — operators
do not add categories at runtime, only through a new migration. The
seed runs in the same transaction as the table create, so a fresh
``alembic upgrade head`` produces a fully populated ``categories``
table in one step.

Schema overview
---------------

* ``categories.id`` — UUID primary key (``CHAR(36)``). Same
  pattern as every other domain table — debuggable in raw
  database inspection, portable across SQLite/PostgreSQL.
* ``categories.created_at`` / ``updated_at`` — timezone-aware,
  ``NOT NULL``, ``server_default=func.now()`` so the default is
  honoured even on raw SQL inserts.
* ``categories.name`` — short stable identifier (``"Dining Out"``,
  ``"Transportation"``). The LLM is told to emit this value verbatim
  and the ingestion layer does a case-insensitive match against
  it. Unique.
* ``categories.display_name`` — human-readable label
  (``"Dining Out"``). Marketing can re-brand without
  breaking lookups.
* ``categories.sort_order`` — position in the
  ``GET /api/v1/categories`` response. Lower = first.

Seed data
---------

The 12 names are taken from the project's product decisions
(decision #1) and represent the agreed Y-NAB-derived flat
taxonomy. The seed uses ``op.bulk_insert`` so the rows are
inserted in the same transaction as the table create — either
both succeed or both roll back.

Foreign keys
------------

The ``transactions.category_id`` FK is created in migration
``0006_phase2_merchants_transactions_alter`` (PR #2 portion).
This migration only creates the ``categories`` table itself,
the index on ``sort_order`` (for the ``GET`` ordering), and
the unique index on ``name`` (driven by the
``uq_categories_name`` unique constraint that the
``String(50)`` column carries).

Why a separate index on ``sort_order`` when ``name`` is unique
--------------------------------------------------------------------

``sort_order`` is the column the ``GET /api/v1/categories``
endpoint orders by. The query plan benefits from a B-tree on
``sort_order`` for the 12-row table, and adding it now keeps
the schema consistent with the rest of the project (every
column that drives a query path has an explicit index).
"""

from collections.abc import Sequence
from datetime import datetime, timezone
from uuid import uuid4

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0005_phase2_categories"
down_revision: str | Sequence[str] | None = "0004_timestamp_server_defaults"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# ---------------------------------------------------------------------------
# Seed rows
# ---------------------------------------------------------------------------
#
# The 12 categories are the agreed Y-NAB-derived flat taxonomy. The
# ``sort_order`` column controls the order in which the
# ``GET /api/v1/categories`` endpoint returns them — lower = first.
# Timestamps are stamped in Python at migration time so the bulk
# insert stays self-contained.
#
# Adding / removing / renaming a category is a separate migration:
# a follow-up PR would add a row via a new ``op.bulk_insert`` (or
# drop a row via ``op.execute("DELETE FROM categories WHERE ...")``)
# so the change is reviewable in isolation.

_SEED_CATEGORIES: tuple[dict[str, object], ...] = (
    {
        "name": "Dining Out",
        "display_name": "Dining Out",
        "sort_order": 1,
    },
    {
        "name": "Groceries",
        "display_name": "Groceries",
        "sort_order": 2,
    },
    {
        "name": "Transportation",
        "display_name": "Transportation",
        "sort_order": 3,
    },
    {
        "name": "Shopping",
        "display_name": "Shopping",
        "sort_order": 4,
    },
    {
        "name": "Entertainment",
        "display_name": "Entertainment",
        "sort_order": 5,
    },
    {
        "name": "Bills",
        "display_name": "Bills & Utilities",
        "sort_order": 6,
    },
    {
        "name": "Health",
        "display_name": "Health & Medical",
        "sort_order": 7,
    },
    {
        "name": "Travel",
        "display_name": "Travel",
        "sort_order": 8,
    },
    {
        "name": "Subscriptions",
        "display_name": "Subscriptions",
        "sort_order": 9,
    },
    {
        "name": "Personal Care",
        "display_name": "Personal Care",
        "sort_order": 10,
    },
    {
        "name": "Uncategorized",
        "display_name": "Uncategorized",
        "sort_order": 11,
    },
    {
        "name": "Other",
        "display_name": "Other",
        "sort_order": 12,
    },
)


def upgrade() -> None:
    """Create the ``categories`` table and seed the 12 closed-set rows.

    All DDL runs in batch mode (``render_as_batch=True`` in
    ``alembic/env.py``) so SQLite's limitations around ``ALTER
    TABLE`` do not bite if this migration is later extended. The
    table and the seed rows are inserted in a single transaction
    by Alembic so the database is always consistent.
    """
    op.create_table(
        "categories",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("name", sa.String(length=50), nullable=False),
        sa.Column("display_name", sa.String(length=100), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_categories"),
        sa.UniqueConstraint("name", name="uq_categories_name"),
    )
    op.create_index("ix_categories_name", "categories", ["name"], unique=True)
    op.create_index("ix_categories_sort_order", "categories", ["sort_order"])

    # Seed rows ------------------------------------------------------------
    # Twelve categories, timestamps stamped in Python at migration time so
    # the bulk insert is self-contained. The order of the rows does not
    # matter for the seed; the ``sort_order`` column drives the display
    # order.
    now = datetime.now(timezone.utc)
    categories_table = sa.table(
        "categories",
        sa.column("id", sa.String),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
        sa.column("name", sa.String),
        sa.column("display_name", sa.String),
        sa.column("sort_order", sa.Integer),
    )
    op.bulk_insert(
        categories_table,
        [
            {
                **row,
                "id": str(uuid4()),
                "created_at": now,
                "updated_at": now,
            }
            for row in _SEED_CATEGORIES
        ],
    )


def downgrade() -> None:
    """Drop the ``categories`` table.

    Indexes are dropped implicitly by ``op.drop_table`` — Alembic
    tracks the dependent indexes and removes them first so a
    subsequent re-upgrade produces a clean schema. Downgrading
    past this migration also drops the seeded rows, which is
    fine because the only consumer of those rows is the
    ``transactions.category_id`` FK in migration 0006, which is
    dropped by that migration's own downgrade before this one
    runs.
    """
    op.drop_index("ix_categories_sort_order", table_name="categories")
    op.drop_index("ix_categories_name", table_name="categories")
    op.drop_table("categories")
