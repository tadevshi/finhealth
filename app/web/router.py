"""Web (HTML) routes.

Placeholder router for server-rendered pages. The current scope is
limited to a JSON acknowledgement at ``GET /``; the landing page
itself ships in Work Unit 5 (frontend) once Jinja2 templates and
HTMX/Alpine/Tailwind assets are wired up.
"""

from fastapi import APIRouter

web_router = APIRouter(tags=["web"])


@web_router.get("/", summary="Service liveness for the web root")
async def index() -> dict[str, str]:
    """Return a tiny JSON acknowledgement.

    Kept as JSON (not a template) until WU 5 introduces the base
    template. The response shape is stable so external probes and
    integration tests can rely on it.
    """
    return {"message": "finhealth"}


__all__ = ["web_router"]
