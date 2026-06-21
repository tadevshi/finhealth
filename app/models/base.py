"""SQLAlchemy 2.0 declarative base for all ORM models."""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Declarative base class shared by every ORM model in the project.

    Subclassing this (often together with the mixins in
    :mod:`app.models.mixins`) registers the model with
    ``Base.metadata``, which is what Alembic reads to generate
    migrations.
    """
