"""ORM model for the ``credit_cards`` table.

A :class:`CreditCard` belongs to exactly one :class:`Bank` and may
have many :class:`Statement` rows over time. We deliberately store
the *masked* card number (``"XXXX XXXX XXXX 0951"``) — the full PAN
is never persisted anywhere in this system, which keeps the
application's threat surface small and avoids PCI-DSS scope.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base
from app.models.mixins import TimestampMixin, UUIDMixin, UUIDType

if TYPE_CHECKING:
    from app.models.bank import Bank
    from app.models.statement import Statement


class CreditCard(UUIDMixin, TimestampMixin, Base):
    """A credit card issued by a :class:`Bank`.

    The masked number is the only card identifier we store; the PAN
    itself never reaches the database. ``cardholder`` is the printed
    name on the card — useful when the user owns several cards on the
    same account and the bank statement does not disambiguate.

    Attributes
    ----------
    id:
        UUID primary key.
    bank_id:
        Foreign key to :class:`Bank`. Indexed for join performance.
    card_number_masked:
        Last-four masked form, e.g. ``"XXXX XXXX XXXX 0951"``.
    cardholder:
        Printed cardholder name (``"JOHN DOE"``).
    currency:
        ISO-4217 currency code (``"CLP"``, ``"USD"``). 3-letter codes
        are the dominant convention, hence ``String(3)``.
    is_active:
        When ``False`` the card is hidden from new uploads but its
        statements and transactions remain in the database.
    bank:
        Many-to-one relationship to :class:`Bank`.
    statements:
        One-to-many relationship to :class:`Statement`.
    """

    __tablename__ = "credit_cards"

    bank_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType(),
        ForeignKey("banks.id", ondelete="CASCADE"),
        index=True,
    )
    card_number_masked: Mapped[str] = mapped_column(String(25))
    cardholder: Mapped[str] = mapped_column(String(100))
    currency: Mapped[str] = mapped_column(String(3))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    # Relationships ---------------------------------------------------------
    bank: Mapped[Bank] = relationship(back_populates="credit_cards", lazy="joined")
    statements: Mapped[list[Statement]] = relationship(
        back_populates="credit_card",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
