"""ORM model for the ``statements`` table.

A :class:`Statement` represents a single monthly PDF statement that
the user has uploaded for a given :class:`CreditCard`. We track the
file's storage path plus a SHA-256 hash of its contents — the hash is
unique per card so the same file uploaded twice for the same card is
rejected (idempotent ingestion), while the same file on a different
card is still allowed.

The ``status`` enum is stored as a string in the database (``Enum``
with ``native_enum=False``) so it survives database engine swaps
without requiring an ``ALTER TYPE`` migration. A native enum would
also be inappropriate for SQLite.
"""

from __future__ import annotations

import enum
import uuid
from datetime import date
from typing import TYPE_CHECKING

from sqlalchemy import Date, Enum, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base
from app.models.mixins import TimestampMixin, UUIDMixin, UUIDType

if TYPE_CHECKING:
    from app.models.credit_card import CreditCard
    from app.models.transaction import Transaction


class StatementStatus(enum.StrEnum):
    """Lifecycle states of a statement ingestion job.

    The values are stored as their string form in the database, so the
    Python and SQL representations match. This is the *only* place the
    status names are defined — Pydantic schemas and the ingestion
    pipeline import :class:`StatementStatus` from here.
    """

    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class Statement(UUIDMixin, TimestampMixin, Base):
    """A monthly PDF statement belonging to a :class:`CreditCard`.

    The combination of ``credit_card_id`` and ``file_hash`` is unique
    so re-uploading the same PDF for the same card is a no-op
    (idempotency). The same file on a different card is fine.

    Attributes
    ----------
    id:
        UUID primary key.
    credit_card_id:
        Foreign key to :class:`CreditCard`. Indexed.
    period_start, period_end:
        Inclusive billing period the statement covers.
    statement_date:
        Date the bank issued the statement.
    file_path:
        Path to the stored PDF, relative to the configured upload
        directory (``settings.PDF_UPLOAD_DIR``).
    file_hash:
        SHA-256 of the original file contents, lowercase hex.
    status:
        Current lifecycle state. Defaults to :attr:`StatementStatus.PENDING`.
    credit_card:
        Many-to-one relationship to :class:`CreditCard`.
    transactions:
        One-to-many relationship to :class:`Transaction`.
    """

    __tablename__ = "statements"
    __table_args__ = (
        UniqueConstraint(
            "credit_card_id",
            "file_hash",
            name="uq_statements_credit_card_id_file_hash",
        ),
    )

    credit_card_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType(),
        ForeignKey("credit_cards.id", ondelete="CASCADE"),
        index=True,
    )
    period_start: Mapped[date] = mapped_column(Date)
    period_end: Mapped[date] = mapped_column(Date)
    statement_date: Mapped[date] = mapped_column(Date)
    file_path: Mapped[str] = mapped_column(String(512))
    file_hash: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[StatementStatus] = mapped_column(
        Enum(StatementStatus, native_enum=False, length=20),
        default=StatementStatus.PENDING,
        index=True,
    )

    # Relationships ---------------------------------------------------------
    credit_card: Mapped[CreditCard] = relationship(
        back_populates="statements",
        lazy="joined",
    )
    transactions: Mapped[list[Transaction]] = relationship(
        back_populates="statement",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
