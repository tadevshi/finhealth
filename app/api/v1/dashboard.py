"""Dashboard HTTP endpoints (Phase 3, PR #9).

Exposes the five aggregation endpoints the Phase 3 dashboard
page (PR #10) and any future client need:

* :func:`dashboard_summary` — ``GET /api/v1/dashboard/summary``
  returns the single-month KPI tile payload
  (:class:`app.schemas.dashboard.SummaryResponse`).
* :func:`dashboard_categories` — ``GET /api/v1/dashboard/categories``
  returns the always-12-row category breakdown
  (``list[CategoryBreakdown]``).
* :func:`dashboard_merchants` — ``GET /api/v1/dashboard/merchants``
  returns the top-N merchants
  (``list[MerchantBreakdown]``).
* :func:`dashboard_monthly` — ``GET /api/v1/dashboard/monthly``
  returns the time series for the bar chart
  (``list[MonthlyDataPoint]``).
* :func:`dashboard_recurring` — ``GET /api/v1/dashboard/recurring``
  returns the active recurring rules with an in-band
  occurrence in the period
  (``list[RecurringRuleResponse]``).

Why a single router for five endpoints
--------------------------------------

The five endpoints share the same prefix, the same tags, the
same dependency-injection shape, and the same query-param
validation contract (``period`` / ``range`` / ``card_id``).
Splitting them into five modules would mean five
:class:`APIRouter` instances and five ``include_router`` calls
for a total of one line of net benefit; keeping them together
matches the rest of the project (banks, statements,
transactions, categories, merchants, recurring are all
single-router modules).

Thin layer
----------

The endpoint module is a thin HTTP wrapper over
:class:`app.services.dashboard.DashboardService` (PR #8). The
service is the single source of truth for the per-currency
contract, the 12-row categories guarantee, and the recurring
in-band check. The endpoint layer's only job is to:

1. Parse and validate the query params.
2. Translate validation failures into ``400 Bad Request``.
3. Construct the per-request :class:`DashboardService` with
   the request-scoped :class:`AsyncSession`.
4. Hand the parsed args to the service and serialise the
   result.

No SQL, no aggregation logic, no per-currency arithmetic
lives here — every one of those concerns is in the service
where the unit tests cover them.

Multi-currency
--------------

The application has no FX rate table (Phase 4 is out of
scope for currency conversion), so the dashboard never sums
across currencies. The service's Pydantic models use
``*_per_currency`` dicts (``{"CLP": ..., "USD": ...}``); the
endpoint layer passes them through unchanged.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import date as date_typ
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session
from app.schemas.dashboard import (
    CategoryBreakdown,
    MerchantBreakdown,
    MonthlyDataPoint,
    SummaryResponse,
)
from app.schemas.domain import RecurringRuleResponse
from app.services.dashboard import DashboardService
from app.services.dashboard_selection import RangeMode, from_api_range

logger = logging.getLogger(__name__)

router: APIRouter = APIRouter(prefix="/dashboard", tags=["dashboard"])

# ---------------------------------------------------------------------------
# Validation constants
# ---------------------------------------------------------------------------

#: ``period`` accepts the ISO ``YYYY-MM`` month label per the
#: spec (``period=2026-07``). The regex enforces the
#: zero-padded month, rejects ``2026-7`` (the spec scenario
#: "Invalid ``period`` returns 400"), and rejects any other
#: shape. The service normalises the parsed year+month to
#: the first day of the month.
_PERIOD_RE: re.Pattern[str] = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")

#: Allowed values for ``range``. The spec is explicit:
#: ``{3, 6, 12, 0}``; ``0`` means all-time. Any other int
#: returns 400 per the spec scenario "Invalid ``range`` returns 400".
_ALLOWED_RANGES: frozenset[int] = frozenset({0, 3, 6, 12})

#: Default ``range`` for every endpoint that accepts it.
#: Mirrors :data:`app.services.dashboard._DEFAULT_RANGE_MONTHS`
#: so a caller that omits ``range`` gets a 6-month lookback.
_DEFAULT_RANGE: int = 6

#: Default ``limit`` for the merchants endpoint. Mirrors
#: :data:`app.services.dashboard._DEFAULT_MERCHANT_LIMIT`
#: so the service default and the API default agree.
_DEFAULT_MERCHANT_LIMIT: int = 10

#: Upper cap on ``limit`` for the merchants endpoint. The
#: spec mandates a max of 50 (``limit > 50`` returns 400 per
#: the spec scenario "``limit`` above the cap returns 400").
_MAX_MERCHANT_LIMIT: int = 50


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_period(period: str) -> date_typ:
    """Parse the ``period`` query param to a calendar :class:`date`.

    The param accepts the ISO ``YYYY-MM`` month label per the
    spec. Any other shape (missing month, unpadded month,
    extra characters) returns 400 with an error body that
    names ``period`` as the offending field. The service
    normalises the date to the first day of the month; the
    endpoint layer hands the parsed date through unchanged.
    """
    match = _PERIOD_RE.match(period)
    if match is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(f"period must be an ISO 'YYYY-MM' month label (got {period!r})"),
        )
    year_str, month_str = match.groups()
    return date_typ(int(year_str), int(month_str), 1)


def _parse_card_id(card_id: str) -> uuid.UUID | Literal["all"]:
    """Parse the ``card_id`` query param to a UUID or the ``"all"`` sentinel.

    The spec is explicit (Requirement: Multi-Card Aggregation
    with "Todas" Default): ``card_id`` MUST be a valid UUID
    or the literal string ``"all"``; empty string, ``null``,
    ``"none"``, or any non-UUID non-``"all"`` value returns
    400. FastAPI's automatic string parsing does not enforce
    this — the endpoint layer translates each failure to a
    400 with a clear error body.
    """
    if card_id == "all":
        return "all"
    if not card_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="card_id must be a UUID or the literal string 'all' (got empty string)",
        )
    try:
        return uuid.UUID(card_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"card_id must be a UUID or the literal string 'all' (got {card_id!r})",
        ) from exc


def _parse_range(range_months: int) -> RangeMode:
    """Validate the ``range`` query param against the allowed set.

    ``range_months`` MUST be one of ``{0, 3, 6, 12}`` per the
    spec; any other int returns 400. The default is ``6``
    (six-month lookback including the current month).
    """
    if range_months not in _ALLOWED_RANGES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"range must be one of {{0, 3, 6, 12}} (got {range_months!r})",
        )
    return from_api_range(range_months)


def _parse_limit(limit: int) -> int:
    """Validate the ``limit`` query param for the merchants endpoint.

    The spec mandates ``1 <= limit <= 50``; any other value
    returns 400 per the scenario "``limit`` above the cap
    returns 400" (and its symmetric low-side counterpart).
    """
    if limit < 1 or limit > _MAX_MERCHANT_LIMIT:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(f"limit must be between 1 and {_MAX_MERCHANT_LIMIT} (got {limit!r})"),
        )
    return limit


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get(
    "/summary",
    response_model=SummaryResponse,
    summary="KPI tile payload for a single calendar month",
    responses={
        status.HTTP_200_OK: {
            "description": "KPI payload (totals, daily avg, top category / merchant, prior-month %).",
            "model": SummaryResponse,
        },
        status.HTTP_400_BAD_REQUEST: {
            "description": (
                "Invalid ``period`` (not ``YYYY-MM``), ``range`` "
                "(not in ``{0, 3, 6, 12}``), or ``card_id`` (not a UUID or ``'all'``)."
            ),
        },
    },
)
async def dashboard_summary(
    session: Annotated[AsyncSession, Depends(get_session)],
    period: Annotated[
        str,
        Query(
            description="ISO 'YYYY-MM' month label, e.g. '2026-07'.",
        ),
    ],
    range_months: Annotated[
        int,
        Query(
            alias="range",
            description="Lookback window for the prior-period comparison: 0, 3, 6, or 12.",
        ),
    ] = _DEFAULT_RANGE,
    card_id: Annotated[
        str,
        Query(
            description="UUID of a single card, or 'all' for every card.",
        ),
    ] = "all",
) -> SummaryResponse:
    """Return the KPI tile payload for the requested calendar month.

    The ``period`` query param is the ISO ``YYYY-MM`` month
    label; the service normalises to ``[first_day, last_day]``
    of that month internally. ``range_months`` selects the
    lookback window for the prior-period percentage; the
    service only uses it for logging (the comparison is
    always against the immediately prior month, regardless
    of the window size — see the spec scenario "``range=0``
    is all-time"). ``card_id`` filters to a single card or
    aggregates every card with the sentinel ``"all"``.

    All three params are validated against the spec's
    contract; a 400 is returned for any of:

    * ``period`` not matching the ``YYYY-MM`` regex.
    * ``range_months`` outside ``{0, 3, 6, 12}``.
    * ``card_id`` not a UUID and not the literal ``"all"``.

    The endpoint still forwards the typed range mode explicitly;
    direct service callers also get the same all-time behaviour
    when calling ``summary(..., range_months=0)`` without
    ``range_mode``. The Pydantic ``SummaryResponse`` model wraps
    the service's payload unchanged — no extra fields, no
    transformation.
    """
    parsed_period = _parse_period(period)
    parsed_range = _parse_range(range_months)
    parsed_card_id = _parse_card_id(card_id)
    return await DashboardService(session).summary(
        period=parsed_period,
        range_months=range_months,
        range_mode=parsed_range,
        card_id=parsed_card_id,
    )


@router.get(
    "/categories",
    response_model=list[CategoryBreakdown],
    summary="All 12 closed-set categories with per-currency rollups",
    responses={
        status.HTTP_200_OK: {
            "description": (
                "A list of 12 ``CategoryBreakdown`` rows (one per seeded closed-set "
                "category), ordered by the largest single-currency total descending."
            ),
            "model": list[CategoryBreakdown],
        },
        status.HTTP_400_BAD_REQUEST: {
            "description": (
                "Invalid ``period`` (not ``YYYY-MM``) or ``card_id`` (not a UUID or ``'all'``)."
            ),
        },
    },
)
async def dashboard_categories(
    session: Annotated[AsyncSession, Depends(get_session)],
    period: Annotated[
        str,
        Query(
            description="ISO 'YYYY-MM' month label, e.g. '2026-07'.",
        ),
    ],
    card_id: Annotated[
        str,
        Query(
            description="UUID of a single card, or 'all' for every card.",
        ),
    ] = "all",
) -> list[CategoryBreakdown]:
    """Return the 12 closed-set category rows for the requested month.

    The service always returns exactly 12 rows — the
    ``LEFT JOIN`` from ``Category`` to the aggregated
    ``Transaction`` table guarantees the zero-spend rows
    survive the join. The endpoint does not re-assert the
    length-12 invariant at the HTTP boundary; the service
    is the single source of truth for that contract.
    """
    parsed_period = _parse_period(period)
    parsed_card_id = _parse_card_id(card_id)
    return await DashboardService(session).categories(
        period=parsed_period,
        card_id=parsed_card_id,
    )


@router.get(
    "/merchants",
    response_model=list[MerchantBreakdown],
    summary="Top-N merchants by total spent in the period",
    responses={
        status.HTTP_200_OK: {
            "description": (
                "A list of ``MerchantBreakdown`` rows, ordered by the largest "
                "single-currency total descending. May be empty for an empty period."
            ),
            "model": list[MerchantBreakdown],
        },
        status.HTTP_400_BAD_REQUEST: {
            "description": (
                "Invalid ``period`` (not ``YYYY-MM``), ``card_id`` (not a UUID or ``'all'``), "
                "or ``limit`` (not in ``[1, 50]``)."
            ),
        },
    },
)
async def dashboard_merchants(
    session: Annotated[AsyncSession, Depends(get_session)],
    period: Annotated[
        str,
        Query(
            description="ISO 'YYYY-MM' month label, e.g. '2026-07'.",
        ),
    ],
    card_id: Annotated[
        str,
        Query(
            description="UUID of a single card, or 'all' for every card.",
        ),
    ] = "all",
    limit: Annotated[
        int,
        Query(
            description="Maximum number of rows to return (1..50).",
        ),
    ] = _DEFAULT_MERCHANT_LIMIT,
) -> list[MerchantBreakdown]:
    """Return the top-N merchants by total spent in the period.

    The response length is ``min(limit, distinct_merchants_in_period)``
    per the spec. ``limit`` defaults to ``10`` (matches the
    service default) and is capped at ``50``; values outside
    the ``[1, 50]`` range return 400.
    """
    parsed_period = _parse_period(period)
    parsed_card_id = _parse_card_id(card_id)
    parsed_limit = _parse_limit(limit)
    return await DashboardService(session).merchants(
        period=parsed_period,
        card_id=parsed_card_id,
        limit=parsed_limit,
    )


@router.get(
    "/monthly",
    response_model=list[MonthlyDataPoint],
    summary="Time series of monthly totals for the bar chart",
    responses={
        status.HTTP_200_OK: {
            "description": (
                "A list of ``MonthlyDataPoint`` rows, ordered by ``month`` ascending. "
                "May be empty when the dataset has no transactions."
            ),
            "model": list[MonthlyDataPoint],
        },
        status.HTTP_400_BAD_REQUEST: {
            "description": (
                "Invalid ``range`` (not in ``{0, 3, 6, 12}``) or ``card_id`` "
                "(not a UUID or ``'all'``)."
            ),
        },
    },
)
async def dashboard_monthly(
    session: Annotated[AsyncSession, Depends(get_session)],
    range_months: Annotated[
        int,
        Query(
            alias="range",
            description="Lookback window in months: 0 (all-time), 3, 6, or 12.",
        ),
    ] = _DEFAULT_RANGE,
    card_id: Annotated[
        str,
        Query(
            description="UUID of a single card, or 'all' for every card.",
        ),
    ] = "all",
) -> list[MonthlyDataPoint]:
    """Return the monthly time series for the requested lookback window.

    Unlike the other endpoints, this one does not take a
    ``period`` query param — it returns a *window* of
    months, not a single month. ``range_months=0`` returns
    every distinct month in the dataset (all-time);
    ``range_months=3|6|12`` returns the last N months
    inclusive of the current month. Months with zero
    transactions are still present in the response (with
    empty ``total_per_currency`` and ``transaction_count=0``)
    so the bar chart has a continuous x-axis.
    """
    _parse_range(range_months)
    parsed_card_id = _parse_card_id(card_id)
    return await DashboardService(session).monthly(
        range_months=range_months,
        card_id=parsed_card_id,
    )


@router.get(
    "/recurring",
    response_model=list[RecurringRuleResponse],
    summary="Active recurring rules with an in-band occurrence in the period",
    responses={
        status.HTTP_200_OK: {
            "description": (
                "A list of active ``RecurringRule`` rows that have at least one "
                "in-band transaction in the period. May be empty when no rule matches."
            ),
            "model": list[RecurringRuleResponse],
        },
        status.HTTP_400_BAD_REQUEST: {
            "description": (
                "Invalid ``period`` (not ``YYYY-MM``) or ``card_id`` (not a UUID or ``'all'``)."
            ),
        },
    },
)
async def dashboard_recurring(
    session: Annotated[AsyncSession, Depends(get_session)],
    period: Annotated[
        str,
        Query(
            description="ISO 'YYYY-MM' month label, e.g. '2026-07'.",
        ),
    ],
    card_id: Annotated[
        str,
        Query(
            description="UUID of a single card, or 'all' for every card.",
        ),
    ] = "all",
) -> list[RecurringRuleResponse]:
    """Return the active recurring rules with an in-band occurrence.

    The service returns plain ``dict`` instances whose keys
    match :class:`app.schemas.domain.RecurringRuleResponse`'s
    field names. The endpoint layer instantiates the Pydantic
    models so the API response carries the same shape as
    ``GET /api/v1/recurring`` (PR #5) — unchanged, per the
    spec requirement that the dashboard reuses the Phase 2
    response model as-is.

    ``period`` is required (the in-band check is bounded to a
    calendar month); ``card_id`` defaults to ``"all"``.
    """
    parsed_period = _parse_period(period)
    parsed_card_id = _parse_card_id(card_id)
    rows = await DashboardService(session).recurring(
        period=parsed_period,
        card_id=parsed_card_id,
    )
    # The service returns ``list[dict[str, object]]`` whose
    # keys match ``RecurringRuleResponse``'s field names.
    # Re-shape here so the API contract is a real
    # ``RecurringRuleResponse`` list (not ``dict``) — this
    # also lets Pydantic's ``sanitize_confidence`` validator
    # run on the response.
    return [RecurringRuleResponse(**row) for row in rows]


__all__ = [
    "dashboard_categories",
    "dashboard_merchants",
    "dashboard_monthly",
    "dashboard_recurring",
    "dashboard_summary",
    "router",
]
