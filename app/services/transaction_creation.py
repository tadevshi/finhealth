"""Internal transaction-creation service: request-wide parent planning.

PR3 slice of ``add-transaction-creation-api``. This module owns the
*planning half* of the creation flow described in
``openspec/changes/add-transaction-creation-api/design.md``:

* one outer transaction is started before the first database read;
* persisted statements and credit cards are fetched in batched scalar
  projections (no eager child collections);
* every referenced ``statement_id`` is classified as an existing parent
  (referenced by ID only) or a new parent (exactly one nested metadata
  object, anywhere in input order);
* parent-definition violations raise :class:`TransactionCreationError`
  with the lowest offending input index, before any write happens.

Persistence (new statement inserts, enrichment, transaction rows and
response snapshots) is the next chain slice:
:meth:`TransactionCreationService.create_many` stops at an explicit
``NotImplementedError`` boundary once planning succeeds, and no route is
mounted yet.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.credit_card import CreditCard
from app.models.statement import Statement

if TYPE_CHECKING:
    from app.schemas.domain import (
        StatementMetadataCreate,
        TransactionCreate,
        TransactionResponse,
    )

_MIN_BATCH_ITEMS: Final[int] = 1
_MAX_BATCH_ITEMS: Final[int] = 200


class TransactionCreationError(Exception):
    """Domain failure for the transaction creation flow.

    Carries a stable machine-readable ``code``, a safe ``message`` that
    never echoes request contents, and optional ``field`` / zero-based
    ``index`` attribution. HTTP status selection stays in the router
    (next chain slice); the codes raised here map to 409
    (``statement_already_exists``), 404 (``credit_card_not_found``),
    422 (``statement_metadata_required``, ``duplicate_statement_metadata``,
    ``invalid_batch``) and 500 (``creation_failed``).
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        field: str | None = None,
        index: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.field = field
        self.index = index


@dataclass(frozen=True)
class ParentPlan:
    """Resolved parent definition for one referenced ``statement_id``.

    ``metadata`` is the one authoritative nested metadata object for a
    new parent (``None`` for existing parents, which are referenced by ID
    only). ``card`` is the resolved parent card whose currency governs
    per-item currency validation in the next chain slice.
    """

    statement_id: uuid.UUID
    is_existing: bool
    metadata: StatementMetadataCreate | None
    card: CreditCard
    first_reference_index: int
    metadata_index: int | None


@dataclass
class _ParentReferences:
    """Mutable per-UUID accumulation of input references."""

    first_reference_index: int
    metadata: StatementMetadataCreate | None = None
    metadata_index: int | None = None
    second_metadata_index: int | None = None

    def record(self, index: int, metadata: StatementMetadataCreate | None) -> None:
        """Record one item's nested metadata (null means omitted)."""
        if metadata is None:
            return
        if self.metadata is None:
            self.metadata = metadata
            self.metadata_index = index
        elif self.second_metadata_index is None:
            self.second_metadata_index = index


def _violation_index(error: TransactionCreationError) -> int:
    """Sort key so the lowest offending input index is reported first."""
    return error.index if error.index is not None else -1


class TransactionCreationService:
    """Create transactions atomically against existing or new statements.

    The route-facing entry point is :meth:`create_many`. In this chain
    slice it performs request-wide parent planning inside one outer
    transaction and then stops at the persistence boundary; the next
    slice completes the write path on top of the exact same planning
    pass.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_many(self, items: list[TransactionCreate]) -> list[TransactionResponse]:
        """Create transactions atomically against existing or new parents.

        Starts one outer transaction before the first read, resolves the
        request-wide parent plan, and raises
        :class:`TransactionCreationError` before any write on invalid
        plans. This chain slice stops at the persistence boundary once
        planning succeeds; the next slice completes statement inserts,
        enrichment, transaction rows and response snapshots on top of the
        same planning pass.
        """
        self._validate_batch_bounds(items)
        async with self._session.begin():
            await self.plan_parents(items)
            raise NotImplementedError("transaction persistence lands in the next chain slice (PR4)")

    async def plan_parents(self, items: list[TransactionCreate]) -> dict[uuid.UUID, ParentPlan]:
        """Resolve the request-wide parent plan for every referenced statement.

        Internal planning seam: the caller owns the transaction boundary
        (:meth:`create_many` starts one outer transaction before the
        first read). Read-only — a planning failure never writes.
        """
        references = self._collect_references(items)
        persisted = await self._fetch_statement_cards(set(references))
        self._validate_parent_plans(references, persisted)
        cards = await self._resolve_cards(references, persisted)
        return self._build_plans(references, persisted, cards)

    # ------------------------------------------------------------------
    # Planning internals
    # ------------------------------------------------------------------

    def _validate_batch_bounds(self, items: list[TransactionCreate]) -> None:
        """Re-enforce the schema batch bounds defensively at the service edge."""
        if not _MIN_BATCH_ITEMS <= len(items) <= _MAX_BATCH_ITEMS:
            raise TransactionCreationError(
                "invalid_batch",
                "A creation request must contain between 1 and 200 transactions.",
                field="transactions",
            )

    def _collect_references(
        self, items: list[TransactionCreate]
    ) -> dict[uuid.UUID, _ParentReferences]:
        """Group items by statement UUID, tracking reference/metadata indexes."""
        references: dict[uuid.UUID, _ParentReferences] = {}
        for index, item in enumerate(items):
            ref = references.get(item.statement_id)
            if ref is None:
                ref = _ParentReferences(first_reference_index=index)
                references[item.statement_id] = ref
            ref.record(index, item.statement)
        return references

    async def _fetch_statement_cards(
        self, statement_ids: set[uuid.UUID]
    ) -> dict[uuid.UUID, uuid.UUID]:
        """Batch-fetch persisted parents as a scalar ``id -> card_id`` projection."""
        if not statement_ids:
            return {}
        result = await self._session.execute(
            select(Statement.id, Statement.credit_card_id).where(Statement.id.in_(statement_ids))
        )
        return {row[0]: row[1] for row in result.all()}

    async def _fetch_cards(self, card_ids: set[uuid.UUID]) -> dict[uuid.UUID, CreditCard]:
        """Batch-fetch the credit cards needed by the parent plans."""
        if not card_ids:
            return {}
        result = await self._session.execute(select(CreditCard).where(CreditCard.id.in_(card_ids)))
        return {card.id: card for card in result.scalars().all()}

    def _validate_parent_plans(
        self,
        references: dict[uuid.UUID, _ParentReferences],
        persisted: dict[uuid.UUID, uuid.UUID],
    ) -> None:
        """Validate parent definitions; raise the lowest-index violation.

        Existing parents must be referenced by ID only (metadata is a 409
        conflict even when it matches). Missing parents require exactly
        one metadata object anywhere in input order; additional objects
        fail at the second metadata-bearing index. The parent-definition
        pass precedes card resolution, per the design's validation order.
        """
        violations: list[TransactionCreationError] = []
        for statement_id, ref in references.items():
            if statement_id in persisted:
                if ref.metadata is not None:
                    violations.append(
                        TransactionCreationError(
                            "statement_already_exists",
                            "Statement already exists; reference it by statement_id"
                            " only and omit nested statement metadata.",
                            field="statement",
                            index=ref.metadata_index,
                        )
                    )
            elif ref.metadata is None:
                violations.append(
                    TransactionCreationError(
                        "statement_metadata_required",
                        "Statement does not exist; supply nested statement metadata to create it.",
                        field="statement",
                        index=ref.first_reference_index,
                    )
                )
            elif ref.second_metadata_index is not None:
                violations.append(
                    TransactionCreationError(
                        "duplicate_statement_metadata",
                        "At most one nested statement metadata object is allowed per statement_id.",
                        field="statement",
                        index=ref.second_metadata_index,
                    )
                )
        if violations:
            raise min(violations, key=_violation_index)

    async def _resolve_cards(
        self,
        references: dict[uuid.UUID, _ParentReferences],
        persisted: dict[uuid.UUID, uuid.UUID],
    ) -> dict[uuid.UUID, CreditCard]:
        """Fetch every plan's card and reject unknown new-parent cards.

        Unknown cards in new-parent metadata raise ``credit_card_not_found``
        at the lowest metadata-bearing index. Existing parents resolve
        through their persisted FK; a missing row is an integrity violation
        handled defensively in :meth:`_build_plans`.
        """
        needed: set[uuid.UUID] = set(persisted.values())
        for statement_id, ref in references.items():
            if statement_id not in persisted and ref.metadata is not None:
                needed.add(ref.metadata.credit_card_id)
        cards = await self._fetch_cards(needed)
        not_found = [
            TransactionCreationError(
                "credit_card_not_found",
                "Credit card referenced by statement metadata does not exist.",
                field="statement.credit_card_id",
                index=ref.metadata_index,
            )
            for statement_id, ref in references.items()
            if statement_id not in persisted
            and ref.metadata is not None
            and ref.metadata.credit_card_id not in cards
        ]
        if not_found:
            raise min(not_found, key=_violation_index)
        return cards

    def _build_plans(
        self,
        references: dict[uuid.UUID, _ParentReferences],
        persisted: dict[uuid.UUID, uuid.UUID],
        cards: dict[uuid.UUID, CreditCard],
    ) -> dict[uuid.UUID, ParentPlan]:
        """Assemble one :class:`ParentPlan` per referenced statement UUID."""
        plans: dict[uuid.UUID, ParentPlan] = {}
        for statement_id, ref in references.items():
            persisted_card_id = persisted.get(statement_id)
            if persisted_card_id is not None:
                card = cards.get(persisted_card_id)
                if card is None:
                    # Defensive: a persisted statement always carries a card FK,
                    # so this is unreachable through FK-consistent data (design
                    # maps it to a generic creation failure, never a 404).
                    raise TransactionCreationError(
                        "creation_failed", "Transaction creation failed."
                    )
                plans[statement_id] = ParentPlan(
                    statement_id=statement_id,
                    is_existing=True,
                    metadata=None,
                    card=card,
                    first_reference_index=ref.first_reference_index,
                    metadata_index=None,
                )
                continue
            metadata = ref.metadata
            card = cards.get(metadata.credit_card_id) if metadata is not None else None
            if metadata is None or card is None:
                # Defensive: plan and card validation guarantee both for new
                # parents; invalid linkage fails generically instead of writing.
                raise TransactionCreationError("creation_failed", "Transaction creation failed.")
            plans[statement_id] = ParentPlan(
                statement_id=statement_id,
                is_existing=False,
                metadata=metadata,
                card=card,
                first_reference_index=ref.first_reference_index,
                metadata_index=ref.metadata_index,
            )
        return plans


__all__ = [
    "ParentPlan",
    "TransactionCreationError",
    "TransactionCreationService",
]
