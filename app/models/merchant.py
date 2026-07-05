"""ORM models for the ``merchants`` and ``merchant_aliases`` tables.

The Phase 2 PR #4 change introduces a canonical merchant entity so
the user can answer questions like "what did I spend at Lider?"
without scanning every row manually. The pair of tables is split on
purpose:

* :class:`Merchant` — one row per canonical merchant (e.g.
  ``"mcdonalds"``, ``"lider"``, ``"paris"``). The ``name`` column is
  the short stable identifier used for lookups and is unique.
* :class:`MerchantAlias` — one row per bank-description variant
  that resolves to a :class:`Merchant`. The raw ``alias_text`` is
  preserved verbatim for audit/debugging; the ``normalized`` column
  is the lowercase+accent-stripped form that drives the lookup.
  ``UNIQUE(alias_text)`` prevents the same raw string from being
  bound to two merchants; the non-unique ``normalized`` index keeps
  the per-row lookup cheap on a growing table.

The two tables map to the same UUIDMixin + TimestampMixin pattern as
:class:`app.models.category.Category` (UUID PK, ``CHAR(36)`` for
debuggability, timezone-aware ``created_at`` / ``updated_at`` with
``server_default=func.now()``).

Why a separate alias table
--------------------------

The raw bank description is needed for two things: debugging (what
did the bank actually print?) and re-derivation (the same bank can
change its format over time). Storing both the raw and the normalized
form on the same row would work for the first upload, but a single
merchant typically has many aliases (``"MCDONALDS SUC 12"``,
``"MCDONALDS SUC 13"``, ``"MCDONALDS SUC VINA"``) — keeping the
``UNIQUE`` on the raw text and an index on the normalized form lets
us hit on the canonical key while still enforcing that no two
merchants accidentally claim the same raw string.
"""

from __future__ import annotations

import enum
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, Enum, Float, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base
from app.models.mixins import TimestampMixin, UUIDMixin, UUIDType

if TYPE_CHECKING:
    from app.models.category import Category
    from app.models.transaction import Transaction


class MerchantAliasSource(enum.StrEnum):
    """Origin of a :class:`MerchantAlias` row.

    The values are stored as their string form in the database
    (``Enum`` with ``native_enum=False``) so they survive a database
    engine swap without an ``ALTER TYPE`` migration. SQLite has no
    native enum type, so the portable form is also the only form
    that works today.

    * ``auto`` — created by the deterministic normalizer on first
      sight of a bank description. Default; covers ~80% of cases.
    * ``user`` — created by the user via ``POST /api/v1/merchants/
      {id}/aliases`` to bind an extra bank-description variant to
      an existing merchant.
    * ``llm`` — created by the opt-in LLM helper when the
      deterministic path missed. Carries a ``confidence`` score.
    """

    AUTO = "auto"
    USER = "user"
    LLM = "llm"


class Merchant(UUIDMixin, TimestampMixin, Base):
    """A canonical merchant (e.g. ``"mcdonalds"``).

    The ``name`` column is the short stable identifier used in code
    and lookups. The ``default_category_id`` FK to
    :class:`app.models.category.Category` carries the "sensible
    default category" the :data:`app.services.merchants.KNOWN_MERCHANT_PATTERNS`
    dict provides — ``NULL`` for auto-created merchants that did
    not match a known pattern (the user can re-tag them by hand).

    Attributes
    ----------
    id:
        UUID primary key (from :class:`UUIDMixin`).
    name:
        Short stable identifier (unique). The same identifier the
        :mod:`app.services.merchants` module uses as the lookup
        key for the alias table.
    default_category_id:
        Optional FK to :class:`Category`. ``ON DELETE SET NULL``
        so deleting a category does not cascade to drop the
        merchant (the merchant just becomes "untagged" until the
        user re-binds it).
    is_active:
        Soft-delete flag. ``True`` for new rows; flipped to
        ``False`` when the user hides a merchant from the API list.
    aliases:
        One-to-many relationship to :class:`MerchantAlias`.
        ``selectin`` load so a list of merchants can serialise
        its aliases in one round-trip.
    transactions:
        One-to-many relationship to :class:`Transaction`. Backed
        by the ``Transaction.merchant_ref`` relationship.
    """

    __tablename__ = "merchants"

    name: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    default_category_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType(),
        ForeignKey("categories.id", ondelete="SET NULL"),
        nullable=True,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Relationships ---------------------------------------------------------
    default_category: Mapped[Category | None] = relationship(lazy="joined")
    aliases: Mapped[list[MerchantAlias]] = relationship(
        back_populates="merchant",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    merchant_transactions: Mapped[list[Transaction]] = relationship(
        back_populates="merchant_ref",
        lazy="selectin",
    )


class MerchantAlias(UUIDMixin, TimestampMixin, Base):
    """A bank-description variant bound to a :class:`Merchant`.

    The same merchant typically has many aliases (one per bank
    statement format / branch / installment marker). The
    ``alias_text`` column is the raw description as it appeared on
    the statement (preserved verbatim for debugging); the
    ``normalized`` column is the lowercase+accent-stripped form the
    :func:`app.services.merchants.normalize` helper computes and
    uses as the lookup key.

    Two constraints govern the row:

    * ``UNIQUE(alias_text)`` — no two merchants can claim the same
      raw description. The constraint is enforced at the database
      level so a race between two concurrent ingests fails
      cleanly with an :class:`sqlalchemy.exc.IntegrityError` (the
      service catches it and re-queries, per design decision D3).
    * Non-unique index on ``normalized`` — speeds up the per-row
      lookup in :meth:`app.services.merchants.MerchantNormalizer.resolve_merchant`.

    Attributes
    ----------
    id:
        UUID primary key.
    merchant_id:
        FK to :class:`Merchant`. ``ON DELETE CASCADE`` so dropping
        a merchant drops its aliases too.
    alias_text:
        Raw description as it appeared on the statement.
        ``UNIQUE``, ``VARCHAR(200)``.
    normalized:
        The :func:`app.services.merchants.normalize` form. Indexed
        (non-unique) so the lookup is cheap on a growing table.
    source:
        One of :class:`MerchantAliasSource`. Tracks the origin of
        the row for audit.
    confidence:
        Optional confidence score (0-1). Populated only when
        ``source='llm'``; ``NULL`` for ``auto`` and ``user``
        aliases.
    """

    __tablename__ = "merchant_aliases"

    merchant_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType(),
        ForeignKey("merchants.id", ondelete="CASCADE"),
        index=True,
    )
    alias_text: Mapped[str] = mapped_column(String(200), unique=True)
    normalized: Mapped[str] = mapped_column(String(200), index=True)
    source: Mapped[MerchantAliasSource] = mapped_column(
        Enum(
            MerchantAliasSource,
            native_enum=False,
            length=16,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        default=MerchantAliasSource.AUTO,
        nullable=False,
    )
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Relationships ---------------------------------------------------------
    merchant: Mapped[Merchant] = relationship(back_populates="aliases", lazy="joined")


__all__ = ["Merchant", "MerchantAlias", "MerchantAliasSource"]
