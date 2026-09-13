"""ORM model for the ``statements`` table.

A :class:`Statement` represents a monthly statement belonging to a
:class:`CreditCard`, created in two ways:

* **PDF ingestion** — tracks the file's storage path plus a SHA-256 hash
  of its contents; the hash is unique per card so the same file uploaded
  twice for the same card is rejected (idempotent ingestion), while the
  same file on a different card is still allowed.
* **Transaction creation API** — a file-free statement created under a
  caller-supplied UUID with null ``file_path``/``file_hash`` and
  ``source=api``. Ordinary PostgreSQL nullable unique semantics allow
  multiple null hashes per card.

``source`` records how the *statement* was created (PDF upload vs. API);
it is not a per-transaction provenance marker. The ``status`` and
``source`` enums are stored as strings (``Enum`` with
``native_enum=False``) so they survive database engine swaps without an
``ALTER TYPE`` migration."""

from __future__ import annotations

import enum
import uuid
from datetime import date
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    Date,
    Enum,
    ForeignKey,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
)
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


class StatementSource(enum.StrEnum):
    """How a statement row was created.

    The values are stored as their string form in the database
    (non-native enum, length 3) with a named CHECK constraint
    (``ck_statements_source``) so the database rejects unknown values.

    * ``pdf`` — created by the PDF upload/ingestion pipeline (default;
      historical rows are backfilled to this value by migration 0002).
    * ``api`` — created file-free by the transaction creation API.
    """

    PDF = "pdf"
    API = "api"


class Statement(UUIDMixin, TimestampMixin, Base):
    """A monthly statement belonging to a :class:`CreditCard`.

    The combination of ``credit_card_id`` and ``file_hash`` is unique
    for *non-null* hashes so re-uploading the same PDF for the same
    card is a no-op (idempotency). The same file on a different card is
    fine. API-created statements carry null file fields and multiple
    null hashes on the same card are allowed by ordinary nullable
    unique semantics.

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
        directory (``settings.PDF_UPLOAD_DIR``). Null for file-free
        API-created statements.
    file_hash:
        SHA-256 of the original file contents, lowercase hex. Null for
        file-free API-created statements.
    source:
        Statement creation provenance (:class:`StatementSource`).
        Defaults to :attr:`StatementSource.PDF`.
    status:
        Current lifecycle state. Defaults to :attr:`StatementStatus.PENDING`.
    credit_card:
        Many-to-one relationship to :class:`CreditCard`.
    transactions:
        One-to-many relationship to :class:`Transaction`.
    """

    __tablename__ = "statements"
    __table_args__ = (
        # Name-parity with migration 0001: create_all schemas must carry
        # the same PK name so exact-name conflict mapping works in tests.
        PrimaryKeyConstraint("id", name="pk_statements"),
        UniqueConstraint(
            "credit_card_id",
            "file_hash",
            name="uq_statements_credit_card_id_file_hash",
        ),
        CheckConstraint(
            "source IN ('pdf', 'api')",
            name="ck_statements_source",
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
    file_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    file_hash: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    source: Mapped[StatementSource] = mapped_column(
        Enum(
            StatementSource,
            native_enum=False,
            length=3,
            create_constraint=False,  # the named CHECK lives in __table_args__
            # ``values_callable`` tells SQLAlchemy to store the
            # StrEnum *value* (``"pdf"``) rather than the *name*
            # (``"PDF"``), matching the migration's stored form.
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        default=StatementSource.PDF,
        server_default=StatementSource.PDF.value,
        nullable=False,
    )
    status: Mapped[StatementStatus] = mapped_column(
        Enum(
            StatementStatus,
            native_enum=False,
            length=20,
            # ``values_callable`` tells SQLAlchemy to store the
            # StrEnum *value* (``"failed"``) rather than the
            # *name* (``"FAILED"``). The migration 0002 also
            # stores the lowercase form, so the two stay in
            # lock-step.
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        default=StatementStatus.PENDING,
        index=True,
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

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
