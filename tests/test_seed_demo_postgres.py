"""PostgreSQL coverage for deterministic demo recurring-rule seeds."""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.cli import seed_demo
from app.core.config import Settings
from app.db.engine import create_engine
from app.models import RecurringRule
from app.services.dashboard import DashboardService


async def _recurring_snapshot(settings: Settings) -> list[tuple[object, str, str, int]]:
    engine = create_engine(settings.database_url)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            rows = (
                (
                    await session.execute(
                        select(RecurringRule).order_by(
                            RecurringRule.merchant_id, RecurringRule.currency
                        )
                    )
                )
                .scalars()
                .all()
            )
            return [(row.id, row.period_label, row.currency, row.period_days) for row in rows]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_seeded_recurring_rules_keep_canonical_labels_and_deterministic_ids(
    test_settings: Settings,
) -> None:
    """Recurring labels remain canonical while IDs encode the seed tuple."""
    await seed_demo.seed_demo()
    first = await _recurring_snapshot(test_settings)
    await seed_demo.seed_demo()
    second = await _recurring_snapshot(test_settings)

    assert first == second
    assert {(label, currency, days) for _, label, currency, days in first} == {
        (label, currency, days) for _, days, label, _, _, currency, _ in seed_demo.RECURRING_RULES
    }
    assert all(len(label) <= 16 for _, label, _, _ in first)

    engine = create_engine(test_settings.database_url)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            for merch_slug, days, label, _, _, currency, _ in seed_demo.RECURRING_RULES:
                merchant_id = seed_demo._seed_uuid(f"merchant/{merch_slug}")
                stable_key = f"recurring/{merchant_id}/{currency}/{days}"
                rule = await session.get(RecurringRule, seed_demo._seed_uuid(stable_key))
                assert rule is not None
                assert rule.period_label == label
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_seed_rejects_recurring_rule_identity_collision(test_settings: Settings) -> None:
    """A row at a deterministic seed ID must match the canonical seed tuple."""
    await seed_demo.seed_demo()
    engine = create_engine(test_settings.database_url)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            rule = (await session.execute(select(RecurringRule))).scalars().first()
            assert rule is not None
            rule.period_label = "collision"
            await session.commit()
    finally:
        await engine.dispose()

    with pytest.raises(RuntimeError, match="seed recurring rule collision"):
        await seed_demo.seed_demo()


@pytest.mark.asyncio
async def test_monthly_all_time_dashboard_query_runs_on_postgresql(test_settings: Settings) -> None:
    """All-time month grouping uses PostgreSQL's supported date formatter."""
    await seed_demo.seed_demo()
    engine = create_engine(test_settings.database_url)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            rows = await DashboardService(session).monthly(range_months=0)
    finally:
        await engine.dispose()

    assert [row.month for row in rows] == [f"2026-{month:02d}" for month in range(1, 8)]
    assert all(row.transaction_count > 0 for row in rows)
