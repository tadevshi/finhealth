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
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session
from app.models.bank import Bank
from app.models.transaction import Transaction

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
    session: Annotated[AsyncSession, Depends(get_session)],
    *,
    statement_id: uuid.UUID | None,
    date_from: date_typ | None,
    date_to: date_typ | None,
    min_amount: Decimal | None,
    max_amount: Decimal | None,
    description: str | None,
    currency: str | None,
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

    Parameters mirror the route-level Query params one-to-one.
    ``statement_id`` is the only filter added in this refactor
    pass; the upcoming Category UI work will add two more
    (``category_id`` and ``uncategorized``) without touching the
    helper's signature, because they ride the same SQL builder.

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

    # Stable order: oldest transaction first, with a
    # tiebreaker on the primary key so two rows with the same
    # date do not shift between pages.
    query = query.order_by(Transaction.date.asc(), Transaction.id.asc())

    result = await session.execute(query)
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
    )

    app_name: str = request.app.state.settings.APP_NAME
    context: dict[str, Any] = {
        "app_name": app_name,
        "transactions": transactions,
        "total": len(transactions),
        "filters": {
            "statement_id": statement_id,
            "date_from": date_from,
            "date_to": date_to,
            "min_amount": min_amount,
            "max_amount": max_amount,
            "description": description,
            "currency": currency,
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
) -> HTMLResponse:
    """Render just the ``<tr>`` rows of the transactions table.

    This is the HTMX-friendly endpoint. The filter form posts
    (via ``hx-get``) to this URL and the response is swapped
    into ``#transaction-list-body`` by HTMX — no full page
    reload, no client-side templating, no JSON-to-DOM.

    The query delegates to :func:`_query_transactions` so a
    refresh of the full page and a HTMX filter request are
    guaranteed to return the same rows.
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
    )

    context: dict[str, Any] = {"transactions": transactions, "error": None}
    return templates.TemplateResponse(
        request=request,
        name="partials/transactions_table.html",
        context=context,
    )


__all__ = ["TEMPLATES_DIR", "templates", "web_router"]
