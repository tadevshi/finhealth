"""ORM model for the ``banks`` table.

A :class:`Bank` represents an issuing financial institution the user
holds one or more credit cards with. The ``password_formula`` field
encodes the convention that bank uses for encrypting monthly PDF
statements — for example, ``"rut_sin_dv"`` (RUT without the
verification digit) or ``"rut_ultimos_4"`` (last four RUT digits). The
ingestion pipeline reads this string at decryption time so we do not
have to ship bank-specific logic in code.

Storing the formula in data (not in code) is deliberate: when a bank
rolls out a new convention we add a row, not a release.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base
from app.models.mixins import TimestampMixin, UUIDMixin

if TYPE_CHECKING:
    from app.models.credit_card import CreditCard


class Bank(UUIDMixin, TimestampMixin, Base):
    """An issuing bank (e.g. Santander, Itaú, Banco de Chile).

    The ``name`` column is the canonical short identifier used in code
    and lookups (e.g. ``"santander"``); ``display_name`` is the
    human-readable label shown in the UI. Keeping both means the
    internal identifier never has to change when marketing re-brands
    the bank.

    Attributes
    ----------
    id:
        UUID primary key (from :class:`UUIDMixin`).
    name:
        Short, stable identifier (unique). Lowercase, no spaces.
    display_name:
        Human-readable name shown in the UI.
    password_formula:
        Token describing how the bank encrypts its statement PDFs.
        Examples: ``"rut_sin_dv"``, ``"rut_ultimos_4"``.
    is_active:
        When ``False`` the bank is hidden from new card registrations
        but existing rows are preserved for historical data.
    credit_cards:
        One-to-many relationship to :class:`CreditCard`.
    """

    __tablename__ = "banks"

    name: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(100))
    password_formula: Mapped[str] = mapped_column(String(50))
    is_active: Mapped[bool] = mapped_column(default=True)

    # Relationships ---------------------------------------------------------
    credit_cards: Mapped[list[CreditCard]] = relationship(
        back_populates="bank",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
