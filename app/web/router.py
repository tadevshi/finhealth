"""Web (HTML) routes.

Server-rendered pages using Jinja2 templates. The base template
(``app/web/templates/base.html``) wires the full frontend stack
(HTMX, Alpine.js, Tailwind CSS) and the dark-mode toggle, so a
single ``index`` handler is enough for the Phase 0 MVP.

The router is mounted at the application root (no prefix) by
:mod:`app.main`. ``GET /`` redirects to ``/upload``; the API
surface lives under ``/api/v1`` and is wired separately by
:mod:`app.api.v1.router`.
"""

from __future__ import annotations

import uuid
from datetime import date as date_typ
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session
from app.models.bank import Bank
from app.models.category import Category
from app.models.credit_card import CreditCard
from app.models.merchant import Merchant
from app.models.statement import Statement
from app.models.transaction import Transaction
from app.services.dashboard import DashboardService
from app.services.dashboard_selection import (
    DashboardSelection,
    RangeMode,
    YearMonth,
    parse_selection,
    range_mode_options,
    resolve_window,
)

# Templates directory resolved relative to this file so the router
# works regardless of the working directory the app is launched from
# (uvicorn, pytest, Docker, ...). The same pattern is used by
# ``app.main`` for the static files mount.
TEMPLATES_DIR: Path = Path(__file__).parent / "templates"
templates: Jinja2Templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

web_router: APIRouter = APIRouter(tags=["web"])


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _query_transactions(
    session: AsyncSession,
    *,
    statement_id: uuid.UUID | None,
    date_from: date_typ | None,
    date_to: date_typ | None,
    min_amount: Decimal | None,
    max_amount: Decimal | None,
    description: str | None,
    currency: str | None,
    category_id: list[uuid.UUID] | None = None,
    uncategorized: bool = False,
) -> list[Transaction]:
    """Build and execute the transactions list query for the web layer.

    Centralised so :func:`transactions_page` and
    :func:`transactions_rows_partial` produce identical results for
    the same filter set — a refresh of the full page and an HTMX
    partial request are guaranteed to agree. The Query
    declarations stay on the route functions (FastAPI needs them
    there to validate the wire format); the body of the query
    lives here.

    The function takes a session as its first positional argument
    so the route handlers can stay trivial::

        transactions = await _query_transactions(
            session, statement_id=..., date_from=..., ...
        )

    The session is a plain :class:`AsyncSession` (not a FastAPI
    ``Depends``) because the helper is called directly by the
    route functions, not by FastAPI's dependency injector. The
    route-level ``Depends(get_session)`` provides the session to
    the caller; the helper just receives whatever the caller
    hands it.

    Parameters mirror the route-level Query params one-to-one.
    ``category_id`` and ``uncategorized`` were added in
    Phase 2 PR #3 (Categories UI) and ride the same SQL
    builder; both compose as a parenthesized ``or_`` so the
    precedence is correct when combined with the AND
    filters above (see ``app/api/v1/transactions.py`` for the
    parallel implementation on the JSON API).

    Filters compose with ``AND``. A filter that is ``None`` is
    not applied. ``description`` uses SQL ``ILIKE`` so the match
    is case-insensitive — the only sensible default for a
    free-text search box.
    """
    query = select(Transaction)
    if statement_id is not None:
        query = query.where(Transaction.statement_id == statement_id)
    if date_from is not None:
        query = query.where(Transaction.date >= date_from)
    if date_to is not None:
        query = query.where(Transaction.date <= date_to)
    if min_amount is not None:
        # ``amount`` is signed; bounding the *absolute* value
        # means a refund of $1.000 and a charge of $1.000 both
        # match ``min_amount=500``. ``InstrumentedAttribute``
        # does not expose ``.abs()`` directly, so we use SQL's
        # ``func.abs`` and compare in SQL rather than Python.
        query = query.where(func.abs(Transaction.amount) >= min_amount)
    if max_amount is not None:
        query = query.where(func.abs(Transaction.amount) <= max_amount)
    if description is not None:
        # ``ilike`` is PostgreSQL-specific; SQLite's ``LIKE``
        # is already case-insensitive for ASCII. We use
        # ``func.lower`` on both sides so the SQL is portable.
        needle = f"%{description.lower()}%"
        query = query.where(func.lower(Transaction.description).like(needle))
    if currency is not None:
        query = query.where(Transaction.currency == currency)

    # Category filters — the closed-set ``category_id`` UUID and
    # the "untagged" sentinel compose as a parenthesized ``OR``:
    #
    # * both supplied  -> ``category_id IN (...) OR (IS NULL OR low_confidence=True)``
    # * only UUIDs     -> ``category_id IN (...)``
    # * only untagged  -> ``(category_id IS NULL OR low_confidence=True)``
    # * neither        -> no WHERE clause
    from sqlalchemy import or_  # local import keeps the helper

    # import cluster small for the common no-filter case.
    from sqlalchemy.sql.elements import ColumnElement

    if category_id or uncategorized:
        # ``ColumnElement`` is the common supertype so the
        # ``.in_(...)`` and ``.is_(...)`` calls (which return
        # different SQL element types) compose into one
        # list. ``BinaryExpression`` in the type stub is too
        # narrow for the union.
        clauses: list[ColumnElement[bool]] = []
        if category_id:
            clauses.append(Transaction.category_id.in_(category_id))
        if uncategorized:
            clauses.append(
                or_(
                    Transaction.category_id.is_(None),
                    Transaction.low_confidence.is_(True),
                )
            )
        query = query.where(or_(*clauses))

    # Stable order: oldest transaction first, with a
    # tiebreaker on the primary key so two rows with the same
    # date do not shift between pages.
    query = query.order_by(Transaction.date.asc(), Transaction.id.asc())

    result = await session.execute(query)
    return list(result.scalars().all())


async def _list_categories(session: AsyncSession) -> list[Category]:
    """Return every :class:`Category` row ordered by ``sort_order`` ascending.

    Centralised so the partial template and the filter form
    see the same ordering. Mirrors the JSON endpoint at
    ``GET /api/v1/categories``; the response shape is the
    same (sorted by ``sort_order``).
    """
    result = await session.execute(select(Category).order_by(Category.sort_order.asc()))
    return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Page routes
# ---------------------------------------------------------------------------


@web_router.get(
    "/",
    response_class=RedirectResponse,
    summary="Root — redirect to the upload page",
    status_code=307,
)
async def index() -> RedirectResponse:
    """Redirect ``/`` to ``/upload``.

    The upload page is the natural starting point: every other
    page is downstream of having ingested a statement. Using a
    307 (rather than 302) preserves the HTTP method, which
    matters if a future endpoint accepts POST at the root.
    """
    return RedirectResponse(url="/upload", status_code=307)


@web_router.get(
    "/upload",
    response_class=HTMLResponse,
    summary="Statement upload page (drag-and-drop form)",
    responses={
        200: {
            "description": "Server-rendered upload page with the bank dropdown "
            "and the drag-and-drop zone.",
            "content": {"text/html": {}},
        },
    },
)
async def upload_page(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> HTMLResponse:
    """Render the upload page.

    The bank dropdown is populated server-side from the
    ``banks`` table so the page is meaningful with JavaScript
    disabled and the LLM never has to make a follow-up request
    just to render a ``<select>``.

    Parameters
    ----------
    request:
        The current FastAPI request — required by
        :class:`Jinja2Templates` for URL generation.
    session:
        A request-scoped :class:`AsyncSession` used to read the
        active bank rows. The dependency rolls back on
        exception, so a render error cannot leave a half-built
        transaction open.
    """
    result = await session.execute(
        select(Bank).where(Bank.is_active.is_(True)).order_by(Bank.display_name.asc())
    )
    banks = list(result.scalars().all())

    app_name: str = request.app.state.settings.APP_NAME
    context: dict[str, Any] = {"app_name": app_name, "banks": banks}
    return templates.TemplateResponse(
        request=request,
        name="upload.html",
        context=context,
    )


@web_router.get(
    "/transactions",
    response_class=HTMLResponse,
    summary="Filterable, paginated transaction list page",
    responses={
        200: {
            "description": "Server-rendered transactions page with the filter "
            "form and the (possibly empty) table.",
            "content": {"text/html": {}},
        },
    },
)
async def transactions_page(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    statement_id: Annotated[
        uuid.UUID | None,
        Query(description="Optional filter to a single statement."),
    ] = None,
    date_from: Annotated[
        date_typ | None,
        Query(description="Inclusive lower bound on the posting date."),
    ] = None,
    date_to: Annotated[
        date_typ | None,
        Query(description="Inclusive upper bound on the posting date."),
    ] = None,
    min_amount: Annotated[
        Decimal | None,
        Query(description="Inclusive lower bound on the absolute amount."),
    ] = None,
    max_amount: Annotated[
        Decimal | None,
        Query(description="Inclusive upper bound on the absolute amount."),
    ] = None,
    description: Annotated[
        str | None,
        Query(description="Case-insensitive substring match on the description."),
    ] = None,
    currency: Annotated[
        str | None,
        Query(description="ISO-4217 code ('CLP' or 'USD')."),
    ] = None,
    category_id: Annotated[
        list[uuid.UUID] | None,
        Query(
            description=(
                "Repeatable filter — limit the page to transactions whose "
                "category_id matches any of the supplied UUIDs. Combine with "
                "`uncategorized=true` to also include untagged rows."
            ),
        ),
    ] = None,
    uncategorized: Annotated[
        bool,
        Query(
            description=(
                "When true, also include transactions whose category_id is "
                "NULL or whose low_confidence flag is true."
            ),
        ),
    ] = False,
) -> HTMLResponse:
    """Render the transactions page with the first page of rows.

    The page does *not* paginate yet — Phase 1 is small enough
    that a single page of up to ``MAX_PAGE_SIZE`` rows is fine.
    A future WU will add cursor pagination and a footer.

    The query is delegated to :func:`_query_transactions` so
    the full page and the HTMX partial see the same rows for
    the same filter set. The Query declarations stay on the
    route (FastAPI needs them there to validate the wire
    format); the body of the query lives in the helper.

    The category list is loaded via :func:`_list_categories`
    so the per-row ``<select>`` and the filter form's
    multi-select see the same 12 options in the same order.
    """
    transactions = await _query_transactions(
        session,
        statement_id=statement_id,
        date_from=date_from,
        date_to=date_to,
        min_amount=min_amount,
        max_amount=max_amount,
        description=description,
        currency=currency,
        category_id=category_id,
        uncategorized=uncategorized,
    )
    categories = await _list_categories(session)

    app_name: str = request.app.state.settings.APP_NAME
    context: dict[str, Any] = {
        "app_name": app_name,
        "transactions": transactions,
        "categories": categories,
        "total": len(transactions),
        "filters": {
            "statement_id": statement_id,
            "date_from": date_from,
            "date_to": date_to,
            "min_amount": min_amount,
            "max_amount": max_amount,
            "description": description,
            "currency": currency,
            "category_id": category_id or [],
            "uncategorized": uncategorized,
        },
    }
    return templates.TemplateResponse(
        request=request,
        name="transactions.html",
        context=context,
    )


@web_router.get(
    "/transactions/rows",
    response_class=HTMLResponse,
    summary="HTMX partial: just the table body rows",
    responses={
        200: {
            "description": "Rendered ``<tr>`` rows for the current filter set. "
            "Intended as the ``hx-target`` of the filter form.",
            "content": {"text/html": {}},
        },
    },
)
async def transactions_rows_partial(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    statement_id: Annotated[
        uuid.UUID | None,
        Query(description="Optional filter to a single statement."),
    ] = None,
    date_from: Annotated[
        date_typ | None,
        Query(description="Inclusive lower bound on the posting date."),
    ] = None,
    date_to: Annotated[
        date_typ | None,
        Query(description="Inclusive upper bound on the posting date."),
    ] = None,
    min_amount: Annotated[
        Decimal | None,
        Query(description="Inclusive lower bound on the absolute amount."),
    ] = None,
    max_amount: Annotated[
        Decimal | None,
        Query(description="Inclusive upper bound on the absolute amount."),
    ] = None,
    description: Annotated[
        str | None,
        Query(description="Case-insensitive substring match on the description."),
    ] = None,
    currency: Annotated[
        str | None,
        Query(description="ISO-4217 code ('CLP' or 'USD')."),
    ] = None,
    category_id: Annotated[
        list[uuid.UUID] | None,
        Query(
            description=(
                "Repeatable filter — limit the partial to transactions whose "
                "category_id matches any of the supplied UUIDs."
            ),
        ),
    ] = None,
    uncategorized: Annotated[
        bool,
        Query(
            description=(
                "When true, also include transactions whose category_id is "
                "NULL or whose low_confidence flag is true."
            ),
        ),
    ] = False,
) -> HTMLResponse:
    """Render just the ``<tr>`` rows of the transactions table.

    This is the HTMX-friendly endpoint. The filter form posts
    (via ``hx-get``) to this URL and the response is swapped
    into ``#transaction-list-body`` by HTMX — no full page
    reload, no client-side templating, no JSON-to-DOM.

    The query delegates to :func:`_query_transactions` so a
    refresh of the full page and a HTMX filter request are
    guaranteed to return the same rows. The category list is
    loaded via :func:`_list_categories` so the per-row
    ``<select>`` markup stays meaningful when the partial is
    re-rendered (e.g. on first paint or after a PATCH swap).
    """
    transactions = await _query_transactions(
        session,
        statement_id=statement_id,
        date_from=date_from,
        date_to=date_to,
        min_amount=min_amount,
        max_amount=max_amount,
        description=description,
        currency=currency,
        category_id=category_id,
        uncategorized=uncategorized,
    )
    categories = await _list_categories(session)

    context: dict[str, Any] = {
        "transactions": transactions,
        "categories": categories,
        "error": None,
    }
    return templates.TemplateResponse(
        request=request,
        name="partials/transactions_table.html",
        context=context,
    )


# ---------------------------------------------------------------------------
# Dashboard (Phase 3, PR #10)
# ---------------------------------------------------------------------------


#: Period picker options exposed to the UI. ``0`` = current month
#: only (the dashboard always pivots on the current month for
#: the single-period KPIs), ``3`` / ``6`` / ``12`` = rolling
#: windows, ``-1`` = all-time (the "no upper bound" sentinel the
#: service handles via ``range_months=0``).
_DASHBOARD_RANGE_OPTIONS: tuple[int, ...] = (0, 3, 6, 12, -1)

#: Default period picker value. ``0`` = current month, which is
#: the spec scenario "Period picker defaults to the current
#: month".
_DASHBOARD_DEFAULT_RANGE: int = 0

#: Sentinel passed to the service as ``range_months=0`` when the
#: user picks "Todo el historial" (the picker sends ``-1``). The
#: service treats ``0`` as all-time; the negative value is a UI
#: convenience so the picker can keep the "default = current
#: month = 0" semantic.
_DASHBOARD_ALL_TIME: int = -1


def _parse_range_option(value: int) -> int:
    """Normalise the period picker value to a service-friendly int.

    The picker ships ``-1`` for "all-time" (the spec scenario
    "Period picker defaults to the current month" lets us keep
    the default as the natural number ``0``); the service
    expects ``0`` to mean all-time. The other values
    (``3`` / ``6`` / ``12``) pass through unchanged.
    """
    if value == _DASHBOARD_ALL_TIME:
        return 0
    if value not in _DASHBOARD_RANGE_OPTIONS:
        # Defensive: the picker restricts the options, so an
        # out-of-set value is a programming error. Fall back
        # to the current-month default rather than 400 — the
        # API surface validates the same set on
        # ``/api/v1/dashboard/*`` already.
        return _DASHBOARD_DEFAULT_RANGE
    return value


def _current_period() -> str:
    """Return the current month as an ISO ``YYYY-MM`` string.

    The dashboard pivots on the current month for the KPI
    sections; the rolling window and the all-time sections
    use the service's own window logic. Centralised so the
    initial render and the partial refresh always agree on
    the "now" anchor.
    """
    today = date_typ.today()
    return f"{today.year:04d}-{today.month:02d}"


def _parse_dashboard_period(period: str) -> date_typ:
    try:
        return YearMonth.parse(period).first_day()
    except ValueError:
        return date_typ.today().replace(day=1)


def _card_label(card_id: str, cards: list[CreditCard]) -> str:
    """Return the human-readable label for the active card filter.

    ``"all"`` returns "Todas las cards"; a UUID string returns
    ``"<bank> - <masked>"`` for the matching card. When the
    card id is a UUID but does not match any active card (the
    card was deactivated after the URL was built), the raw id
    is returned as a fallback so the page is still meaningful.
    """
    if card_id == "all":
        return "Todas las cards"
    try:
        card_uuid = uuid.UUID(card_id)
    except ValueError:
        return card_id
    for card in cards:
        if card.id == card_uuid:
            return f"{card.bank.display_name} - {card.card_number_masked}"
    return card_id


def _parse_card_filter(card_id: str) -> uuid.UUID | Literal["all"]:
    """Parse a ``card_id`` query param to a UUID or the ``"all"`` sentinel.

    The web layer is forgiving: an invalid value falls back
    to ``"all"`` instead of raising an HTTPException so a
    tampered URL still renders a meaningful page. The API
    endpoints return 400 for the same input — the JSON
    surface is strict, the HTML surface is friendly. Mirrors
    :func:`app.api.v1.dashboard._parse_card_id` for the
    accept-set; differs only in the failure mode.
    """
    if card_id == "all":
        return "all"
    if not card_id:
        return "all"
    try:
        return uuid.UUID(card_id)
    except ValueError:
        return "all"


async def _earliest_transaction_date(
    session: AsyncSession,
    *,
    card_id: uuid.UUID | Literal["all"],
) -> date_typ | None:
    stmt = select(func.min(Transaction.date))
    if card_id != "all":
        stmt = stmt.join(Transaction.statement).where(Statement.credit_card_id == card_id)
    return await session.scalar(stmt)


async def _dashboard_context(
    request: Request,
    session: AsyncSession,
    *,
    selection: DashboardSelection,
    full_page: bool,
) -> dict[str, Any]:
    cards = await _list_active_cards(session)
    card_label_text = _card_label(str(selection.card_id), cards)
    labels = selection.labels(card_name=card_label_text)
    earliest = await _earliest_transaction_date(session, card_id=selection.card_id)
    window_start, window_end = resolve_window(
        selection,
        today=date_typ.today(),
        earliest=earliest,
    )
    service = DashboardService(session)
    period_date = selection.period.first_day()
    summary = await service.summary(
        period=period_date,
        range_months=selection.range_mode.api_range(),
        card_id=selection.card_id,
        range_mode=selection.range_mode,
    )
    categories = await service.categories(period=period_date, card_id=selection.card_id)
    merchants = await service.merchants(period=period_date, card_id=selection.card_id)
    monthly = await service.monthly_window(
        window_start=window_start, window_end=window_end, card_id=selection.card_id
    )
    recurring_rows = await service.recurring(period=period_date, card_id=selection.card_id)
    merchant_names = await _lookup_merchant_names(
        session, [uuid.UUID(str(row["merchant_id"])) for row in recurring_rows]
    )
    recur_count = len(recurring_rows)
    recur_monthly = sum(
        int(row.get("amount_min", 0) or 0) for row in recurring_rows if row.get("currency") == "CLP"
    )
    context: dict[str, Any] = {
        "request": request,
        "cards": cards,
        "summary": summary,
        "categories": categories,
        "merchants": merchants,
        "monthly": monthly,
        "recurring": recurring_rows,
        "merchants_by_id": merchant_names,
        "period_label": labels.period_label,
        "period_iso": selection.period.iso(),
        "card_label": labels.card_label,
        "range_label": labels.range_label,
        "range_mode_options": range_mode_options(),
        "selected_period": selection.period.iso(),
        "selected_range_mode": selection.range_mode.wire_value(),
        "selected_card_id": str(selection.card_id),
        "recur_count": recur_count,
        "recur_monthly": recur_monthly,
        "window_start": window_start,
        "window_end": window_end,
    }
    if full_page:
        context["app_name"] = request.app.state.settings.APP_NAME
    return context


async def _list_active_cards(session: AsyncSession) -> list[CreditCard]:
    """Return every active :class:`CreditCard` row, ordered for the picker.

    The picker lists cards in ``(bank.display_name,
    card_number_masked)`` order so the user sees a stable
    alphabetical-by-bank sequence. ``bank`` is eagerly loaded
    via the model's ``lazy="joined"`` relationship so the
    template does not pay for an N+1.
    """
    result = await session.execute(
        select(CreditCard)
        .where(CreditCard.is_active.is_(True))
        .order_by(CreditCard.bank_id.asc(), CreditCard.card_number_masked.asc())
    )
    return list(result.scalars().all())


async def _lookup_merchant_names(
    session: AsyncSession, merchant_ids: list[uuid.UUID]
) -> dict[uuid.UUID, str]:
    """Resolve ``merchant_id`` UUIDs to display names for the recurring partial.

    The recurring section is the only section that needs the
    merchant name in the template. Loading every merchant name
    in a single round-trip keeps the partial render to one
    extra query (instead of one per row).
    """
    if not merchant_ids:
        return {}
    result = await session.execute(
        select(Merchant.id, Merchant.name).where(Merchant.id.in_(merchant_ids))
    )
    return dict[uuid.UUID, str](result.all())  # type: ignore[arg-type]


@web_router.get(
    "/dashboard",
    response_class=HTMLResponse,
    summary="Phase 3 dashboard page (HTMX partials + Tailwind bars)",
    responses={
        200: {
            "description": (
                "Server-rendered dashboard page with the card picker, the period "
                "picker, and the five HTMX-loaded sections (KPIs, categories, "
                "merchants, monthly bars, recurring)."
            ),
            "content": {"text/html": {}},
        },
    },
)
async def dashboard_page(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    period: Annotated[
        str,
        Query(
            description="ISO 'YYYY-MM' period label.",
        ),
    ] = "",
    range_mode: Annotated[
        str,
        Query(description="Dashboard web range mode."),
    ] = "current",
    card_id: Annotated[
        str,
        Query(
            description=("Card filter — ``'all'`` for every card, or a UUID for a single card."),
        ),
    ] = "all",
) -> HTMLResponse:
    """Render the full ``/dashboard`` page on the first paint.

    The route handler calls :class:`DashboardService` directly
    (not the JSON API endpoints) for the initial render, so
    the first paint is one DB roundtrip per section (the
    service is the single source of truth for the
    per-currency contract and the 12-row categories
    guarantee). Subsequent picker changes hit the dedicated
    ``/dashboard/sections/*`` partial endpoints, which call
    the same service with the new filter set.

    The card filter is validated the same way the API
    endpoints validate it — non-UUID non-``"all"`` values
    fall back to ``"all"`` so a tampered URL is still safe
    (the API surface returns 400 for the same input).
    """
    period_value = period or _current_period()
    try:
        selection = parse_selection(period=period_value, card_id=card_id, range_mode=range_mode)
    except (ValueError, TypeError):
        selection = DashboardSelection(
            period=YearMonth.parse(_current_period()),
            card_id="all",
            range_mode=RangeMode.current(),
        )
    context = await _dashboard_context(request, session, selection=selection, full_page=True)
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context=context,
    )


@web_router.get(
    "/dashboard/sections",
    response_class=HTMLResponse,
    summary="HTMX partial: all dashboard sections for one selection",
    include_in_schema=False,
)
async def dashboard_sections(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    period: Annotated[str, Query(description="ISO 'YYYY-MM' period label.")],
    card_id: Annotated[str, Query(description="UUID or 'all'.")] = "all",
    range_mode: Annotated[str, Query(description="Dashboard web range mode.")] = "current",
) -> HTMLResponse:
    """Render all five dashboard sections from one selection state."""
    selection = parse_selection(period=period, card_id=card_id, range_mode=range_mode)
    context = await _dashboard_context(request, session, selection=selection, full_page=False)
    return templates.TemplateResponse(
        request=request,
        name="partials/dashboard_sections.html",
        context=context,
    )


@web_router.get(
    "/dashboard/sections/summary",
    response_class=HTMLResponse,
    summary="HTMX partial: KPI grid for the dashboard",
    include_in_schema=False,
)
async def dashboard_section_summary(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    period: Annotated[str, Query(description="ISO 'YYYY-MM' month label.")],
    range_months: Annotated[
        int, Query(alias="range", description="Lookback window in months.")
    ] = 6,
    card_id: Annotated[str, Query(description="UUID or 'all'.")] = "all",
) -> HTMLResponse:
    """HTMX partial: render the KPI grid for the requested period.

    The partial reuses the same template the page's initial
    paint inlines, so the picker change and the first paint
    are guaranteed to look identical.
    """
    del range_months  # the summary itself does not use the range
    try:
        year_str, month_str = period.split("-")
        period_date = date_typ(int(year_str), int(month_str), 1)
    except (ValueError, AttributeError):
        period_date = date_typ.today().replace(day=1)
        period = f"{period_date.year:04d}-{period_date.month:02d}"
    cards = await _list_active_cards(session)
    parsed_card_id = _parse_card_filter(card_id)
    service = DashboardService(session)
    summary = await service.summary(period=period_date, card_id=parsed_card_id)

    # ``Suscripciones`` KPI card needs the live recurring rules, not a
    # hard-coded count. We re-use the same service call the rest of
    # the dashboard makes; the rules are already filtered by the
    # service for in-band occurrences in the period.
    recurring_rows = await service.recurring(period=period_date, card_id=parsed_card_id)
    recur_count = len(recurring_rows)
    # Sum the per-rule minimum amount (CLP only — USD is rare in the
    # recurring dataset and would skew the total without FX conversion).
    recur_monthly = 0
    for row in recurring_rows:
        amount = row.get("amount_min", 0) or 0
        currency = row.get("currency", "CLP")
        if currency == "CLP":
            recur_monthly += int(amount)

    context: dict[str, Any] = {
        "summary": summary,
        "period_label": period,
        "card_label": _card_label(card_id, cards),
        "range_label": "",
        "recur_count": recur_count,
        "recur_monthly": recur_monthly,
    }
    return templates.TemplateResponse(
        request=request,
        name="partials/dashboard_summary.html",
        context=context,
    )


@web_router.get(
    "/dashboard/sections/categories",
    response_class=HTMLResponse,
    summary="HTMX partial: 12 closed-set category rows",
    include_in_schema=False,
)
async def dashboard_section_categories(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    period: Annotated[str, Query(description="ISO 'YYYY-MM' month label.")],
    range_months: Annotated[
        int, Query(alias="range", description="Lookback window in months.")
    ] = 6,
    card_id: Annotated[str, Query(description="UUID or 'all'.")] = "all",
) -> HTMLResponse:
    """HTMX partial: render the 12 closed-set category rows."""
    del range_months
    try:
        year_str, month_str = period.split("-")
        period_date = date_typ(int(year_str), int(month_str), 1)
    except (ValueError, AttributeError):
        period_date = date_typ.today().replace(day=1)
        period = f"{period_date.year:04d}-{period_date.month:02d}"
    cards = await _list_active_cards(session)
    parsed_card_id = _parse_card_filter(card_id)
    service = DashboardService(session)
    categories = await service.categories(period=period_date, card_id=parsed_card_id)
    context: dict[str, Any] = {
        "categories": categories,
        "period_label": period,
        "card_label": _card_label(card_id, cards),
    }
    return templates.TemplateResponse(
        request=request,
        name="partials/dashboard_categories.html",
        context=context,
    )


@web_router.get(
    "/dashboard/sections/merchants",
    response_class=HTMLResponse,
    summary="HTMX partial: top-N merchants for the period",
    include_in_schema=False,
)
async def dashboard_section_merchants(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    period: Annotated[str, Query(description="ISO 'YYYY-MM' month label.")],
    range_months: Annotated[
        int, Query(alias="range", description="Lookback window in months.")
    ] = 6,
    card_id: Annotated[str, Query(description="UUID or 'all'.")] = "all",
) -> HTMLResponse:
    """HTMX partial: render the top-N merchants for the period."""
    del range_months
    try:
        year_str, month_str = period.split("-")
        period_date = date_typ(int(year_str), int(month_str), 1)
    except (ValueError, AttributeError):
        period_date = date_typ.today().replace(day=1)
        period = f"{period_date.year:04d}-{period_date.month:02d}"
    cards = await _list_active_cards(session)
    parsed_card_id = _parse_card_filter(card_id)
    service = DashboardService(session)
    merchants = await service.merchants(period=period_date, card_id=parsed_card_id)
    context: dict[str, Any] = {
        "merchants": merchants,
        "period_label": period,
        "card_label": _card_label(card_id, cards),
    }
    return templates.TemplateResponse(
        request=request,
        name="partials/dashboard_merchants.html",
        context=context,
    )


@web_router.get(
    "/dashboard/sections/monthly",
    response_class=HTMLResponse,
    summary="HTMX partial: monthly Tailwind bar chart",
    include_in_schema=False,
)
async def dashboard_section_monthly(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    range_months: Annotated[
        int, Query(alias="range", description="Lookback window in months.")
    ] = 6,
    card_id: Annotated[str, Query(description="UUID or 'all'.")] = "all",
) -> HTMLResponse:
    """HTMX partial: render the monthly Tailwind bar chart."""
    normalised = _parse_range_option(range_months)
    parsed_card_id = _parse_card_filter(card_id)
    cards = await _list_active_cards(session)
    service = DashboardService(session)
    monthly = await service.monthly(range_months=normalised, card_id=parsed_card_id)
    range_label_map = {0: "Mes actual", 3: "3 meses", 6: "6 meses", 12: "12 meses"}
    if normalised == 0 and range_months != 0:
        range_label = "Todo el historial"
    elif normalised == 0:
        range_label = range_label_map[0]
    else:
        range_label = range_label_map.get(normalised, f"{normalised} meses")
    context: dict[str, Any] = {
        "monthly": monthly,
        "card_label": _card_label(card_id, cards),
        "range_label": range_label,
    }
    return templates.TemplateResponse(
        request=request,
        name="partials/dashboard_monthly.html",
        context=context,
    )


@web_router.get(
    "/dashboard/sections/recurring",
    response_class=HTMLResponse,
    summary="HTMX partial: active recurring rules for the period",
    include_in_schema=False,
)
async def dashboard_section_recurring(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    period: Annotated[str, Query(description="ISO 'YYYY-MM' month label.")],
    card_id: Annotated[str, Query(description="UUID or 'all'.")] = "all",
) -> HTMLResponse:
    """HTMX partial: render the active recurring rules for the period."""
    try:
        year_str, month_str = period.split("-")
        period_date = date_typ(int(year_str), int(month_str), 1)
    except (ValueError, AttributeError):
        period_date = date_typ.today().replace(day=1)
        period = f"{period_date.year:04d}-{period_date.month:02d}"
    cards = await _list_active_cards(session)
    parsed_card_id = _parse_card_filter(card_id)
    service = DashboardService(session)
    recurring_rows = await service.recurring(period=period_date, card_id=parsed_card_id)
    merchant_names = await _lookup_merchant_names(
        session, [uuid.UUID(str(row["merchant_id"])) for row in recurring_rows]
    )
    context: dict[str, Any] = {
        "recurring": recurring_rows,
        "merchants_by_id": merchant_names,
        "period_label": period,
        "card_label": _card_label(card_id, cards),
    }
    return templates.TemplateResponse(
        request=request,
        name="partials/dashboard_recurring.html",
        context=context,
    )


__all__ = ["TEMPLATES_DIR", "templates", "web_router"]
