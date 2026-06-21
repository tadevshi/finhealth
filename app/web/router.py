"""Web (HTML) routes.

Server-rendered pages using Jinja2 templates. The base template
(``app/web/templates/base.html``) wires the full frontend stack
(HTMX, Alpine.js, Tailwind CSS) and the dark-mode toggle, so a single
``index`` handler is enough for the Phase 0 MVP.

The router is mounted at the application root (no prefix) by
:mod:`app.main`. ``GET /`` renders the landing page; the API surface
lives under ``/api/v1`` and is wired separately by
:mod:`app.api.v1.router`.
"""

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

# Templates directory resolved relative to this file so the router
# works regardless of the working directory the app is launched from
# (uvicorn, pytest, Docker, ...). The same pattern is used by
# ``app.main`` for the static files mount.
TEMPLATES_DIR: Path = Path(__file__).parent / "templates"
templates: Jinja2Templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

web_router: APIRouter = APIRouter(tags=["web"])


@web_router.get(
    "/",
    response_class=HTMLResponse,
    summary="Landing page",
    responses={
        200: {
            "description": "Server-rendered HTML landing page with the "
            "HTMX/Alpine/Tailwind shell and dark-mode toggle.",
            "content": {"text/html": {}},
        },
    },
)
async def index(request: Request) -> HTMLResponse:
    """Render the landing page.

    The template context is intentionally minimal: ``app_name`` comes
    from :attr:`app.state.settings.APP_NAME` (set by the factory in
    :mod:`app.main`) so the header reflects the configured name. No
    database access is performed — the page is a static marketing /
    status placeholder until Phase 3 introduces the dashboard.
    """
    # ``app.state.settings`` is set by ``create_app``; the type is
    # ``Settings`` (a pydantic model) so ``.APP_NAME`` is typed.
    app_name: str = request.app.state.settings.APP_NAME
    context: dict[str, Any] = {"app_name": app_name}
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context=context,
    )


__all__ = ["TEMPLATES_DIR", "templates", "web_router"]
