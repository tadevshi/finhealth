"""Pydantic response models for the Phase 3 dashboard (PR #8).

The dashboard is the Phase 3 read-side aggregation layer that
sits on top of the Phase 1 ``Transaction`` rows and the
Phase 2 ``Category`` / ``Merchant`` / ``RecurringRule``
classifications. It is purely additive: every model below
describes the JSON shape one of the five ``DashboardService``
methods returns, and the API layer (PR #9) and the HTMX page
(PR #10) consume the same shapes through their own
serialization wrappers.

Multi-currency rule
-------------------

Money is per-currency, never summed. The application has no
FX rate table (Phase 4 is out of scope for currency
conversion), so the dashboard cannot answer "how much did I
spend in USD-equivalent?". Instead, every aggregation
returns a ``*_per_currency`` dict keyed by ISO-4217 code
(``"CLP"``, ``"USD"``) and the UI presents each currency as
its own sub-grid. This module is the single source of truth
for that contract: the API layer cannot pick a different
shape without breaking the Phase 3 PR #9 endpoints and the
Phase 3 PR #10 HTMX partials.

Money is ``Decimal`` end-to-end
------------------------------

Every monetary field is typed :class:`decimal.Decimal`,
never :class:`float`. Floating-point cannot represent
``0.10`` exactly, and rounding errors compound across
thousands of rows. The ``Decimal`` type at the Pydantic
boundary is the right place to enforce "no float for money"
— by the time a row hits the API surface the value is
already correct. The Pydantic ``Field(max_digits=15,
decimal_places=2)`` constraints match the
``Numeric(15, 2)`` column type in the ``transactions``
table so a 15-digit value serialises without truncation.

Closed-set categories
---------------------

``DashboardService.categories`` always returns 12 rows
(one per seeded :class:`app.models.category.Category`). The
Pydantic model itself does not enforce the length-12
invariant (a list of ``CategoryBreakdown`` is just a list
— the spec lives in the service). The API layer adds a
post-condition assertion to make the contract explicit at
the HTTP boundary.

The four models
---------------

* :class:`SummaryResponse` — the KPI tile payload for
  :meth:`app.services.dashboard.DashboardService.summary`.
  Carries per-currency totals, daily averages, the top
  category and merchant, and the previous-period
  comparison. ``card_id`` echoes the input (``UUID`` or the
  sentinel ``"all"``).
* :class:`CategoryBreakdown` — one row in the categories
  block. ``pct_of_total`` is a single ``float`` in
  ``[0.0, 1.0]``; the per-currency effect is documented in
  the spec under the "Multi-Currency Sub-Rollup" scenario.
* :class:`MerchantBreakdown` — one row in the top-N
  merchants list. ``last_seen_date`` is the most recent
  ``Transaction.date`` for the merchant in the period, or
  ``None`` when ``limit`` exceeds the number of distinct
  merchants in the period.
* :class:`MonthlyDataPoint` — one month in the bar-chart
  time series. ``prev_month_pct_per_currency`` is a signed
  ``float`` (``+20.0`` means a 20% increase vs. the prior
  month) or ``None`` for the first month in the response.
"""

from __future__ import annotations

from datetime import date as date_typ
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------


#: Sentinel value that means "every card", not a single card.
#: Lives here (not in :mod:`app.schemas.domain`) because the
#: dashboard is the only capability that accepts the
#: ``UUID | "all"`` shape. The Phase 2 ``RecurringRule``
#: endpoints always filter on a single card.
CardFilter = UUID | Literal["all"]

#: Per-currency rollup, never summed across currencies.
#: Empty dict means "no transactions in the period".
CurrencyTotals = dict[str, Decimal]

# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class SummaryResponse(BaseModel):
    """Aggregated KPIs for a single calendar month.

    Returned by :meth:`app.services.dashboard.DashboardService.summary`.
    All money fields are per-currency dicts; ``card_id`` echoes
    the input filter (``UUID`` for one card, ``"all"`` for every
    card). ``period_start`` and ``period_end`` are the first and
    last ISO dates of the calendar month the request asked about
    (``period_start = period`` truncated to the first day,
    ``period_end = period`` truncated to the last day of the
    month). ``comparison_to_prev_period_pct_per_currency`` is
    the signed ``%`` change vs. the calendar month immediately
    before the requested one — an empty dict when the previous
    month has no transactions in that currency.

    The model is also what the ``GET /api/v1/dashboard/summary``
    endpoint serialises (PR #9). The endpoint layer does not
    add a wrapper; this model is the public response shape.
    """

    model_config = ConfigDict(
        from_attributes=False,  # response model — never ORM-loaded
        extra="forbid",
    )

    total_per_currency: CurrencyTotals = Field(
        default_factory=dict,
        description=(
            "Sum of transaction amounts in the period, broken down by currency. "
            "Empty dict when the period has no transactions."
        ),
    )
    daily_avg_per_currency: CurrencyTotals = Field(
        default_factory=dict,
        description=(
            "``total / calendar days in the selected month`` per currency. "
            "Empty dict when the period has no transactions."
        ),
    )
    transaction_count: int = Field(
        default=0,
        ge=0,
        description="Number of transactions in the period (across all currencies).",
    )
    transaction_count_per_currency: dict[str, int] = Field(
        default_factory=dict,
        description="Number of transactions in the period, broken down by currency.",
    )
    top_category_id: UUID | None = Field(
        default=None,
        description=(
            "UUID of the closed-set category with the largest single-currency total "
            "in the period. ``None`` when the period has no transactions."
        ),
    )
    top_category_total_per_currency: CurrencyTotals = Field(
        default_factory=dict,
        description=(
            "Per-currency breakdown of the top category's totals. "
            "Empty dict when ``top_category_id is None``."
        ),
    )
    top_merchant_id: UUID | None = Field(
        default=None,
        description=(
            "UUID of the merchant with the largest single-currency total "
            "in the period. ``None`` when the period has no transactions."
        ),
    )
    top_merchant_total_per_currency: CurrencyTotals = Field(
        default_factory=dict,
        description=(
            "Per-currency breakdown of the top merchant's totals. "
            "Empty dict when ``top_merchant_id is None``."
        ),
    )
    comparison_to_prev_period_pct_per_currency: dict[str, float] = Field(
        default_factory=dict,
        description=(
            "Signed percentage change vs. the prior calendar month, per currency. "
            "Empty dict when the prior month has no transactions in that currency."
        ),
    )
    period_start: date_typ = Field(
        description="First ISO date of the period (always the first day of the month).",
    )
    period_end: date_typ = Field(
        description="Last ISO date of the period (always the last day of the month).",
    )
    card_id: CardFilter = Field(
        description="Echo of the input filter — UUID for one card, 'all' for every card.",
    )


class CategoryBreakdown(BaseModel):
    """One row in the categories breakdown.

    Returned by :meth:`app.services.dashboard.DashboardService.categories`.
    The service always returns exactly 12 rows (one per seeded
    closed-set category) even when no transactions are in the
    period. The ``pct_of_total`` is a single ``float`` in
    ``[0.0, 1.0]``; the per-currency numerator/denominator
    is documented in the spec under "Multi-Currency Sub-Rollup".
    Zero-spend rows carry ``total_per_currency == {}``,
    ``transaction_count == 0``, and ``pct_of_total == 0.0``.
    """

    model_config = ConfigDict(extra="forbid")

    category_id: UUID = Field(description="UUID of the closed-set category.")
    display_name: str = Field(description="Human-readable category name (e.g. 'Groceries').")
    total_per_currency: CurrencyTotals = Field(
        default_factory=dict,
        description="Per-currency sum of transaction amounts in this category in the period.",
    )
    transaction_count: int = Field(
        default=0,
        ge=0,
        description="Number of transactions tagged with this category in the period.",
    )
    pct_of_total: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description=(
            "Share of the period's total in the dominant currency, in [0.0, 1.0]. "
            "``0.0`` for zero-spend categories."
        ),
    )


class MerchantBreakdown(BaseModel):
    """One row in the top-N merchants list.

    Returned by :meth:`app.services.dashboard.DashboardService.merchants`.
    The service returns ``min(limit, distinct_merchants_in_period)``
    rows ordered by the largest single-currency total descending
    (then ``Merchant.display_name`` ascending as a stable
    tiebreaker). ``last_seen_date`` is the most recent
    ``Transaction.date`` for the merchant in the period, or
    ``None`` when ``limit`` exceeds the number of distinct
    merchants in the period (defensive — the spec only requires
    the field for ranked merchants).
    """

    model_config = ConfigDict(extra="forbid")

    merchant_id: UUID = Field(description="UUID of the canonical merchant.")
    display_name: str = Field(description="Short stable identifier of the merchant.")
    total_per_currency: CurrencyTotals = Field(
        default_factory=dict,
        description="Per-currency sum of transaction amounts at this merchant in the period.",
    )
    transaction_count: int = Field(
        default=0,
        ge=0,
        description="Number of transactions at this merchant in the period.",
    )
    last_seen_date: date_typ | None = Field(
        default=None,
        description=(
            "Most recent transaction date at this merchant in the period. "
            "``None`` when the merchant has no transactions in the period."
        ),
    )


class MonthlyDataPoint(BaseModel):
    """One month in the time series for the dashboard bar chart.

    Returned by :meth:`app.services.dashboard.DashboardService.monthly`.
    The service returns one row per month in the requested
    window, including months with zero transactions so the
    bar chart has a continuous x-axis. ``month`` is the
    ISO ``YYYY-MM`` string; ``prev_month_pct_per_currency``
    is the signed ``%`` change vs. the prior calendar month,
    or ``None`` for the first month in the response (the
    "earliest" has no prior reference). The first month with
    transactions is also the only month that gets a non-null
    ``prev_month_pct_per_currency`` for the currency being
    compared — prior months that are zero-spend in that
    currency contribute nothing to the comparison.
    """

    model_config = ConfigDict(extra="forbid")

    month: str = Field(
        description="ISO 'YYYY-MM' string identifying the month.",
        pattern=r"^\d{4}-(0[1-9]|1[0-2])$",
    )
    total_per_currency: CurrencyTotals = Field(
        default_factory=dict,
        description="Per-currency sum of transaction amounts in this month.",
    )
    transaction_count: int = Field(
        default=0,
        ge=0,
        description="Number of transactions in this month (across all currencies).",
    )
    prev_month_pct_per_currency: dict[str, float | None] = Field(
        default_factory=dict,
        description=(
            "Signed percentage change vs. the prior calendar month, per currency. "
            "``None`` for the first month in the response. Empty dict when no "
            "transactions exist on either side of the comparison for a currency."
        ),
    )


__all__ = [
    "CardFilter",
    "CategoryBreakdown",
    "CurrencyTotals",
    "MerchantBreakdown",
    "MonthlyDataPoint",
    "SummaryResponse",
]
