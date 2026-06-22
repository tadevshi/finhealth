"""Aggregated API v1 router.

Mounts every v1 endpoint module under a single :class:`APIRouter` so
the app factory only needs to include one object. New endpoint
modules should be added here (and only here) when the API surface
grows.

Endpoint modules
----------------

* :mod:`app.api.v1.banks` — read-only bank list (Phase 1, WU 5).
* :mod:`app.api.v1.health` — liveness probe.
* :mod:`app.api.v1.statements` — statement upload and lookup
  (Phase 1, WU 4).
* :mod:`app.api.v1.transactions` — transaction listing and
  category editing (Phase 1, WU 4).
"""

from fastapi import APIRouter

from app.api.v1.banks import router as banks_router
from app.api.v1.health import router as health_router
from app.api.v1.statements import router as statements_router
from app.api.v1.transactions import router as transactions_router

api_v1_router: APIRouter = APIRouter()
# ``health_router`` does not carry an explicit ``APIRouter``
# annotation (the original module relied on a ``# type: ignore``
# on the assignment). The new routers do, so they are
# type-clean; the health ignore is the only one needed.
api_v1_router.include_router(health_router)  # type: ignore[has-type]
api_v1_router.include_router(banks_router)
api_v1_router.include_router(statements_router)
api_v1_router.include_router(transactions_router)

__all__ = ["api_v1_router"]
