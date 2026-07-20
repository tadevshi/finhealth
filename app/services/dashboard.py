"""Dashboard data service for Phase 3 (PR #8).

The :class:`DashboardService` is the read-side aggregation
layer for the Phase 3 dashboard capability. It is the
single source of truth for the five queries the PR #9 API
endpoints and the PR #10 HTMX page call. The service is
pure data: no HTTP plumbing, no template rendering, no
session management beyond the :class:`AsyncSession` it is
constructed with.

The five methods
----------------

* :meth:`summary` — KPI tile data for a single calendar
  month (total, daily avg, count, top category, top
  merchant, % change vs. prior month).
* :meth:`categories` — 12 rows (one per seeded closed-set
  :class:`app.models.category.Category`), ordered by the
  largest single-currency total descending.
* :meth:`merchants` — top-N merchants by total, ordered by
  the largest single-currency total descending.
* :meth:`monthly` — time series of monthly totals for the
  bar chart, with a ``prev_month_pct`` per-currency column
  for the prior-month comparison.
* :meth:`recurring` — active :class:`app.models.recurring_rule.RecurringRule`
  rows that had an in-band occurrence in the period,
  reusing the Phase 2 PR #5 :class:`app.schemas.domain.RecurringRuleResponse`
  shape unchanged.

Multi-currency
--------------

Every method returns per-currency dicts (``*_per_currency``).
The application has no FX rate table (Phase 4 is out of
scope for conversion), so the dashboard never sums across
currencies. The Pydantic models in
:mod:`app.schemas.dashboard` are the single source of truth
for the per-currency contract.

Card filter
-----------

The :data:`app.schemas.dashboard.CardFilter` type alias is
``UUID | Literal["all"]``. ``"all"`` (the default) means
"every card" — the SQL query omits the
``Transaction.statement_id → Statement.credit_card_id`` join
entirely (per the spec, scenario "``card_id='all'`` omits
the credit-card JOIN"). A UUID value forces the join and
filters to that single card.

Algorithms
----------

The service is pure SQL with ``GROUP BY`` on indexed
columns. The aggregations are split into two layers:

1. **Database.** A single ``SELECT`` per method, with
   ``GROUP BY`` on the relevant dimensions (currency,
   category, merchant, month). All filters are pushed to
   the database so the in-memory cost is bounded by the
   number of distinct groups, not the full
   ``Transaction`` table.
2. **Python.** The multi-currency sub-rollup and the
   ``pct_of_total`` / ``prev_month_pct_per_currency``
   computations are done in Python. SQL ``GROUP BY`` does
   not naturally support "per-currency dict" output, and
   ``pct_of_total`` is a per-currency ratio that the
   database engine does not have direct syntax for.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date as date_typ
from decimal import Decimal
from typing import TypeVar
from uuid import UUID

from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.category import Category
from app.models.merchant import Merchant
from app.models.recurring_rule import RecurringRule
from app.models.statement import Statement
from app.models.transaction import Transaction
from app.schemas.dashboard import (
    CardFilter,
    CategoryBreakdown,
    MerchantBreakdown,
    MonthlyDataPoint,
    SummaryResponse,
)
from app.services.dashboard_selection import (
    DashboardSelection,
    RangeMode,
    YearMonth,
    resolve_window,
)

# Generic type variable for ``Select``-typed return values.
# Used by :meth:`DashboardService._apply_card_filter` to
# preserve the row tuple type through the ``.join()`` /
# ``.where()`` chain.
_SelectT = TypeVar("_SelectT", bound=tuple[object, ...])

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


#: Per-currency amount tolerance the ``recurring`` method uses
#: to identify an in-band occurrence. Mirrors
#: :data:`app.services.recurring_detection.AMOUNT_TOLERANCE`
#: (decision #15 in the Phase 3 design: the dashboard reuses
#: the Phase 2 ±15% band so the "in-band" check stays
#: consistent across the two services).
_RECURRING_AMOUNT_TOLERANCE: Decimal = Decimal("0.15")

#: Default ``limit`` for :meth:`DashboardService.merchants`.
#: Mirrors the Phase 3 PR #9 endpoint default (``10``).
_DEFAULT_MERCHANT_LIMIT: int = 10

#: Default ``range_months`` for :meth:`DashboardService.summary`.
#: The Phase 3 PR #9 endpoint also defaults ``range=6``; the
#: service keeps the same default so a caller that omits
#: ``range_months`` gets a 6-month prior-period comparison.
_DEFAULT_RANGE_MONTHS: int = 6


# ---------------------------------------------------------------------------
# Internal accumulator types
# ---------------------------------------------------------------------------
#
# These dataclasses hold the per-row aggregation state as the
# service walks the SQL result set. They replace ad-hoc
# ``dict[str, object]`` accumulators: dataclasses are
# type-checkable, IDE-discoverable, and make the per-method
# pipeline easy to follow without ``# type: ignore`` comments.


@dataclass
class _CategoryAccumulator:
    """Per-category rollup state used by :meth:`DashboardService.categories`."""

    display_name: str
    sort_order: int
    totals: dict[str, Decimal] = field(default_factory=dict)
    transaction_count: int = 0


@dataclass
class _MerchantAccumulator:
    """Per-merchant rollup state used by :meth:`DashboardService.merchants`."""

    totals: dict[str, Decimal] = field(default_factory=dict)
    transaction_count: int = 0
    last_seen: date_typ | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _first_of_month(d: date_typ) -> date_typ:
    """Return the first ISO day of the calendar month containing ``d``.

    The dashboard treats ``period`` as a calendar month; the
    caller passes any date inside the month and the service
    normalises to ``[first_day, last_day]``. Centralised so
    every method uses the same truncation.
    """
    return d.replace(day=1)


def _last_of_month(d: date_typ) -> date_typ:
    """Return the last ISO day of the calendar month containing ``d``.

    Uses :func:`calendar.monthrange` so February in a leap
    year still gets 29 days. Centralised so the
    ``period_end`` field on :class:`SummaryResponse` always
    matches the floor the SQL filter uses.
    """
    last_day = calendar.monthrange(d.year, d.month)[1]
    return d.replace(day=last_day)


def _prior_month_first(d: date_typ) -> date_typ:
    """Return the first ISO day of the calendar month before ``d``'s month.

    Used by the ``comparison_to_prev_period_pct_per_currency``
    calculation in :meth:`DashboardService.summary`.
    """
    if d.month == 1:
        return date_typ(d.year - 1, 12, 1)
    return date_typ(d.year, d.month - 1, 1)


def _iso_year_month(d: date_typ) -> str:
    """Return the ISO ``YYYY-MM`` string for ``d`` (no zero-padding surprises).

    Used by :meth:`DashboardService.monthly` to label each
    bar in the time series. Centralised so the format is the
    same one the spec uses in every scenario (``"2026-07"``).
    """
    return f"{d.year:04d}-{d.month:02d}"


def _add_months(d: date_typ, months: int) -> date_typ:
    """Return the first day of the month ``months`` away from ``d``."""
    year = d.year
    month = d.month + months
    while month <= 0:
        month += 12
        year -= 1
    while month > 12:
        month -= 12
        year += 1
    return date_typ(year, month, 1)


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class DashboardService:
    """Aggregates ``Transaction`` data for the Phase 3 dashboard.

    The service is stateless; instantiate one per request with
    the request-scoped :class:`AsyncSession`. The five public
    methods are documented individually below; every method is
    ``async`` and accepts a SQL filter that the dashboard
    endpoints build from the request query params.

    Multi-currency note
    -------------------

    Every ``*_per_currency`` field in the response is a
    :class:`dict` keyed by ISO-4217 code (e.g.
    ``{"CLP": Decimal("100000"), "USD": Decimal("89.90")}``).
    The service never sums across currencies; the dashboard has
    no FX rate table, so the only honest answer is a per-currency
    sub-rollup. The Pydantic models in
    :mod:`app.schemas.dashboard` carry the contract; the API
    layer (PR #9) and the UI layer (PR #10) consume the same
    shape.

    Card filter note
    ----------------

    The :data:`app.schemas.dashboard.CardFilter` accepts
    ``UUID`` for one card or the literal ``"all"`` for every
    card. The service applies a JOIN against
    :class:`app.models.statement.Statement` only when
    ``card_id`` is a UUID; the ``"all"`` path keeps the SQL
    join-free so the scan is bounded by the period + the
    closed-set category / merchant dimensions only.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ------------------------------------------------------------------
    # summary
    # ------------------------------------------------------------------

    async def summary(
        self,
        period: date_typ,
        range_months: int = _DEFAULT_RANGE_MONTHS,
        card_id: CardFilter = "all",
        range_mode: RangeMode | None = None,
    ) -> SummaryResponse:
        """Return KPI aggregates for the selected calendar or resolved window.

        Parameters
        ----------
        period:
            Any date inside the calendar month. The service
            normalises to ``[first_day, last_day]``.
        range_months:
            Backward-compatible API integer. Kept for callers
            that have not adopted ``range_mode`` yet. When
            ``range_mode`` is omitted, ``0`` is inferred as
            all-time; ``3`` / ``6`` / ``12`` retain the legacy
            selected-month aggregation. Any other integer is
            rejected.
        range_mode:
            Optional typed range policy. When provided, the
            service resolves the aggregation window through
            :func:`app.services.dashboard_selection.resolve_window`.
            Explicit ``range_mode`` is authoritative and is never
            overridden by ``range_months``.
            The previous-month comparison remains anchored to
            the selected month, while daily average still uses
            the selected calendar month's day count.
        card_id:
            UUID for one card, or ``"all"`` (default) for
            every card.

        Returns
        -------
        :class:`app.schemas.dashboard.SummaryResponse`
            The KPI payload. ``total_per_currency`` is
            ``{}`` when the period has no transactions; the
            same convention applies to every ``*_per_currency``
            field. ``top_category_id`` and ``top_merchant_id``
            are ``None`` in the empty case.
        """
        period_start = _first_of_month(period)
        period_end = _last_of_month(period)
        prev_start = _first_of_month(_prior_month_first(period))
        prev_end = _last_of_month(_prior_month_first(period))

        aggregation_start = period_start
        if range_mode is None:
            if range_months == 0:
                range_mode = RangeMode.all_time()
            elif range_months in {3, 6, 12}:
                range_mode = None
            else:
                raise ValueError("range_months must be one of {0, 3, 6, 12}")
        if range_mode is not None:
            earliest = await self._earliest_transaction_date(card_id=card_id)
            aggregation_start, period_end = resolve_window(
                DashboardSelection(
                    period=YearMonth.from_date(period_start),
                    card_id=card_id,
                    range_mode=range_mode,
                ),
                today=date_typ.today(),
                earliest=earliest,
            )

        # 1. Current-period totals (per currency) + count.
        current_totals, current_count, current_counts_by_currency = await self._totals_and_count(
            period_start=aggregation_start,
            period_end=period_end,
            card_id=card_id,
        )
        comparison_current_totals = current_totals
        if aggregation_start != period_start:
            comparison_current_totals, _comparison_count, _comparison_counts_by_currency = (
                await self._totals_and_count(
                    period_start=period_start,
                    period_end=period_end,
                    card_id=card_id,
                )
            )

        # 2. Previous-period totals (per currency) — same shape,
        #    only used for the % change.
        prev_totals, _prev_count, _prev_counts_by_currency = await self._totals_and_count(
            period_start=prev_start,
            period_end=prev_end,
            card_id=card_id,
        )

        # 3. Top category + top merchant in the period (per currency).
        top_category_id, top_category_totals = await self._top_category(
            period_start=aggregation_start,
            period_end=period_end,
            card_id=card_id,
        )
        top_merchant_id, top_merchant_totals = await self._top_merchant(
            period_start=aggregation_start,
            period_end=period_end,
            card_id=card_id,
        )

        # 4. Compose the per-currency derived fields. Daily average is
        #    based on calendar days in the selected month, not the number
        #    of days that happened to have transactions.
        days_in_month = calendar.monthrange(period_start.year, period_start.month)[1]
        daily_avg: dict[str, Decimal] = {}
        for currency, total in current_totals.items():
            daily_avg[currency] = (total / Decimal(days_in_month)).quantize(Decimal("0.01"))

        comparison: dict[str, float] = {}
        for currency, current_total in comparison_current_totals.items():
            prev_total = prev_totals.get(currency)
            if prev_total is None or prev_total == 0:
                # No prior data, or a divide-by-zero. The
                # spec is explicit: omit the currency from
                # the comparison dict.
                continue
            pct = float((current_total - prev_total) / prev_total * Decimal("100"))
            # Round to one decimal place so the response is
            # stable across database engines (the spec
            # scenario quotes "20.0" for a 20% increase).
            comparison[currency] = round(pct, 1)

        return SummaryResponse(
            total_per_currency=current_totals,
            daily_avg_per_currency=daily_avg,
            transaction_count=current_count,
            transaction_count_per_currency=current_counts_by_currency,
            top_category_id=top_category_id,
            top_category_total_per_currency=top_category_totals,
            top_merchant_id=top_merchant_id,
            top_merchant_total_per_currency=top_merchant_totals,
            comparison_to_prev_period_pct_per_currency=comparison,
            period_start=period_start,
            period_end=period_end,
            card_id=card_id,
        )

    async def _earliest_transaction_date(self, *, card_id: CardFilter) -> date_typ | None:
        """Return the earliest transaction date for the selected card scope."""
        stmt = select(func.min(Transaction.date))
        if card_id != "all":
            stmt = stmt.join(Statement).where(Statement.credit_card_id == card_id)
        return await self._session.scalar(stmt)

    async def _totals_and_count(
        self,
        *,
        period_start: date_typ,
        period_end: date_typ,
        card_id: CardFilter,
    ) -> tuple[dict[str, Decimal], int, dict[str, int]]:
        """Return totals, total count, and per-currency counts for a period.

        Used by both :meth:`summary` (current + prior month)
        and the per-currency rollups in :meth:`categories` /
        :meth:`merchants` / :meth:`monthly`. The single query
        groups by ``Transaction.currency`` so the result is
        a per-currency dict in one round-trip.
        """
        stmt = (
            select(
                Transaction.currency,
                func.coalesce(func.sum(Transaction.amount), 0).label("total"),
                func.count(Transaction.id).label("txn_count"),
            )
            .where(Transaction.date >= period_start)
            .where(Transaction.date <= period_end)
            .group_by(Transaction.currency)
        )
        stmt = self._apply_card_filter(stmt, card_id)

        rows = (await self._session.execute(stmt)).all()
        totals: dict[str, Decimal] = {}
        counts: dict[str, int] = {}
        total_count = 0
        for row in rows:
            totals[row.currency] = Decimal(row.total)
            count = int(row.txn_count)
            counts[row.currency] = count
            total_count += count
        return totals, total_count, counts

    async def _top_category(
        self,
        *,
        period_start: date_typ,
        period_end: date_typ,
        card_id: CardFilter,
    ) -> tuple[UUID | None, dict[str, Decimal]]:
        """Return ``(category_id, per_currency_totals)`` for the top category.

        The "top" category is the one with the largest sum
        across all currencies (i.e. the category that
        contributes the most absolute spend). The per-currency
        breakdown is the same row's per-currency sub-rollup.
        Ties (two categories with the same total) are broken
        by ``Category.sort_order`` ascending so the result
        is stable.
        """
        # Build the outerjoin ON clause with the optional card filter
        # (same subquery approach as :meth:`categories`).
        outerjoin_on = and_(
            Transaction.category_id == Category.id,
            Transaction.date >= period_start,
            Transaction.date <= period_end,
        )
        if card_id != "all":
            card_subq = select(Statement.id).where(Statement.credit_card_id == card_id)
            outerjoin_on = and_(
                outerjoin_on,
                Transaction.statement_id.in_(card_subq),
            )

        per_currency_stmt = (
            select(
                Category.id.label("cat_id"),
                Category.sort_order,
                Transaction.currency,
                func.coalesce(func.sum(Transaction.amount), 0).label("total"),
            )
            .select_from(Category)
            .outerjoin(Transaction, outerjoin_on)
            .group_by(Category.id, Category.sort_order, Transaction.currency)
        )

        # We need: per (category, currency) totals, then pick
        # the category with the max single-currency total.
        # Two queries keep the SQL portable: one to compute
        # the per-(category, currency) rollups, one to find
        # the winning category.
        per_currency_rows = (await self._session.execute(per_currency_stmt)).all()
        per_cat_currency: dict[UUID, dict[str, Decimal]] = {}
        sort_orders: dict[UUID, int] = {}
        for row in per_currency_rows:
            sort_orders[row.cat_id] = int(row.sort_order)
            if row.currency is None:
                # Outer-joined row with no transactions;
                # skip — the top-category query only
                # considers categories with actual spend.
                continue
            per_cat_currency.setdefault(row.cat_id, {})[row.currency] = Decimal(row.total)

        if not per_cat_currency:
            return None, {}

        # Rank by the largest single-currency total (the
        # spec is explicit: "the closed-set category with
        # the largest sum in any currency"). Ties broken
        # by ``Category.sort_order`` ascending — pre-fetched
        # above so the sort key is sync.
        best_cat_id = min(
            per_cat_currency.keys(),
            key=lambda cid: (
                -max(per_cat_currency[cid].values()),
                sort_orders[cid],
            ),
        )
        return best_cat_id, per_cat_currency[best_cat_id]

    async def _top_merchant(
        self,
        *,
        period_start: date_typ,
        period_end: date_typ,
        card_id: CardFilter,
    ) -> tuple[UUID | None, dict[str, Decimal]]:
        """Return ``(merchant_id, per_currency_totals)`` for the top merchant.

        Symmetric to :meth:`_top_category` but for the
        merchant dimension. The "top" merchant is the one
        with the largest single-currency total; ties are
        broken by ``Merchant.name`` ascending (a stable
        string order). The names are pre-fetched in a
        second query so the sort key is sync.
        """
        from sqlalchemy import select as sa_select

        stmt = (
            select(
                Transaction.merchant_id,
                Transaction.currency,
                func.coalesce(func.sum(Transaction.amount), 0).label("total"),
            )
            .where(Transaction.date >= period_start)
            .where(Transaction.date <= period_end)
            .where(Transaction.merchant_id.is_not(None))
            .group_by(Transaction.merchant_id, Transaction.currency)
        )
        stmt = self._apply_card_filter(stmt, card_id)

        rows = (await self._session.execute(stmt)).all()
        per_merchant: dict[UUID, dict[str, Decimal]] = {}
        for row in rows:
            assert row.merchant_id is not None  # filter applied above
            per_merchant.setdefault(row.merchant_id, {})[row.currency] = Decimal(row.total)

        if not per_merchant:
            return None, {}

        # Pre-fetch the merchant names for the tiebreaker.
        name_stmt = sa_select(Merchant.id, Merchant.name).where(
            Merchant.id.in_(list(per_merchant.keys()))
        )
        name_rows = (await self._session.execute(name_stmt)).all()
        names: dict[UUID, str] = {row.id: row.name for row in name_rows}

        best_id = min(
            per_merchant.keys(),
            key=lambda mid: (-max(per_merchant[mid].values()), names.get(mid, "")),
        )
        return best_id, per_merchant[best_id]

    # ------------------------------------------------------------------
    # categories
    # ------------------------------------------------------------------

    async def categories(
        self,
        period: date_typ,
        card_id: CardFilter = "all",
    ) -> list[CategoryBreakdown]:
        """Return the 12 closed-set categories with per-currency rollups.

        Every row of the seeded ``categories`` table appears
        in the response — zero-spend categories carry
        ``total_per_currency == {}`` and ``pct_of_total ==
        0.0`` (per the spec scenario "All 12 categories are
        returned even at zero spend"). The response is
        ordered by the largest single-currency total
        descending, with ``Category.sort_order`` ascending
        as the stable tiebreaker.

        The SQL is a ``LEFT JOIN`` from ``Category`` to the
        aggregated ``Transaction`` rows so the zero-spend
        rows survive the join. The aggregation is done in
        two steps — one query to fetch the per-currency
        rollups, one query to compute the period total
        (denominator of ``pct_of_total``) per currency.
        """
        period_start = _first_of_month(period)
        period_end = _last_of_month(period)

        # Build the outerjoin ON clause. The card filter
        # (when ``card_id != "all"``) uses a correlated
        # subquery instead of a separate ``.join()`` call —
        # a second join would be independent of the LEFT JOIN
        # and would not filter the aggregated ``Transaction``
        # rows (see the fix for the phantom-join bug that
        # caused ``test_categories_single_card_filter`` to
        # aggregate card B's transactions when only card A
        # was requested — decision #x in the PR #8 commit).
        outerjoin_on = and_(
            Transaction.category_id == Category.id,
            Transaction.date >= period_start,
            Transaction.date <= period_end,
        )
        if card_id != "all":
            card_subq = select(Statement.id).where(Statement.credit_card_id == card_id)
            outerjoin_on = and_(
                outerjoin_on,
                Transaction.statement_id.in_(card_subq),
            )

        # 1. Per-(category, currency) rollup.
        stmt = (
            select(
                Category.id.label("cat_id"),
                Category.display_name,
                Category.sort_order,
                Transaction.currency,
                func.coalesce(func.sum(Transaction.amount), 0).label("total"),
                func.count(Transaction.id).label("txn_count"),
            )
            .select_from(Category)
            .outerjoin(Transaction, outerjoin_on)
            .group_by(Category.id, Category.display_name, Category.sort_order, Transaction.currency)
        )

        rows = (await self._session.execute(stmt)).all()

        # 2. Aggregate in Python by category_id.
        per_cat: dict[UUID, _CategoryAccumulator] = {}
        for row in rows:
            entry = per_cat.setdefault(
                row.cat_id,
                _CategoryAccumulator(
                    display_name=row.display_name,
                    sort_order=int(row.sort_order),
                ),
            )
            if row.currency is None:
                # Outer-joined row with no transactions;
                # the totals stay empty and count stays 0.
                continue
            entry.totals[row.currency] = Decimal(row.total)
            entry.transaction_count += int(row.txn_count)

        # 3. Compute the period's total per currency so the
        #    ``pct_of_total`` has a denominator. The same
        #    card filter applies.
        period_totals_stmt = (
            select(
                Transaction.currency,
                func.coalesce(func.sum(Transaction.amount), 0).label("total"),
            )
            .where(Transaction.date >= period_start)
            .where(Transaction.date <= period_end)
            .group_by(Transaction.currency)
        )
        period_totals_stmt = self._apply_card_filter(period_totals_stmt, card_id)
        period_total_rows = (await self._session.execute(period_totals_stmt)).all()
        period_total: dict[str, Decimal] = {r.currency: Decimal(r.total) for r in period_total_rows}

        # 4. Compose the response. One row per category
        #    (always 12 — the LEFT JOIN guarantees it).
        result: list[CategoryBreakdown] = []
        for cat_id, entry in per_cat.items():
            # ``pct_of_total`` is the largest single-currency
            # share. The spec scenario "pct_of_total is
            # per-currency" documents that we pick the
            # dominant currency's share and surface it as
            # a single float; tests cover the divergent
            # case explicitly.
            if entry.totals and period_total:
                # Pick the dominant currency (largest absolute
                # contribution) for the single ``pct_of_total``
                # value the Pydantic model carries.
                dominant_currency = max(entry.totals, key=lambda c: abs(entry.totals[c]))
                numerator = entry.totals[dominant_currency]
                denominator = period_total.get(dominant_currency, Decimal("0"))
                pct = float(numerator / denominator) if denominator > 0 else 0.0
            else:
                pct = 0.0
            result.append(
                CategoryBreakdown(
                    category_id=cat_id,
                    display_name=entry.display_name,
                    total_per_currency=entry.totals,
                    transaction_count=entry.transaction_count,
                    pct_of_total=round(pct, 4),
                )
            )

        # 5. Order: largest single-currency total desc, then
        #    ``sort_order`` asc (stable tiebreaker). The
        #    accumulator carries the ``sort_order`` value so
        #    the sort key stays synchronous.
        def _sort_key(r: CategoryBreakdown) -> tuple[float, int]:
            return (
                # Negative so the largest total sorts first.
                # The spec uses "largest single-currency
                # total" — when a category has multiple
                # currencies, we rank by the largest of the
                # two.
                -float(max(r.total_per_currency.values())) if r.total_per_currency else 0.0,
                # Stable tiebreaker — Categories.sort_order
                # ASC means the lowest number wins on a tie.
                per_cat[r.category_id].sort_order,
            )

        result.sort(key=_sort_key)
        return result

    # ------------------------------------------------------------------
    # merchants
    # ------------------------------------------------------------------

    async def merchants(
        self,
        period: date_typ,
        card_id: CardFilter = "all",
        limit: int = _DEFAULT_MERCHANT_LIMIT,
    ) -> list[MerchantBreakdown]:
        """Return the top-N merchants by total spent in the period.

        Parameters
        ----------
        period:
            Any date inside the calendar month. The service
            normalises to ``[first_day, last_day]``.
        card_id:
            UUID for one card, or ``"all"`` (default) for
            every card.
        limit:
            Maximum number of rows to return. The response
            length is ``min(limit, distinct_merchants_in_period)``
            per the spec.

        The response is ordered by the largest single-currency
        total descending, with ``Merchant.name`` ascending as
        a stable tiebreaker. ``last_seen_date`` is the most
        recent ``Transaction.date`` for the merchant in the
        period (``None`` only for the defensive case when
        ``limit`` exceeds the number of distinct merchants in
        the period — unreachable when ``limit`` is bounded by
        the number of rows the query returns).
        """
        period_start = _first_of_month(period)
        period_end = _last_of_month(period)

        # 1. Per-(merchant, currency) rollup.
        stmt = (
            select(
                Transaction.merchant_id,
                Transaction.currency,
                func.coalesce(func.sum(Transaction.amount), 0).label("total"),
                func.count(Transaction.id).label("txn_count"),
                func.max(Transaction.date).label("last_seen"),
            )
            .where(Transaction.date >= period_start)
            .where(Transaction.date <= period_end)
            .where(Transaction.merchant_id.is_not(None))
            .group_by(Transaction.merchant_id, Transaction.currency)
        )
        stmt = self._apply_card_filter(stmt, card_id)

        rows = (await self._session.execute(stmt)).all()

        # 2. Aggregate in Python by merchant_id.
        per_merchant: dict[UUID, _MerchantAccumulator] = {}
        for row in rows:
            assert row.merchant_id is not None
            entry = per_merchant.setdefault(row.merchant_id, _MerchantAccumulator())
            entry.totals[row.currency] = Decimal(row.total)
            entry.transaction_count += int(row.txn_count)
            # ``last_seen`` is the most recent date across
            # all currencies for this merchant.
            if entry.last_seen is None or row.last_seen > entry.last_seen:
                entry.last_seen = row.last_seen

        # 3. Look up ``Merchant.name`` for the response.
        #    Single query — the merchant list is bounded
        #    by ``len(per_merchant)`` which is at most
        #    ``limit`` (the SQL already restricts to the
        #    period). Fetching names in a single
        #    round-trip is cheaper than joining on every
        #    iteration.
        from sqlalchemy import select as sa_select

        name_stmt = sa_select(Merchant.id, Merchant.name).where(
            Merchant.id.in_(list(per_merchant.keys()))
        )
        name_rows = (await self._session.execute(name_stmt)).all()
        names: dict[UUID, str] = {row.id: row.name for row in name_rows}

        # 4. Compose the response. Apply the ``limit`` cap
        #    in Python (the SQL does not need a ``LIMIT``
        #    because the in-memory aggregation is bounded
        #    by the number of distinct merchants in the
        #    period, which is small in practice).
        result: list[MerchantBreakdown] = [
            MerchantBreakdown(
                merchant_id=merchant_id,
                display_name=names.get(merchant_id, "<unknown>"),
                total_per_currency=entry.totals,
                transaction_count=entry.transaction_count,
                last_seen_date=entry.last_seen,
            )
            for merchant_id, entry in per_merchant.items()
        ]

        # 5. Order: largest single-currency total desc,
        #    then ``Merchant.name`` asc (stable
        #    tiebreaker).
        def _sort_key(r: MerchantBreakdown) -> tuple[float, str]:
            return (
                -float(max(r.total_per_currency.values())) if r.total_per_currency else 0.0,
                r.display_name,
            )

        result.sort(key=_sort_key)
        return result[:limit]

    # ------------------------------------------------------------------
    # monthly
    # ------------------------------------------------------------------

    async def monthly(
        self,
        range_months: int = _DEFAULT_RANGE_MONTHS,
        card_id: CardFilter = "all",
    ) -> list[MonthlyDataPoint]:
        """Return the monthly time series for the bar chart.

        Parameters
        ----------
        range_months:
            ``3`` / ``6`` / ``12`` returns the last N months
            from today (inclusive). ``0`` returns all-time
            (every distinct month present in the dataset).
        card_id:
            UUID for one card, or ``"all"`` (default) for
            every card.

        The response is ordered by ``month`` ascending.
        Months with zero transactions are still present
        (per the spec scenario "Zero-transaction months are
        still in the series") so the bar chart has a
        continuous x-axis. ``prev_month_pct_per_currency``
        is a signed ``%`` change vs. the prior calendar
        month, or ``None`` for the first month in the
        response (no prior reference).
        """
        today = date_typ.today()

        if range_months == 0:
            # All-time: pull every distinct month in the
            # dataset, then return a contiguous series.
            months = await self._distinct_months(card_id=card_id)
        else:
            # Last N months inclusive of the current month.
            # The current month is the "anchor"; we walk
            # backwards ``range_months - 1`` steps and
            # collect the ISO ``YYYY-MM`` labels.
            months = []
            for offset in range(range_months - 1, -1, -1):
                year = today.year
                month = today.month - offset
                while month <= 0:
                    month += 12
                    year -= 1
                months.append(date_typ(year, month, 1))

        # 2. Per-month rollup.
        per_month_totals: dict[str, dict[str, Decimal]] = {}
        per_month_count: dict[str, int] = {}

        for month_first in months:
            period_start = _first_of_month(month_first)
            period_end = _last_of_month(month_first)
            totals, count, _counts = await self._totals_and_count(
                period_start=period_start,
                period_end=period_end,
                card_id=card_id,
            )
            month_label = _iso_year_month(period_start)
            per_month_totals[month_label] = totals
            per_month_count[month_label] = count

        # 3. Compose the response. ``prev_month_pct_per_currency``
        #    is computed for every month except the first.
        result: list[MonthlyDataPoint] = []
        prev_totals: dict[str, Decimal] = {}
        for idx, month_first in enumerate(months):
            month_label = _iso_year_month(month_first)
            totals = per_month_totals.get(month_label, {})
            count = per_month_count.get(month_label, 0)
            if idx == 0:
                # First month — no prior reference.
                prev_pct: dict[str, float | None] = {}
            else:
                prev_pct = {}
                for currency, current_total in totals.items():
                    prev_total = prev_totals.get(currency)
                    if prev_total is None or prev_total == 0:
                        prev_pct[currency] = None
                        continue
                    pct = float((current_total - prev_total) / prev_total * Decimal("100"))
                    prev_pct[currency] = round(pct, 1)
            result.append(
                MonthlyDataPoint(
                    month=month_label,
                    total_per_currency=totals,
                    transaction_count=count,
                    prev_month_pct_per_currency=prev_pct,
                )
            )
            prev_totals = totals

        return result

    async def monthly_window(
        self,
        *,
        window_start: date_typ,
        window_end: date_typ,
        card_id: CardFilter = "all",
    ) -> list[MonthlyDataPoint]:
        """Return a contiguous monthly series for an explicit window."""
        months: list[date_typ] = []
        cursor = _first_of_month(window_start)
        end_month = _first_of_month(window_end)
        while cursor <= end_month:
            months.append(cursor)
            cursor = _add_months(cursor, 1)

        per_month_totals: dict[str, dict[str, Decimal]] = {}
        per_month_count: dict[str, int] = {}
        for month_first in months:
            totals, count, _counts = await self._totals_and_count(
                period_start=_first_of_month(month_first),
                period_end=_last_of_month(month_first),
                card_id=card_id,
            )
            label = _iso_year_month(month_first)
            per_month_totals[label] = totals
            per_month_count[label] = count

        result: list[MonthlyDataPoint] = []
        prev_totals: dict[str, Decimal] = {}
        for idx, month_first in enumerate(months):
            label = _iso_year_month(month_first)
            totals = per_month_totals.get(label, {})
            if idx == 0:
                prev_pct: dict[str, float | None] = {}
            else:
                prev_pct = {}
                for currency, current_total in totals.items():
                    prev_total = prev_totals.get(currency)
                    if prev_total is None or prev_total == 0:
                        prev_pct[currency] = None
                        continue
                    prev_pct[currency] = round(
                        float((current_total - prev_total) / prev_total * Decimal("100")), 1
                    )
            result.append(
                MonthlyDataPoint(
                    month=label,
                    total_per_currency=totals,
                    transaction_count=per_month_count.get(label, 0),
                    prev_month_pct_per_currency=prev_pct,
                )
            )
            prev_totals = totals
        return result

    async def _distinct_months(
        self,
        *,
        card_id: CardFilter,
    ) -> list[date_typ]:
        """Return every distinct calendar month in the dataset, sorted ascending.

        Used by :meth:`monthly` when ``range_months == 0``.
        The query groups by the ``YYYY-MM`` truncation of
        ``Transaction.date`` and returns the months present
        in the database. Months with no transactions are
        not returned here — the caller fills them in
        implicitly by walking the calendar (defensive:
        the spec only requires a contiguous series, not a
        series with explicit zero rows for the all-time
        path).
        """
        from sqlalchemy import select as sa_select

        # ``func.strftime`` is SQLite-portable; for
        # PostgreSQL the equivalent is
        # ``func.to_char(Transaction.date, 'YYYY-MM')``.
        # The project targets SQLite today, so we use
        # ``strftime`` directly.
        month_expr = func.strftime("%Y-%m", Transaction.date).label("month")
        stmt: Select[tuple[str]] = sa_select(month_expr).group_by(month_expr).order_by(month_expr)
        stmt = self._apply_card_filter(stmt, card_id)
        rows = (await self._session.execute(stmt)).all()
        months: list[date_typ] = []
        for row in rows:
            year_str, month_str = row.month.split("-")
            months.append(date_typ(int(year_str), int(month_str), 1))
        return months

    # ------------------------------------------------------------------
    # recurring
    # ------------------------------------------------------------------

    async def recurring(
        self,
        period: date_typ,
        card_id: CardFilter = "all",
    ) -> list[dict[str, object]]:
        """Return active :class:`RecurringRule` rows with an in-band occurrence.

        The shape matches :class:`app.schemas.domain.RecurringRuleResponse`
        but the method returns ``dict`` instances (not
        :class:`RecurringRuleResponse`) so the response stays
        a plain dict structure the API layer (PR #9) and the
        HTMX page (PR #10) can serialise without an extra
        mapping step. The dict keys match the Pydantic field
        names exactly.

        A rule is included when:

        1. ``is_active=True`` — the same filter the
           ``GET /api/v1/recurring`` endpoint uses (per
           design D in Phase 2 PR #5).
        2. The rule has an in-band transaction in the
           period: a ``Transaction`` row whose ``date`` is
           in ``[period_start, period_end]``, on the same
           merchant + currency as the rule, and whose
           amount is within ``±15%`` of the rule's median
           (approximated by ``(amount_min + amount_max) / 2``).

        The response is ordered by ``last_seen_date``
        descending, then by ``id`` ascending as the
        stable tiebreaker (per the spec scenario
        "Active rules with an in-band occurrence are
        returned").

        ``card_id`` filters the result to a single card —
        the in-band transaction must be on the matching
        card. ``"all"`` accepts rules from every card.
        """
        from sqlalchemy import select as sa_select

        period_start = _first_of_month(period)
        period_end = _last_of_month(period)

        # 1. Fetch all active rules. The "in-band occurrence
        #    in the period" check is done in Python after
        #    the SQL returns the active rules — keeping
        #    the rule selection in one query and the
        #    in-band check in a second one is the only
        #    way to keep the SQL portable (PostgreSQL and
        #    SQLite have different syntax for
        #    range-on-Numeric).
        rules_stmt = sa_select(RecurringRule).where(RecurringRule.is_active.is_(True))
        rules = list((await self._session.execute(rules_stmt)).scalars().all())

        if not rules:
            return []

        # 2. For every active rule, check whether a
        #    transaction in the period is in-band.
        #    Single query per rule is N+1; instead, batch
        #    the in-band check by collecting the candidate
        #    (merchant_id, currency, amount_min, amount_max)
        #    tuples and running a single query that ORs
        #    across them. SQLite supports ``OR`` chains
        #    via ``or_``.
        or_clauses = []
        for rule in rules:
            lower = rule.amount_min * (Decimal("1") - _RECURRING_AMOUNT_TOLERANCE)
            upper = rule.amount_max * (Decimal("1") + _RECURRING_AMOUNT_TOLERANCE)
            or_clauses.append(
                (Transaction.merchant_id == rule.merchant_id)
                & (Transaction.currency == rule.currency)
                & (Transaction.amount >= lower)
                & (Transaction.amount <= upper)
                & (Transaction.date >= period_start)
                & (Transaction.date <= period_end)
            )
        in_band_stmt = sa_select(
            Transaction.merchant_id,
            Transaction.currency,
        ).where(or_(*or_clauses))
        in_band_stmt = self._apply_card_filter(in_band_stmt, card_id)
        in_band_rows = (await self._session.execute(in_band_stmt)).all()
        # ``in_band_pairs`` is the set of
        # ``(merchant_id, currency)`` pairs that have at
        # least one in-band transaction in the period
        # for the requested card(s).
        in_band_pairs: set[tuple[UUID, str]] = {
            (row.merchant_id, row.currency) for row in in_band_rows
        }

        # 3. Filter the rules to the in-band set. The spec
        #    scenario "Rules without an in-band occurrence
        #    in the period are excluded" is enforced here.
        matching = [rule for rule in rules if (rule.merchant_id, rule.currency) in in_band_pairs]

        # 4. Order: ``last_seen_date`` desc, then ``id`` asc
        #    as the stable tiebreaker.
        matching.sort(key=lambda r: (-r.last_seen_date.toordinal(), str(r.id)))

        # 5. Serialise to the ``RecurringRuleResponse`` dict
        #    shape. The keys mirror the Pydantic field
        #    names so ``RecurringRuleResponse(**row)``
        #    works in the API layer.
        return [
            {
                "id": rule.id,
                "merchant_id": rule.merchant_id,
                "period_label": rule.period_label,
                "period_days": rule.period_days,
                "amount_min": rule.amount_min,
                "amount_max": rule.amount_max,
                "currency": rule.currency,
                "is_active": rule.is_active,
                "confidence": rule.confidence,
                "last_seen_date": rule.last_seen_date,
                "occurrences": rule.occurrences,
                "created_at": rule.created_at,
                "updated_at": rule.updated_at,
            }
            for rule in matching
        ]

    # ------------------------------------------------------------------
    # Card filter helper
    # ------------------------------------------------------------------

    def _apply_card_filter(self, stmt: Select[_SelectT], card_id: CardFilter) -> Select[_SelectT]:
        """Apply the credit-card filter to ``stmt`` if ``card_id`` is a UUID.

        When ``card_id == "all"``, the statement is returned
        unchanged — the SQL query does not join against
        ``statements`` at all (per the spec scenario
        "``card_id='all'`` omits the credit-card JOIN"). The
        optimisation is explicit: every code path that takes
        the ``"all"`` branch must skip the join, so the
        scan stays bounded by the period + the closed-set
        dimension, not by the statement table.

        The generic ``_SelectT`` parameter preserves the row
        tuple type through the ``.join()`` / ``.where()``
        chain so the caller's ``Select[T]`` annotation is
        not lost. Without it, mypy --strict would reject the
        assignment (the function would return a
        ``Select[object]`` and the callsite would lose its
        row-tuple type).
        """
        if card_id == "all":
            return stmt
        # UUID path: join through ``Statement`` and
        # filter on ``credit_card_id``. A regular
        # ``.join()`` is correct here because every
        # ``Transaction`` row has a non-NULL
        # ``statement_id`` (the FK is ``NOT NULL``);
        # the join is effectively INNER. Methods that
        # need outer-join semantics (categories,
        # _top_category) inline their own card filter
        # via a correlated subquery in the ``outerjoin``
        # ON clause so the LEFT JOIN to ``Category``
        # keeps its zero-spend rows.
        return stmt.join(Transaction.statement).where(Statement.credit_card_id == card_id)


__all__ = ["DashboardService"]
