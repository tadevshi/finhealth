"""Health check endpoint.

Exposes a single ``GET /api/v1/health`` route that reports service
liveness and the most recent database round-trip status. The endpoint
is intentionally cheap: a single ``SELECT 1`` is enough to prove the
async SQLAlchemy stack and SQLite file are both reachable.
"""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app import __version__
from app.db.session import get_session
from app.schemas.health import HealthResponse

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Service liveness and database reachability probe",
    responses={
        status.HTTP_200_OK: {
            "description": "Service is up and the database responded to a probe query.",
            "model": HealthResponse,
        },
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "description": "Service is up but the database did not respond.",
            "model": HealthResponse,
        },
    },
)
async def health_check(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> JSONResponse:
    """Return service health, including a live database round-trip.

    On a successful round-trip the response is ``200`` with
    ``database: "ok"``. If the query raises — for example because the
    database file is missing, locked, or the engine cannot connect —
    the response is ``503`` with ``database: "error"`` and the
    original exception is logged. The ``status`` field is always
    ``"ok"`` whenever the endpoint responds: the service is up by
    definition; only the database sub-system can be down.
    """
    try:
        await session.execute(text("SELECT 1"))
    except Exception as exc:
        logger.exception("Health check database query failed: %s", exc)
        payload = HealthResponse(status="ok", database="error", version=__version__)
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content=payload.model_dump(),
        )

    payload = HealthResponse(status="ok", database="ok", version=__version__)
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content=payload.model_dump(),
    )
