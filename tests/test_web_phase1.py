"""Integration tests for the Phase 1 web pages.

Covers Work Unit 5 of the Phase 1 chain:

* ``GET /upload`` — renders the drag-and-drop form, populates the
  bank dropdown from the database, and ships the right form
  attributes so the client-side Alpine + fetch flow works.
* ``GET /transactions`` — renders the filter form and an empty
  state when the database is empty.
* ``GET /transactions/rows`` — the HTMX partial endpoint returns
  just the ``<tr>`` rows.
* Filter combinations are honoured by both the page and the
  partial.

The tests build a throwaway FastAPI app, seed a small set of
banks/transactions, and drive the routes with ``httpx``. The
``client`` fixture in :mod:`tests.conftest` creates the schema
for us; this module only seeds the rows it needs.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import Settings
from app.db.engine import create_engine
from app.models.bank import Bank
from app.models.credit_card import CreditCard
from app.models.statement import Statement, StatementStatus
from app.models.transaction import Transaction

# ---------------------------------------------------------------------------
# Constants — markers used in the assertions
# ---------------------------------------------------------------------------

UPLOAD_PATH = "/upload"
TRANSACTIONS_PATH = "/transactions"
ROWS_PATH = "/transactions/rows"

# Form-field test ids — pin the contract so the test catches a
# template rename. The card number, cardholder, and currency
# are no longer in the form: they are read off the PDF by the
# LLM. The form only ships the bank dropdown, the RUT
# input, and the file picker.
BANK_SELECT_TESTID = 'data-testid="bank-select"'
RUT_INPUT_TESTID = 'data-testid="rut-input"'
SUBMIT_TESTID = 'data-testid="upload-submit"'
FILE_INPUT_TESTID = 'data-testid="file-input"'
FILE_NAME_TESTID = 'data-testid="file-name"'

# Transactions page markers
FILTER_FORM_TESTID = 'data-testid="filter-form"'
TABLE_TESTID = 'data-testid="transactions-table"'
TBODY_TESTID = 'data-testid="transaction-list-body"'
ROW_TESTID = 'data-testid="transaction-row"'
CATEGORY_INPUT_TESTID = 'data-testid="category-input"'
DESCRIPTION_TESTID = 'data-testid="transaction-description"'
AMOUNT_TESTID = 'data-testid="transaction-amount"'


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def seeded_banks(test_settings: Settings) -> AsyncIterator[list[Bank]]:
    """Insert the three real Chilean banks and yield them.

    Uses ``Base.metadata.create_all`` via the engine so the
    ``banks`` table exists before the seed insert. The
    ``test_settings`` fixture already creates a fresh file
    per test, so there is no risk of cross-test pollution.
    """
    engine = create_engine(test_settings)
    try:
        from app.models.base import Base

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        now = datetime.now(UTC)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            banks = [
                Bank(
                    name="santander",
                    display_name="Banco Santander",
                    password_formula="rut_sin_dv",
                    is_active=True,
                    created_at=now,
                    updated_at=now,
                ),
                Bank(
                    name="itau",
                    display_name="Itaú",
                    password_formula="rut_sin_dv",
                    is_active=True,
                    created_at=now,
                    updated_at=now,
                ),
                Bank(
                    name="banco_de_chile",
                    display_name="Banco de Chile",
                    password_formula="rut_ultimos_4",
                    is_active=True,
                    created_at=now,
                    updated_at=now,
                ),
            ]
            session.add_all(banks)
            await session.commit()
            for bank in banks:
                await session.refresh(bank)
            yield banks
    finally:
        await engine.dispose()


@pytest.fixture
async def seeded_transactions(
    test_settings: Settings, seeded_banks: list[Bank]
) -> AsyncIterator[list[Transaction]]:
    """Insert a bank, a card, a statement, and three transactions.

    Yields the created transactions so tests can assert against
    them by id, description, etc.
    """
    engine = create_engine(test_settings)
    try:
        from app.models.base import Base

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            now = datetime.now(UTC)
            bank = seeded_banks[0]
            card = CreditCard(
                bank_id=bank.id,
                card_number_masked="XXXX XXXX XXXX 0463",
                cardholder="LUIS SOTILLO",
                currency="CLP",
                is_active=True,
                created_at=now,
                updated_at=now,
            )
            session.add(card)
            await session.flush()

            statement = Statement(
                credit_card_id=card.id,
                period_start=date(2026, 4, 1),
                period_end=date(2026, 4, 30),
                statement_date=date(2026, 4, 22),
                file_path="/tmp/test.pdf",
                file_hash="a" * 64,
                status=StatementStatus.COMPLETED,
                created_at=now,
                updated_at=now,
            )
            session.add(statement)
            await session.flush()

            txns = [
                Transaction(
                    statement_id=statement.id,
                    date=date(2026, 4, 5),
                    description="SUPERMERCADOS LIDER",
                    amount=Decimal("12450"),
                    currency="CLP",
                    created_at=now,
                    updated_at=now,
                ),
                Transaction(
                    statement_id=statement.id,
                    date=date(2026, 4, 10),
                    description="COMBUSTIBLE COPEC",
                    amount=Decimal("35000"),
                    currency="CLP",
                    created_at=now,
                    updated_at=now,
                ),
                Transaction(
                    statement_id=statement.id,
                    date=date(2026, 4, 15),
                    description="PARIS",
                    amount=Decimal("89900"),
                    currency="CLP",
                    category="Shopping",
                    installment_number=3,
                    installment_total=6,
                    installment_value=Decimal("89900"),
                    created_at=now,
                    updated_at=now,
                ),
            ]
            session.add_all(txns)
            await session.commit()
            for txn in txns:
                await session.refresh(txn)
            yield txns
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Upload page
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upload_page_returns_200_html(client: AsyncClient) -> None:
    """``GET /upload`` renders with status 200 and HTML content type."""
    response = await client.get(UPLOAD_PATH)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")


@pytest.mark.asyncio
async def test_upload_page_extends_base_layout(client: AsyncClient) -> None:
    """The page inherits the shared ``base.html`` (Tailwind, HTMX, Alpine)."""
    body = (await client.get(UPLOAD_PATH)).text
    assert "tailwindcss" in body
    assert "htmx" in body
    assert "alpinejs" in body


@pytest.mark.asyncio
async def test_upload_page_has_required_form_fields(client: AsyncClient) -> None:
    """Every form field and the submit button are present with their testids.

    Card number, cardholder, and currency are no longer in
    the form: the LLM reads them off the PDF. The form ships
    only the bank dropdown, the RUT input, and the file
    picker.
    """
    body = (await client.get(UPLOAD_PATH)).text

    assert BANK_SELECT_TESTID in body
    assert RUT_INPUT_TESTID in body
    assert SUBMIT_TESTID in body
    assert FILE_INPUT_TESTID in body
    assert FILE_NAME_TESTID in body


@pytest.mark.asyncio
async def test_upload_page_omits_removed_form_fields(client: AsyncClient) -> None:
    """Card number, cardholder, and currency are no longer in the form.

    Regression guard: a refactor that accidentally re-adds
    them would break the UX goal of having the LLM populate
    those fields.
    """
    body = (await client.get(UPLOAD_PATH)).text
    assert 'name="card_number_masked"' not in body
    assert 'name="cardholder"' not in body
    assert 'name="currency"' not in body


@pytest.mark.asyncio
async def test_upload_page_populates_bank_dropdown(
    client: AsyncClient, seeded_banks: list[Bank]
) -> None:
    """Every seeded bank is rendered as an ``<option>`` in the dropdown.

    The dropdown value is the bank ``name`` (the stable internal
    identifier, matching the form field ``name="bank_name"`` in
    the upload endpoint), not ``display_name``. ``display_name``
    is shown as the user-facing label.
    """
    body = (await client.get(UPLOAD_PATH)).text
    for bank in seeded_banks:
        assert f'value="{bank.name}"' in body
        assert bank.display_name in body


@pytest.mark.asyncio
async def test_upload_page_has_alpine_drag_and_drop(client: AsyncClient) -> None:
    """The drop zone is wired with Alpine ``@dragover``/``@drop`` handlers."""
    body = (await client.get(UPLOAD_PATH)).text
    assert "@dragover.prevent" in body
    assert "@dragleave.prevent" in body
    assert "@drop.prevent" in body


@pytest.mark.asyncio
async def test_upload_page_has_drop_zone_state(client: AsyncClient) -> None:
    """The Alpine component tracks ``dragging`` and ``file`` state."""
    body = (await client.get(UPLOAD_PATH)).text
    assert "dragging:" in body or "dragging =" in body
    assert "file:" in body or "file =" in body


@pytest.mark.asyncio
async def test_upload_page_uses_only_pdf(client: AsyncClient, seeded_banks: list[Bank]) -> None:
    """The file input has ``accept="application/pdf,.pdf"``."""
    body = (await client.get(UPLOAD_PATH)).text
    assert 'accept="application/pdf' in body


@pytest.mark.asyncio
async def test_upload_page_renders_with_no_banks(client: AsyncClient) -> None:
    """The upload page renders with the empty-state option when no banks are seeded.

    Defence against a fresh checkout where the seed migration has
    not been run: the page should still load (no 500) and show the
    ``-- Select a bank --`` placeholder.
    """
    response = await client.get(UPLOAD_PATH)
    assert response.status_code == 200
    assert "-- Select a bank --" in response.text


# ---------------------------------------------------------------------------
# Transactions page
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transactions_page_returns_200_html(client: AsyncClient) -> None:
    """``GET /transactions`` returns 200 with text/html."""
    response = await client.get(TRANSACTIONS_PATH)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")


@pytest.mark.asyncio
async def test_transactions_page_renders_filter_form(client: AsyncClient) -> None:
    """The filter form is present with all its inputs."""
    body = (await client.get(TRANSACTIONS_PATH)).text
    assert FILTER_FORM_TESTID in body
    assert 'name="date_from"' in body
    assert 'name="date_to"' in body
    assert 'name="min_amount"' in body
    assert 'name="max_amount"' in body
    assert 'name="description"' in body
    assert 'name="currency"' in body


@pytest.mark.asyncio
async def test_transactions_page_renders_empty_state(client: AsyncClient) -> None:
    """With no transactions, the page shows the empty-state row."""
    body = (await client.get(TRANSACTIONS_PATH)).text
    assert TABLE_TESTID in body
    assert TBODY_TESTID in body
    # Empty-state message is in the rendered partial
    assert "No transactions match" in body


@pytest.mark.asyncio
async def test_transactions_page_renders_transactions(
    client: AsyncClient, seeded_transactions: list[Transaction]
) -> None:
    """Seeded transactions are rendered as ``<tr>`` rows in the table."""
    body = (await client.get(TRANSACTIONS_PATH)).text
    assert TABLE_TESTID in body
    assert body.count(ROW_TESTID) == len(seeded_transactions)
    for txn in seeded_transactions:
        assert txn.description in body


@pytest.mark.asyncio
async def test_transactions_page_total_count(
    client: AsyncClient, seeded_transactions: list[Transaction]
) -> None:
    """The page header shows the count of matching transactions."""
    body = (await client.get(TRANSACTIONS_PATH)).text
    assert f"{len(seeded_transactions)} transaction(s)" in body


@pytest.mark.asyncio
async def test_transactions_page_uses_htmx_for_filtering(
    client: AsyncClient, seeded_transactions: list[Transaction]
) -> None:
    """The filter form posts via ``hx-get`` to the partial endpoint."""
    body = (await client.get(TRANSACTIONS_PATH)).text
    assert 'hx-get="/transactions/rows"' in body
    assert 'hx-target="#transaction-list-body"' in body


@pytest.mark.asyncio
async def test_transactions_page_categories_have_htmx_patch(
    client: AsyncClient, seeded_transactions: list[Transaction]
) -> None:
    """The category inputs are wired with ``hx-patch`` to update the row."""
    body = (await client.get(TRANSACTIONS_PATH)).text
    assert CATEGORY_INPUT_TESTID in body
    # The hx-patch attribute appears at least once (one per row)
    assert 'hx-patch="/api/v1/transactions/' in body


@pytest.mark.asyncio
async def test_transactions_page_filter_by_description(
    client: AsyncClient, seeded_transactions: list[Transaction]
) -> None:
    """A ``?description=`` filter narrows the rows to the matching ones."""
    response = await client.get(TRANSACTIONS_PATH, params={"description": "PARIS"})
    body = response.text
    assert "PARIS" in body
    assert "SUPERMERCADOS" not in body
    assert "COPEC" not in body


@pytest.mark.asyncio
async def test_transactions_page_filter_by_min_amount(
    client: AsyncClient, seeded_transactions: list[Transaction]
) -> None:
    """A ``?min_amount=`` filter excludes amounts below the threshold."""
    response = await client.get(TRANSACTIONS_PATH, params={"min_amount": "20000"})
    body = response.text
    # COPEC ($35,000) and PARIS ($89,900) pass; LIDER ($12,450) does not.
    assert "COPEC" in body
    assert "PARIS" in body
    assert "LIDER" not in body


@pytest.mark.asyncio
async def test_transactions_page_filter_by_currency(
    client: AsyncClient, seeded_transactions: list[Transaction]
) -> None:
    """A ``?currency=`` filter returns only rows with that currency."""
    response = await client.get(TRANSACTIONS_PATH, params={"currency": "USD"})
    body = response.text
    # All seeded transactions are CLP, so no rows match USD
    assert "No transactions match" in body


# ---------------------------------------------------------------------------
# HTMX partial
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transactions_rows_returns_partial_html(client: AsyncClient) -> None:
    """``GET /transactions/rows`` returns just the ``<tr>`` rows (HTML)."""
    response = await client.get(ROWS_PATH)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    body = response.text
    # The partial does NOT include the surrounding <table> or
    # <thead> — it is the table body only.
    assert "<table" not in body
    assert "<thead" not in body


@pytest.mark.asyncio
async def test_transactions_rows_empty_state(client: AsyncClient) -> None:
    """The partial returns the empty-state row when no rows match."""
    body = (await client.get(ROWS_PATH)).text
    assert "No transactions match" in body


@pytest.mark.asyncio
async def test_transactions_rows_with_data(
    client: AsyncClient, seeded_transactions: list[Transaction]
) -> None:
    """The partial renders the seeded transactions as ``<tr>`` rows."""
    body = (await client.get(ROWS_PATH)).text
    assert body.count(ROW_TESTID) == len(seeded_transactions)
    for txn in seeded_transactions:
        assert txn.description in body


@pytest.mark.asyncio
async def test_transactions_rows_filter_by_description(
    client: AsyncClient, seeded_transactions: list[Transaction]
) -> None:
    """The partial honours the ``description`` filter the same way the page does."""
    response = await client.get(ROWS_PATH, params={"description": "PARIS"})
    body = response.text
    assert "PARIS" in body
    assert "SUPERMERCADOS" not in body


@pytest.mark.asyncio
async def test_transactions_rows_amount_is_formatted(
    client: AsyncClient, seeded_transactions: list[Transaction]
) -> None:
    """The amount column shows the formatted absolute value (e.g. ``12,450.00``)."""
    body = (await client.get(ROWS_PATH)).text
    assert "12,450.00" in body
    assert "35,000.00" in body
    assert "89,900.00" in body


@pytest.mark.asyncio
async def test_transactions_rows_filter_by_statement_id(
    client: AsyncClient, seeded_transactions: list[Transaction]
) -> None:
    """A bogus ``statement_id`` returns the empty state."""
    response = await client.get(ROWS_PATH, params={"statement_id": str(uuid.uuid4())})
    body = response.text
    assert "No transactions match" in body
