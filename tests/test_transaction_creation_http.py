"""HTTP tests for the transaction-creation routes (PR4).

Covers ``POST /api/v1/transactions`` and ``POST /api/v1/transactions/batch``:

* existing-parent (ID-only) and new-parent (nested metadata) 201s;
* mixed batches, input-order responses and batch counts;
* 1-200 bounds, duplicate-looking rows and repeated submissions;
* indexed domain errors (422/409/404/400) and generic 500 bodies;
* statement-level provenance for API-created and appended PDF parents.

Every test runs against the disposable PostgreSQL database from
``tests.conftest`` (``client`` fixture); durable-state assertions use an
independent session.
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db.engine import create_engine
from app.db.session import create_session_factory
from app.main import create_app
from app.models import Statement, Transaction
from app.services.transaction_creation import TransactionCreationError

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


# ---------------------------------------------------------------------------
# Seeding helpers (independent engine per call)
# ---------------------------------------------------------------------------


async def _seed_statement(
    test_settings: object,
    currency: str = "CLP",
) -> tuple[uuid.UUID, uuid.UUID]:
    """Seed one bank/card/statement parent; return (statement_id, card_id)."""
    engine = create_engine(test_settings.database_url)  # type: ignore[attr-defined]
    try:
        factory = create_session_factory(engine)
        async with factory() as session:
            from app.models import Bank, CreditCard

            bank = Bank(
                name="http_creation_bank",
                display_name="HTTP Creation Bank",
                password_formula="rut_sin_dv",
            )
            card = CreditCard(
                bank=bank,
                card_number_masked="XXXX XXXX XXXX 4242",
                cardholder="HTTP USER",
                currency=currency,
            )
            statement = Statement(
                credit_card=card,
                period_start=date(2026, 9, 1),
                period_end=date(2026, 9, 30),
                statement_date=date(2026, 10, 1),
                file_path="http/test.pdf",
                file_hash="a" * 64,
            )
            session.add_all([bank, card, statement])
            await session.commit()
            return statement.id, card.id
    finally:
        await engine.dispose()


async def _seed_categories(test_settings: object) -> dict[str, uuid.UUID]:
    """Seed two closed-set categories; return ``name -> id``."""
    engine = create_engine(test_settings.database_url)  # type: ignore[attr-defined]
    try:
        factory = create_session_factory(engine)
        async with factory() as session:
            from app.models import Category

            rows = [
                Category(name="Groceries", display_name="Groceries", sort_order=2),
                Category(name="Dining Out", display_name="Dining Out", sort_order=1),
            ]
            session.add_all(rows)
            await session.commit()
            return {row.name: row.id for row in rows}
    finally:
        await engine.dispose()


def _state_engine(test_settings: object) -> AsyncEngine:
    """A fresh engine for durable-state assertions."""
    return create_engine(test_settings.database_url)  # type: ignore[attr-defined]


async def _table_count(engine: AsyncEngine, model: type) -> int:
    """Count rows of ``model`` in a fresh session."""
    factory = create_session_factory(engine)
    async with factory() as session:
        result = await session.execute(select(func.count()).select_from(model))
        return int(result.scalar_one())


def _detail(response_json: dict[str, object]) -> dict[str, object]:
    """The domain error envelope carried in ``detail``."""
    detail = response_json["detail"]
    assert isinstance(detail, dict)
    return detail


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


class TestCreationRoutes:
    async def test_single_existing_parent_returns_201(
        self, client: AsyncClient, test_settings: object
    ) -> None:
        """ID-only linkage to an existing parent returns 201 and is visible via GET."""
        statement_id, _ = await _seed_statement(test_settings)
        response = await client.post(
            "/api/v1/transactions", json=_tx_payload(statement_id=str(statement_id))
        )
        assert response.status_code == 201
        body = response.json()
        assert body["statement_id"] == str(statement_id)
        assert body["description"] == "LIDER COM 3"
        assert body["amount"] == "12500.00"
        assert body["currency"] == "CLP"
        assert body["category"] is None
        assert body["category_id"] is None
        assert body["low_confidence"] is True
        assert body["installment_number"] is None
        uuid.UUID(body["id"])  # a populated response UUID

        listed = await client.get(f"/api/v1/transactions?statement_id={statement_id}")
        assert listed.status_code == 200
        assert len(listed.json()) == 1
        assert listed.json()[0]["id"] == body["id"]

    async def test_single_new_parent_returns_201(
        self, client: AsyncClient, test_settings: object
    ) -> None:
        """Nested metadata creates the parent under the caller UUID, readable via GET."""
        _, card_id = await _seed_statement(test_settings)
        new_id = uuid.uuid4()
        payload = _tx_payload(
            statement_id=str(new_id),
            statement=_metadata(credit_card_id=str(card_id)),
        )
        response = await client.post("/api/v1/transactions", json=payload)
        assert response.status_code == 201
        assert response.json()["statement_id"] == str(new_id)

        statement_response = await client.get(f"/api/v1/statements/{new_id}")
        assert statement_response.status_code == 200
        statement_body = statement_response.json()
        assert statement_body["source"] == "api"
        assert statement_body["status"] == "completed"
        assert statement_body["file_path"] is None
        assert statement_body["file_hash"] is None
        assert statement_body["credit_card_id"] == str(card_id)
        assert [tx["description"] for tx in statement_body["transactions"]] == ["LIDER COM 3"]

    async def test_mixed_batch_returns_201_in_input_order(
        self, client: AsyncClient, test_settings: object
    ) -> None:
        """A mixed batch commits both parents and preserves input order."""
        statement_id, card_id = await _seed_statement(test_settings)
        new_id = uuid.uuid4()
        items = [
            _tx_payload(statement_id=str(statement_id), date="2026-09-20", description="SECOND"),
            _tx_payload(
                statement_id=str(new_id),
                statement=_metadata(credit_card_id=str(card_id)),
                date="2026-09-10",
                description="FIRST-NEW",
            ),
            _tx_payload(statement_id=str(statement_id), date="2026-09-05", description="OLDEST"),
            _tx_payload(statement_id=str(new_id), description="FOURTH"),
        ]
        response = await client.post("/api/v1/transactions/batch", json={"transactions": items})
        assert response.status_code == 201
        body = response.json()
        assert body["count"] == 4
        assert [tx["description"] for tx in body["transactions"]] == [
            "SECOND",
            "FIRST-NEW",
            "OLDEST",
            "FOURTH",
        ]
        assert body["transactions"][1]["statement_id"] == str(new_id)

        engine = _state_engine(test_settings)
        try:
            factory = create_session_factory(engine)
            async with factory() as session:
                result = await session.execute(
                    select(Transaction).where(Transaction.statement_id == new_id)
                )
                assert len(result.scalars().all()) == 2
        finally:
            await engine.dispose()

    @pytest.mark.parametrize("count", [1, 200])
    async def test_batch_bounds_success(
        self, client: AsyncClient, test_settings: object, count: int
    ) -> None:
        """Batches of 1 and 200 items commit with an accurate count."""
        statement_id, _ = await _seed_statement(test_settings)
        items = [
            _tx_payload(statement_id=str(statement_id), description=f"ROW {index}")
            for index in range(count)
        ]
        response = await client.post("/api/v1/transactions/batch", json={"transactions": items})
        assert response.status_code == 201
        assert response.json()["count"] == count
        assert len(response.json()["transactions"]) == count

    @pytest.mark.parametrize("count", [0, 201])
    async def test_batch_bounds_rejected_without_writes(
        self, client: AsyncClient, test_settings: object, count: int
    ) -> None:
        """0 and 201 items fail 422 and persist nothing."""
        statement_id, _ = await _seed_statement(test_settings)
        items = [
            _tx_payload(statement_id=str(statement_id), description=f"ROW {index}")
            for index in range(count)
        ]
        response = await client.post("/api/v1/transactions/batch", json={"transactions": items})
        assert response.status_code == 422
        errors = response.json()["detail"]
        assert any(tuple(error["loc"]) == ("body", "transactions") for error in errors), errors
        engine = _state_engine(test_settings)
        try:
            assert await _table_count(engine, Transaction) == 0
        finally:
            await engine.dispose()

    async def test_duplicate_looking_rows_and_retries_not_deduplicated(
        self, client: AsyncClient, test_settings: object
    ) -> None:
        """Identical items and repeated submissions append distinct rows."""
        statement_id, _ = await _seed_statement(test_settings)
        items = [_tx_payload(statement_id=str(statement_id)) for _ in range(3)]
        response = await client.post("/api/v1/transactions/batch", json={"transactions": items})
        assert response.status_code == 201
        ids = {tx["id"] for tx in response.json()["transactions"]}
        assert len(ids) == 3

        again = await client.post(
            "/api/v1/transactions", json=_tx_payload(statement_id=str(statement_id))
        )
        assert again.status_code == 201
        engine = _state_engine(test_settings)
        try:
            assert await _table_count(engine, Transaction) == 4
        finally:
            await engine.dispose()


# ---------------------------------------------------------------------------
# Domain error mapping
# ---------------------------------------------------------------------------


class TestCreationRouteErrors:
    @pytest.mark.parametrize("matching", [True, False])
    async def test_existing_parent_metadata_conflicts_409(
        self, client: AsyncClient, test_settings: object, matching: bool
    ) -> None:
        """Metadata for an existing parent is a 409 with no writes."""
        statement_id, card_id = await _seed_statement(test_settings)
        overrides: dict[str, object] = {} if matching else {"period_end": "2026-09-28"}
        payload = _tx_payload(
            statement_id=str(statement_id),
            statement=_metadata(credit_card_id=str(card_id), **overrides),
        )
        response = await client.post("/api/v1/transactions", json=payload)
        assert response.status_code == 409
        detail = _detail(response.json())
        assert detail["code"] == "statement_already_exists"
        assert detail["field"] == "statement"
        assert "index" not in detail  # single-route mapping omits the index

        engine = _state_engine(test_settings)
        try:
            assert await _table_count(engine, Statement) == 1
            assert await _table_count(engine, Transaction) == 0
        finally:
            await engine.dispose()

    async def test_missing_metadata_422(self, client: AsyncClient, test_settings: object) -> None:
        """A missing parent without metadata is a 422 at the first reference."""
        await _seed_statement(test_settings)
        response = await client.post(
            "/api/v1/transactions", json=_tx_payload(statement_id=str(uuid.uuid4()))
        )
        assert response.status_code == 422
        detail = _detail(response.json())
        assert detail["code"] == "statement_metadata_required"
        assert detail["field"] == "statement"
        assert "index" not in detail  # single-route mapping omits the index

    async def test_duplicate_metadata_422(self, client: AsyncClient, test_settings: object) -> None:
        """A second metadata object for one new UUID is a 422 at its index."""
        _, card_id = await _seed_statement(test_settings)
        new_id = uuid.uuid4()
        metadata = _metadata(credit_card_id=str(card_id))
        items = [
            _tx_payload(statement_id=str(new_id), statement=metadata),
            _tx_payload(statement_id=str(new_id)),
            _tx_payload(statement_id=str(new_id), statement=metadata),
        ]
        response = await client.post("/api/v1/transactions/batch", json={"transactions": items})
        assert response.status_code == 422
        detail = _detail(response.json())
        assert detail["code"] == "duplicate_statement_metadata"
        assert detail["index"] == 2

    async def test_unknown_new_parent_card_404(
        self, client: AsyncClient, test_settings: object
    ) -> None:
        """An unknown card in new-parent metadata is a 404 at the metadata index."""
        await _seed_statement(test_settings)
        response = await client.post(
            "/api/v1/transactions",
            json=_tx_payload(statement_id=str(uuid.uuid4()), statement=_metadata()),
        )
        assert response.status_code == 404
        detail = _detail(response.json())
        assert detail["code"] == "credit_card_not_found"
        assert detail["field"] == "statement.credit_card_id"
        assert "index" not in detail  # single-route mapping omits the index

    async def test_unknown_category_404_indexed(
        self, client: AsyncClient, test_settings: object
    ) -> None:
        """An unknown category UUID is a 404 with the item index and no writes."""
        statement_id, _ = await _seed_statement(test_settings)
        items = [
            _tx_payload(statement_id=str(statement_id)),
            _tx_payload(statement_id=str(statement_id), category_id=str(uuid.uuid4())),
        ]
        response = await client.post("/api/v1/transactions/batch", json={"transactions": items})
        assert response.status_code == 404
        detail = _detail(response.json())
        assert detail["code"] == "category_not_found"
        assert detail["field"] == "category_id"
        assert detail["index"] == 1

        engine = _state_engine(test_settings)
        try:
            assert await _table_count(engine, Transaction) == 0
        finally:
            await engine.dispose()

    async def test_currency_mismatch_400(self, client: AsyncClient, test_settings: object) -> None:
        """A currency mismatch with the parent card is a 400 business-rule error."""
        statement_id, _ = await _seed_statement(test_settings, currency="USD")
        response = await client.post(
            "/api/v1/transactions",
            json=_tx_payload(statement_id=str(statement_id), currency="CLP"),
        )
        assert response.status_code == 400
        detail = _detail(response.json())
        assert detail["code"] == "currency_mismatch"
        assert detail["field"] == "currency"
        assert "index" not in detail  # single-route mapping omits the index

    async def test_unsupported_currency_400(
        self, client: AsyncClient, test_settings: object
    ) -> None:
        """An unsupported three-character code is a 400 business-rule error."""
        statement_id, _ = await _seed_statement(test_settings)
        response = await client.post(
            "/api/v1/transactions",
            json=_tx_payload(statement_id=str(statement_id), currency="EUR"),
        )
        assert response.status_code == 400
        detail = _detail(response.json())
        assert detail["code"] == "unsupported_currency"
        assert "index" not in detail  # single-route mapping omits the index

    async def test_schema_validation_422_locations(
        self, client: AsyncClient, test_settings: object
    ) -> None:
        """Schema errors stay FastAPI-standard with indexed body locations."""
        statement_id, _ = await _seed_statement(test_settings)
        items = [
            _tx_payload(statement_id=str(statement_id)),
            _tx_payload(statement_id=str(statement_id), amount=12.5),
        ]
        response = await client.post("/api/v1/transactions/batch", json={"transactions": items})
        assert response.status_code == 422
        errors = response.json()["detail"]
        assert ("body", "transactions", 1, "amount") in [tuple(error["loc"]) for error in errors]

        unknown = await client.post(
            "/api/v1/transactions",
            json=_tx_payload(statement_id=str(statement_id), id="x"),
        )
        assert unknown.status_code == 422

    async def test_generic_500_body_without_partial_writes(
        self, client: AsyncClient, test_settings: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unexpected failure surfaces a generic 500 and persists nothing."""
        _, card_id = await _seed_statement(test_settings)
        new_id = uuid.uuid4()
        from app.api.v1 import transactions as transactions_module

        class _ExplodingService:
            def __init__(self, session: object) -> None:
                self._session = session

            async def create_many(self, items: list[object]) -> list[object]:
                raise RuntimeError("injected route failure")

        monkeypatch.setattr(transactions_module, "TransactionCreationService", _ExplodingService)
        app = create_app(test_settings)  # type: ignore[arg-type]
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://testserver") as c:
            items = [
                _tx_payload(
                    statement_id=str(new_id), statement=_metadata(credit_card_id=str(card_id))
                ),
            ]
            response = await c.post("/api/v1/transactions/batch", json={"transactions": items})
        assert response.status_code == 500

        engine = _state_engine(test_settings)
        try:
            assert await _table_count(engine, Statement) == 1
            assert await _table_count(engine, Transaction) == 0
        finally:
            await engine.dispose()

    async def test_creation_failed_envelope_500(
        self, test_settings: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A domain ``creation_failed`` maps to a 500 envelope without index."""
        from app.api.v1 import transactions as transactions_module

        class _FailingService:
            def __init__(self, session: object) -> None:
                self._session = session

            async def create_many(self, items: list[object]) -> list[object]:
                raise TransactionCreationError("creation_failed", "Transaction creation failed.")

        monkeypatch.setattr(transactions_module, "TransactionCreationService", _FailingService)
        app = create_app(test_settings)  # type: ignore[arg-type]
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://testserver") as c:
            response = await c.post(
                "/api/v1/transactions", json=_tx_payload(statement_id=str(uuid.uuid4()))
            )
        assert response.status_code == 500
        detail = _detail(response.json())
        assert detail["code"] == "creation_failed"
        assert "index" not in detail


# ---------------------------------------------------------------------------
# Provenance preservation
# ---------------------------------------------------------------------------


class TestStatementProvenancePreservation:
    async def test_api_append_keeps_pdf_statement_source(
        self, client: AsyncClient, test_settings: object
    ) -> None:
        """Appending to an existing PDF statement leaves source and files intact."""
        statement_id, _ = await _seed_statement(test_settings)
        response = await client.post(
            "/api/v1/transactions", json=_tx_payload(statement_id=str(statement_id))
        )
        assert response.status_code == 201

        statement_response = await client.get(f"/api/v1/statements/{statement_id}")
        body = statement_response.json()
        assert body["source"] == "pdf"
        assert body["file_path"] == "http/test.pdf"
        assert body["file_hash"] == "a" * 64
        assert len(body["transactions"]) == 1
