"""Integration tests for the health check endpoint.

The tests run against an in-process FastAPI app built with the
``client`` fixture from :mod:`tests.conftest`. The app talks to a
throwaway SQLite file, so the suite stays hermetic and parallel-safe.
"""

from collections.abc import AsyncGenerator

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app import __version__
from app.db.session import get_session
from app.schemas.health import HealthResponse

HEALTH_PATH = "/api/v1/health"


@pytest.mark.asyncio
async def test_health_returns_ok_with_db(client: AsyncClient) -> None:
    """A healthy database yields 200 with ``status=ok`` and ``database=ok``."""
    response = await client.get(HEALTH_PATH)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"
    assert body["version"] == __version__

    # Round-trip through the schema to confirm the response matches
    # the published contract exactly.
    parsed = HealthResponse.model_validate(body)
    assert parsed.status == "ok"
    assert parsed.database == "ok"
    assert parsed.version == __version__


@pytest.mark.asyncio
async def test_health_executes_select_one(client: AsyncClient) -> None:
    """A second consecutive call also succeeds — no leaked connection state.

    A real ``SELECT 1`` round-trip on every request is the health
    probe's whole purpose; two consecutive 200s prove the
    per-request engine is created, used, and disposed cleanly.
    """
    first = await client.get(HEALTH_PATH)
    second = await client.get(HEALTH_PATH)

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["database"] == "ok"
    assert second.json()["database"] == "ok"


@pytest.mark.asyncio
async def test_health_response_schema_contract(client: AsyncClient) -> None:
    """The response is JSON, content-type ``application/json``, and the schema validates."""
    response = await client.get(HEALTH_PATH)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")

    # Strict validation against the Pydantic schema — extra fields,
    # wrong types, or missing keys all fail.
    HealthResponse.model_validate(response.json())


@pytest.mark.asyncio
async def test_health_is_listed_in_openapi(client: AsyncClient) -> None:
    """The health route is exposed in the auto-generated OpenAPI document."""
    response = await client.get("/openapi.json")

    assert response.status_code == 200
    spec = response.json()
    assert HEALTH_PATH in spec["paths"]
    assert "get" in spec["paths"][HEALTH_PATH]
    operation = spec["paths"][HEALTH_PATH]["get"]
    assert "health" in operation["tags"]
    assert operation["summary"]  # non-empty


@pytest.mark.asyncio
async def test_cors_headers_present_on_health_response(client: AsyncClient) -> None:
    """CORS middleware is wired and surfaces an ``Access-Control-Allow-Origin`` header.

    The test asks for the configured origin directly; FastAPI's
    ``CORSMiddleware`` reflects the request ``Origin`` only when
    that origin is in the allow-list (which ``test_settings``
    configures with ``http://testserver``).
    """
    response = await client.get(
        HEALTH_PATH,
        headers={"Origin": "http://testserver"},
    )

    assert response.status_code == 200
    assert response.headers.get("access-control-allow-origin") == "http://testserver"


@pytest.mark.asyncio
async def test_health_preflight_returns_cors_headers(client: AsyncClient) -> None:
    """An OPTIONS preflight from an allowed origin returns the CORS allow headers."""
    response = await client.options(
        HEALTH_PATH,
        headers={
            "Origin": "http://testserver",
            "Access-Control-Request-Method": "GET",
        },
    )

    assert response.status_code in (200, 204)
    assert response.headers.get("access-control-allow-origin") == "http://testserver"
    allowed_methods = response.headers.get("access-control-allow-methods", "")
    assert "GET" in allowed_methods.upper()


class _FailingSession:
    """Stub session that raises on ``execute`` to simulate a DB outage."""

    async def execute(self, *_args: object, **_kwargs: object) -> object:
        """Always raise a ``RuntimeError`` mimicking a broken DB driver."""
        raise RuntimeError("simulated DB outage")

    async def commit(self) -> None:
        """No-op — stub sessions never reach a real transaction."""

    async def rollback(self) -> None:
        """No-op — stub sessions never reach a real transaction."""

    async def close(self) -> None:
        """No-op — stub sessions own no resources."""


@pytest.fixture
def client_with_failing_db(
    client: AsyncClient,
) -> AsyncGenerator[AsyncClient, None]:
    """Yield a client whose ``get_session`` always returns a failing session.

    Used to exercise the ``database: "error"`` branch of the health
    endpoint. The dependency override is removed after the test so
    the cached ``client`` fixture is not mutated for downstream
    users.
    """
    from fastapi import FastAPI

    app = client._transport.app  # type: ignore[attr-defined]
    assert isinstance(app, FastAPI), "ASGITransport app must be a FastAPI instance"

    async def _failing_dependency() -> AsyncGenerator[AsyncSession, None]:
        yield _FailingSession()  # type: ignore[return-value,misc]

    app.dependency_overrides[get_session] = _failing_dependency
    try:
        yield client
    finally:
        app.dependency_overrides.pop(get_session, None)


@pytest.mark.asyncio
async def test_health_returns_503_when_db_fails(
    client_with_failing_db: AsyncClient,
) -> None:
    """A failing database probe yields 503 with ``database: "error"``.

    The ``status`` field stays ``"ok"`` (the service is up; only the
    database sub-system is down). ``version`` still reflects
    :data:`app.__version__` so client-side compatibility checks are
    not affected by the outage.
    """
    response = await client_with_failing_db.get(HEALTH_PATH)

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "ok"
    assert body["database"] == "error"
    assert body["version"] == __version__
