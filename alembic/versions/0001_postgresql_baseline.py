"""Destructive PostgreSQL baseline for the current FinHealth schema."""

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa

from alembic import op

revision: str = "0001_postgresql_baseline"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TIMESTAMP = sa.DateTime(timezone=True)
_SEED_BANKS = (
    ("00000000-0000-0000-0000-000000000001", "santander", "Banco Santander", "rut_sin_dv"),
    ("00000000-0000-0000-0000-000000000002", "itau", "Itaú", "rut_sin_dv"),
    (
        "00000000-0000-0000-0000-000000000003",
        "banco_de_chile",
        "Banco de Chile",
        "rut_ultimos_4",
    ),
)
_SEED_CATEGORIES = (
    ("10000000-0000-0000-0000-000000000001", "Dining Out", "Dining Out", 1),
    ("10000000-0000-0000-0000-000000000002", "Groceries", "Groceries", 2),
    ("10000000-0000-0000-0000-000000000003", "Transportation", "Transportation", 3),
    ("10000000-0000-0000-0000-000000000004", "Shopping", "Shopping", 4),
    ("10000000-0000-0000-0000-000000000005", "Entertainment", "Entertainment", 5),
    ("10000000-0000-0000-0000-000000000006", "Bills", "Bills & Utilities", 6),
    ("10000000-0000-0000-0000-000000000007", "Health", "Health & Medical", 7),
    ("10000000-0000-0000-0000-000000000008", "Travel", "Travel", 8),
    ("10000000-0000-0000-0000-000000000009", "Subscriptions", "Subscriptions", 9),
    ("10000000-0000-0000-0000-000000000010", "Personal Care", "Personal Care", 10),
    ("10000000-0000-0000-0000-000000000011", "Uncategorized", "Uncategorized", 11),
    ("10000000-0000-0000-0000-000000000012", "Other", "Other", 12),
)


def _timestamps() -> tuple[sa.Column[object], sa.Column[object]]:
    return (
        sa.Column("created_at", _TIMESTAMP, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", _TIMESTAMP, nullable=False, server_default=sa.func.now()),
    )


def upgrade() -> None:
    """Create the complete current schema and deterministic reference data."""
    op.create_table(
        "banks",
        sa.Column("id", sa.String(36), nullable=False),
        *_timestamps(),
        sa.Column("name", sa.String(50), nullable=False),
        sa.Column("display_name", sa.String(100), nullable=False),
        sa.Column("password_formula", sa.String(50), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.PrimaryKeyConstraint("id", name="pk_banks"),
        sa.UniqueConstraint("name", name="uq_banks_name"),
    )
    op.create_index("ix_banks_name", "banks", ["name"], unique=True)

    op.create_table(
        "categories",
        sa.Column("id", sa.String(36), nullable=False),
        *_timestamps(),
        sa.Column("name", sa.String(50), nullable=False),
        sa.Column("display_name", sa.String(100), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_categories"),
        sa.UniqueConstraint("name", name="uq_categories_name"),
    )
    op.create_index("ix_categories_sort_order", "categories", ["sort_order"])

    op.create_table(
        "credit_cards",
        sa.Column("id", sa.String(36), nullable=False),
        *_timestamps(),
        sa.Column("bank_id", sa.String(36), nullable=False),
        sa.Column("card_number_masked", sa.String(25), nullable=False),
        sa.Column("cardholder", sa.String(100), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.ForeignKeyConstraint(
            ["bank_id"], ["banks.id"], name="fk_credit_cards_bank_id_banks", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_credit_cards"),
    )
    op.create_index("ix_credit_cards_bank_id", "credit_cards", ["bank_id"])

    op.create_table(
        "statements",
        sa.Column("id", sa.String(36), nullable=False),
        *_timestamps(),
        sa.Column("credit_card_id", sa.String(36), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("statement_date", sa.Date(), nullable=False),
        sa.Column("file_path", sa.String(512), nullable=False),
        sa.Column("file_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["credit_card_id"],
            ["credit_cards.id"],
            name="fk_statements_credit_card_id_credit_cards",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_statements"),
        sa.UniqueConstraint(
            "credit_card_id", "file_hash", name="uq_statements_credit_card_id_file_hash"
        ),
    )
    for name, columns in (
        ("ix_statements_credit_card_id", ["credit_card_id"]),
        ("ix_statements_file_hash", ["file_hash"]),
        ("ix_statements_status", ["status"]),
    ):
        op.create_index(name, "statements", columns)

    op.create_table(
        "merchants",
        sa.Column("id", sa.String(36), nullable=False),
        *_timestamps(),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("default_category_id", sa.String(36), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.ForeignKeyConstraint(
            ["default_category_id"],
            ["categories.id"],
            name="fk_merchants_default_category_id_categories",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_merchants"),
        sa.UniqueConstraint("name", name="uq_merchants_name"),
    )
    op.create_index("ix_merchants_name", "merchants", ["name"], unique=True)

    op.create_table(
        "merchant_aliases",
        sa.Column("id", sa.String(36), nullable=False),
        *_timestamps(),
        sa.Column("merchant_id", sa.String(36), nullable=False),
        sa.Column("alias_text", sa.String(200), nullable=False),
        sa.Column("normalized", sa.String(200), nullable=False),
        sa.Column("source", sa.String(16), nullable=False, server_default=sa.text("'auto'")),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(
            ["merchant_id"],
            ["merchants.id"],
            name="fk_merchant_aliases_merchant_id_merchants",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_merchant_aliases"),
        sa.UniqueConstraint("alias_text", name="uq_merchant_aliases_alias_text"),
    )
    for name, columns in (
        ("ix_merchant_aliases_merchant_id", ["merchant_id"]),
        ("ix_merchant_aliases_normalized", ["normalized"]),
    ):
        op.create_index(name, "merchant_aliases", columns)

    op.create_table(
        "recurring_rules",
        sa.Column("id", sa.String(36), nullable=False),
        *_timestamps(),
        sa.Column("merchant_id", sa.String(36), nullable=False),
        sa.Column("period_days", sa.Integer(), nullable=False),
        sa.Column("period_label", sa.String(16), nullable=False),
        sa.Column("amount_min", sa.Numeric(15, 2), nullable=False),
        sa.Column("amount_max", sa.Numeric(15, 2), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("last_seen_date", sa.Date(), nullable=False),
        sa.Column("occurrences", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.ForeignKeyConstraint(
            ["merchant_id"],
            ["merchants.id"],
            name="fk_recurring_rules_merchant_id_merchants",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_recurring_rules"),
        sa.UniqueConstraint(
            "merchant_id",
            "amount_min",
            "amount_max",
            "currency",
            "period_days",
            name="uq_recurring_rules_upsert_key",
        ),
    )
    op.create_index(
        "ix_recurring_rules_merchant_currency_period",
        "recurring_rules",
        ["merchant_id", "currency", "period_days"],
    )
    op.create_index("ix_recurring_rules_is_active", "recurring_rules", ["is_active"])

    op.create_table(
        "transactions",
        sa.Column("id", sa.String(36), nullable=False),
        *_timestamps(),
        sa.Column("statement_id", sa.String(36), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("description", sa.String(500), nullable=False),
        sa.Column("amount", sa.Numeric(15, 2), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("category", sa.String(50), nullable=True),
        sa.Column("installment_number", sa.Integer(), nullable=True),
        sa.Column("installment_total", sa.Integer(), nullable=True),
        sa.Column("installment_value", sa.Numeric(15, 2), nullable=True),
        sa.Column("raw_json", sa.JSON(), nullable=True),
        sa.Column("category_id", sa.String(36), nullable=True),
        sa.Column("low_confidence", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("merchant_id", sa.String(36), nullable=True),
        sa.Column("recurring_rule_id", sa.String(36), nullable=True),
        sa.ForeignKeyConstraint(
            ["statement_id"],
            ["statements.id"],
            name="fk_transactions_statement_id_statements",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["category_id"],
            ["categories.id"],
            name="fk_transactions_category_id_categories",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["merchant_id"],
            ["merchants.id"],
            name="fk_transactions_merchant_id_merchants",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["recurring_rule_id"],
            ["recurring_rules.id"],
            name="fk_transactions_recurring_rule_id_recurring_rules",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_transactions"),
    )
    for name, columns in (
        ("ix_transactions_statement_id", ["statement_id"]),
        ("ix_transactions_date", ["date"]),
        ("ix_transactions_category_id", ["category_id"]),
        ("ix_transactions_merchant_id", ["merchant_id"]),
        ("ix_transactions_recurring_rule_id", ["recurring_rule_id"]),
    ):
        op.create_index(name, "transactions", columns)

    now = datetime.now(UTC)
    banks = sa.table(
        "banks",
        sa.column("id"),
        sa.column("created_at"),
        sa.column("updated_at"),
        sa.column("name"),
        sa.column("display_name"),
        sa.column("password_formula"),
        sa.column("is_active"),
    )
    categories = sa.table(
        "categories",
        sa.column("id"),
        sa.column("created_at"),
        sa.column("updated_at"),
        sa.column("name"),
        sa.column("display_name"),
        sa.column("sort_order"),
    )
    op.bulk_insert(
        banks,
        [
            dict(
                zip(("id", "name", "display_name", "password_formula"), row, strict=True),
                created_at=now,
                updated_at=now,
                is_active=True,
            )
            for row in _SEED_BANKS
        ],
    )
    op.bulk_insert(
        categories,
        [
            dict(
                zip(("id", "name", "display_name", "sort_order"), row, strict=True),
                created_at=now,
                updated_at=now,
            )
            for row in _SEED_CATEGORIES
        ],
    )


def downgrade() -> None:
    """The destructive baseline intentionally has no downgrade path."""
