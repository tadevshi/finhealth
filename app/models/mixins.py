"""Reusable mixins and column types for ORM models.

Two mixins are provided:

* :class:`UUIDMixin` — adds a UUID primary key column. The UUID is
  stored as ``CHAR(36)`` (TEXT in SQLite) so values are debuggable in
  raw database inspection, and is auto-generated on insert via
  ``uuid.uuid4``.
* :class:`TimestampMixin` — adds timezone-aware ``created_at`` and
  ``updated_at`` columns. The database server (``func.now()``) sets
  both on insert; ``updated_at`` is also refreshed on every UPDATE.

:class:`UUIDType` is a :class:`sqlalchemy.types.TypeDecorator` that
maps between :class:`uuid.UUID` in Python and ``CHAR(36)`` at the
SQL boundary. SQLite has no native UUID type, so storing the canonical
string form keeps things portable and human-readable.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import TypeDecorator


class UUIDType(TypeDecorator[uuid.UUID]):
    """Persist :class:`uuid.UUID` values as ``CHAR(36)`` strings.

    The decorator converts to a canonical string on bind and back to
    a :class:`uuid.UUID` on result. ``None`` round-trips as ``None``.
    The implementation is dialect-agnostic — it works on SQLite
    (the project's Phase 0 backend) and any future RDBMS without
    changes.
    """

    impl = String(36)
    cache_ok = True

    def process_bind_param(
        self,
        value: uuid.UUID | None,
        dialect: Any,
    ) -> str | None:
        """Convert a Python UUID to its canonical string form."""
        if value is None:
            return None
        return str(value)

    def process_result_value(
        self,
        value: str | None,
        dialect: Any,
    ) -> uuid.UUID | None:
        """Convert a string from the database back to a UUID."""
        if value is None:
            return None
        return uuid.UUID(value)


class UUIDMixin:
    """Mixin that adds a UUID primary key column.

    The column is named ``id``, uses :class:`UUIDType`, and defaults
    to a fresh :func:`uuid.uuid4` value at insert time.
    """

    id: Mapped[uuid.UUID] = mapped_column(
        UUIDType(),
        primary_key=True,
        default=uuid.uuid4,
    )


class TimestampMixin:
    """Mixin that adds timezone-aware ``created_at`` and ``updated_at``.

    Both columns are declared as ``DateTime(timezone=True)`` so the
    database stores the offset (the project standard is UTC). Defaults
    are applied at the database level via :func:`sqlalchemy.func.now`
    so values are set even when the application clock is wrong, and
    ``updated_at`` is refreshed automatically on UPDATE.
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )
