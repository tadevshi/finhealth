"""FastAPI application factory.

The factory pattern (``create_app(settings)``) keeps the application
object free of import-time global state, which is what makes the
test suite able to spin up isolated app instances pointing at a
temporary database. A module-level ``app`` is also exposed so the
common development workflow ``uvicorn app.main:app --reload``
remains a one-liner.
"""

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.api.v1.router import api_v1_router
from app.core.config import Settings, get_settings
from app.core.lifespan import Lifespan, create_lifespan
from app.web.router import web_router

STATIC_DIR = Path(__file__).parent / "static"


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build a fully-configured :class:`FastAPI` instance.

    Parameters
    ----------
    settings:
        Optional :class:`Settings` override. When ``None`` the
        cached :func:`get_settings` singleton is used. Tests pass a
        custom ``Settings`` pointing at a temporary database so the
        application boots and tears down against an isolated file.

    Returns
    -------
    FastAPI
        A FastAPI app with the lifespan, CORS middleware, static
        files mount, and both API and web routers configured.
    """
    if settings is None:
        settings = get_settings()

    lifespan: Lifespan = create_lifespan(settings)

    app = FastAPI(
        title=settings.APP_NAME,
        version=__version__,
        debug=settings.DEBUG,
        lifespan=lifespan,
    )
    app.state.settings = settings

    # CORS ----------------------------------------------------------------
    # ``allow_origins`` honours the configured list directly. In
    # development the default ``http://localhost:8000`` is sufficient
    # and the ``.env.example`` shows the JSON-list override syntax.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Static files --------------------------------------------------------
    # Mounted under ``/static`` so future CSS/JS/image assets are
    # served straight from ``app/static/`` without code changes.
    # The directory is created on demand so a fresh checkout works
    # even before the first asset is added.
    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # Routers -------------------------------------------------------------
    # API routes are namespaced under ``/api/v1``; web routes (HTML
    # pages) live at the root and are added in Work Unit 5.
    app.include_router(api_v1_router, prefix="/api/v1")
    app.include_router(web_router)

    return app


# Module-level instance for ``uvicorn app.main:app --reload``.
# The factory remains the source of truth — this binding is a thin
# convenience that triggers the same code path used in production
# deployments and tests.
app: FastAPI = create_app()


# ``__all__`` keeps ``from app.main import *`` predictable and tells
# type checkers which names are part of the module's public surface.
__all__ = ["app", "create_app"]
