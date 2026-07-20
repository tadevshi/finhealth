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
the same fallback the app uses, ``sqlite+aiosqlite:///data/finhealth.db``)
so it works against the dev database without any extra config.
"""

from __future__ import annotations

import asyncio
import calendar
import unicodedata
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.models.bank import Bank
from app.models.base import Base
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
SEED_PROVENANCE = "finhealth-demo-seed:v1"
SEED_NAMESPACE = uuid.UUID("3d28a55c-099b-5e94-9386-55aa9fbcc884")

CATEGORY_ALIASES: dict[str, str] = {
    "dining": "Dining Out",
    "dining out": "Dining Out",
    "groceries": "Groceries",
    "grocery": "Groceries",
    "transport": "Transportation",
    "transportation": "Transportation",
    "services": "Bills",
    "bills": "Bills",
    "utilities": "Bills",
    "subscriptions": "Subscriptions",
    "shopping": "Shopping",
    "health": "Health",
    "travel": "Travel",
    "entertainment": "Entertainment",
    "personal care": "Personal Care",
    "other": "Other",
    "uncategorized": "Uncategorized",
}

# Months we seed a statement for. Jan .. Jul 2026 is the
# v5-design mockup window.
STATEMENT_MONTHS = [
    (2026, 1),
    (2026, 2),
    (2026, 3),
    (2026, 4),
    (2026, 5),
    (2026, 6),
    (2026, 7),
]

# Transactions per month. Currency matches the card.
# The third element is the category *name* (Category has a
# ``name`` column with the canonical slug-style value, e.g.
# "groceries", "dining", "transport").
# (year, month, merchant_slug, category_name, amount_int, currency)
TX_PLAN: list[tuple[int, int, str, str, int, str]] = [
    # ---- July 2026 (current month, the dashboard's default) ----
    (2026, 7, "jumbo", "groceries", 98_750, "CLP"),
    (2026, 7, "jumbo", "groceries", 45_200, "CLP"),
    (2026, 7, "lider", "groceries", 32_100, "CLP"),
    (2026, 7, "starbucks", "dining", 12_500, "CLP"),
    (2026, 7, "uber", "transport", 18_900, "CLP"),
    (2026, 7, "uber", "transport", 8_750, "CLP"),
    (2026, 7, "enel", "services", 34_200, "CLP"),
    (2026, 7, "vtr", "services", 22_990, "CLP"),
    (2026, 7, "netflix", "subscriptions", 11_900, "CLP"),
    (2026, 7, "spotify", "subscriptions", 5_990, "CLP"),
    (2026, 7, "amazon", "shopping", 72_300, "CLP"),
    # ---- June 2026 ----
    (2026, 6, "jumbo", "groceries", 112_400, "CLP"),
    (2026, 6, "starbucks", "dining", 15_200, "CLP"),
    (2026, 6, "uber", "transport", 22_100, "CLP"),
    (2026, 6, "enel", "services", 31_800, "CLP"),
    (2026, 6, "netflix", "subscriptions", 11_900, "CLP"),
    # ---- May 2026 ----
    (2026, 5, "jumbo", "groceries", 104_200, "CLP"),
    (2026, 5, "lider", "groceries", 28_900, "CLP"),
    (2026, 5, "uber", "transport", 19_500, "CLP"),
    (2026, 5, "spotify", "subscriptions", 5_990, "CLP"),
    # ---- April 2026 ----
    (2026, 4, "jumbo", "groceries", 96_300, "CLP"),
    (2026, 4, "netflix", "subscriptions", 11_900, "CLP"),
    # ---- March 2026 ----
    (2026, 3, "jumbo", "groceries", 89_500, "CLP"),
    (2026, 3, "uber", "transport", 17_800, "CLP"),
    # ---- February 2026 ----
    (2026, 2, "jumbo", "groceries", 84_100, "CLP"),
    # ---- January 2026 ----
    (2026, 1, "jumbo", "groceries", 78_400, "CLP"),
    # ---- USD transactions on the second card (Feb .. Jul) ----
    (2026, 7, "amazon", "shopping", 85, "USD"),
    (2026, 7, "apple", "subscriptions", 15, "USD"),
    (2026, 6, "amazon", "shopping", 42, "USD"),
    (2026, 5, "apple", "subscriptions", 15, "USD"),
    (2026, 4, "amazon", "shopping", 120, "USD"),
    (2026, 3, "amazon", "shopping", 68, "USD"),
    (2026, 2, "amazon", "shopping", 35, "USD"),
]

# Recurring rules. The seed uses ``period_days`` (an int) and a
# human-readable ``period_label`` because the model stores both
# (the detector computes ``period_days`` from the actual
# occurrences; the label is a presentation convenience).
RECURRING_RULES: list[tuple[str, int, str, int, int, str, float]] = [
    # merchant_slug, period_days, period_label, amount_min, amount_max, currency, confidence
    ("netflix", 30, "monthly", 11_900, 11_900, "CLP", 0.97),
    ("spotify", 30, "monthly", 5_990, 5_990, "CLP", 0.95),
    ("apple", 30, "monthly", 15, 15, "USD", 0.98),
]


# --- Helpers ------------------------------------------------------------


def _month_range(year: int, month: int) -> tuple[date, date]:
    """Return (first_day, last_day) of the given (year, month)."""
    return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])


def _tx_date_for_month(year: int, month: int, slot: int) -> date:
    """Spread transactions across the month deterministically."""
    last_day = _month_range(year, month)[1].day
    return date(year, month, min(2 + slot * 3, last_day))


def _normalize_key(value: str) -> str:
    """Return a case-folded Unicode-normalized category alias key."""
    normalized = unicodedata.normalize("NFKD", value.strip().casefold())
    return "".join(ch for ch in normalized if not unicodedata.combining(ch))


def resolve_category_name(value: str) -> str:
    """Resolve a seed plan category key to the canonical closed-set name."""
    return CATEGORY_ALIASES.get(_normalize_key(value), "Uncategorized")


def _seed_uuid(key: str) -> uuid.UUID:
    return uuid.uuid5(SEED_NAMESPACE, key)


def _seed_marker(stable_key: str) -> str:
    return f"{SEED_PROVENANCE}:{stable_key}"


def _contains_seed_marker(value: object) -> bool:
    if isinstance(value, dict):
        return value.get("seed_provenance") == SEED_PROVENANCE
    return isinstance(value, str) and SEED_PROVENANCE in value


def _assert_unmarked_or_seed(row: object | None, stable_key: str) -> None:
    """Reject exact deterministic-key collisions that are not seed-owned."""
    if row is None:
        return
    marker_value: object
    if isinstance(row, Bank):
        marker_value = row.display_name
    elif isinstance(row, CreditCard):
        marker_value = row.cardholder
    elif isinstance(row, Merchant):
        marker_value = row.name
    elif isinstance(row, Category):
        marker_value = row.display_name
    elif isinstance(row, Statement):
        marker_value = row.error_message
    elif isinstance(row, RecurringRule):
        marker_value = row.period_label
    elif isinstance(row, Transaction):
        marker_value = row.raw_json
    else:
        raise TypeError(f"unsupported seed-owned row type: {type(row)!r}")
    if not _contains_seed_marker(marker_value):
        row_name = type(row).__name__.replace("Rule", " rule").lower()
        raise RuntimeError(f"seed {row_name} collision for {stable_key}")
    if (
        isinstance(row, Transaction)
        and isinstance(row.raw_json, dict)
        and row.raw_json.get("stable_key") != stable_key
    ):
        raise RuntimeError(f"seed transaction collision for {stable_key}")


def _marked_text(value: str, stable_key: str) -> str:
    marker = _seed_marker(stable_key)
    if marker in value:
        return value
    return f"{value} [{marker}]"


def _mark_seed_owned(row: object, stable_key: str) -> None:
    """Write seed ownership into the safest text/JSON field this schema exposes."""
    if isinstance(row, Bank):
        row.display_name = _marked_text(BANK_DISPLAY, stable_key)
    elif isinstance(row, CreditCard):
        row.cardholder = _marked_text(row.cardholder, stable_key)
    elif isinstance(row, Merchant):
        row.name = _marked_text(row.name, stable_key)
    elif isinstance(row, Category):
        row.display_name = _marked_text(row.display_name, stable_key)
    elif isinstance(row, Statement):
        row.error_message = _seed_marker(stable_key)
    elif isinstance(row, RecurringRule):
        row.period_label = _marked_text(row.period_label, stable_key)


def _statement_hash(card_key: str, year: int, month: int) -> str:
    return uuid.uuid5(SEED_NAMESPACE, f"statement/{card_key}/{year:04d}-{month:02d}").hex


def _tx_key(year: int, month: int, currency: str, slot: int, merchant: str, amount: int) -> str:
    return f"transaction/{year:04d}-{month:02d}/{currency}/{slot:02d}/{merchant}/{amount}"


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
        bank_key = f"bank/{BANK_NAME}"
        bank = await session.get(Bank, _seed_uuid(bank_key))
        _assert_unmarked_or_seed(bank, bank_key)
        if bank is None:
            now = datetime.now(UTC)
            bank = Bank(
                id=_seed_uuid(bank_key),
                name=BANK_NAME,
                display_name=BANK_DISPLAY,
                password_formula="rut_sin_dv",
                is_active=True,
                created_at=now,
                updated_at=now,
            )
            _mark_seed_owned(bank, bank_key)
            session.add(bank)
            await session.commit()
            await session.refresh(bank)
            print(f"[seed] created bank: {bank.display_name} ({bank.id})")
        else:
            print(f"[seed] bank already exists: {bank.display_name}")

        # --- Cards ---------------------------------------------------
        async def _get_or_create_card(
            mask: str,
            cardholder: str,
            currency: str,
        ) -> CreditCard:
            stable_key = f"card/{mask}"
            existing = await session.get(CreditCard, _seed_uuid(stable_key))
            if existing is not None:
                _assert_unmarked_or_seed(existing, stable_key)
                return existing
            card = CreditCard(
                id=_seed_uuid(stable_key),
                bank_id=bank.id,
                card_number_masked=mask,
                cardholder=cardholder,
                currency=currency,
                is_active=True,
            )
            _mark_seed_owned(card, stable_key)
            session.add(card)
            await session.commit()
            await session.refresh(card)
            return card

        card_clp = await _get_or_create_card(
            CARD_CLP_MASK,
            "WEB USER A",
            "CLP",
        )
        card_usd = await _get_or_create_card(
            CARD_USD_MASK,
            "WEB USER B",
            "USD",
        )
        print(f"[seed] cards ready (CLP {card_clp.id}, USD {card_usd.id})")

        # --- Merchants ----------------------------------------------
        merchant_slugs = sorted(
            {m for _, _, m, _, _, _ in TX_PLAN} | {m for m, *_ in RECURRING_RULES}
        )
        merchants: dict[str, Merchant] = {}
        for slug in merchant_slugs:
            stable_key = f"merchant/{slug}"
            existing = await session.get(Merchant, _seed_uuid(stable_key))
            if existing is None:
                m = Merchant(id=_seed_uuid(stable_key), name=slug, is_active=True)
                _mark_seed_owned(m, stable_key)
                session.add(m)
                await session.commit()
                await session.refresh(m)
                merchants[slug] = m
            else:
                _assert_unmarked_or_seed(existing, stable_key)
                merchants[slug] = existing
        print(f"[seed] {len(merchants)} merchants ready")

        # --- Categories ---------------------------------------------
        closed_set = [
            "Dining Out",
            "Groceries",
            "Transportation",
            "Shopping",
            "Entertainment",
            "Bills",
            "Health",
            "Travel",
            "Subscriptions",
            "Personal Care",
            "Uncategorized",
            "Other",
        ]
        cat_rows = (await session.execute(select(Category))).scalars().all()
        categories_by_name = {c.name: c for c in cat_rows}
        for sort_order, canonical in enumerate(closed_set, start=1):
            stable_key = f"category/{canonical}"
            exact = await session.get(Category, _seed_uuid(stable_key))
            if exact is not None:
                _assert_unmarked_or_seed(exact, stable_key)
                categories_by_name[canonical] = exact
            elif canonical not in categories_by_name:
                category = Category(
                    id=_seed_uuid(stable_key),
                    name=canonical,
                    display_name=canonical,
                    sort_order=sort_order,
                )
                _mark_seed_owned(category, stable_key)
                session.add(category)
                await session.flush()
                categories_by_name[canonical] = category
        await session.commit()
        print(f"[seed] {len(categories_by_name)} categories ready")

        # --- Statements (one per month per card) --------------------
        clp_statements: dict[tuple[int, int], Statement] = {}
        usd_statements: dict[tuple[int, int], Statement] = {}

        async def _get_or_create_statement(
            card: CreditCard,
            year: int,
            month: int,
        ) -> Statement:
            period_start, period_end = _month_range(year, month)
            seed_hash = _statement_hash(str(card.id), year, month)
            stable_key = f"statement/{card.id}/{year:04d}-{month:02d}"
            existing = (
                await session.execute(select(Statement).where(Statement.file_hash == seed_hash))
            ).scalar_one_or_none()
            if existing is not None:
                _assert_unmarked_or_seed(existing, stable_key)
                return existing
            s = Statement(
                id=_seed_uuid(stable_key),
                credit_card_id=card.id,
                period_start=period_start,
                period_end=period_end,
                statement_date=period_end,
                file_path=f"/tmp/seed-demo-{card.id}-{year}-{month:02d}.pdf",
                file_hash=seed_hash,
                status=StatementStatus.COMPLETED,
            )
            _mark_seed_owned(s, stable_key)
            session.add(s)
            await session.commit()
            await session.refresh(s)
            return s

        for year, month in STATEMENT_MONTHS:
            clp_statements[(year, month)] = await _get_or_create_statement(
                card_clp,
                year,
                month,
            )
            if (year, month) != (2026, 1):
                usd_statements[(year, month)] = await _get_or_create_statement(
                    card_usd,
                    year,
                    month,
                )
        print(f"[seed] {len(clp_statements)} CLP + {len(usd_statements)} USD statements ready")

        # --- Transactions -------------------------------------------
        slot_per_month: dict[tuple[int, int, str], int] = {}
        inserted = 0
        for year, month, merch_slug, cat_slug, amount, currency in TX_PLAN:
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
            canonical_category = resolve_category_name(cat_slug)
            category = categories_by_name[canonical_category]
            stable_key = _tx_key(year, month, currency, slot, merch_slug, amount)
            tx_id = _seed_uuid(stable_key)
            existing_tx = await session.get(Transaction, tx_id)
            raw_json = {
                "seed_provenance": SEED_PROVENANCE,
                "stable_key": stable_key,
                "statement_hash": stmt.file_hash,
                "category_key": cat_slug,
                "canonical_category": canonical_category,
            }
            if existing_tx is not None:
                _assert_unmarked_or_seed(existing_tx, stable_key)
                existing_tx.statement_id = stmt.id
                existing_tx.merchant_id = merchants[merch_slug].id
                existing_tx.category_id = category.id
                existing_tx.category = canonical_category
                existing_tx.amount = Decimal(amount)
                existing_tx.currency = currency
                existing_tx.date = txn_date
                existing_tx.description = f"{merch_slug} {canonical_category} seed"
                existing_tx.raw_json = raw_json
            else:
                session.add(
                    Transaction(
                        id=tx_id,
                        statement_id=stmt.id,
                        merchant_id=merchants[merch_slug].id,
                        category_id=category.id,
                        category=canonical_category,
                        amount=Decimal(amount),
                        currency=currency,
                        date=txn_date,
                        description=f"{merch_slug} {canonical_category} seed",
                        raw_json=raw_json,
                    )
                )
            inserted += 1
        await session.commit()
        print(f"[seed] {inserted} transactions inserted")

        # --- Recurring rules ----------------------------------------
        rules_added = 0
        for (
            merch_slug,
            period_days,
            period_label,
            amt_min,
            amt_max,
            currency,
            conf,
        ) in RECURRING_RULES:
            stable_key = f"recurring/{merchants[merch_slug].id}/{currency}/{period_days}"
            rule_id = _seed_uuid(stable_key)
            existing = (
                await session.execute(select(RecurringRule).where(RecurringRule.id == rule_id))
            ).scalar_one_or_none()
            if existing is not None:
                _assert_unmarked_or_seed(existing, stable_key)
                continue
            rule = RecurringRule(
                id=rule_id,
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
            _mark_seed_owned(rule, stable_key)
            session.add(rule)
            rules_added += 1
        await session.commit()
        print(f"[seed] {rules_added} recurring rules inserted")

    print("[seed] done. Open http://localhost:8000/dashboard to see the result.")


def main() -> None:
    asyncio.run(seed_demo())


if __name__ == "__main__":
    main()
