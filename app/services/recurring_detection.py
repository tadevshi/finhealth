"""Deterministic recurring-transaction detector.

The :class:`RecurringDetector` is the Phase 2 PR #5 service
that runs at the end of every successful
:meth:`app.services.ingestion.IngestionService.ingest_statement`
call. It groups the last 90 days of transactions on the
same ``credit_card_id`` by ``(merchant_id, currency)``,
requires ≥3 in-band occurrences within ±15% of the
median amount, classifies the cadence from the median
interval between consecutive postings, and upserts a
:class:`app.models.recurring_rule.RecurringRule` row
keyed on the
``(merchant_id, amount_min, amount_max, currency, period_days)``
composite. The detector is fully deterministic + statistical
— no LLM call, no async I/O outside the database — so the
test surface is a real in-memory SQLite database and a
hand-rolled transaction fixture.

Algorithm
---------

1. **One-query scan.** Fetch every
   :class:`app.models.transaction.Transaction` row on the
   same ``credit_card_id`` as the just-ingested statement
   with ``date >= statement.period_end - timedelta(days=90)``,
   ``installment_number IS NULL``, and
   ``merchant_id IS NOT NULL``. The three filters are
   pushed to the database so the per-statement cost is
   O(transactions in the 90-day window) regardless of
   how big the historical table grows.

2. **Group by ``(merchant_id, currency)``.** Per spec
   scenario, two different currencies on the same merchant
   produce two separate rules. The grouping is done in
   Python because the keys are simple tuples — a single
   ``defaultdict(list)`` keeps the code readable.

3. **Per-group filtering.** For each group, compute the
   median amount, the ±15% band, drop the outliers
   (amounts outside the band), and re-check the
   ≥3-occurrence threshold on the in-band subset. The
   threshold is *after* the outlier filter: 3 occurrences
   with 1 outlier means only 2 in-band rows, which does
   not qualify.

4. **Period classification.** Compute the median interval
   between consecutive in-band dates, then map it to a
   label by the design thresholds (weekly ≤10d, biweekly
   ≤18d, monthly ≤45d, quarterly ≤120d, yearly ≤400d;
   anything above yearly is also "yearly" — the
   v1 detector does not produce "unknown"). The
   ``period_days`` column is the median interval rounded
   to the nearest integer.

5. **Upsert by composite key.** Look up an existing rule
   by ``(merchant_id, amount_min, amount_max, currency, period_days)``.
   On hit, UPDATE ``last_seen_date`` / ``occurrences`` /
   ``confidence`` on the existing row. On miss, INSERT a
   new row with ``is_active=True`` and
   ``occurrences=in_band_count``. The upsert key
   intentionally ignores ``is_active`` (per design D) so
   a deactivated rule is still updated on the next
   detector run — the user only flips visibility, the
   detector keeps the data fresh.

6. **Confidence.** Compute
   ``round(min(1.0, occurrences / 5) * max(0.0, 1.0 - (amount_max - amount_min) / median_amount), 4)``
   in Python (design D5, decision #10, design D1). Python
   arithmetic keeps the rounding deterministic and easy
   to test — a SQL ``ROUND`` would tie the score to the
   database engine's float representation.

7. **FK backfill.** Single ``UPDATE`` over the
   just-ingested statement's transactions:
   ``WHERE merchant_id == rule.merchant_id AND amount >= rule.amount_min AND amount <= rule.amount_max AND currency == rule.currency``.
   The amount bounds are the in-band range from step 3
   (design D3), so the FK only points to in-band rows.

Logging
-------

The detector logs the rule count at the end of
:meth:`detect` with the level differentiated by the
``partial_success`` flag (decisions #7, #14):
``logger.info`` on a full-success ingest, ``logger.warning``
on a partial-success ingest (one or more chunks failed
but the statement still completed). The level split is
the only user-visible signal of the partial-success
condition at the detector layer; the rest of the
ingestion pipeline already logs its own warning per
chunk.
"""

from __future__ import annotations

import logging
import statistics
import uuid
from collections import defaultdict
from datetime import date as date_typ
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import and_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.recurring_rule import RecurringRule
from app.models.statement import Statement
from app.models.transaction import Transaction

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


#: The 90-day window the detector scans, per the spec. The
#: window is per-statement (``statement.period_end - 90d``)
#: and applies to the in-memory filter on the rows the
#: single scan query returns. A 91st-day row at the same
#: merchant does not contribute to the rule.
DETECTION_WINDOW_DAYS: int = 90

#: The minimum in-band occurrences to qualify as a
#: recurring pattern, per the spec.
MIN_OCCURRENCES: int = 3

#: The amount-tolerance band, expressed as a fraction
#: of the median. ±0.15 means an in-band amount must be
#: in ``[median * 0.85, median * 1.15]``. The spec calls
#: this "±15%".
AMOUNT_TOLERANCE: float = 0.15

#: The number of occurrences at which the occurrence
#: factor saturates at 1.0. The confidence formula is
#: ``min(1.0, occurrences / 5)``, so 5+ occurrences gives
#: the full 1.0 multiplier on the amount-consistency
#: factor. A pattern with 10 occurrences and a tight
#: amount range still gets ``confidence ≈ 1.0`` — the
#: formula does not keep growing above 1.0.
SATURATION_OCCURRENCES: int = 5

#: Period-classification thresholds, in days. The
#: detector takes the *median* of the inter-arrival
#: intervals, rounds it to the nearest integer, and
#: applies the thresholds in order. The first match
#: wins. Per the design (decision B), anything > 400d
#: is also classified as ``"yearly"`` (the
#: detector does not produce an "unknown" bucket in v1).
PERIOD_THRESHOLDS_DAYS: tuple[tuple[int, str], ...] = (
    (10, "weekly"),
    (18, "biweekly"),
    (45, "monthly"),
    (120, "quarterly"),
    (400, "yearly"),
)


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class RecurringDetector:
    """Detect and upsert recurring-transaction rules for a single statement.

    Parameters
    ----------
    session:
        The :class:`AsyncSession` to use for the scan and
        the upsert. The session is *not* auto-committed —
        :meth:`detect` calls ``commit()`` once at the end
        so a single detector run lands all the upserts
        atomically.
    partial_success:
        ``True`` when the just-ingested statement reached
        ``status=COMPLETED`` with one or more chunk
        failures. Drives the log level at the end of
        :meth:`detect` (``info`` on full-success,
        ``warning`` on partial-success, per decision #7).
        The flag does NOT affect the algorithm — every
        statement at ``status=COMPLETED`` runs the same
        detector path regardless of how the LLM did on
        each chunk.
    """

    def __init__(self, session: AsyncSession, *, partial_success: bool = False) -> None:
        self._session = session
        self._partial_success = partial_success

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def detect(self, statement: Statement) -> list[RecurringRule]:
        """Run the full detection pass for ``statement`` and return the rules.

        The full pass:

        1. Single-query scan (90-day window, no
           installments, ``merchant_id IS NOT NULL``).
        2. Group by ``(merchant_id, currency)``.
        3. Per-group filter (≥3, ±15% band).
        4. Classify period, compute confidence.
        5. Upsert by composite key.
        6. FK backfill on the just-ingested statement.
        7. Commit + log.

        Returns
        -------
        list[RecurringRule]
            The list of rules the pass created or
            updated, in detection order. Empty when no
            pattern qualified. The list is the *post-upsert*
            state of every rule the pass touched — the
            Pydantic response model can serialise it
            without a second round-trip.
        """
        cutoff = statement.period_end - timedelta(days=DETECTION_WINDOW_DAYS)

        # 1. One-query scan. The three filters are pushed
        #    to the database so the in-memory cost is
        #    bounded by the 90-day window, not the full
        #    historical table.
        rows = await self._scan_window(
            credit_card_id=statement.credit_card_id,
            cutoff=cutoff,
        )

        if not rows:
            self._log_completion(statement.id, rule_count=0)
            return []

        # 2. Group by ``(merchant_id, currency)`` in Python.
        #    A simple dict-of-lists keeps the algorithm
        #    readable; the per-group work is cheap.
        groups: dict[tuple[uuid.UUID, str], list[Transaction]] = defaultdict(list)
        for row in rows:
            assert row.merchant_id is not None  # filter applied in the query
            key = (row.merchant_id, row.currency)
            groups[key].append(row)

        upserted: list[RecurringRule] = []

        for (merchant_id, currency), group_rows in groups.items():
            rule = await self._process_group(
                statement=statement,
                merchant_id=merchant_id,
                currency=currency,
                rows=group_rows,
            )
            if rule is not None:
                upserted.append(rule)

        # 7. Commit once at the end so all the upserts
        #    land atomically. The HTTP layer can read the
        #    ``RecurringRule`` rows via ``GET /api/v1/recurring``
        #    without an extra round-trip.
        await self._session.commit()

        self._log_completion(statement.id, rule_count=len(upserted))
        return upserted

    # ------------------------------------------------------------------
    # Algorithm steps (split for testability)
    # ------------------------------------------------------------------

    async def _scan_window(
        self,
        *,
        credit_card_id: uuid.UUID,
        cutoff: date_typ,
    ) -> list[Transaction]:
        """Return the 90-day window of non-installment transactions with a merchant.

        The result is a list of in-memory :class:`Transaction`
        objects. The ``selectin``-loaded relationships
        (``merchant_ref``, ``recurring_rule_ref``) are
        present so the per-row work in :meth:`_process_group`
        and the FK backfill can stay on the application
        side.

        The ``credit_card_id`` filter is on the parent
        :class:`Statement` (the :class:`Transaction` row
        carries ``statement_id``, not ``credit_card_id``
        directly) — the join is pushed to the database so
        the in-memory cost is bounded by the 90-day
        window, not the full historical table.
        """
        stmt = (
            select(Transaction)
            .join(Transaction.statement)
            .where(
                and_(
                    Statement.credit_card_id == credit_card_id,
                    Transaction.date >= cutoff,
                    Transaction.installment_number.is_(None),
                    Transaction.merchant_id.is_not(None),
                )
            )
            .order_by(Transaction.date.asc())
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def _process_group(
        self,
        *,
        statement: Statement,
        merchant_id: uuid.UUID,
        currency: str,
        rows: list[Transaction],
    ) -> RecurringRule | None:
        """Detect, classify, and upsert the rule for one ``(merchant, currency)`` group.

        Returns the post-upsert :class:`RecurringRule` row
        (inserted or updated), or ``None`` if the group
        did not qualify. The function is split out from
        :meth:`detect` so the per-group logic is unit
        testable against a hand-crafted transaction
        fixture.
        """
        # 3a. Median amount + in-band filter. The band is
        #     ``median ± AMOUNT_TOLERANCE``; amounts outside
        #     the band are outliers and are dropped.
        amounts_all = [row.amount for row in rows]
        median_amount = statistics.median(amounts_all)
        amount_min_inclusive = median_amount * (Decimal("1") - Decimal(str(AMOUNT_TOLERANCE)))
        amount_max_inclusive = median_amount * (Decimal("1") + Decimal(str(AMOUNT_TOLERANCE)))

        in_band = [
            row for row in rows if amount_min_inclusive <= row.amount <= amount_max_inclusive
        ]

        # 3b. ≥3 in-band occurrences, after the outlier
        #     filter. A group with 3 rows where 1 is an
        #     outlier has only 2 in-band rows, which does
        #     not qualify.
        if len(in_band) < MIN_OCCURRENCES:
            return None

        # 4. Period classification. The median interval
        #    between consecutive in-band dates is the
        #    cadence estimate. ``period_days`` is the
        #    integer-rounded median; ``period_label`` is
        #    the bucket.
        sorted_in_band = sorted(in_band, key=lambda r: r.date)
        intervals = [
            (sorted_in_band[i + 1].date - sorted_in_band[i].date).days
            for i in range(len(sorted_in_band) - 1)
        ]
        # A group of 3+ transactions on the same date has
        # all-zero intervals — that yields ``median_interval=0``,
        # which ``_classify_period`` would bucket as ``"weekly"``
        # with ``period_days=0``. That is a meaningless rule
        # (a zero-day cadence is not a subscription), so we
        # skip the rule entirely. The same skip applies when
        # ``intervals`` is empty (defensive — at this point
        # ``in_band`` has at least 3 rows, so ``intervals``
        # always has at least 2 entries in practice, but the
        # guard keeps the function honest).
        if not intervals or all(i == 0 for i in intervals):
            return None
        median_interval = round(statistics.median(intervals))
        period_label, period_days = self._classify_period(median_interval)

        # Use the *in-band* amount bounds (not the
        # ±15% band the filter used) for the rule row —
        # the rule is the user's record of the pattern,
        # and a row at $9.99 in a $9.50-$10.50 band should
        # show amount_min=9.99 / amount_max=9.99, not
        # the wider band.
        in_band_amounts = [row.amount for row in in_band]
        amount_min = min(in_band_amounts)
        amount_max = max(in_band_amounts)

        # 5. Upsert by composite key. The key matches on
        #    the four columns that uniquely identify a
        #    pattern; ``is_active`` is ignored (per design
        #    D) so a deactivated rule is still updated on
        #    the next run.
        existing = await self._find_rule(
            merchant_id=merchant_id,
            amount_min=amount_min,
            amount_max=amount_max,
            currency=currency,
            period_days=period_days,
        )

        new_in_band_count = len(in_band)
        last_seen_date = max(row.date for row in in_band)
        confidence = self._compute_confidence(
            occurrences=new_in_band_count,
            amount_min=amount_min,
            amount_max=amount_max,
            median_amount=median_amount,
        )

        if existing is None:
            rule = RecurringRule(
                merchant_id=merchant_id,
                period_days=period_days,
                period_label=period_label,
                amount_min=amount_min,
                amount_max=amount_max,
                currency=currency,
                is_active=True,
                confidence=confidence,
                last_seen_date=last_seen_date,
                occurrences=new_in_band_count,
            )
            self._session.add(rule)
            await self._session.flush()  # populate rule.id for the FK backfill
        else:
            existing.period_label = period_label
            existing.last_seen_date = last_seen_date
            existing.occurrences = new_in_band_count
            existing.confidence = confidence
            await self._session.flush()
            rule = existing

        # 6. FK backfill. Set ``recurring_rule_id`` on the
        #    just-ingested statement's transactions that
        #    match the in-band amount range on the same
        #    merchant + currency. The amount bounds are
        #    the rule's in-band range, NOT the ±15% filter
        #    band — a row at $9.99 in a $9.50-$10.50 band
        #    is in-band, a row at $10.60 is not.
        await self._backfill_fk(
            statement_id=statement.id,
            rule_id=rule.id,
            merchant_id=merchant_id,
            amount_min=amount_min,
            amount_max=amount_max,
            currency=currency,
        )

        return rule

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _find_rule(
        self,
        *,
        merchant_id: uuid.UUID,
        amount_min: Decimal,
        amount_max: Decimal,
        currency: str,
        period_days: int,
    ) -> RecurringRule | None:
        """Look up a rule by the composite upsert key.

        The composite index
        ``ix_recurring_rules_merchant_currency_period``
        on ``(merchant_id, currency, period_days)``
        covers three of the four filter columns; the
        ``amount_min`` / ``amount_max`` are checked in
        application code after the index hit.
        """
        stmt = select(RecurringRule).where(
            and_(
                RecurringRule.merchant_id == merchant_id,
                RecurringRule.currency == currency,
                RecurringRule.period_days == period_days,
                RecurringRule.amount_min == amount_min,
                RecurringRule.amount_max == amount_max,
            )
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    @staticmethod
    def _classify_period(median_interval_days: int) -> tuple[str, int]:
        """Map a median interval to a ``(period_label, period_days)`` tuple.

        Walks the thresholds in :data:`PERIOD_THRESHOLDS_DAYS`
        in order; the first match wins. The ``period_days``
        is the integer-rounded median interval (the
        caller passes it in) — the function just decides
        the label. An interval > 400 days is classified
        as ``"yearly"`` (the detector does not produce
        an "unknown" bucket in v1).
        """
        for threshold, label in PERIOD_THRESHOLDS_DAYS:
            if median_interval_days <= threshold:
                return label, median_interval_days
        # > 400d → "yearly" (the last bucket)
        return "yearly", median_interval_days

    @staticmethod
    def _compute_confidence(
        *,
        occurrences: int,
        amount_min: Decimal,
        amount_max: Decimal,
        median_amount: Decimal,
    ) -> float:
        """Return the detector's 0.0-1.0 confidence score (decision #10, design D1/D5).

        ``min(1.0, occurrences / 5) * max(0.0, 1.0 - (amount_max - amount_min) / median_amount)``

        rounded to four decimal places. The arithmetic is
        Python-side (design D5) so the score is the same
        regardless of the database engine's float
        representation. A degenerate
        ``median_amount == 0`` (theoretically impossible
        after the in-band filter — every amount is
        strictly positive) returns ``0.0`` defensively so
        the function never raises on a domain edge case.
        """
        if median_amount == 0:
            return 0.0
        occurrence_factor = min(1.0, occurrences / SATURATION_OCCURRENCES)
        # Convert the Decimal spread to ``float`` so the
        # arithmetic with the ``1.0`` literal is well-typed
        # under --strict (Decimal - float raises on
        # mypy's stricter operand rules).
        spread = float((amount_max - amount_min) / median_amount)
        amount_factor = max(0.0, 1.0 - spread)
        confidence: float = round(occurrence_factor * amount_factor, 4)
        return confidence

    async def _backfill_fk(
        self,
        *,
        statement_id: uuid.UUID,
        rule_id: uuid.UUID,
        merchant_id: uuid.UUID,
        amount_min: Decimal,
        amount_max: Decimal,
        currency: str,
    ) -> None:
        """Set ``recurring_rule_id`` on the just-ingested statement's matching transactions.

        The filter is the in-band amount range on the same
        merchant + currency, restricted to the
        just-ingested statement. The amount bounds come
        from the rule's in-band range (NOT the ±15%
        filter band) — design D3.

        The ``ON DELETE SET NULL`` clause on the FK means
        a future rule deletion (e.g. via a "delete rule"
        endpoint in a future PR) will not cascade to drop
        the transaction — the historical FK is preserved
        for audit.
        """
        stmt = (
            update(Transaction)
            .where(
                and_(
                    Transaction.statement_id == statement_id,
                    Transaction.merchant_id == merchant_id,
                    Transaction.currency == currency,
                    Transaction.amount >= amount_min,
                    Transaction.amount <= amount_max,
                )
            )
            .values(recurring_rule_id=rule_id)
        )
        await self._session.execute(stmt)

    def _log_completion(self, statement_id: uuid.UUID, *, rule_count: int) -> None:
        """Log the completion line with the level split on ``partial_success``.

        Decisions #7, #14: ``info`` on full-success,
        ``warning`` on partial-success (one or more chunks
        failed but the statement still completed). The
        statement id and the rule count are both
        included so an operator can correlate the log
        line with the database rows.
        """
        if self._partial_success:
            logger.warning(
                "Recurring detection complete for statement=%s: %d rules (partial-success ingest)",
                statement_id,
                rule_count,
            )
        else:
            logger.info(
                "Recurring detection complete for statement=%s: %d rules",
                statement_id,
                rule_count,
            )


__all__ = [
    "AMOUNT_TOLERANCE",
    "DETECTION_WINDOW_DAYS",
    "MIN_OCCURRENCES",
    "PERIOD_THRESHOLDS_DAYS",
    "SATURATION_OCCURRENCES",
    "RecurringDetector",
]
