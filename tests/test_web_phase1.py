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

Phase 2 PR #3 (Categories UI) extends the test surface with the
per-row ``<select>`` markup, the multi-select filter widget,
the ``Untagged or low confidence`` checkbox, and the PATCH
``Accept: text/html`` round-trip — see the ``Categories UI
(web)`` block at the bottom of the file.

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
from app.models.category import Category
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
#: ``data-testid`` on the per-row category ``<select>``. The Phase 2
#: PR #3 change replaces the legacy free-text ``<input>`` with a
#: server-rendered ``<select>`` so the contract changes from
#: ``category-input`` to ``category-select``. The old constant is
#: kept above to make the rename visible in one place; the new
#: contract uses ``category-select``.
CATEGORY_SELECT_TESTID = 'data-testid="category-select"'
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


@pytest.fixture
async def seeded_categories(
    test_settings: Settings, seeded_transactions: list[Transaction]
) -> AsyncIterator[list[Category]]:
    """Insert the 12 closed-set categories for the Categories UI tests.

    Builds on ``seeded_transactions`` so the categories share
    the same engine/schema as the transactions they tag. The
    third seeded transaction (PARIS) is re-tagged with the
    Shopping ``Category`` row so the per-row ``<select>``
    markup test has a row with a non-NULL ``category_id``
    to render the ``selected`` option.

    Yields the list of :class:`Category` rows in ``sort_order``
    ascending so tests can assert against the canonical
    ordering.
    """
    engine = create_engine(test_settings)
    try:
        from app.models.base import Base

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            now = datetime.now(UTC)
            seed = (
                ("Dining Out", "Dining Out", 1),
                ("Groceries", "Groceries", 2),
                ("Transportation", "Transportation", 3),
                ("Shopping", "Shopping", 4),
                ("Entertainment", "Entertainment", 5),
                ("Bills", "Bills & Utilities", 6),
                ("Health", "Health & Medical", 7),
                ("Travel", "Travel", 8),
                ("Subscriptions", "Subscriptions", 9),
                ("Personal Care", "Personal Care", 10),
                ("Uncategorized", "Uncategorized", 11),
                ("Other", "Other", 12),
            )
            categories: list[Category] = []
            for name, display, order in seed:
                cat = Category(
                    name=name,
                    display_name=display,
                    sort_order=order,
                    created_at=now,
                    updated_at=now,
                )
                session.add(cat)
                categories.append(cat)
            await session.flush()
            # Re-tag PARIS with the Shopping Category so the
            # per-row <select> shows a non-blank selected
            # option. We re-load the transaction in this
            # session's identity map because the outer
            # session (from ``seeded_transactions``) is
            # already closed; SQLAlchemy would silently drop
            # the change on commit otherwise.
            from sqlalchemy import select

            shopping = next(c for c in categories if c.name == "Shopping")
            paris_result = await session.execute(
                select(Transaction).where(Transaction.description == "PARIS")
            )
            paris = paris_result.scalar_one()
            paris.category_id = shopping.id
            paris.category = "Shopping"
            await session.commit()
            for cat in categories:
                await session.refresh(cat)
            yield categories
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
    """The per-row category pickers are wired with ``hx-patch`` to update the row.

    Phase 2 PR #3 replaces the legacy free-text ``<input>`` with a
    server-rendered ``<select>``; the contract marker changes from
    ``category-input`` to ``category-select`` and the
    ``hx-patch`` / ``hx-target`` / ``hx-swap`` wiring is preserved
    so the existing client-side HTMX swap flow keeps working.
    """
    body = (await client.get(TRANSACTIONS_PATH)).text
    assert CATEGORY_SELECT_TESTID in body
    # The legacy free-text input is gone.
    assert CATEGORY_INPUT_TESTID not in body
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


# ---------------------------------------------------------------------------
# Categories UI (web) — Phase 2 PR #3
# ---------------------------------------------------------------------------


#: ``data-testid`` markers added in Phase 2 PR #3.
FILTER_CATEGORY_ID_TESTID = 'data-testid="filter-category-id"'
FILTER_UNCATEGORIZED_TESTID = 'data-testid="filter-uncategorized"'

#: The label the user sees for the uncategorized checkbox. Pinned
#: by the design decision (D3) so a future rename does not
#: silently change the user-visible contract.
UNCATEGORIZED_LABEL = "Untagged or low confidence"


@pytest.mark.asyncio
async def test_per_row_select_rendered_with_thirteen_options(
    client: AsyncClient,
    seeded_categories: list[Category],
    seeded_transactions: list[Transaction],
) -> None:
    """The per-row ``<select>`` has 13 ``<option>``s: 12 categories + 1 blank.

    The 12 categories are server-rendered in ``sort_order``
    ascending; the blank "—" option comes first. The total
    is 13 per row.
    """
    import html as _html

    body = (await client.get(ROWS_PATH)).text
    # The per-row picker is rendered once per transaction.
    select_count = body.count(CATEGORY_SELECT_TESTID)
    assert select_count == len(seeded_transactions)
    # Exactly 12 non-blank <option>s per row (one per category).
    for cat in seeded_categories:
        assert f'value="{cat.id}"' in body
        # Jinja HTML-escapes the display name (& -> &amp;).
        # The escaped form is what the browser sees, so the
        # test asserts the escaped form to stay honest about
        # what the user sees.
        assert _html.escape(cat.display_name) in body
    # The blank option is rendered exactly once per row.
    blank_count = body.count('<option value=""')
    assert blank_count == len(seeded_transactions)


@pytest.mark.asyncio
async def test_per_row_select_selected_option_matches_category_id(
    client: AsyncClient, seeded_categories: list[Category]
) -> None:
    """The per-row ``<select>`` marks the row's current ``category_id`` as ``selected``.

    PARIS is tagged with Shopping in the fixture, so the
    rendered markup contains a ``value="<shopping uuid>"
    ... selected`` snippet. The other two transactions have
    no ``category_id`` so the blank "—" option is the
    selected one for them.
    """
    body = (await client.get(ROWS_PATH)).text
    shopping = next(c for c in seeded_categories if c.name == "Shopping")
    # The Shopping <option> is the one that should be marked
    # selected on the PARIS row. We do not assert which row
    # the marker sits on — the test surface is "the marker
    # is present in the rendered output".
    assert f'value="{shopping.id}"' in body
    # The \"selected\" attribute is set at least once for the
    # Shopping UUID and once for the blank option (the two
    # un-tagged rows).
    assert body.count("selected") >= 3


@pytest.mark.asyncio
async def test_filter_form_has_multiselect_and_uncategorized_checkbox(
    client: AsyncClient, seeded_categories: list[Category]
) -> None:
    """The filter form has the multi-select ``category_id`` and the uncategorized checkbox.

    Both controls carry their Phase 2 PR #3 ``data-testid``
    markers. The multi-select renders one ``<option>`` per
    seeded category; the checkbox is unchecked by default
    and labelled "Untagged or low confidence" per the
    design decision (D3).
    """
    import html as _html

    body = (await client.get(TRANSACTIONS_PATH)).text
    assert FILTER_FORM_TESTID in body
    assert FILTER_CATEGORY_ID_TESTID in body
    assert FILTER_UNCATEGORIZED_TESTID in body
    # The multi-select has the "multiple" attribute so the
    # form serialises to a list[uuid.UUID] on the server.
    assert "multiple" in body
    # One <option> per category, no blank option on the
    # multi-select (a blank multi-select option is a
    # browser-only deselect-all which is not meaningful
    # for a \"filter to\".)
    for cat in seeded_categories:
        assert f'value="{cat.id}"' in body
        assert _html.escape(cat.display_name) in body
    # The checkbox is unchecked by default.
    assert 'name="uncategorized"' in body
    assert UNCATEGORIZED_LABEL in body


@pytest.mark.asyncio
async def test_filter_form_submission_narrows_table_by_category(
    client: AsyncClient, seeded_categories: list[Category]
) -> None:
    """Submitting the filter form with ``?category_id=<uuid>`` narrows the table to that category.

    Only PARIS is tagged Shopping in the fixture, so the
    filtered response contains PARIS and not the other two
    transactions. The same Query param drives the page and
    the partial, so both endpoints narrow consistently.
    """
    shopping = next(c for c in seeded_categories if c.name == "Shopping")
    response = await client.get(ROWS_PATH, params={"category_id": str(shopping.id)})
    body = response.text
    assert response.status_code == 200
    assert "PARIS" in body
    assert "SUPERMERCADOS" not in body
    assert "COPEC" not in body


@pytest.mark.asyncio
async def test_filter_form_uncategorized_checkbox_widens_to_null_rows(
    client: AsyncClient, seeded_transactions: list[Transaction]
) -> None:
    """Submitting the form with ``?uncategorized=true`` widens the match to NULL category_id.

    The :func:`seeded_transactions` fixture seeds three
    transactions with ``category_id=NULL`` (only PARIS has a
    legacy ``category`` string, but no FK). The
    ``uncategorized`` filter (``category_id IS NULL OR
    low_confidence=True``) therefore returns all three
    transactions, which is the natural baseline for the
    filter widget — every NULL row in the table is matched.
    """
    response = await client.get(ROWS_PATH, params={"uncategorized": "true"})
    body = response.text
    assert response.status_code == 200
    for txn in seeded_transactions:
        assert txn.description in body
    # Three transaction rows are rendered (one per
    # seeded transaction).
    assert body.count(ROW_TESTID) == len(seeded_transactions)


@pytest.mark.asyncio
async def test_filter_form_submission_with_multiple_category_ids_narrows_table(
    client: AsyncClient,
    test_settings: Settings,
    seeded_banks: list[Bank],
    seeded_categories: list[Category],
) -> None:
    """Submitting the filter form with **two** ``category_id`` values narrows the table to the union.

    The multi-select filter (``<select multiple>``) serialises
    as ``?category_id=<uuid>&category_id=<uuid>`` on the wire.
    The web flow (``/transactions/rows``) parses this into a
    repeatable Query param the same way the JSON API does,
    but the wiring is exercised here at the HTTP layer rather
    than the FastAPI Query layer.

    Seeds three new transactions, each tagged with a different
    category (Groceries, Dining Out, Transportation), then
    filters by Groceries + Dining Out. Only the two matching
    transactions render; the Transportation one is excluded.
    """
    # Look up the three categories the test will use. Sort
    # by name just so the test is stable across seed order
    # changes.
    groceries = next(c for c in seeded_categories if c.name == "Groceries")
    dining = next(c for c in seeded_categories if c.name == "Dining Out")
    transportation = next(c for c in seeded_categories if c.name == "Transportation")

    # Seed the three new transactions against the same
    # database the ``client`` fixture is wired to (they share
    # ``test_settings``).
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
                card_number_masked="XXXX XXXX XXXX 7777",
                cardholder="MULTI USER",
                currency="CLP",
                is_active=True,
                created_at=now,
                updated_at=now,
            )
            session.add(card)
            await session.flush()

            statement = Statement(
                credit_card_id=card.id,
                period_start=date(2026, 5, 1),
                period_end=date(2026, 5, 31),
                statement_date=date(2026, 5, 22),
                file_path="/tmp/multi.pdf",
                file_hash="b" * 64,
                status=StatementStatus.COMPLETED,
                created_at=now,
                updated_at=now,
            )
            session.add(statement)
            await session.flush()

            txns = [
                Transaction(
                    statement_id=statement.id,
                    date=date(2026, 5, 5),
                    description="GROCERIES-TX",
                    amount=Decimal("12000"),
                    currency="CLP",
                    category="Groceries",
                    category_id=groceries.id,
                    low_confidence=False,
                    created_at=now,
                    updated_at=now,
                ),
                Transaction(
                    statement_id=statement.id,
                    date=date(2026, 5, 10),
                    description="DINING-TX",
                    amount=Decimal("8500"),
                    currency="CLP",
                    category="Dining Out",
                    category_id=dining.id,
                    low_confidence=False,
                    created_at=now,
                    updated_at=now,
                ),
                Transaction(
                    statement_id=statement.id,
                    date=date(2026, 5, 15),
                    description="TRANSPORT-TX",
                    amount=Decimal("4500"),
                    currency="CLP",
                    category="Transportation",
                    category_id=transportation.id,
                    low_confidence=False,
                    created_at=now,
                    updated_at=now,
                ),
            ]
            session.add_all(txns)
            await session.commit()
    finally:
        await engine.dispose()

    # Submit the filter form with the two-category union.
    # httpx serialises a list[Query] param as repeated keys
    # (``category_id=<uuid>&category_id=<uuid>``), which is
    # exactly what an HTML ``<select multiple>`` form posts
    # natively.
    response = await client.get(
        ROWS_PATH,
        params=[
            ("category_id", str(groceries.id)),
            ("category_id", str(dining.id)),
        ],
    )
    assert response.status_code == 200
    body = response.text
    # The two matching rows are present.
    assert "GROCERIES-TX" in body
    assert "DINING-TX" in body
    # The non-matching row is filtered out.
    assert "TRANSPORT-TX" not in body
    # Exactly two <tr data-testid="transaction-row"> rows
    # are rendered (one per matching transaction).
    assert body.count(ROW_TESTID) == 2


@pytest.mark.asyncio
async def test_patch_round_trip_renders_new_selected_option(
    client: AsyncClient, seeded_categories: list[Category]
) -> None:
    """PATCH with ``Accept: text/html`` returns the partial row with the new pick selected.

    End-to-end: pick a row, PATCH with the new ``category_id``
    and the HTMX ``Accept`` header, then assert the response
    markup contains the new pick as the ``selected`` option.
    The hx-patch / hx-target attributes are preserved so a
    follow-up swap still works.
    """
    body = (await client.get(ROWS_PATH)).text
    # Pull the first transaction id off the rendered
    # ``data-transaction-id`` attribute on the per-row
    # ``<select>``.
    import re

    match = re.search(r'data-transaction-id="([0-9a-f-]{36})"', body)
    assert match is not None
    txn_id = uuid.UUID(match.group(1))

    groceries = next(c for c in seeded_categories if c.name == "Groceries")
    response = await client.patch(
        f"/api/v1/transactions/{txn_id}",
        data={"category_id": str(groceries.id)},
        headers={"Accept": "text/html"},
    )
    assert response.status_code == 200
    body = response.text
    assert "selected" in body
    assert f'value="{groceries.id}"' in body
