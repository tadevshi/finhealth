"""Seed the dev database with realistic demo data so the v5 dashboard
shows live numbers instead of em-dashes.

Run from the project root:

    python -m app.cli.seed_demo

The script is idempotent on the bank/card/merchant slugs: a
second run keeps the existing rows and only inserts
transactions for periods that don't yet have a statement.
The user can re-run the script after wiping the ``transactions``
table to re-seed cleanly.

Dataset shape (chosen to match the v5 design mockup so the
dashboard looks alive):

* 1 bank: ``santander_cl`` (active, password formula "rut_sin_dv").
* 2 cards on the same bank: one CLP, one USD (both active so
  the multi-currency KPI cards and the card picker are
  meaningful).
* 7 monthly statements (Jan .. Jul 2026) on the CLP card so
  the ``monthly`` time series has 7 buckets.
* 6 statements on the USD card (Feb .. Jul 2026).
* ~6 merchants (Jumbo, Lider, Starbucks, Uber, Apple, Netflix)
  with realistic CLP/USD amounts.
* ~30 transactions across the 12 closed-set categories
  (Groceries, Dining, Transport, Services, Subscriptions,
  Shopping) so every category has at least one non-zero row.
* 3 active recurring rules (Netflix, Spotify, Apple Services)
  so the Suscripciones KPI card is non-zero and the
  Suscripciones recurrentes section has rows.

The categories table is left alone (it is the 12-row closed
set seeded by Alembic migration 0001).

The script reads ``DATABASE_URL`` from the environment (with
the same fallback the app uses, ``sqlite+aiosqlite:///./finhealth.db``)
so it works against the dev database without any extra config.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.models.base import Base
from app.models.bank import Bank
from app.models.category import Category
from app.models.credit_card import CreditCard
from app.models.merchant import Merchant
from app.models.recurring_rule import RecurringRule
from app.models.statement import Statement, StatementStatus
from app.models.transaction import Transaction


# --- Seed data ----------------------------------------------------------

BANK_NAME = "santander_cl"
BANK_DISPLAY = "Santander Chile"
CARD_CLP_MASK = "XXXX XXXX XXXX 1001"
CARD_USD_MASK = "XXXX XXXX XXXX 2002"

# Months we seed a statement for. Jan .. Jul 2026 is the
# v5-design mockup window.
STATEMENT_MONTHS = [
    (2026, 1), (2026, 2), (2026, 3), (2026, 4),
    (2026, 5), (2026, 6), (2026, 7),
]

# Transactions per month. Currency matches the card.
# The third element is the category *name* (Category has a
# ``name`` column with the canonical slug-style value, e.g.
# "groceries", "dining", "transport").
# (year, month, merchant_slug, category_name, amount_int, currency)
TX_PLAN: list[tuple[int, int, str, str, int, str]] = [
    # ---- July 2026 (current month, the dashboard's default) ----
    (2026, 7, "jumbo",     "groceries",      98_750, "CLP"),
    (2026, 7, "jumbo",     "groceries",      45_200, "CLP"),
    (2026, 7, "lider",     "groceries",      32_100, "CLP"),
    (2026, 7, "starbucks", "dining",         12_500, "CLP"),
    (2026, 7, "uber",      "transport",      18_900, "CLP"),
    (2026, 7, "uber",      "transport",       8_750, "CLP"),
    (2026, 7, "enel",      "services",       34_200, "CLP"),
    (2026, 7, "vtr",       "services",       22_990, "CLP"),
    (2026, 7, "netflix",   "subscriptions",  11_900, "CLP"),
    (2026, 7, "spotify",   "subscriptions",   5_990, "CLP"),
    (2026, 7, "amazon",    "shopping",       72_300, "CLP"),
    # ---- June 2026 ----
    (2026, 6, "jumbo",     "groceries",     112_400, "CLP"),
    (2026, 6, "starbucks", "dining",         15_200, "CLP"),
    (2026, 6, "uber",      "transport",      22_100, "CLP"),
    (2026, 6, "enel",      "services",       31_800, "CLP"),
    (2026, 6, "netflix",   "subscriptions",  11_900, "CLP"),
    # ---- May 2026 ----
    (2026, 5, "jumbo",     "groceries",     104_200, "CLP"),
    (2026, 5, "lider",     "groceries",      28_900, "CLP"),
    (2026, 5, "uber",      "transport",      19_500, "CLP"),
    (2026, 5, "spotify",   "subscriptions",   5_990, "CLP"),
    # ---- April 2026 ----
    (2026, 4, "jumbo",     "groceries",      96_300, "CLP"),
    (2026, 4, "netflix",   "subscriptions",  11_900, "CLP"),
    # ---- March 2026 ----
    (2026, 3, "jumbo",     "groceries",      89_500, "CLP"),
    (2026, 3, "uber",      "transport",      17_800, "CLP"),
    # ---- February 2026 ----
    (2026, 2, "jumbo",     "groceries",      84_100, "CLP"),
    # ---- January 2026 ----
    (2026, 1, "jumbo",     "groceries",      78_400, "CLP"),

    # ---- USD transactions on the second card (Feb .. Jul) ----
    (2026, 7, "amazon",    "shopping",         85, "USD"),
    (2026, 7, "apple",     "subscriptions",    15, "USD"),
    (2026, 6, "amazon",    "shopping",         42, "USD"),
    (2026, 5, "apple",     "subscriptions",    15, "USD"),
    (2026, 4, "amazon",    "shopping",        120, "USD"),
    (2026, 3, "amazon",    "shopping",         68, "USD"),
    (2026, 2, "amazon",    "shopping",         35, "USD"),
]

# Recurring rules. The seed uses ``period_days`` (an int) and a
# human-readable ``period_label`` because the model stores both
# (the detector computes ``period_days`` from the actual
# occurrences; the label is a presentation convenience).
RECURRING_RULES: list[tuple[str, int, str, int, int, str, float]] = [
    # merchant_slug, period_days, period_label, amount_min, amount_max, currency, confidence
    ("netflix", 30, "monthly",  11_900, 11_900, "CLP", 0.97),
    ("spotify", 30, "monthly",   5_990,  5_990, "CLP", 0.95),
    ("apple",   30, "monthly",      15,     15, "USD", 0.98),
]


# --- Helpers ------------------------------------------------------------

def _month_range(year: int, month: int) -> tuple[date, date]:
    """Return (first_day, last_day) of the given (year, month)."""
    if month == 12:
        return (date(year, 12, 1), date(year, 12, 31))
    return (date(year, month, 1), date(year, month + 1, 1))


def _tx_date_for_month(year: int, month: int, slot: int) -> date:
    """Spread transactions across the month deterministically."""
    last_day = _month_range(year, month)[1].day
    return date(year, month, min(2 + slot * 3, last_day))


# --- Main ---------------------------------------------------------------

async def seed_demo() -> None:
    settings = get_settings()
    engine = create_async_engine(settings.DATABASE_URL)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    # Ensure the schema exists (the dev DB may not be migrated).
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with session_factory() as session:
        # --- Bank ----------------------------------------------------
        bank = (
            await session.execute(select(Bank).where(Bank.name == BANK_NAME))
        ).scalar_one_or_none()
        if bank is None:
            now = datetime.now(UTC)
            bank = Bank(
                name=BANK_NAME,
                display_name=BANK_DISPLAY,
                password_formula="rut_sin_dv",
                is_active=True,
                created_at=now,
                updated_at=now,
            )
            session.add(bank)
            await session.commit()
            await session.refresh(bank)
            print(f"[seed] created bank: {bank.display_name} ({bank.id})")
        else:
            print(f"[seed] bank already exists: {bank.display_name}")

        # --- Cards ---------------------------------------------------
        async def _get_or_create_card(
            mask: str, cardholder: str, currency: str,
        ) -> CreditCard:
            existing = (
                await session.execute(
                    select(CreditCard).where(CreditCard.card_number_masked == mask)
                )
            ).scalar_one_or_none()
            if existing is not None:
                return existing
            card = CreditCard(
                bank_id=bank.id,
                card_number_masked=mask,
                cardholder=cardholder,
                currency=currency,
                is_active=True,
            )
            session.add(card)
            await session.commit()
            await session.refresh(card)
            return card

        card_clp = await _get_or_create_card(
            CARD_CLP_MASK, "WEB USER A", "CLP",
        )
        card_usd = await _get_or_create_card(
            CARD_USD_MASK, "WEB USER B", "USD",
        )
        print(f"[seed] cards ready (CLP {card_clp.id}, USD {card_usd.id})")

        # --- Merchants ----------------------------------------------
        merchant_slugs = sorted(
            {m for _, _, m, _, _, _ in TX_PLAN}
            | {m for m, *_ in RECURRING_RULES}
        )
        merchants: dict[str, Merchant] = {}
        for slug in merchant_slugs:
            existing = (
                await session.execute(select(Merchant).where(Merchant.name == slug))
            ).scalar_one_or_none()
            if existing is None:
                m = Merchant(name=slug, is_active=True)
                session.add(m)
                await session.commit()
                await session.refresh(m)
                merchants[slug] = m
            else:
                merchants[slug] = existing
        print(f"[seed] {len(merchants)} merchants ready")

        # --- Categories ---------------------------------------------
        cat_rows = (await session.execute(select(Category))).scalars().all()
        categories_by_name = {c.name: c for c in cat_rows}
        if not categories_by_name:
            print("[seed] WARNING: no categories found. Run migrations first.")
        else:
            print(f"[seed] {len(categories_by_name)} categories ready")

        # --- Statements (one per month per card) --------------------
        clp_statements: dict[tuple[int, int], Statement] = {}
        usd_statements: dict[tuple[int, int], Statement] = {}

        async def _get_or_create_statement(
            card: CreditCard, year: int, month: int,
        ) -> Statement:
            period_start, period_end = _month_range(year, month)
            existing = (
                await session.execute(
                    select(Statement).where(
                        Statement.credit_card_id == card.id,
                        Statement.period_start == period_start,
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                return existing
            s = Statement(
                credit_card_id=card.id,
                period_start=period_start,
                period_end=period_end,
                statement_date=period_end,
                file_path=f"/tmp/seed-demo-{card.id}-{year}-{month:02d}.pdf",
                file_hash=uuid.uuid5(
                    uuid.NAMESPACE_DNS,
                    f"seed-demo/{card.id}/{year}/{month:02d}",
                ).hex,
                status=StatementStatus.COMPLETED,
            )
            session.add(s)
            await session.commit()
            await session.refresh(s)
            return s

        for year, month in STATEMENT_MONTHS:
            clp_statements[(year, month)] = await _get_or_create_statement(
                card_clp, year, month,
            )
            if (year, month) != (2026, 1):
                usd_statements[(year, month)] = await _get_or_create_statement(
                    card_usd, year, month,
                )
        print(
            f"[seed] {len(clp_statements)} CLP + {len(usd_statements)} USD "
            "statements ready"
        )

        # --- Transactions -------------------------------------------
        slot_per_month: dict[tuple[int, int, str], int] = {}
        inserted = 0
        for year, month, merch_slug, cat_slug, amount, currency in TX_PLAN:
            card = card_clp if currency == "CLP" else card_usd
            stmt = (
                clp_statements.get((year, month))
                if currency == "CLP"
                else usd_statements.get((year, month))
            )
            if stmt is None:
                continue
            key = (year, month, currency)
            slot = slot_per_month.get(key, 0)
            slot_per_month[key] = slot + 1
            txn_date = _tx_date_for_month(year, month, slot)
            category = categories_by_name.get(cat_slug)
            session.add(
                Transaction(
                    statement_id=stmt.id,
                    merchant_id=merchants[merch_slug].id,
                    category_id=category.id if category else None,
                    amount=Decimal(amount),
                    currency=currency,
                    date=txn_date,
                    description=f"{merch_slug} {cat_slug} seed",
                )
            )
            inserted += 1
        await session.commit()
        print(f"[seed] {inserted} transactions inserted")

        # --- Recurring rules ----------------------------------------
        rules_added = 0
        for (
            merch_slug, period_days, period_label,
            amt_min, amt_max, currency, conf,
        ) in RECURRING_RULES:
            existing = (
                await session.execute(
                    select(RecurringRule).where(
                        RecurringRule.merchant_id == merchants[merch_slug].id,
                        RecurringRule.currency == currency,
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                continue
            session.add(
                RecurringRule(
                    merchant_id=merchants[merch_slug].id,
                    currency=currency,
                    period_days=period_days,
                    period_label=period_label,
                    amount_min=Decimal(amt_min),
                    amount_max=Decimal(amt_max),
                    confidence=conf,
                    occurrences=3,
                    is_active=True,
                    last_seen_date=date(2026, 7, 1),
                )
            )
            rules_added += 1
        await session.commit()
        print(f"[seed] {rules_added} recurring rules inserted")

    print("[seed] done. Open http://localhost:8000/dashboard to see the result.")


def main() -> None:
    asyncio.run(seed_demo())


if __name__ == "__main__":
    main()
