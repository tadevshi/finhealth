"""Aggregated API v1 router.

Mounts every v1 endpoint module under a single :class:`APIRouter` so
the app factory only needs to include one object. New endpoint
modules should be added here (and only here) when the API surface
grows.
"""

from fastapi import APIRouter

from app.api.v1.health import router as health_router

api_v1_router: APIRouter = APIRouter()
# Mypy cannot resolve FastAPI's ``Annotated[ForwardRef("APIRouter"), ...]``
# parameter type on ``APIRouter.include_router`` in nested module
# contexts. The runtime value is unambiguously an ``APIRouter`` and
# the call is exercised by the integration tests, so the silence is
# safe and well-scoped.
api_v1_router.include_router(health_router)  # type: ignore[has-type]

__all__ = ["api_v1_router"]
