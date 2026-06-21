"""finhealth — personal financial analyzer and spend control."""

__version__ = "0.1.0"

# Re-export ``create_app`` so ``from app import create_app`` works.
# Importing ``app.main`` here is safe: ``app.main`` only imports from
# sub-modules of the ``app`` package (never from the package root),
# so there is no circular import. The cost is a one-time import of
# FastAPI + the rest of the application, which is acceptable for a
# CLI / WSGI entry point but heavier than what a pure helper module
# would pay. Callers that only need ``__version__`` should import it
# explicitly via ``from app import __version__``.
from app.main import create_app

__all__ = ["__version__", "create_app"]
