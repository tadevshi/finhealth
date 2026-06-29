"""ORM model for the ``categories`` table.

A :class:`Category` is one entry in the closed-set 12-category Y-NAB
taxonomy introduced in Phase 2. The taxonomy is *flat* (no parent/child
hierarchy) and is seeded at migration time — operators do not add
categories at runtime through the API, only through a new migration.

The two name columns are split on purpose:

* ``name`` — short stable identifier used in code and lookups (e.g.
  ``"Dining Out"``). The LLM is told to emit this value verbatim and the
  ingestion layer does a case-insensitive match against it.
* ``display_name`` — human-readable label shown in the UI (e.g.
  ``"Food & Dining"``). Marketing can re-brand without breaking
  lookups.

``sort_order`` controls the order in which the categories are
returned by ``GET /api/v1/categories``. The seed sets it to a stable
alphabetical-by-usage ordering; the UI relies on the order so the
`<select>` reads naturally.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base
from app.models.mixins import TimestampMixin, UUIDMixin

if TYPE_CHECKING:
    from app.models.transaction import Transaction


class Category(UUIDMixin, TimestampMixin, Base):
    """One entry in the 12-category Y-NAB taxonomy.

    The closed set is seeded at migration time. The ``name`` column
    is unique; ``display_name`` is not (two entries may share a
    human-readable label during a rename, although the API rejects
    such collisions on POST).

    Attributes
    ----------
    id:
        UUID primary key (from :class:`UUIDMixin`).
    name:
        Short stable identifier (unique). The LLM is told to emit
        this value verbatim.
    display_name:
        Human-readable label shown in the UI.
    sort_order:
        Position in the list returned by ``GET /api/v1/categories``.
        Lower = first.
    transactions:
        One-to-many relationship to :class:`Transaction`. A category
        that is currently unused still exists in the table; deleting
        a category sets the FK to ``NULL`` on the dependent rows
        (the ``ON DELETE SET NULL`` clause on the migration).
        Back-populates ``Transaction.category_ref`` (the relationship
        is named ``category_ref`` on the ``Transaction`` side because
        the ``category`` name is already taken by the denormalized
        ``String(50)`` column).
    """

    __tablename__ = "categories"

    name: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(100))
    sort_order: Mapped[int] = mapped_column(Integer)

    # Relationships ---------------------------------------------------------
    transactions: Mapped[list[Transaction]] = relationship(
        back_populates="category_ref",
        lazy="selectin",
    )


__all__ = ["Category"]
