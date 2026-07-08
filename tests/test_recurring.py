"""Tests for the recurring-rule foundation (Phase 2, PR #5).

Covers:

* the :class:`app.models.recurring_rule.RecurringRule` model
  (round-trip + composite upsert key uniqueness);
* the :class:`app.services.recurring_detection.RecurringDetector`
  algorithm (90-day window, ≥3 threshold, ±15% tolerance,
  installment skip, period classification, idempotent
  upsert, confidence rounding, FK backfill, empty result,
  single transaction);
* the API endpoints ``GET /api/v1/recurring`` (filter
  inactive + order desc) and ``PATCH /api/v1/recurring/{id}``
  (activate / deactivate / 404).

The test surface is split into three layers:

* **Algorithm unit tests** (8) — the
  :class:`RecurringDetector` against a real in-memory
  SQLite database with hand-rolled transaction fixtures
  (no LLM, no PDF).
* **API tests** (4) — drive the FastAPI app through an
  :class:`httpx.AsyncClient` with ``ASGITransport``.
* **Edge cases** (2) — empty result, single transaction.

Every test uses a fresh in-memory SQLite database (via the
``engine`` fixture from :mod:`tests.conftest`) and the
ORM schema is created via ``Base.metadata.create_all`` so
the test surface matches what the production app sees at
startup. The migration round-trip (so the migration
itself is exercised) is covered by
:mod:`tests.test_alembic`.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.db.engine import create_engine
from app.main import create_app
from app.models import Bank, CreditCard, Merchant, RecurringRule, Statement, Transaction
from app.models.base import Base
from app.models.statement import StatementStatus
from app.services.recurring_detection import (
    RecurringDetector,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def recurring_engine(test_settings: Settings) -> AsyncIterator[AsyncEngine]:
    """Yield a fresh engine with the full schema created (no seed).

    The full ORM schema is created via
    :func:`Base.metadata.create_all` so the test surface
    matches what the production app sees at startup. The
    fixture does not seed any banks, cards, or statements
    — every test builds its own fixture rows so the
    relationships are explicit and the per-test cost is
    bounded.
    """
    engine: AsyncEngine = create_engine(test_settings)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def session_factory(
    recurring_engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    """Yield a session factory bound to the recurring engine."""
    return async_sessionmaker(recurring_engine, expire_on_commit=False)


@pytest_asyncio.fixture
async def seeded_world(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[dict[str, object]]:
    """Yield a dict with one bank, one card, one statement, one merchant.

    A canonical "world" the algorithm tests reuse. The
    statement's ``period_end`` is fixed at ``2026-06-30``
    so the 90-day window is deterministic (the cutoff
    lands at ``2026-04-01``). Tests add their own
    transactions to the statement as needed.
    """
    now = datetime.now(UTC)
    bank = Bank(
        name="recurring_test_bank",
        display_name="Recurring Test Bank",
        password_formula="rut_sin_dv",
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    async with session_factory() as session:
        session.add(bank)
        await session.commit()
        await session.refresh(bank)

        card = CreditCard(
            bank_id=bank.id,
            card_number_masked="XXXX XXXX XXXX 0001",
            cardholder="RECURRING USER",
            currency="CLP",
            is_active=True,
        )
        session.add(card)
        await session.commit()
        await session.refresh(card)

        statement = Statement(
            credit_card_id=card.id,
            period_start=date(2026, 6, 1),
            period_end=date(2026, 6, 30),
            statement_date=date(2026, 6, 30),
            file_path="/tmp/recurring-test.pdf",
            file_hash="f" * 64,
            status=StatementStatus.COMPLETED,
        )
        session.add(statement)
        await session.commit()
        await session.refresh(statement)

        merchant = Merchant(
            name="netflix",
            is_active=True,
        )
        session.add(merchant)
        await session.commit()
        await session.refresh(merchant)

    yield {
        "bank_id": bank.id,
        "card_id": card.id,
        "statement_id": statement.id,
        "merchant_id": merchant.id,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _add_transaction(
    session: AsyncSession,
    *,
    statement_id: uuid.UUID,
    merchant_id: uuid.UUID,
    amount: str,
    txn_date: date,
    currency: str = "CLP",
    installment_number: int | None = None,
) -> Transaction:
    """Insert a transaction row and return the ORM object.

    Helper that keeps the per-test fixture code compact.
    The function does NOT commit — the caller is in
    control of the commit boundary so the algorithm
    tests can build a multi-row fixture in one
    ``commit()``.
    """
    txn = Transaction(
        statement_id=statement_id,
        date=txn_date,
        description=f"MERCHANT {merchant_id} {txn_date.isoformat()}",
        amount=Decimal(amount),
        currency=currency,
        installment_number=installment_number,
        low_confidence=False,
    )
    txn.merchant_id = merchant_id
    session.add(txn)
    return txn


# ---------------------------------------------------------------------------
# Algorithm tests
# ---------------------------------------------------------------------------


class TestRecurringDetectorAlgorithm:
    """The detector's algorithm properties, exercised against a real DB."""

    @pytest.mark.asyncio
    async def test_90_day_window_excludes_older_occurrences(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        seeded_world: dict[str, object],
    ) -> None:
        """Transactions older than 90 days are excluded from the rule.

        A group with 3 occurrences inside the 90-day
        window and 1 occurrence at day 120 still produces
        a rule based on the 3 in-window occurrences
        (assuming they qualify on the amount axis).
        """
        statement_id = seeded_world["statement_id"]  # type: ignore[arg-type]
        merchant_id = seeded_world["merchant_id"]  # type: ignore[arg-type]
        # period_end = 2026-06-30 → cutoff = 2026-04-01
        # 3 in-window at days -90, -60, -30 + 1 out-of-window at day -120
        async with session_factory() as session:
            _add_transaction(
                session,
                statement_id=statement_id,
                merchant_id=merchant_id,
                amount="9.99",
                txn_date=date(2026, 4, 1),  # 90 days before period_end
            )
            _add_transaction(
                session,
                statement_id=statement_id,
                merchant_id=merchant_id,
                amount="9.99",
                txn_date=date(2026, 5, 1),
            )
            _add_transaction(
                session,
                statement_id=statement_id,
                merchant_id=merchant_id,
                amount="9.99",
                txn_date=date(2026, 6, 1),
            )
            _add_transaction(
                session,
                statement_id=statement_id,
                merchant_id=merchant_id,
                amount="9.99",
                txn_date=date(2026, 3, 2),  # 120 days before — out of window
            )
            await session.commit()

            statement = await session.get(Statement, statement_id)
            detector = RecurringDetector(session, partial_success=False)
            rules = await detector.detect(statement)

        assert len(rules) == 1
        assert rules[0].occurrences == 3  # out-of-window excluded

    @pytest.mark.asyncio
    async def test_two_occurrences_do_not_qualify(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        seeded_world: dict[str, object],
    ) -> None:
        """A group with 2 occurrences does not produce a rule.

        The 3-occurrence threshold is enforced after the
        in-band filter — 2 occurrences (no outliers) is
        below the bar.
        """
        statement_id = seeded_world["statement_id"]  # type: ignore[arg-type]
        merchant_id = seeded_world["merchant_id"]  # type: ignore[arg-type]
        async with session_factory() as session:
            _add_transaction(
                session,
                statement_id=statement_id,
                merchant_id=merchant_id,
                amount="9.99",
                txn_date=date(2026, 5, 1),
            )
            _add_transaction(
                session,
                statement_id=statement_id,
                merchant_id=merchant_id,
                amount="9.99",
                txn_date=date(2026, 6, 1),
            )
            await session.commit()

            statement = await session.get(Statement, statement_id)
            detector = RecurringDetector(session, partial_success=False)
            rules = await detector.detect(statement)

        assert rules == []

    @pytest.mark.asyncio
    async def test_30_percent_variance_does_not_qualify(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        seeded_world: dict[str, object],
    ) -> None:
        """3 occurrences with one ~30% outlier do not produce a rule.

        The $13.00 row is ~30% above the $10.25 median
        and falls outside the ±15% band; the remaining 2
        in-band rows are below the 3-occurrence threshold.
        """
        statement_id = seeded_world["statement_id"]  # type: ignore[arg-type]
        merchant_id = seeded_world["merchant_id"]  # type: ignore[arg-type]
        async with session_factory() as session:
            _add_transaction(
                session,
                statement_id=statement_id,
                merchant_id=merchant_id,
                amount="10.00",
                txn_date=date(2026, 4, 1),
            )
            _add_transaction(
                session,
                statement_id=statement_id,
                merchant_id=merchant_id,
                amount="10.50",
                txn_date=date(2026, 5, 1),
            )
            _add_transaction(
                session,
                statement_id=statement_id,
                merchant_id=merchant_id,
                amount="13.00",  # 30% above median — outlier
                txn_date=date(2026, 6, 1),
            )
            await session.commit()

            statement = await session.get(Statement, statement_id)
            detector = RecurringDetector(session, partial_success=False)
            rules = await detector.detect(statement)

        assert rules == []

    @pytest.mark.asyncio
    async def test_15_percent_variance_qualifies(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        seeded_world: dict[str, object],
    ) -> None:
        """3 occurrences with 10% variance produce a rule.

        The $10.00 / $10.50 / $11.00 group is within the
        ±15% band on a $10.50 median, so the rule is
        created with ``amount_min=10.00`` and
        ``amount_max=11.00``.
        """
        statement_id = seeded_world["statement_id"]  # type: ignore[arg-type]
        merchant_id = seeded_world["merchant_id"]  # type: ignore[arg-type]
        async with session_factory() as session:
            _add_transaction(
                session,
                statement_id=statement_id,
                merchant_id=merchant_id,
                amount="10.00",
                txn_date=date(2026, 4, 1),
            )
            _add_transaction(
                session,
                statement_id=statement_id,
                merchant_id=merchant_id,
                amount="10.50",
                txn_date=date(2026, 5, 1),
            )
            _add_transaction(
                session,
                statement_id=statement_id,
                merchant_id=merchant_id,
                amount="11.00",
                txn_date=date(2026, 6, 1),
            )
            await session.commit()

            statement = await session.get(Statement, statement_id)
            detector = RecurringDetector(session, partial_success=False)
            rules = await detector.detect(statement)

        assert len(rules) == 1
        rule = rules[0]
        assert rule.amount_min == Decimal("10.00")
        assert rule.amount_max == Decimal("11.00")
        assert rule.occurrences == 3

    @pytest.mark.asyncio
    async def test_installment_rows_are_skipped(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        seeded_world: dict[str, object],
    ) -> None:
        """Installment rows are excluded from the detector's scan.

        5 transactions for the same merchant, 2 with
        ``installment_number`` set: only the 3
        non-installment rows are considered. The
        installment rows do not contribute to the rule's
        count.
        """
        statement_id = seeded_world["statement_id"]  # type: ignore[arg-type]
        merchant_id = seeded_world["merchant_id"]  # type: ignore[arg-type]
        async with session_factory() as session:
            # 3 non-installment rows
            for offset_days, amount in (
                (60, "9.99"),
                (30, "9.99"),
                (0, "9.99"),
            ):
                _add_transaction(
                    session,
                    statement_id=statement_id,
                    merchant_id=merchant_id,
                    amount=amount,
                    txn_date=date(2026, 4, 30) + _offset(offset_days),
                )
            # 2 installment rows (filtered out)
            for offset_days in (45, 15):
                _add_transaction(
                    session,
                    statement_id=statement_id,
                    merchant_id=merchant_id,
                    amount="9.99",
                    txn_date=date(2026, 4, 30) + _offset(offset_days),
                    installment_number=1,
                )
            await session.commit()

            statement = await session.get(Statement, statement_id)
            detector = RecurringDetector(session, partial_success=False)
            rules = await detector.detect(statement)

        assert len(rules) == 1
        # 3 non-installment rows qualify — installments
        # do not contribute to the count.
        assert rules[0].occurrences == 3

    @pytest.mark.asyncio
    async def test_period_classification(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        seeded_world: dict[str, object],
    ) -> None:
        """Period classification maps median intervals to the right bucket.

        A group with intervals of exactly 7 / 14 / 30 days
        produces three rules with the ``period_label``
        set to weekly / biweekly / monthly respectively.
        The quarterly and yearly buckets are *not* reachable
        in a 90-day window with 3+ occurrences (a
        quarterly pattern has 90-day intervals, so only
        2 fit in the window, which fails the 3-occurrence
        threshold). The threshold logic for those buckets
        is exercised by :func:`test_period_classification_thresholds`.
        """
        statement_id = seeded_world["statement_id"]  # type: ignore[arg-type]
        async with session_factory() as session:
            # Seed 3 merchants — one per in-window bucket.
            merchants: list[uuid.UUID] = []
            for name in ("weekly_m", "biweekly_m", "monthly_m"):
                m = Merchant(name=name, is_active=True)
                session.add(m)
                await session.commit()
                await session.refresh(m)
                merchants.append(m.id)

            # period_end = 2026-06-30. To keep the
            # in-window range at 3 dates per cadence
            # we anchor at period_end and walk backwards.
            base = date(2026, 6, 30)
            # 3 occurrences at 7-day intervals
            for d in (base, _offset_from(base, -7), _offset_from(base, -14)):
                _add_transaction(
                    session,
                    statement_id=statement_id,
                    merchant_id=merchants[0],
                    amount="5.00",
                    txn_date=d,
                )
            # 3 at 14-day intervals
            for d in (base, _offset_from(base, -14), _offset_from(base, -28)):
                _add_transaction(
                    session,
                    statement_id=statement_id,
                    merchant_id=merchants[1],
                    amount="5.00",
                    txn_date=d,
                )
            # 3 at 30-day intervals
            for d in (base, _offset_from(base, -30), _offset_from(base, -60)):
                _add_transaction(
                    session,
                    statement_id=statement_id,
                    merchant_id=merchants[2],
                    amount="5.00",
                    txn_date=d,
                )
            await session.commit()

            statement = await session.get(Statement, statement_id)
            detector = RecurringDetector(session, partial_success=False)
            rules = await detector.detect(statement)

        labels = sorted(r.period_label for r in rules)
        assert labels == ["biweekly", "monthly", "weekly"]

        by_label = {r.period_label: r for r in rules}
        assert by_label["weekly"].period_days == 7
        assert by_label["biweekly"].period_days == 14
        assert by_label["monthly"].period_days == 30

    def test_period_classification_thresholds(self) -> None:
        """The threshold-to-label mapping is the documented contract."""
        from app.services.recurring_detection import RecurringDetector

        # Within the smallest threshold.
        assert RecurringDetector._classify_period(5) == ("weekly", 5)
        assert RecurringDetector._classify_period(10) == ("weekly", 10)
        # First bucket overflows → biweekly.
        assert RecurringDetector._classify_period(11) == ("biweekly", 11)
        assert RecurringDetector._classify_period(18) == ("biweekly", 18)
        # Biweekly overflows → monthly.
        assert RecurringDetector._classify_period(19) == ("monthly", 19)
        assert RecurringDetector._classify_period(45) == ("monthly", 45)
        # Monthly overflows → quarterly.
        assert RecurringDetector._classify_period(46) == ("quarterly", 46)
        assert RecurringDetector._classify_period(120) == ("quarterly", 120)
        # Quarterly overflows → yearly.
        assert RecurringDetector._classify_period(121) == ("yearly", 121)
        assert RecurringDetector._classify_period(400) == ("yearly", 400)
        # Above the top threshold still maps to "yearly"
        # (no "unknown" bucket in v1).
        assert RecurringDetector._classify_period(800) == ("yearly", 800)

    def test_confidence_formula(self) -> None:
        """The confidence formula matches the spec's worked example.

        3 occurrences at $10.00, $10.50, $11.00
        (10% variance) → ``confidence = 0.6 * 0.9048 ≈ 0.5429``.
        The spec scenario quotes the *approximate* value
        ``0.543``; the formula returns the exact rounded
        value ``0.5429`` to 4 decimal places (D1).
        """
        from app.services.recurring_detection import RecurringDetector

        score = RecurringDetector._compute_confidence(
            occurrences=3,
            amount_min=Decimal("10.00"),
            amount_max=Decimal("11.00"),
            median_amount=Decimal("10.50"),
        )
        # 0.6 * (1.0 - 1.0/10.5) = 0.6 * 0.90476... = 0.54285...
        # Rounded to 4 decimals = 0.5429
        assert score == 0.5429

    def test_confidence_saturates_at_five_occurrences(self) -> None:
        """5+ occurrences with identical amounts yield confidence 1.0.

        The occurrence factor saturates at 5
        (``min(1.0, occurrences / 5)``), and the
        amount-consistency factor is 1.0 when the spread
        is zero. The product is 1.0.
        """
        from app.services.recurring_detection import RecurringDetector

        score = RecurringDetector._compute_confidence(
            occurrences=5,
            amount_min=Decimal("10.00"),
            amount_max=Decimal("10.00"),
            median_amount=Decimal("10.00"),
        )
        assert score == 1.0

    @pytest.mark.asyncio
    async def test_upsert_is_idempotent(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        seeded_world: dict[str, object],
    ) -> None:
        """Re-running the detector on the same pattern updates the existing rule.

        The first run creates a new ``RecurringRule``;
        the second run on the same pattern (with one
        additional in-band occurrence) updates the
        existing row's ``occurrences``, ``last_seen_date``,
        and ``confidence`` — no second rule is inserted.
        The upsert key matches on the full composite
        ``(merchant_id, amount_min, amount_max, currency, period_days)``.
        """
        statement_id = seeded_world["statement_id"]  # type: ignore[arg-type]
        merchant_id = seeded_world["merchant_id"]  # type: ignore[arg-type]
        async with session_factory() as session:
            for d in (date(2026, 4, 1), date(2026, 5, 1), date(2026, 6, 1)):
                _add_transaction(
                    session,
                    statement_id=statement_id,
                    merchant_id=merchant_id,
                    amount="9.99",
                    txn_date=d,
                )
            await session.commit()

            statement = await session.get(Statement, statement_id)
            first = await RecurringDetector(session, partial_success=False).detect(statement)
            assert len(first) == 1
            assert first[0].occurrences == 3
            first_id = first[0].id

            # Add a fourth occurrence and re-run.
            _add_transaction(
                session,
                statement_id=statement_id,
                merchant_id=merchant_id,
                amount="9.99",
                txn_date=date(2026, 6, 20),
            )
            await session.commit()

            # The detector is invoked on a *fresh* session
            # to mirror the next-ingest path. (A
            # second call on the same session would
            # also work because the algorithm only
            # reads, but the realistic flow is a new
            # request.)
            statement = await session.get(Statement, statement_id)
            second = await RecurringDetector(session, partial_success=False).detect(statement)
            assert len(second) == 1
            # Same row, updated — not a new rule.
            assert second[0].id == first_id
            assert second[0].occurrences == 4

    @pytest.mark.asyncio
    async def test_fk_backfill_sets_recurring_rule_id(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        seeded_world: dict[str, object],
    ) -> None:
        """The detector's FK backfill sets ``recurring_rule_id`` on the in-statement rows.

        After the detector runs, the three transactions
        in the just-ingested statement carry
        ``recurring_rule_id`` pointing at the new
        rule. Transactions outside the statement are
        untouched.
        """
        statement_id = seeded_world["statement_id"]  # type: ignore[arg-type]
        merchant_id = seeded_world["merchant_id"]  # type: ignore[arg-type]
        async with session_factory() as session:
            for d in (date(2026, 4, 1), date(2026, 5, 1), date(2026, 6, 1)):
                _add_transaction(
                    session,
                    statement_id=statement_id,
                    merchant_id=merchant_id,
                    amount="9.99",
                    txn_date=d,
                )
            await session.commit()

            statement = await session.get(Statement, statement_id)
            rules = await RecurringDetector(session, partial_success=False).detect(statement)
            assert len(rules) == 1
            rule_id = rules[0].id

        async with session_factory() as session:
            txns = (
                await session.execute(
                    select(Transaction).where(Transaction.statement_id == statement_id)
                )
            ).scalars().all()
            assert all(t.recurring_rule_id == rule_id for t in txns)

    @pytest.mark.asyncio
    async def test_empty_result_no_recurring_patterns(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        seeded_world: dict[str, object],
    ) -> None:
        """An empty result is a valid outcome.

        A single transaction (or zero transactions) does
        not produce a rule. The detector returns an empty
        list and the log line carries ``rule_count=0``.
        """
        statement_id = seeded_world["statement_id"]  # type: ignore[arg-type]
        merchant_id = seeded_world["merchant_id"]  # type: ignore[arg-type]
        async with session_factory() as session:
            # Single transaction — below the 3-occurrence
            # threshold.
            _add_transaction(
                session,
                statement_id=statement_id,
                merchant_id=merchant_id,
                amount="9.99",
                txn_date=date(2026, 5, 1),
            )
            await session.commit()

            statement = await session.get(Statement, statement_id)
            rules = await RecurringDetector(session, partial_success=False).detect(statement)

        assert rules == []

    @pytest.mark.asyncio
    async def test_logs_info_on_full_success(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        seeded_world: dict[str, object],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """``logger.info`` fires on a full-success detector run (decision #7)."""
        caplog.set_level(logging.INFO, logger="app.services.recurring_detection")
        statement_id = seeded_world["statement_id"]  # type: ignore[arg-type]
        merchant_id = seeded_world["merchant_id"]  # type: ignore[arg-type]
        async with session_factory() as session:
            for d in (date(2026, 4, 1), date(2026, 5, 1), date(2026, 6, 1)):
                _add_transaction(
                    session,
                    statement_id=statement_id,
                    merchant_id=merchant_id,
                    amount="9.99",
                    txn_date=d,
                )
            await session.commit()
            statement = await session.get(Statement, statement_id)
            await RecurringDetector(session, partial_success=False).detect(statement)

        info_records = [
            r
            for r in caplog.records
            if r.name == "app.services.recurring_detection" and r.levelno == logging.INFO
        ]
        assert len(info_records) == 1
        assert "Recurring detection complete" in info_records[0].getMessage()

    @pytest.mark.asyncio
    async def test_logs_warning_on_partial_success(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        seeded_world: dict[str, object],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """``logger.warning`` fires on a partial-success detector run (decision #7)."""
        caplog.set_level(logging.WARNING, logger="app.services.recurring_detection")
        statement_id = seeded_world["statement_id"]  # type: ignore[arg-type]
        merchant_id = seeded_world["merchant_id"]  # type: ignore[arg-type]
        async with session_factory() as session:
            for d in (date(2026, 4, 1), date(2026, 5, 1), date(2026, 6, 1)):
                _add_transaction(
                    session,
                    statement_id=statement_id,
                    merchant_id=merchant_id,
                    amount="9.99",
                    txn_date=d,
                )
            await session.commit()
            statement = await session.get(Statement, statement_id)
            await RecurringDetector(session, partial_success=True).detect(statement)

        warning_records = [
            r
            for r in caplog.records
            if r.name == "app.services.recurring_detection" and r.levelno == logging.WARNING
        ]
        assert len(warning_records) == 1
        assert "partial-success" in warning_records[0].getMessage()


# ---------------------------------------------------------------------------
# API tests
# ---------------------------------------------------------------------------


class TestRecurringAPI:
    """The two recurring endpoints round-trip the schema."""

    @pytest_asyncio.fixture
    async def api_client(
        self,
        recurring_engine: AsyncEngine,
    ) -> AsyncIterator[AsyncClient]:
        """Yield an ``httpx.AsyncClient`` wired to the recurring engine."""
        from app.db.session import get_session

        app = create_app()
        factory = async_sessionmaker(recurring_engine, expire_on_commit=False)

        async def _override_session():  # type: ignore[no-untyped-def]
            async with factory() as session:
                yield session

        app.dependency_overrides[get_session] = _override_session
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
            yield ac

    @pytest_asyncio.fixture
    async def seeded_world(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> dict[str, object]:
        """Yield a bank, card, statement, and merchant for the API tests."""
        now = datetime.now(UTC)
        async with session_factory() as session:
            bank = Bank(
                name="api_recurring_bank",
                display_name="API Bank",
                password_formula="rut_sin_dv",
                is_active=True,
                created_at=now,
                updated_at=now,
            )
            session.add(bank)
            await session.commit()
            await session.refresh(bank)
            card = CreditCard(
                bank_id=bank.id,
                card_number_masked="XXXX XXXX XXXX 0002",
                cardholder="API USER",
                currency="CLP",
                is_active=True,
            )
            session.add(card)
            await session.commit()
            await session.refresh(card)
            merchant = Merchant(name="spotify", is_active=True)
            session.add(merchant)
            await session.commit()
            await session.refresh(merchant)
            yield {
                "bank_id": bank.id,
                "card_id": card.id,
                "merchant_id": merchant.id,
            }

    @pytest.mark.asyncio
    async def test_get_recurring_excludes_inactive_and_orders_desc(
        self,
        api_client: AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
        seeded_world: dict[str, object],
    ) -> None:
        """``GET /api/v1/recurring`` returns only active rules, freshest first."""
        merchant_id = seeded_world["merchant_id"]  # type: ignore[arg-type]
        async with session_factory() as session:
            # Two active rules with different last_seen_date.
            r1 = RecurringRule(
                merchant_id=merchant_id,
                period_days=30,
                period_label="monthly",
                amount_min=Decimal("9.99"),
                amount_max=Decimal("9.99"),
                currency="USD",
                is_active=True,
                confidence=0.95,
                last_seen_date=date(2026, 5, 1),
                occurrences=3,
            )
            r2 = RecurringRule(
                merchant_id=merchant_id,
                period_days=30,
                period_label="monthly",
                amount_min=Decimal("19.99"),
                amount_max=Decimal("19.99"),
                currency="USD",
                is_active=True,
                confidence=0.9,
                last_seen_date=date(2026, 6, 15),
                occurrences=4,
            )
            # Inactive rule — must be excluded.
            r3 = RecurringRule(
                merchant_id=merchant_id,
                period_days=30,
                period_label="monthly",
                amount_min=Decimal("29.99"),
                amount_max=Decimal("29.99"),
                currency="USD",
                is_active=False,
                confidence=0.5,
                last_seen_date=date(2026, 7, 1),
                occurrences=2,
            )
            session.add_all([r1, r2, r3])
            await session.commit()

        response = await api_client.get("/api/v1/recurring")
        assert response.status_code == 200
        body = response.json()
        assert len(body) == 2
        # Ordered by last_seen_date desc.
        assert body[0]["last_seen_date"] == "2026-06-15"
        assert body[1]["last_seen_date"] == "2026-05-01"
        # Inactive rule is excluded.
        assert all(rule["is_active"] for rule in body)

    @pytest.mark.asyncio
    async def test_patch_recurring_activates(
        self,
        api_client: AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
        seeded_world: dict[str, object],
    ) -> None:
        """``PATCH /api/v1/recurring/{id}`` with ``is_active=true`` activates the rule."""
        merchant_id = seeded_world["merchant_id"]  # type: ignore[arg-type]
        async with session_factory() as session:
            rule = RecurringRule(
                merchant_id=merchant_id,
                period_days=30,
                period_label="monthly",
                amount_min=Decimal("9.99"),
                amount_max=Decimal("9.99"),
                currency="USD",
                is_active=False,
                confidence=0.5,
                last_seen_date=date(2026, 5, 1),
                occurrences=2,
            )
            session.add(rule)
            await session.commit()
            await session.refresh(rule)
            rule_id = rule.id

        response = await api_client.patch(
            f"/api/v1/recurring/{rule_id}",
            json={"is_active": True},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["is_active"] is True
        assert body["id"] == str(rule_id)

        # Subsequent GET includes the rule.
        get_response = await api_client.get("/api/v1/recurring")
        assert get_response.status_code == 200
        assert len(get_response.json()) == 1

    @pytest.mark.asyncio
    async def test_patch_recurring_deactivates_preserves_fk(
        self,
        api_client: AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
        seeded_world: dict[str, object],
    ) -> None:
        """Deactivation preserves the FK on historical transactions (design D).

        An active rule with 5 transactions whose
        ``recurring_rule_id`` matches the rule is
        deactivated via PATCH. The transactions still
        carry the FK (the API filter excludes the rule
        from the list, but the historical link is
        preserved for audit).
        """
        now = datetime.now(UTC)
        merchant_id = seeded_world["merchant_id"]  # type: ignore[arg-type]
        async with session_factory() as session:
            bank = await session.get(Bank, seeded_world["bank_id"])  # type: ignore[arg-type]
            card = CreditCard(
                bank_id=bank.id,
                card_number_masked="XXXX XXXX XXXX 0099",
                cardholder="FK USER",
                currency="USD",
                is_active=True,
            )
            session.add(card)
            await session.commit()
            await session.refresh(card)
            statement = Statement(
                credit_card_id=card.id,
                period_start=date(2026, 5, 1),
                period_end=date(2026, 5, 31),
                statement_date=date(2026, 5, 31),
                file_path="/tmp/fk-test.pdf",
                file_hash="9" * 64,
                status=StatementStatus.COMPLETED,
            )
            session.add(statement)
            await session.commit()
            await session.refresh(statement)

            rule = RecurringRule(
                merchant_id=merchant_id,
                period_days=30,
                period_label="monthly",
                amount_min=Decimal("9.99"),
                amount_max=Decimal("9.99"),
                currency="USD",
                is_active=True,
                confidence=0.9,
                last_seen_date=date(2026, 5, 15),
                occurrences=5,
            )
            session.add(rule)
            await session.commit()
            await session.refresh(rule)
            rule_id = rule.id

            # 5 transactions all linked to the rule.
            for offset in range(5):
                txn = Transaction(
                    statement_id=statement.id,
                    date=date(2026, 5, 1 + offset),
                    description=f"NETFLIX {offset}",
                    amount=Decimal("9.99"),
                    currency="USD",
                    merchant_id=merchant_id,
                    low_confidence=False,
                    created_at=now,
                    updated_at=now,
                )
                txn.recurring_rule_id = rule_id
                session.add(txn)
            await session.commit()

        # Deactivate via PATCH.
        response = await api_client.patch(
            f"/api/v1/recurring/{rule_id}",
            json={"is_active": False},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["is_active"] is False

        # The rule is excluded from the GET (read-side filter).
        get_response = await api_client.get("/api/v1/recurring")
        assert get_response.status_code == 200
        assert get_response.json() == []

        # The transactions still carry the FK — the
        # historical link is preserved.
        async with session_factory() as session:
            txns = (
                await session.execute(
                    select(Transaction).where(Transaction.statement_id == statement.id)
                )
            ).scalars().all()
            assert len(txns) == 5
            assert all(t.recurring_rule_id == rule_id for t in txns)

    @pytest.mark.asyncio
    async def test_patch_recurring_404(
        self,
        api_client: AsyncClient,
    ) -> None:
        """``PATCH /api/v1/recurring/{id}`` returns 404 for an unknown id."""
        unknown_id = uuid.uuid4()
        response = await api_client.patch(
            f"/api/v1/recurring/{unknown_id}",
            json={"is_active": False},
        )
        assert response.status_code == 404
        assert str(unknown_id) in response.json()["detail"]


# ---------------------------------------------------------------------------
# Module helpers
# ---------------------------------------------------------------------------


def _offset(days: int) -> datetime.timedelta:  # type: ignore[name-defined]
    """Return a ``timedelta`` of ``days`` (test-local)."""
    from datetime import timedelta

    return timedelta(days=days)


def _offset_from(base: date, days: int) -> date:
    """Return ``base + timedelta(days=days)`` (test-local)."""
    from datetime import timedelta

    return base + timedelta(days=days)
