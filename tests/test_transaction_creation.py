"""Tests for ``add-transaction-creation-api``.

Covers the closed creation schemas (``StatementMetadataCreate``, the
extended ``TransactionCreate``, bounded ``TransactionBatchCreate`` and
``TransactionBatchResponse``), the request-wide parent-planning half of
``TransactionCreationService`` and, from PR4 on, the atomic persistence
half: new API statements, category/currency/merchant policy and ordered
response snapshots inside one outer transaction. HTTP routes live in
:mod:`tests.test_transaction_creation_http`.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError
from sqlalchemy import event, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.db.session import create_session_factory
from app.models import (
    Bank,
    Category,
    CreditCard,
    Merchant,
    MerchantAlias,
    Statement,
    Transaction,
)
from app.models.statement import StatementSource, StatementStatus
from app.schemas import (
    StatementMetadataCreate,
    TransactionBatchCreate,
    TransactionBatchResponse,
    TransactionCreate,
    TransactionResponse,
)
from app.services.merchants import KNOWN_MERCHANT_PATTERNS, MerchantNormalizer, normalize
from app.services.transaction_creation import (
    ParentPlan,
    TransactionCreationError,
    TransactionCreationService,
)

# ---------------------------------------------------------------------------
# Payload helpers
# ---------------------------------------------------------------------------

_CARD_ID = uuid.UUID("33333333-4333-4333-8333-333333333333")
_STATEMENT_ID = uuid.UUID("44444444-4444-4444-8444-444444444444")


def _metadata(**overrides: object) -> dict[str, object]:
    """Valid nested statement metadata; a ``None`` override removes the key."""
    payload: dict[str, object] = {
        "credit_card_id": str(_CARD_ID),
        "period_start": "2026-09-01",
        "period_end": "2026-09-30",
        "statement_date": "2026-10-01",
    }
    for key, value in overrides.items():
        if value is None:
            payload.pop(key, None)
        else:
            payload[key] = value
    return payload


def _tx_payload(**overrides: object) -> dict[str, object]:
    """Valid single transaction creation payload."""
    payload: dict[str, object] = {
        "statement_id": str(_STATEMENT_ID),
        "date": "2026-09-10",
        "description": "LIDER COM 3",
        "amount": "12500.00",
        "currency": "CLP",
    }
    payload.update(overrides)
    return payload


def _tx_response() -> TransactionResponse:
    """Build one minimal valid ``TransactionResponse`` for batch-shape tests."""
    now = datetime(2026, 9, 10, tzinfo=UTC)
    return TransactionResponse(
        id=uuid.uuid4(),
        statement_id=_STATEMENT_ID,
        date=date(2026, 9, 10),
        description="LIDER COM 3",
        amount=Decimal("12500.00"),
        currency="CLP",
        category=None,
        category_id=None,
        low_confidence=True,
        installment_number=None,
        installment_total=None,
        installment_value=None,
        raw_json=None,
        created_at=now,
        updated_at=now,
    )


# ---------------------------------------------------------------------------
# StatementMetadataCreate — closed nested creation metadata
# ---------------------------------------------------------------------------


class TestStatementMetadataCreate:
    def test_requires_all_four_fields(self) -> None:
        """Each of the four metadata fields is required; null removes it."""
        for required in ("credit_card_id", "period_start", "period_end", "statement_date"):
            with pytest.raises(ValidationError):
                StatementMetadataCreate.model_validate(_metadata(**{required: None}))

    @pytest.mark.parametrize(
        "forbidden",
        [
            "id",
            "file_path",
            "file_hash",
            "source",
            "status",
            "error_message",
            "currency",
            "created_at",
            "updated_at",
            "totally_unknown",
        ],
    )
    def test_rejects_nested_server_and_unknown_fields(self, forbidden: str) -> None:
        """The object is closed: server-assigned and unknown fields are rejected."""
        with pytest.raises(ValidationError):
            StatementMetadataCreate.model_validate(_metadata(**{forbidden: "x"}))

    def test_allows_statement_date_outside_period(self) -> None:
        """Statement date may fall after or before the billing period."""
        after = StatementMetadataCreate.model_validate(_metadata(statement_date="2026-10-15"))
        assert after.statement_date == date(2026, 10, 15)
        before = StatementMetadataCreate.model_validate(_metadata(statement_date="2026-08-15"))
        assert before.statement_date == date(2026, 8, 15)

    def test_rejects_reversed_period(self) -> None:
        """``period_start > period_end`` is a content error."""
        with pytest.raises(ValidationError):
            StatementMetadataCreate.model_validate(
                _metadata(period_start="2026-10-01", period_end="2026-09-30")
            )

    def test_allows_single_day_period(self) -> None:
        """Equal period bounds stay valid."""
        meta = StatementMetadataCreate.model_validate(
            _metadata(period_start="2026-09-30", period_end="2026-09-30")
        )
        assert meta.period_start == meta.period_end == date(2026, 9, 30)


# ---------------------------------------------------------------------------
# TransactionCreate — extended creation input
# ---------------------------------------------------------------------------


class TestTransactionCreate:
    def test_minimal_input_omits_statement_and_category_id(self) -> None:
        """Minimal valid input leaves the nested statement and category ID null."""
        tx = TransactionCreate.model_validate(_tx_payload())
        assert tx.statement_id == _STATEMENT_ID
        assert tx.statement is None
        assert tx.category_id is None

    def test_statement_null_means_omitted(self) -> None:
        """An explicit nested ``null`` is treated exactly like omission."""
        tx = TransactionCreate.model_validate(_tx_payload(statement=None))
        assert tx.statement is None

    def test_statement_accepts_nested_metadata(self) -> None:
        """A valid nested metadata object is parsed into ``statement``."""
        tx = TransactionCreate.model_validate(_tx_payload(statement=_metadata()))
        assert tx.statement is not None
        assert tx.statement.credit_card_id == _CARD_ID
        assert tx.statement.period_end == date(2026, 9, 30)

    def test_requires_non_null_statement_id(self) -> None:
        """Statement linkage is mandatory and non-null, even with metadata."""
        payload = _tx_payload(statement=_metadata())
        del payload["statement_id"]
        with pytest.raises(ValidationError):
            TransactionCreate.model_validate(payload)
        with pytest.raises(ValidationError):
            TransactionCreate.model_validate(_tx_payload(statement_id=None))

    def test_category_id_optional_uuid(self) -> None:
        """``category_id`` accepts a UUID string or stays null."""
        category_id = uuid.uuid4()
        tx = TransactionCreate.model_validate(_tx_payload(category_id=str(category_id)))
        assert tx.category_id == category_id

    @pytest.mark.parametrize(
        "forbidden",
        [
            "id",
            "merchant_id",
            "low_confidence",
            "recurring_rule_id",
            "credit_card_id",
            "created_at",
            "updated_at",
            "unknown_field",
        ],
    )
    def test_rejects_unknown_top_level_fields(self, forbidden: str) -> None:
        """Top-level input stays closed: server-assigned fields are rejected."""
        with pytest.raises(ValidationError):
            TransactionCreate.model_validate(_tx_payload(**{forbidden: "x"}))

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("12500.00", Decimal("12500.00")),
            ("-12500.50", Decimal("-12500.50")),
            ("0.00", Decimal("0.00")),
            (12500, Decimal("12500")),
            ("9999999999999.99", Decimal("9999999999999.99")),
        ],
    )
    def test_accepts_decimal_strings_and_integers(self, raw: str | int, expected: Decimal) -> None:
        """Money arrives as JSON decimal strings or integers, stored as Decimal."""
        tx = TransactionCreate.model_validate(_tx_payload(amount=raw))
        assert tx.amount == expected

    @pytest.mark.parametrize("bad", [12.5, -0.5, float("nan"), float("inf"), True, False])
    def test_rejects_float_and_bool_money(self, bad: object) -> None:
        """Floats and bools never become money on either money field."""
        with pytest.raises(ValidationError):
            TransactionCreate.model_validate(_tx_payload(amount=bad))
        with pytest.raises(ValidationError):
            TransactionCreate.model_validate(_tx_payload(installment_value=bad))

    @pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity"])
    def test_rejects_non_finite_string_money(self, bad: str) -> None:
        """Non-finite decimal strings are rejected, not coerced."""
        with pytest.raises(ValidationError):
            TransactionCreate.model_validate(_tx_payload(amount=bad))

    @pytest.mark.parametrize("bad", ["0.001", "10000000000000.00", "1e30"])
    def test_rejects_excess_precision_and_range(self, bad: str) -> None:
        """Money keeps Numeric(15,2) semantics: no rounding, no silent overflow."""
        with pytest.raises(ValidationError):
            TransactionCreate.model_validate(_tx_payload(amount=bad))

    def test_installment_bounds_and_integer_overflow(self) -> None:
        """Installment numbers are >= 1 and fit PostgreSQL INTEGER capacity."""
        tx = TransactionCreate.model_validate(
            _tx_payload(installment_number=1, installment_total=12, installment_value="1041.66")
        )
        assert (tx.installment_number, tx.installment_total) == (1, 12)
        for field in ("installment_number", "installment_total"):
            with pytest.raises(ValidationError):
                TransactionCreate.model_validate(_tx_payload(**{field: 0}))
            with pytest.raises(ValidationError):
                TransactionCreate.model_validate(_tx_payload(**{field: 2**31}))
        tx = TransactionCreate.model_validate(
            _tx_payload(installment_number=2**31 - 1, installment_total=2**31 - 1)
        )
        assert tx.installment_total == 2**31 - 1

    @pytest.mark.parametrize(
        "overrides",
        [
            {"statement_id": "not-a-uuid"},
            {"date": "not-a-date"},
            {"currency": "CL"},
            {"currency": "CLPP"},
            {"currency": 123},
            {"description": ""},
            {"category": "x" * 51},
            {"raw_json": "not-an-object"},
        ],
    )
    def test_rejects_malformed_values(self, overrides: dict[str, object]) -> None:
        """Malformed UUID/date/currency/length values are content errors."""
        with pytest.raises(ValidationError):
            TransactionCreate.model_validate(_tx_payload(**overrides))

    def test_accepts_optional_raw_json(self) -> None:
        """``raw_json`` accepts objects, arrays and null, copied unchanged."""
        for raw in ({"note": "x"}, ["a"], None):
            tx = TransactionCreate.model_validate(_tx_payload(raw_json=raw))
            assert tx.raw_json == raw


# ---------------------------------------------------------------------------
# TransactionBatchCreate / TransactionBatchResponse — bounded batch contracts
# ---------------------------------------------------------------------------


class TestTransactionBatchContracts:
    @pytest.mark.parametrize(("count", "valid"), [(0, False), (1, True), (200, True), (201, False)])
    def test_batch_bounds(self, count: int, valid: bool) -> None:
        """Batch input accepts 1-200 items and rejects 0 and 201."""
        payload = {"transactions": [_tx_payload() for _ in range(count)]}
        if valid:
            batch = TransactionBatchCreate.model_validate(payload)
            assert len(batch.transactions) == count
        else:
            with pytest.raises(ValidationError):
                TransactionBatchCreate.model_validate(payload)

    def test_batch_rejects_unknown_top_level_fields(self) -> None:
        """The batch object is closed; ``count`` is never client-supplied."""
        payload = {"transactions": [_tx_payload()], "count": 1}
        with pytest.raises(ValidationError):
            TransactionBatchCreate.model_validate(payload)

    def test_indexed_item_validation_location(self) -> None:
        """Item errors locate ``transactions`` plus the zero-based item index."""
        items = [_tx_payload(), _tx_payload(amount="oops")]
        with pytest.raises(ValidationError) as excinfo:
            TransactionBatchCreate.model_validate({"transactions": items})
        assert ("transactions", 1, "amount") in [error["loc"] for error in excinfo.value.errors()]

    def test_indexed_nested_metadata_location(self) -> None:
        """Nested metadata errors locate the item, the ``statement`` key and field."""
        items = [_tx_payload(statement=_metadata(period_end=None))]
        with pytest.raises(ValidationError) as excinfo:
            TransactionBatchCreate.model_validate({"transactions": items})
        assert ("transactions", 0, "statement", "period_end") in [
            error["loc"] for error in excinfo.value.errors()
        ]

    def test_batch_response_shape(self) -> None:
        """The batch response carries the ordered list and its count."""
        batch = TransactionBatchResponse(transactions=[_tx_response()], count=1)
        assert batch.count == 1
        assert batch.transactions[0].statement_id == _STATEMENT_ID


async def _seed_parent(
    engine: AsyncEngine,
    currency: str = "CLP",
    *,
    status: StatementStatus = StatementStatus.PENDING,
    error_message: str | None = None,
    is_active: bool = True,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Seed one bank/card/statement parent; return (statement_id, card_id)."""
    factory = create_session_factory(engine)
    async with factory() as session:
        bank = Bank(
            name="planning_bank",
            display_name="Planning Bank",
            password_formula="rut_sin_dv",
        )
        card = CreditCard(
            bank=bank,
            card_number_masked="XXXX XXXX XXXX 4242",
            cardholder="PLANNING USER",
            currency=currency,
            is_active=is_active,
        )
        statement = Statement(
            credit_card=card,
            period_start=date(2026, 9, 1),
            period_end=date(2026, 9, 30),
            statement_date=date(2026, 10, 1),
            file_path="planning/test.pdf",
            file_hash="b" * 64,
            status=status,
            error_message=error_message,
        )
        session.add_all([bank, card, statement])
        await session.commit()
        return statement.id, card.id


def _service_item(
    statement_id: uuid.UUID,
    metadata: dict[str, object] | None = None,
    **overrides: object,
) -> TransactionCreate:
    """Build one validated creation item for the service boundary."""
    payload = _tx_payload(statement_id=str(statement_id), **overrides)
    if metadata is not None:
        payload["statement"] = metadata
    return TransactionCreate.model_validate(payload)


def _metadata_for(card_id: uuid.UUID, **overrides: object) -> dict[str, object]:
    """Nested metadata pointing at a seeded card."""
    return _metadata(credit_card_id=str(card_id), **overrides)


async def _plan(engine: AsyncEngine, items: list[TransactionCreate]) -> dict[uuid.UUID, ParentPlan]:
    """Run the internal planner inside one explicit outer transaction."""
    factory = create_session_factory(engine)
    async with factory() as session:
        service = TransactionCreationService(session)
        async with session.begin():
            return await service.plan_parents(items)


async def _expect_plan_error(
    engine: AsyncEngine,
    items: list[TransactionCreate],
    code: str,
    index: int,
    field: str = "statement",
) -> None:
    """Assert the planner fails with exactly the expected error attribution."""
    factory = create_session_factory(engine)
    async with factory() as session:
        with pytest.raises(TransactionCreationError) as excinfo:
            await TransactionCreationService(session).create_many(items)
    error = excinfo.value
    assert (error.code, error.field, error.index) == (code, field, index)


async def _table_count(engine: AsyncEngine, model: type) -> int:
    """Count rows of ``model`` in a fresh session."""
    factory = create_session_factory(engine)
    async with factory() as session:
        result = await session.execute(select(func.count()).select_from(model))
        return int(result.scalar_one())


async def _assert_no_request_created_rows(
    engine: AsyncEngine,
    *,
    statement_ids: set[uuid.UUID],
    descriptions: set[str],
    merchant_names: set[str] | None = None,
) -> None:
    """Verify rollback durability in a fresh session, scoped to request-owned keys."""
    factory = create_session_factory(engine)
    async with factory() as session:
        statement_count = await session.scalar(
            select(func.count()).select_from(Statement).where(Statement.id.in_(statement_ids))
        )
        transaction_count = await session.scalar(
            select(func.count())
            .select_from(Transaction)
            .where(Transaction.description.in_(descriptions))
        )
        merchant_count = await session.scalar(
            select(func.count())
            .select_from(Merchant)
            .where(Merchant.name.in_(merchant_names or descriptions))
        )
        alias_count = await session.scalar(
            select(func.count())
            .select_from(MerchantAlias)
            .where(MerchantAlias.alias_text.in_(descriptions))
        )
    assert statement_count == 0
    assert transaction_count == 0
    assert merchant_count == 0
    assert alias_count == 0


async def _description_count(engine: AsyncEngine, description: str) -> int:
    """Count durable transactions with the exact description in a fresh session."""
    factory = create_session_factory(engine)
    async with factory() as session:
        result = await session.execute(
            select(func.count())
            .select_from(Transaction)
            .where(Transaction.description == description)
        )
        return int(result.scalar_one())


class TestParentPlanningRules:
    @pytest.mark.parametrize("matching", [True, False])
    async def test_existing_parent_with_metadata_conflicts(
        self, engine: AsyncEngine, matching: bool
    ) -> None:
        """Metadata for an existing parent is a 409 conflict, matching or not."""
        statement_id, card_id = await _seed_parent(engine)
        overrides: dict[str, object] = {} if matching else {"period_end": "2026-09-28"}
        items = [_service_item(statement_id, metadata=_metadata_for(card_id, **overrides))]
        await _expect_plan_error(engine, items, "statement_already_exists", 0)

    async def test_missing_parent_without_metadata_is_unprocessable(
        self, engine: AsyncEngine
    ) -> None:
        """A missing parent referenced without metadata is a 422 at first reference."""
        await _seed_parent(engine)
        items = [_service_item(uuid.uuid4())]
        await _expect_plan_error(engine, items, "statement_metadata_required", 0)

    async def test_missing_parent_error_carries_first_reference_index(
        self, engine: AsyncEngine
    ) -> None:
        """The 422 attributes to the first item referencing the missing UUID."""
        statement_id, _ = await _seed_parent(engine)
        items = [_service_item(statement_id), _service_item(uuid.uuid4())]
        await _expect_plan_error(engine, items, "statement_metadata_required", 1)

    @pytest.mark.parametrize("position", [0, 1, 2])
    async def test_single_metadata_anywhere_plans_new_parent(
        self, engine: AsyncEngine, position: int
    ) -> None:
        """Exactly one metadata object may appear anywhere for a new ID."""
        _, card_id = await _seed_parent(engine)
        new_id = uuid.uuid4()
        items = [_service_item(new_id) for _ in range(3)]
        items[position] = _service_item(new_id, metadata=_metadata_for(card_id))
        plans = await _plan(engine, items)
        plan = plans[new_id]
        assert plan.is_existing is False
        assert plan.metadata_index == position
        assert plan.first_reference_index == 0
        assert plan.card is not None and plan.card.currency == "CLP"

    @pytest.mark.parametrize("conflicting", [True, False])
    async def test_duplicate_metadata_rejected_at_second_index(
        self, engine: AsyncEngine, conflicting: bool
    ) -> None:
        """A second metadata object fails at its own index, identical or not."""
        _, card_id = await _seed_parent(engine)
        new_id = uuid.uuid4()
        first = _metadata_for(card_id)
        second = _metadata_for(card_id, statement_date="2026-10-05") if conflicting else dict(first)
        items = [
            _service_item(new_id, metadata=first),
            _service_item(new_id),
            _service_item(new_id, metadata=second),
        ]
        await _expect_plan_error(engine, items, "duplicate_statement_metadata", 2)

    async def test_mixed_existing_and_new_ids_plan_independently(self, engine: AsyncEngine) -> None:
        """Existing and new parents resolve independently with their own cards."""
        existing_id, existing_card = await _seed_parent(engine)
        new_id = uuid.uuid4()
        items = [
            _service_item(existing_id),
            _service_item(new_id, metadata=_metadata_for(existing_card)),
            _service_item(existing_id),
            _service_item(new_id),
        ]
        plans = await _plan(engine, items)
        assert plans[existing_id].is_existing is True
        assert plans[existing_id].metadata is None
        assert plans[existing_id].card is not None
        assert plans[existing_id].card.currency == "CLP"
        assert plans[new_id].is_existing is False
        assert plans[new_id].metadata is not None
        assert plans[new_id].card is not None
        assert plans[new_id].card.currency == "CLP"

    async def test_unknown_new_parent_card_is_not_found(self, engine: AsyncEngine) -> None:
        """An unknown card in new-parent metadata is a 404 at the metadata index."""
        statement_id, _ = await _seed_parent(engine)
        items = [
            _service_item(statement_id),
            _service_item(uuid.uuid4(), metadata=_metadata_for(uuid.uuid4())),
        ]
        await _expect_plan_error(
            engine, items, "credit_card_not_found", 1, field="statement.credit_card_id"
        )

    async def test_lowest_offending_index_wins(self, engine: AsyncEngine) -> None:
        """With several invalid plans, the lowest offending index is reported."""
        statement_id, card_id = await _seed_parent(engine)
        new_id = uuid.uuid4()
        other_new = uuid.uuid4()
        metadata = _metadata_for(card_id)
        # 422 (missing metadata, index 1) beats 409 (existing + metadata, index 3).
        await _expect_plan_error(
            engine,
            [
                _service_item(statement_id),
                _service_item(new_id),
                _service_item(new_id),
                _service_item(statement_id, metadata=metadata),
            ],
            "statement_metadata_required",
            1,
        )
        # 409 (existing + metadata, index 0) beats 422 (missing metadata, index 1).
        await _expect_plan_error(
            engine,
            [
                _service_item(statement_id, metadata=metadata),
                _service_item(new_id),
            ],
            "statement_already_exists",
            0,
        )
        # 422 (missing metadata, index 0) beats 422 (duplicate metadata, index 3).
        await _expect_plan_error(
            engine,
            [
                _service_item(other_new),
                _service_item(new_id, metadata=metadata),
                _service_item(new_id),
                _service_item(new_id, metadata=metadata),
            ],
            "statement_metadata_required",
            0,
        )

    async def test_plan_violations_precede_card_lookup(self, engine: AsyncEngine) -> None:
        """The parent-definition pass runs before card resolution (design order)."""
        statement_id, _ = await _seed_parent(engine)
        items = [
            _service_item(uuid.uuid4(), metadata=_metadata_for(uuid.uuid4())),
            _service_item(statement_id),
            _service_item(uuid.uuid4()),
        ]
        await _expect_plan_error(engine, items, "statement_metadata_required", 2)

    async def test_planning_failure_writes_nothing(self, engine: AsyncEngine) -> None:
        """Planning only reads: every failure path leaves the database untouched."""
        statement_id, card_id = await _seed_parent(engine)
        before_statements = await _table_count(engine, Statement)
        before_transactions = await _table_count(engine, Transaction)
        new_id = uuid.uuid4()
        bad_batches = [
            [_service_item(statement_id, metadata=_metadata_for(card_id))],
            [_service_item(new_id)],
            [
                _service_item(new_id, metadata=_metadata_for(card_id)),
                _service_item(new_id, metadata=_metadata_for(card_id)),
            ],
            [_service_item(new_id, metadata=_metadata_for(uuid.uuid4()))],
        ]
        factory = create_session_factory(engine)
        for batch in bad_batches:
            async with factory() as session:
                with pytest.raises(TransactionCreationError):
                    await TransactionCreationService(session).create_many(batch)
        assert await _table_count(engine, Statement) == before_statements
        assert await _table_count(engine, Transaction) == before_transactions

    async def test_null_metadata_followers_share_one_authority(self, engine: AsyncEngine) -> None:
        """Null/omitted metadata followers resolve to the one authoritative object."""
        _, card_id = await _seed_parent(engine)
        new_id = uuid.uuid4()
        carrier = _service_item(new_id, metadata=_metadata_for(card_id))
        items = [_service_item(new_id), carrier, _service_item(new_id, statement=None)]
        plans = await _plan(engine, items)
        plan = plans[new_id]
        assert plan.metadata is carrier.statement
        assert plan.metadata_index == 1
        assert plan.first_reference_index == 0

    async def test_service_rejects_out_of_bounds_batches(self, engine: AsyncEngine) -> None:
        """The service re-enforces the 1-200 batch bounds defensively."""
        factory = create_session_factory(engine)
        async with factory() as session:
            service = TransactionCreationService(session)
            with pytest.raises(TransactionCreationError) as empty:
                await service.create_many([])
            assert empty.value.code == "invalid_batch"
            with pytest.raises(TransactionCreationError) as oversized:
                await service.create_many([_service_item(uuid.uuid4())] * 201)
            assert oversized.value.code == "invalid_batch"


class TestPlanningTriangulation:
    async def test_multiple_new_parents_plan_independently(self, engine: AsyncEngine) -> None:
        """Two new UUIDs each carry their own single metadata object."""
        _, card_id = await _seed_parent(engine)
        first_new = uuid.uuid4()
        second_new = uuid.uuid4()
        items = [
            _service_item(first_new, metadata=_metadata_for(card_id)),
            _service_item(second_new),
            _service_item(first_new),
            _service_item(second_new, metadata=_metadata_for(card_id, statement_date="2026-10-02")),
        ]
        plans = await _plan(engine, items)
        assert plans[first_new].metadata_index == 0
        assert plans[second_new].metadata_index == 3
        assert plans[first_new].metadata is not plans[second_new].metadata

    async def test_plan_card_currency_follows_the_card(self, engine: AsyncEngine) -> None:
        """Card currency is resolved from the row, not assumed to be CLP."""
        _, usd_card = await _seed_parent(engine, currency="USD")
        new_id = uuid.uuid4()
        items = [_service_item(new_id, metadata=_metadata_for(usd_card))]
        plans = await _plan(engine, items)
        assert plans[new_id].card is not None
        assert plans[new_id].card.currency == "USD"


# ---------------------------------------------------------------------------
# PR4 — atomic persistence (service)
# ---------------------------------------------------------------------------


async def _seed_categories(engine: AsyncEngine) -> dict[str, uuid.UUID]:
    """Seed two closed-set categories; return ``name -> id``."""
    factory = create_session_factory(engine)
    async with factory() as session:
        rows = [
            Category(name="Groceries", display_name="Groceries", sort_order=2),
            Category(name="Dining Out", display_name="Dining Out", sort_order=1),
        ]
        session.add_all(rows)
        await session.commit()
        return {row.name: row.id for row in rows}


class TestAtomicPersistence:
    """PostgreSQL-backed persistence and policy tests for ``create_many``."""

    async def test_new_statement_created_with_api_provenance(self, engine: AsyncEngine) -> None:
        """A new parent is persisted under the caller UUID with API provenance."""
        _, card_id = await _seed_parent(engine)
        await _seed_categories(engine)
        new_id = uuid.uuid4()
        items = [
            _service_item(
                new_id,
                metadata=_metadata_for(card_id),
                description="LIDER COM 3",
            )
        ]
        factory = create_session_factory(engine)
        async with factory() as session:
            snapshots = await TransactionCreationService(session).create_many(items)

        assert len(snapshots) == 1
        snapshot = snapshots[0]
        assert snapshot.statement_id == new_id
        assert snapshot.description == "LIDER COM 3"
        assert snapshot.amount == Decimal("12500.00")
        assert snapshot.created_at is not None
        assert snapshot.updated_at is not None

        async with factory() as session:
            statement = await session.get(Statement, new_id)
            assert statement is not None
            assert statement.credit_card_id == card_id
            assert statement.period_start == date(2026, 9, 1)
            assert statement.period_end == date(2026, 9, 30)
            assert statement.statement_date == date(2026, 10, 1)
            assert statement.source == StatementSource.API
            assert statement.status == StatementStatus.COMPLETED
            assert statement.file_path is None
            assert statement.file_hash is None
            assert statement.error_message is None
            result = await session.execute(
                select(Transaction).where(Transaction.statement_id == new_id)
            )
            rows = list(result.scalars().all())
            assert len(rows) == 1
            assert rows[0].id == snapshot.id
            assert rows[0].recurring_rule_id is None

    async def test_appending_keeps_existing_statement_state(self, engine: AsyncEngine) -> None:
        """Appending to an existing parent never rewrites its lifecycle state."""
        statement_id, _ = await _seed_parent(
            engine, status=StatementStatus.FAILED, error_message="parse boom"
        )
        factory = create_session_factory(engine)
        async with factory() as session:
            before = await session.get(Statement, statement_id)
            assert before is not None
            before_created, before_updated = before.created_at, before.updated_at
            before_source, before_status = before.source, before.status
            before_files, before_error = (before.file_path, before.file_hash), before.error_message

        async with factory() as session:
            await TransactionCreationService(session).create_many([_service_item(statement_id)])

        async with factory() as session:
            after = await session.get(Statement, statement_id)
            assert after is not None
            assert (after.source, after.status) == (before_source, before_status)
            assert (after.file_path, after.file_hash) == before_files
            assert after.error_message == before_error
            assert (after.created_at, after.updated_at) == (before_created, before_updated)

    @pytest.mark.parametrize("with_legacy_string", [True, False])
    async def test_category_id_precedence(
        self, engine: AsyncEngine, with_legacy_string: bool
    ) -> None:
        """A supplied category ID wins and marks the row confident."""
        statement_id, _ = await _seed_parent(engine)
        categories = await _seed_categories(engine)
        overrides: dict[str, object] = {"category_id": str(categories["Groceries"])}
        if with_legacy_string:
            overrides["category"] = "My Own Label"
        factory = create_session_factory(engine)
        async with factory() as session:
            snapshots = await TransactionCreationService(session).create_many(
                [_service_item(statement_id, **overrides)]
            )
        snapshot = snapshots[0]
        assert snapshot.category_id == categories["Groceries"]
        assert snapshot.category == "Groceries"
        assert snapshot.low_confidence is False

    async def test_legacy_category_string_preserved(self, engine: AsyncEngine) -> None:
        """Without an ID the legacy string is stored verbatim, low confidence."""
        statement_id, _ = await _seed_parent(engine)
        await _seed_categories(engine)
        factory = create_session_factory(engine)
        async with factory() as session:
            snapshots = await TransactionCreationService(session).create_many(
                [_service_item(statement_id, category="Bodega de barrio")]
            )
        snapshot = snapshots[0]
        assert snapshot.category == "Bodega de barrio"
        assert snapshot.category_id is None
        assert snapshot.low_confidence is True

    async def test_no_category_stays_uncategorized(self, engine: AsyncEngine) -> None:
        """No category input stores NULL category, low confidence (not 'Uncategorized')."""
        statement_id, _ = await _seed_parent(engine)
        factory = create_session_factory(engine)
        async with factory() as session:
            snapshots = await TransactionCreationService(session).create_many(
                [_service_item(statement_id)]
            )
        snapshot = snapshots[0]
        assert snapshot.category is None
        assert snapshot.category_id is None
        assert snapshot.low_confidence is True

    async def test_unknown_category_not_found_without_writes(self, engine: AsyncEngine) -> None:
        """An unknown category UUID is a 404 at its index and persists nothing."""
        statement_id, _ = await _seed_parent(engine)
        items = [
            _service_item(statement_id),
            _service_item(statement_id, category_id=str(uuid.uuid4())),
        ]
        await _expect_plan_error(engine, items, "category_not_found", 1, field="category_id")
        assert await _table_count(engine, Statement) == 1
        assert await _table_count(engine, Transaction) == 0

    @pytest.mark.parametrize(
        ("item_currency", "card_currency", "expected"),
        [
            ("CLP", "CLP", None),
            ("USD", "USD", None),
            ("EUR", "CLP", "unsupported_currency"),
            ("clp", "CLP", "unsupported_currency"),
            ("CLP", "USD", "currency_mismatch"),
            ("USD", "CLP", "currency_mismatch"),
        ],
    )
    async def test_currency_policy(
        self, engine: AsyncEngine, item_currency: str, card_currency: str, expected: str | None
    ) -> None:
        """Supported exact codes must match the parent card's currency."""
        statement_id, _ = await _seed_parent(engine, currency=card_currency)
        items = [_service_item(statement_id, currency=item_currency)]
        if expected is None:
            factory = create_session_factory(engine)
            async with factory() as session:
                snapshots = await TransactionCreationService(session).create_many(items)
            assert snapshots[0].currency == item_currency
        else:
            await _expect_plan_error(engine, items, expected, 0, field="currency")
            assert await _table_count(engine, Transaction) == 0

    async def test_inactive_card_and_failed_parent_accepted(self, engine: AsyncEngine) -> None:
        """Existing parents are accepted regardless of card activity or status."""
        statement_id, _ = await _seed_parent(
            engine,
            status=StatementStatus.FAILED,
            error_message="extract failed",
            is_active=False,
        )
        factory = create_session_factory(engine)
        async with factory() as session:
            snapshots = await TransactionCreationService(session).create_many(
                [_service_item(statement_id)]
            )
        assert snapshots[0].statement_id == statement_id
        factory = create_session_factory(engine)
        async with factory() as session:
            statement = await session.get(Statement, statement_id)
            assert statement is not None
            assert statement.status == StatementStatus.FAILED
            assert statement.error_message == "extract failed"

    async def test_out_of_period_transaction_accepted(self, engine: AsyncEngine) -> None:
        """Transaction dates outside the billing period stay accepted."""
        statement_id, _ = await _seed_parent(engine)
        factory = create_session_factory(engine)
        async with factory() as session:
            snapshots = await TransactionCreationService(session).create_many(
                [_service_item(statement_id, date="2026-08-01")]
            )
        assert snapshots[0].date == date(2026, 8, 1)

    async def test_batch_output_follows_input_order(self, engine: AsyncEngine) -> None:
        """Responses keep input order despite reversed dates and two parents."""
        existing_id, card_id = await _seed_parent(engine)
        await _seed_categories(engine)
        new_id = uuid.uuid4()
        items = [
            _service_item(existing_id, date="2026-09-20", description="SECOND"),
            _service_item(
                new_id, metadata=_metadata_for(card_id), date="2026-09-10", description="FIRST-NEW"
            ),
            _service_item(existing_id, date="2026-09-05", description="OLDEST"),
            _service_item(new_id, description="FOURTH"),
        ]
        factory = create_session_factory(engine)
        async with factory() as session:
            snapshots = await TransactionCreationService(session).create_many(items)
        assert [snapshot.description for snapshot in snapshots] == [
            "SECOND",
            "FIRST-NEW",
            "OLDEST",
            "FOURTH",
        ]
        assert snapshots[1].statement_id == new_id
        assert snapshots[3].statement_id == new_id

    async def test_merchant_created_then_reused(self, engine: AsyncEngine) -> None:
        """Deterministic normalization creates the merchant once, then reuses it."""
        statement_id, _ = await _seed_parent(engine)
        factory = create_session_factory(engine)
        async with factory() as session:
            await TransactionCreationService(session).create_many(
                [_service_item(statement_id, description="MCDONALDS SUC 12")]
            )
            merchants_after_create = await _table_count(engine, Merchant)
            aliases_after_create = await _table_count(engine, MerchantAlias)
            await TransactionCreationService(session).create_many(
                [_service_item(statement_id, description="MCDONALDS SUC 12")]
            )
        assert merchants_after_create == 1
        assert aliases_after_create == 1
        # Merchant linkage is verified at the database level: the response
        # does not expose merchant_id, so reload the rows.
        factory = create_session_factory(engine)
        async with factory() as session:
            result = await session.execute(select(Transaction))
            rows = list(result.scalars().all())
            assert len(rows) == 2
            assert all(row.merchant_id is not None for row in rows)
            assert len({row.merchant_id for row in rows}) == 1
        assert await _table_count(engine, Merchant) == 1
        assert await _table_count(engine, MerchantAlias) == 1

    @pytest.mark.parametrize(
        ("description", "canonical_len"),
        [
            ("x" * 201, None),
            ("MCDONALDS SUC 12 " * 11, None),
        ],
    )
    async def test_long_description_skips_merchant_binding(
        self, engine: AsyncEngine, description: str, canonical_len: int | None
    ) -> None:
        """Oversized raw/canonical descriptions keep merchant NULL and full text."""
        statement_id, _ = await _seed_parent(engine)
        factory = create_session_factory(engine)
        async with factory() as session:
            snapshots = await TransactionCreationService(session).create_many(
                [_service_item(statement_id, description=description)]
            )
        assert snapshots[0].description == description
        assert await _table_count(engine, Merchant) == 0
        assert await _table_count(engine, MerchantAlias) == 0
        factory = create_session_factory(engine)
        async with factory() as session:
            result = await session.execute(select(Transaction))
            row = result.scalar_one()
            assert row.merchant_id is None
            assert row.description == description

    async def test_llm_path_never_invoked(
        self, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Creation never calls the optional LLM resolver, even on misses."""
        statement_id, _ = await _seed_parent(engine)

        def _explode(*args: object, **kwargs: object) -> None:
            raise AssertionError("resolve_merchant_with_llm must never be called")

        monkeypatch.setattr(MerchantNormalizer, "resolve_merchant_with_llm", _explode)
        factory = create_session_factory(engine)
        async with factory() as session:
            await TransactionCreationService(session).create_many(
                [_service_item(statement_id, description="ZARA STORE 7")]
            )
        factory = create_session_factory(engine)
        async with factory() as session:
            result = await session.execute(select(Transaction))
            row = result.scalar_one()
            assert row.merchant_id is not None  # deterministic auto-create bound it

    async def test_merchant_defaults_do_not_infer_category(self, engine: AsyncEngine) -> None:
        """A known merchant's default category never fills the transaction row."""
        statement_id, _ = await _seed_parent(engine)
        categories = await _seed_categories(engine)
        assert KNOWN_MERCHANT_PATTERNS["lider"] == "Groceries"
        factory = create_session_factory(engine)
        async with factory() as session:
            snapshots = await TransactionCreationService(session).create_many(
                [_service_item(statement_id, description="LIDER COM 3")]
            )
        assert snapshots[0].category is None
        assert snapshots[0].low_confidence is True
        factory = create_session_factory(engine)
        async with factory() as session:
            result = await session.execute(select(Transaction))
            row = result.scalar_one()
            assert row.merchant_id is not None
            merchant = await session.get(Merchant, row.merchant_id)
            assert merchant is not None
            assert merchant.default_category_id == categories["Groceries"]

    async def test_legacy_deprecation_logged_once_per_request(
        self, engine: AsyncEngine, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Legacy category use logs at most one warning; ID-only use logs none."""
        statement_id, _ = await _seed_parent(engine)
        with caplog.at_level(logging.WARNING, logger="app.services.transaction_creation"):
            factory = create_session_factory(engine)
            async with factory() as session:
                await TransactionCreationService(session).create_many(
                    [
                        _service_item(statement_id, category="Alpha"),
                        _service_item(statement_id, category="Beta"),
                    ]
                )
        legacy_records = [
            record for record in caplog.records if "deprecation" in record.getMessage()
        ]
        assert len(legacy_records) == 1

        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="app.services.transaction_creation"):
            factory = create_session_factory(engine)
            async with factory() as session:
                with pytest.raises(TransactionCreationError):
                    await TransactionCreationService(session).create_many(
                        [_service_item(statement_id, category_id=str(uuid.uuid4()))]
                    )
        assert caplog.records == []  # the ID path never logs, even on failure

    async def test_enrichment_failure_rolls_back_everything(
        self, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A late enrichment failure discards parents, merchants and aliases."""
        existing_id, card_id = await _seed_parent(engine)
        new_id = uuid.uuid4()
        items = [
            _service_item(new_id, metadata=_metadata_for(card_id), description="LIDER COM 3"),
            _service_item(existing_id, description="SECOND ROW"),
        ]

        normalizer = MerchantNormalizer()
        original = normalizer.resolve_merchant
        calls = {"count": 0}

        async def _fail_on_second(
            self: MerchantNormalizer,
            session: AsyncSession,
            description: str,
            categories_by_name: dict[str, Category],
        ) -> tuple[Merchant, bool]:
            calls["count"] += 1
            if calls["count"] == 2:
                raise RuntimeError("injected enrichment failure")
            return await original(session, description, categories_by_name)

        monkeypatch.setattr(MerchantNormalizer, "resolve_merchant", _fail_on_second)
        factory = create_session_factory(engine)
        async with factory() as session:
            with pytest.raises(RuntimeError, match="injected enrichment failure"):
                await TransactionCreationService(session).create_many(items)
        assert await _table_count(engine, Statement) == 1
        assert await _table_count(engine, Transaction) == 0
        assert await _table_count(engine, Merchant) == 0
        assert await _table_count(engine, MerchantAlias) == 0

    async def test_concurrent_parent_commit_conflicts_during_flush(
        self, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A committed concurrent winner maps to an atomic 409, with ID-only retry."""
        _, card_id = await _seed_parent(engine)
        new_id = uuid.uuid4()
        items = [_service_item(new_id, metadata=_metadata_for(card_id))]
        started = asyncio.Event()
        release = asyncio.Event()
        original = TransactionCreationService._fetch_statement_cards

        async def _hold(
            self: TransactionCreationService,
            statement_ids: set[uuid.UUID],
        ) -> dict[uuid.UUID, uuid.UUID]:
            found = await original(self, statement_ids)
            started.set()
            await release.wait()
            return found

        monkeypatch.setattr(TransactionCreationService, "_fetch_statement_cards", _hold)
        factory = create_session_factory(engine)
        async with factory() as session:
            task = asyncio.create_task(TransactionCreationService(session).create_many(items))
            await started.wait()
            # A concurrent winner commits the same UUID while the request is
            # between its planning read and the parent insert.
            async with factory() as winner_session:
                winner_session.add(
                    Statement(
                        id=new_id,
                        credit_card_id=card_id,
                        period_start=date(2026, 9, 1),
                        period_end=date(2026, 9, 30),
                        statement_date=date(2026, 10, 1),
                        file_path=None,
                        file_hash=None,
                        source=StatementSource.API,
                        status=StatementStatus.COMPLETED,
                    )
                )
                await winner_session.commit()
            release.set()
            with pytest.raises(TransactionCreationError) as excinfo:
                await task
        error = excinfo.value
        assert (error.code, error.field, error.index) == (
            "statement_creation_conflict",
            "statement_id",
            0,
        )

        # The loser persisted nothing; the winner is intact.
        assert await _table_count(engine, Statement) == 2
        assert await _table_count(engine, Transaction) == 0
        assert await _table_count(engine, Merchant) == 0
        assert await _table_count(engine, MerchantAlias) == 0

        # The explicit ID-only retry succeeds without touching the winner.
        async with factory() as session:
            snapshots = await TransactionCreationService(session).create_many(
                [_service_item(new_id)]
            )
        assert snapshots[0].statement_id == new_id
        assert await _table_count(engine, Statement) == 2
        assert await _table_count(engine, Transaction) == 1

    async def test_failure_after_new_statement_flush_rolls_back_parent(
        self, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failure after a new parent flush leaves no request-created rows durable."""
        _, card_id = await _seed_parent(engine)
        new_id = uuid.uuid4()
        description = "PR5 NEW PARENT FLUSH"
        items = [_service_item(new_id, metadata=_metadata_for(card_id), description=description)]
        original_flush = AsyncSession.flush
        failed = False

        async def _fail_after_parent_flush(
            self: AsyncSession, objects: object | None = None
        ) -> None:
            nonlocal failed
            pending_statement = any(
                isinstance(obj, Statement) and obj.id == new_id for obj in self.sync_session.new
            )
            await original_flush(self, objects)
            if pending_statement and not failed:
                failed = True
                raise RuntimeError("injected failure after statement flush")

        monkeypatch.setattr(AsyncSession, "flush", _fail_after_parent_flush)
        factory = create_session_factory(engine)
        async with factory() as session:
            with pytest.raises(RuntimeError, match="after statement flush"):
                await TransactionCreationService(session).create_many(items)

        await _assert_no_request_created_rows(
            engine,
            statement_ids={new_id},
            descriptions={description},
            merchant_names={normalize(description)},
        )
        assert await _table_count(engine, Statement) == 1  # pre-existing seed only

    async def test_failure_after_merchant_alias_writes_rolls_back_all_request_rows(
        self, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Merchant and alias rows are not durable when a later item fails."""
        existing_id, card_id = await _seed_parent(engine)
        new_id = uuid.uuid4()
        first_description = "PR5 MERCHANT ALIAS WRITTEN"
        second_description = "PR5 FAIL AFTER ALIAS"
        items = [
            _service_item(new_id, metadata=_metadata_for(card_id), description=first_description),
            _service_item(existing_id, description=second_description),
        ]
        original = MerchantNormalizer.resolve_merchant
        calls = 0

        async def _fail_after_first_alias(
            self: MerchantNormalizer,
            session: AsyncSession,
            description: str,
            categories_by_name: dict[str, Category],
        ) -> tuple[Merchant, bool]:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("injected failure after alias write")
            return await original(self, session, description, categories_by_name)

        monkeypatch.setattr(MerchantNormalizer, "resolve_merchant", _fail_after_first_alias)
        factory = create_session_factory(engine)
        async with factory() as session:
            with pytest.raises(RuntimeError, match="after alias write"):
                await TransactionCreationService(session).create_many(items)

        await _assert_no_request_created_rows(
            engine,
            statement_ids={new_id},
            descriptions={first_description, second_description},
            merchant_names={normalize(first_description), normalize(second_description)},
        )
        assert await _table_count(engine, Statement) == 1

    async def test_failure_during_transaction_flush_rolls_back_parent_and_merchants(
        self, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A transaction flush failure rolls back earlier parent/merchant writes."""
        _, card_id = await _seed_parent(engine)
        new_id = uuid.uuid4()
        description = "PR5 TRANSACTION FLUSH"
        items = [_service_item(new_id, metadata=_metadata_for(card_id), description=description)]
        original_flush = AsyncSession.flush

        async def _fail_transaction_flush(
            self: AsyncSession, objects: object | None = None
        ) -> None:
            if any(isinstance(obj, Transaction) for obj in self.sync_session.new):
                raise RuntimeError("injected transaction flush failure")
            await original_flush(self, objects)

        monkeypatch.setattr(AsyncSession, "flush", _fail_transaction_flush)
        factory = create_session_factory(engine)
        async with factory() as session:
            with pytest.raises(RuntimeError, match="transaction flush failure"):
                await TransactionCreationService(session).create_many(items)

        await _assert_no_request_created_rows(
            engine,
            statement_ids={new_id},
            descriptions={description},
            merchant_names={normalize(description)},
        )
        assert await _table_count(engine, Statement) == 1

    async def test_response_snapshot_validation_failure_rolls_back_flushed_rows(
        self, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Snapshot validation happens before commit and can still abort atomically."""
        _, card_id = await _seed_parent(engine)
        new_id = uuid.uuid4()
        description = "PR5 SNAPSHOT VALIDATION"
        items = [_service_item(new_id, metadata=_metadata_for(card_id), description=description)]

        def _fail_snapshot(_row: object) -> TransactionResponse:
            raise ValueError("injected snapshot validation failure")

        monkeypatch.setattr(TransactionResponse, "model_validate", staticmethod(_fail_snapshot))
        factory = create_session_factory(engine)
        async with factory() as session:
            with pytest.raises(ValueError, match="snapshot validation failure"):
                await TransactionCreationService(session).create_many(items)

        await _assert_no_request_created_rows(
            engine,
            statement_ids={new_id},
            descriptions={description},
            merchant_names={normalize(description)},
        )
        assert await _table_count(engine, Statement) == 1

    async def test_before_commit_failure_rolls_back_all_flushed_rows(
        self, engine: AsyncEngine
    ) -> None:
        """A failure immediately before commit leaves no request-created rows durable."""
        _, card_id = await _seed_parent(engine)
        new_id = uuid.uuid4()
        description = "PR5 BEFORE COMMIT"
        items = [_service_item(new_id, metadata=_metadata_for(card_id), description=description)]
        factory = create_session_factory(engine)
        async with factory() as session:

            def _raise_before_commit(_sync_session: object) -> None:
                raise RuntimeError("injected before commit failure")

            event.listen(session.sync_session, "before_commit", _raise_before_commit)
            with pytest.raises(RuntimeError, match="before commit failure"):
                await TransactionCreationService(session).create_many(items)

        await _assert_no_request_created_rows(
            engine,
            statement_ids={new_id},
            descriptions={description},
            merchant_names={normalize(description)},
        )
        assert await _table_count(engine, Statement) == 1

    @pytest.mark.parametrize("different_metadata", [False, True])
    async def test_two_sessions_racing_same_new_statement_conflict_atomically(
        self,
        engine: AsyncEngine,
        monkeypatch: pytest.MonkeyPatch,
        different_metadata: bool,
    ) -> None:
        """Two real sessions both observe absence; one wins and the loser gets atomic 409."""
        _, card_id = await _seed_parent(engine)
        new_id = uuid.uuid4()
        winner_or_loser_descriptions = [
            f"PR5 RACE {'DIFF' if different_metadata else 'MATCH'} A",
            f"PR5 RACE {'DIFF' if different_metadata else 'MATCH'} B",
        ]
        first_metadata = _metadata_for(card_id)
        second_metadata = (
            _metadata_for(card_id, statement_date="2026-10-02")
            if different_metadata
            else dict(first_metadata)
        )
        items_by_request = [
            [
                _service_item(
                    new_id, metadata=first_metadata, description=winner_or_loser_descriptions[0]
                )
            ],
            [
                _service_item(
                    new_id, metadata=second_metadata, description=winner_or_loser_descriptions[1]
                )
            ],
        ]
        observed_absence: list[dict[uuid.UUID, uuid.UUID]] = []
        both_read = asyncio.Event()
        release_inserts = asyncio.Event()
        observation_lock = asyncio.Lock()
        original = TransactionCreationService._fetch_statement_cards

        async def _barrier_after_absence_read(
            self: TransactionCreationService,
            statement_ids: set[uuid.UUID],
        ) -> dict[uuid.UUID, uuid.UUID]:
            found = await original(self, statement_ids)
            async with observation_lock:
                observed_absence.append(found)
                if len(observed_absence) == 2:
                    both_read.set()
            await both_read.wait()
            await release_inserts.wait()
            return found

        monkeypatch.setattr(
            TransactionCreationService, "_fetch_statement_cards", _barrier_after_absence_read
        )
        factory = create_session_factory(engine)

        async def _create(
            index: int,
        ) -> tuple[int, list[TransactionResponse] | TransactionCreationError]:
            async with factory() as session:
                try:
                    return index, await TransactionCreationService(session).create_many(
                        items_by_request[index]
                    )
                except TransactionCreationError as exc:
                    return index, exc

        tasks = [asyncio.create_task(_create(0)), asyncio.create_task(_create(1))]
        await both_read.wait()
        assert observed_absence == [{}, {}]
        release_inserts.set()
        results = await asyncio.gather(*tasks)

        successes = [(index, result) for index, result in results if isinstance(result, list)]
        conflicts = [
            (index, result)
            for index, result in results
            if isinstance(result, TransactionCreationError)
        ]
        assert len(successes) == 1
        assert len(conflicts) == 1
        success_index, success_rows = successes[0]
        conflict_index, conflict = conflicts[0]
        assert success_rows[0].statement_id == new_id
        assert success_rows[0].description == winner_or_loser_descriptions[success_index]
        assert (conflict.code, conflict.field, conflict.index) == (
            "statement_creation_conflict",
            "statement_id",
            0,
        )

        # Before retry, the losing request owns no durable rows; the winner remains intact.
        assert await _table_count(engine, Statement) == 2
        assert await _description_count(engine, winner_or_loser_descriptions[success_index]) == 1
        assert await _description_count(engine, winner_or_loser_descriptions[conflict_index]) == 0

        # Retrying explicitly with ID-only linkage succeeds; resending metadata remains a 409.
        async with factory() as session:
            retry_rows = await TransactionCreationService(session).create_many(
                [_service_item(new_id, description=winner_or_loser_descriptions[conflict_index])]
            )
        assert retry_rows[0].statement_id == new_id
        assert retry_rows[0].description == winner_or_loser_descriptions[conflict_index]
        assert await _table_count(engine, Statement) == 2
        assert await _description_count(engine, winner_or_loser_descriptions[conflict_index]) == 1

        async with factory() as session:
            with pytest.raises(TransactionCreationError) as retry_with_metadata:
                await TransactionCreationService(session).create_many(
                    items_by_request[conflict_index]
                )
        assert retry_with_metadata.value.code == "statement_already_exists"

    async def test_reversed_multi_parent_conflict_rolls_back_earlier_loser_parent(
        self, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A later parent conflict rolls back earlier flushed loser-owned parents."""
        _, card_id = await _seed_parent(engine)
        earlier_new_id = uuid.UUID("11111111-1111-4111-8111-111111111111")
        contested_id = uuid.UUID("22222222-2222-4222-8222-222222222222")
        items = [
            _service_item(
                contested_id,
                metadata=_metadata_for(card_id),
                description="PR5 CONFLICT CONTESTED",
            ),
            _service_item(
                earlier_new_id,
                metadata=_metadata_for(card_id, statement_date="2026-10-02"),
                description="PR5 CONFLICT EARLIER PARENT",
            ),
        ]
        started = asyncio.Event()
        release = asyncio.Event()
        original = TransactionCreationService._fetch_statement_cards

        async def _hold_after_absence_read(
            self: TransactionCreationService,
            statement_ids: set[uuid.UUID],
        ) -> dict[uuid.UUID, uuid.UUID]:
            found = await original(self, statement_ids)
            started.set()
            await release.wait()
            return found

        monkeypatch.setattr(
            TransactionCreationService, "_fetch_statement_cards", _hold_after_absence_read
        )
        factory = create_session_factory(engine)
        async with factory() as loser_session:
            task = asyncio.create_task(TransactionCreationService(loser_session).create_many(items))
            await started.wait()
            async with factory() as winner_session:
                winner_session.add(
                    Statement(
                        id=contested_id,
                        credit_card_id=card_id,
                        period_start=date(2026, 9, 1),
                        period_end=date(2026, 9, 30),
                        statement_date=date(2026, 10, 1),
                        file_path=None,
                        file_hash=None,
                        source=StatementSource.API,
                        status=StatementStatus.COMPLETED,
                    )
                )
                await winner_session.commit()
            release.set()
            with pytest.raises(TransactionCreationError) as excinfo:
                await task

        assert (excinfo.value.code, excinfo.value.field, excinfo.value.index) == (
            "statement_creation_conflict",
            "statement_id",
            0,
        )
        async with factory() as session:
            assert await session.get(Statement, contested_id) is not None
            assert await session.get(Statement, earlier_new_id) is None
        await _assert_no_request_created_rows(
            engine,
            statement_ids={earlier_new_id},
            descriptions={"PR5 CONFLICT CONTESTED", "PR5 CONFLICT EARLIER PARENT"},
            merchant_names={
                normalize("PR5 CONFLICT CONTESTED"),
                normalize("PR5 CONFLICT EARLIER PARENT"),
            },
        )

    async def test_winner_rollback_allows_waiting_request_to_create_parent(
        self, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If the first inserter rolls back, the waiting request can succeed."""
        _, card_id = await _seed_parent(engine)
        contested_id = uuid.uuid4()
        first_inserted = asyncio.Event()
        release_first = asyncio.Event()
        calls = 0
        call_lock = asyncio.Lock()
        original = TransactionCreationService._create_new_statements

        async def _first_inserter_rolls_back(
            self: TransactionCreationService,
            plans: dict[uuid.UUID, ParentPlan],
        ) -> None:
            nonlocal calls
            async with call_lock:
                calls += 1
                call_number = calls
            if call_number == 1:
                await original(self, plans)
                first_inserted.set()
                await release_first.wait()
                raise RuntimeError("injected first-inserter rollback")
            await first_inserted.wait()
            await original(self, plans)

        monkeypatch.setattr(
            TransactionCreationService, "_create_new_statements", _first_inserter_rolls_back
        )
        factory = create_session_factory(engine)
        first_items = [
            _service_item(
                contested_id,
                metadata=_metadata_for(card_id),
                description="PR5 ROLLBACK FIRST",
            )
        ]
        second_items = [
            _service_item(
                contested_id,
                metadata=_metadata_for(card_id),
                description="PR5 ROLLBACK SECOND",
            )
        ]

        async def _create(items: list[TransactionCreate]) -> list[TransactionResponse]:
            async with factory() as session:
                return await TransactionCreationService(session).create_many(items)

        first_task = asyncio.create_task(_create(first_items))
        await first_inserted.wait()
        second_task = asyncio.create_task(_create(second_items))
        await asyncio.sleep(0)
        release_first.set()

        with pytest.raises(RuntimeError, match="first-inserter rollback"):
            await first_task
        second_rows = await second_task
        assert second_rows[0].statement_id == contested_id
        assert second_rows[0].description == "PR5 ROLLBACK SECOND"
        assert await _description_count(engine, "PR5 ROLLBACK FIRST") == 0
        assert await _description_count(engine, "PR5 ROLLBACK SECOND") == 1
        assert await _table_count(engine, Statement) == 2

    async def test_statement_pk_discriminator(self) -> None:
        """Only a 23505 on ``pk_statements`` classifies as the parent conflict."""
        from app.services.transaction_creation import _is_statement_pk_violation

        class _DiagInfo:
            def __init__(self, sqlstate: str | None, constraint_name: str | None) -> None:
                self.sqlstate = sqlstate
                self.constraint_name = constraint_name
                self.__cause__ = None

        @dataclass
        class _Case:
            sqlstate: str | None
            constraint: str | None
            expected: bool

        cases = [
            _Case("23505", "pk_statements", True),
            _Case("23505", "uq_statements_credit_card_id_file_hash", False),
            _Case("23503", "pk_statements", False),
            _Case(None, None, False),
        ]
        for case in cases:
            exc = IntegrityError(
                "INSERT INTO statements ...", None, _DiagInfo(case.sqlstate, case.constraint)
            )
            assert _is_statement_pk_violation(exc) is case.expected
