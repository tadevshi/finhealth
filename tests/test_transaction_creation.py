"""PR3 tests for ``add-transaction-creation-api``.

Covers the closed creation schemas (``StatementMetadataCreate``, the
extended ``TransactionCreate``, bounded ``TransactionBatchCreate`` and
``TransactionBatchResponse``) and the request-wide parent-planning half
of ``TransactionCreationService``. Persistence and HTTP routes are later
chain slices: after planning succeeds the service stops at an explicit
``NotImplementedError`` boundary and never writes.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db.session import create_session_factory
from app.models import Bank, CreditCard, Statement, Transaction
from app.schemas import (
    StatementMetadataCreate,
    TransactionBatchCreate,
    TransactionBatchResponse,
    TransactionCreate,
    TransactionResponse,
)
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


async def _seed_parent(engine: AsyncEngine, currency: str = "CLP") -> tuple[uuid.UUID, uuid.UUID]:
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
        )
        statement = Statement(
            credit_card=card,
            period_start=date(2026, 9, 1),
            period_end=date(2026, 9, 30),
            statement_date=date(2026, 10, 1),
            file_path="planning/test.pdf",
            file_hash="b" * 64,
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

    async def test_create_many_stops_at_persistence_boundary(self, engine: AsyncEngine) -> None:
        """A fully valid plan reaches the explicit PR4 persistence boundary."""
        statement_id, card_id = await _seed_parent(engine)
        new_id = uuid.uuid4()
        items = [
            _service_item(statement_id),
            _service_item(new_id, metadata=_metadata_for(card_id)),
        ]
        before = await _table_count(engine, Statement)
        factory = create_session_factory(engine)
        async with factory() as session:
            with pytest.raises(NotImplementedError):
                await TransactionCreationService(session).create_many(items)
        assert await _table_count(engine, Statement) == before
        assert await _table_count(engine, Transaction) == 0


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
