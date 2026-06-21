"""Database access layer: engine and session factory.

This package wraps the async SQLAlchemy engine, session factory, and
FastAPI dependency used by the rest of the app. It is intentionally
side-effect-free: call :func:`app.db.engine.create_engine` to build an
engine, :func:`app.db.session.create_session_factory` to build a
factory, and :func:`app.db.session.get_session` to use the per-request
session dependency.
"""

from app.db.engine import create_engine
from app.db.session import create_session_factory, get_session

__all__ = ["create_engine", "create_session_factory", "get_session"]
