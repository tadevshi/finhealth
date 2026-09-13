"""Internal transaction-creation service: atomic statement-linked creation.

This module owns the full creation flow described in
``openspec/changes/add-transaction-creation-api/design.md``:

* one outer transaction is started before the first database read;
* persisted statements and credit cards are fetched in batched scalar
  projections (no eager child collections);
* every referenced ``statement_id`` is classified as an existing parent
  (referenced by ID only) or a new parent (exactly one nested metadata
  object, anywhere in input order);
* parent-definition violations raise :class:`TransactionCreationError`
  with the lowest offending input index, before any write happens;
* new statements are inserted in canonical UUID order with ``source=api``,
  ``status=completed`` and null file metadata, then transactions are
  enriched (category policy, currency policy, deterministic merchant
  resolution), flushed once and validated as response snapshots before
  the single commit.

HTTP status selection stays in the router; no FastAPI imports here.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import noload

from app.models.category import Category
from app.models.credit_card import CreditCard
from app.models.merchant import Merchant
from app.models.statement import (
    Statement,
    StatementSource,
    StatementStatus,
)
from app.models.transaction import Transaction
from app.schemas.domain import TransactionCreate, TransactionResponse
from app.services.merchants import MerchantNormalizer, normalize

if TYPE_CHECKING:
    from app.schemas.domain import StatementMetadataCreate

logger = logging.getLogger(__name__)

_MIN_BATCH_ITEMS: Final[int] = 1
_MAX_BATCH_ITEMS: Final[int] = 200

#: Exact supported currencies; anything else is a business-rule failure (400).
_SUPPORTED_CURRENCIES: Final[frozenset[str]] = frozenset({"CLP", "USD"})

#: Parent primary-key constraint name: the authority for parent UUID races.
_STATEMENT_PK_NAME: Final[str] = "pk_statements"

#: Merchant-binding eligibility bounds (repository storage limits): raw
#: descriptions beyond ``MerchantAlias.alias_text`` width and canonical
#: keys beyond ``Merchant.name`` width never reach the resolver.
_MERCHANT_RAW_MAX: Final[int] = 200
_MERCHANT_CANONICAL_MAX: Final[int] = 100


def _is_statement_pk_violation(exc: IntegrityError) -> bool:
    """True only for a PostgreSQL unique violation on the statement PK.

    Mirrors the merchants module discrimination: the SQLSTATE rides on the
    wrapped driver error and the violated constraint name may be exposed on
    it or on its ``__cause__``. Card/hash uniqueness, FK, deadlock and
    connection failures are never classified as parent races.
    """
    orig = exc.orig
    if getattr(orig, "sqlstate", None) != "23505":
        return False
    for candidate in (orig, getattr(orig, "__cause__", None)):
        if getattr(candidate, "constraint_name", None) == _STATEMENT_PK_NAME:
            return True
    return False


class TransactionCreationError(Exception):
    """Domain failure for the transaction creation flow.

    Carries a stable machine-readable ``code``, a safe ``message`` that
    never echoes request contents, and optional ``field`` / zero-based
    ``index`` attribution. HTTP status selection stays in the router;
    the codes raised here map to 409 (``statement_already_exists``,
    ``statement_creation_conflict``), 404 (``credit_card_not_found``,
    ``category_not_found``), 400 (``unsupported_currency``,
    ``currency_mismatch``), 422 (``statement_metadata_required``,
    ``duplicate_statement_metadata``, ``invalid_batch``) and 500
    (``creation_failed``).
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

    The route-facing entry point is :meth:`create_many`. It performs
    request-wide parent planning, creates new API parents in canonical
    UUID order, enriches and validates every transaction in input
    order, and commits statements, transactions, merchants and aliases
    together — or persists none of them.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_many(self, items: list[TransactionCreate]) -> list[TransactionResponse]:
        """Create transactions atomically against existing or new parents.

        Starts one outer transaction before the first read, resolves the
        request-wide parent plan, and raises
        :class:`TransactionCreationError` before any write on invalid
        plans or invalid items. New statements are inserted in canonical
        UUID order and flushed before enrichment; transaction rows are
        built in input order, flushed once and validated as
        :class:`TransactionResponse` snapshots before the single commit.
        Snapshots are returned only after the outer context exits
        successfully; any exception rolls back every request-created
        statement, transaction, merchant and alias.
        """
        self._validate_batch_bounds(items)
        async with self._session.begin():
            plans = await self.plan_parents(items)
            categories = await self._fetch_categories()
            self._validate_items(items, plans, categories)
            await self._create_new_statements(plans)
            rows = await self._build_transaction_rows(items, plans, categories)
            snapshots = await self._snapshot_rows(rows)
        return snapshots

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
        result = await self._session.execute(
            select(CreditCard)
            .where(CreditCard.id.in_(card_ids))
            # ``CreditCard`` carries eager relationships (joined bank,
            # selectin statements). Loading them would silently pull every
            # statement of the card — including a concurrently committed
            # winner — into this session's identity map and turn the parent
            # PK race into an identity conflict instead of the database's
            # unique violation. Plans need only the card's own columns.
            .options(
                noload(CreditCard.bank),
                noload(CreditCard.statements),
            )
        )
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

    # ------------------------------------------------------------------
    # Persistence internals
    # ------------------------------------------------------------------

    async def _fetch_categories(self) -> _CategorySet:
        """Fetch the full category set once, keyed by ID and lowercase name.

        The closed taxonomy is small, so one query serves both the
        per-item category validation (by ID) and the merchant default
        lookup (by name).
        """
        result = await self._session.execute(select(Category))
        by_id: dict[uuid.UUID, Category] = {}
        by_name: dict[str, Category] = {}
        for category in result.scalars().all():
            by_id[category.id] = category
            by_name[category.name.lower()] = category
        return _CategorySet(by_id=by_id, by_name=by_name)

    def _validate_items(
        self,
        items: list[TransactionCreate],
        plans: dict[uuid.UUID, ParentPlan],
        categories: _CategorySet,
    ) -> None:
        """Validate currency and explicit categories in input order.

        Runs before any write: the first failing item raises with its
        zero-based index, so a later invalid item never lets an earlier
        one persist.
        """
        for index, item in enumerate(items):
            self._validate_currency(item.currency, plans[item.statement_id].card, index)
            if item.category_id is not None and item.category_id not in categories.by_id:
                raise TransactionCreationError(
                    "category_not_found",
                    "Category does not exist.",
                    field="category_id",
                    index=index,
                )

    @staticmethod
    def _validate_currency(currency: str, card: CreditCard, index: int) -> None:
        """Enforce exact supported currency matching the parent card."""
        if currency not in _SUPPORTED_CURRENCIES:
            raise TransactionCreationError(
                "unsupported_currency",
                "Only CLP and USD are supported.",
                field="currency",
                index=index,
            )
        if currency != card.currency:
            raise TransactionCreationError(
                "currency_mismatch",
                "Transaction currency must match the parent statement's card currency.",
                field="currency",
                index=index,
            )

    async def _create_new_statements(self, plans: dict[uuid.UUID, ParentPlan]) -> None:
        """Insert each new parent in canonical UUID order, flushed individually.

        API parents carry the caller's UUID, card and dates, null file
        fields/errors, ``source=api`` and ``status=completed`` (the atomic
        write completed; PDF extraction never ran). Insert ordering
        prevents reversed multi-parent requests from deadlocking on the
        parent keys. Only the statement PK unique violation is classified
        as a parent race (409 ``statement_creation_conflict``); every other
        database failure aborts the request generically.
        """
        new_plans = sorted(
            (plan for plan in plans.values() if not plan.is_existing),
            key=lambda plan: plan.statement_id,
        )
        for plan in new_plans:
            metadata = plan.metadata
            assert metadata is not None  # validated by the parent plan
            statement = Statement(
                id=plan.statement_id,
                credit_card_id=plan.card.id,
                period_start=metadata.period_start,
                period_end=metadata.period_end,
                statement_date=metadata.statement_date,
                file_path=None,
                file_hash=None,
                error_message=None,
                source=StatementSource.API,
                status=StatementStatus.COMPLETED,
            )
            self._session.add(statement)
            try:
                await self._session.flush()
            except IntegrityError as exc:
                if not _is_statement_pk_violation(exc):
                    raise
                raise TransactionCreationError(
                    "statement_creation_conflict",
                    "The statement was created concurrently; retry referencing it"
                    " by statement_id only, without nested statement metadata.",
                    field="statement_id",
                    index=plan.metadata_index,
                ) from exc

    async def _build_transaction_rows(
        self,
        items: list[TransactionCreate],
        plans: dict[uuid.UUID, ParentPlan],
        categories: _CategorySet,
    ) -> list[Transaction]:
        """Enrich and build every transaction row in input order.

        Merchants resolve first for all items; the rows are added and
        flushed only after all enrichment completes, so a partially built
        row is never attached via a parent relationship. A single flush
        keeps request-wide rollback trivial: any failure after it still
        discards every request-owned write.
        """
        rows: list[Transaction] = []
        deprecation_logged = False
        for item in items:
            merchant = await self._resolve_merchant(item, categories)
            if not deprecation_logged and _uses_legacy_category(item):
                logger.warning(
                    "Transaction creation deprecation: legacy `category: str` field used;"
                    " clients should migrate to `category_id`."
                )
                deprecation_logged = True
            rows.append(self._build_row(item, merchant, categories))
        self._session.add_all(rows)
        await self._session.flush()
        return rows

    async def _resolve_merchant(
        self, item: TransactionCreate, categories: _CategorySet
    ) -> Merchant | None:
        """Resolve the deterministic merchant, honoring creation's length bounds.

        Descriptions longer than the alias storage width, or canonical
        keys longer than the merchant-name width, are left unbound instead
        of risking a database length error; the full description is still
        preserved verbatim on the transaction row. The optional LLM path
        is never reachable from here.
        """
        canonical = normalize(item.description)
        if len(item.description) > _MERCHANT_RAW_MAX or len(canonical) > _MERCHANT_CANONICAL_MAX:
            return None
        merchant, _was_new = await MerchantNormalizer().resolve_merchant(
            self._session, item.description, categories.by_name
        )
        return merchant

    @staticmethod
    def _build_row(
        item: TransactionCreate, merchant: Merchant | None, categories: _CategorySet
    ) -> Transaction:
        """Build one transaction row with an explicit field allowlist.

        The nested ``statement`` DTO is never passed through ``model_dump``;
        category precedence follows the confirmed table (ID wins with the
        canonical name and low_confidence=False; legacy string stays exact
        and low_confidence=True; absence stays absent, not "Uncategorized").
        Merchant defaults never infer a transaction category; recurring
        linkage stays null.
        """
        category_id: uuid.UUID | None = None
        category_name: str | None = None
        low_confidence = True
        if item.category_id is not None:
            category_id = item.category_id
            category_name = categories.by_id[item.category_id].name
            low_confidence = False
        else:
            category_name = item.category
        return Transaction(
            statement_id=item.statement_id,
            date=item.date,
            description=item.description,
            amount=item.amount,
            currency=item.currency,
            category=category_name,
            category_id=category_id,
            low_confidence=low_confidence,
            merchant_id=merchant.id if merchant is not None else None,
            installment_number=item.installment_number,
            installment_total=item.installment_total,
            installment_value=item.installment_value,
            raw_json=item.raw_json,
        )

    async def _snapshot_rows(self, rows: list[Transaction]) -> list[TransactionResponse]:
        """Build validated response snapshots in input order, before commit.

        Rows are re-fetched in one query so SQL-expression defaults
        (``created_at``/``updated_at``) are populated without a per-row
        refresh; a missing row is an invalid-linkage integrity failure
        mapped to the generic creation error. Snapshots are returned to
        the caller only after the outer transaction commits.
        """
        ids = [row.id for row in rows]
        if not ids:
            return []
        result = await self._session.execute(select(Transaction).where(Transaction.id.in_(ids)))
        persisted = {row.id: row for row in result.scalars().all()}
        if any(row.id not in persisted for row in rows):
            # Defensive: a flushed row must be re-readable inside the same
            # transaction; anything else is invalid parent linkage (design
            # maps it to a generic creation failure, never a 404).
            raise TransactionCreationError("creation_failed", "Transaction creation failed.")
        return [TransactionResponse.model_validate(persisted[row.id]) for row in rows]


def _uses_legacy_category(item: TransactionCreate) -> bool:
    """True when the item actually uses the deprecated legacy category string.

    ID-precedence items alone never trigger the deprecation warning.
    """
    return item.category_id is None and item.category is not None


@dataclass(frozen=True)
class _CategorySet:
    """The category set fetched once per request, keyed two ways."""

    by_id: dict[uuid.UUID, Category]
    by_name: dict[str, Category]


__all__ = [
    "ParentPlan",
    "TransactionCreationError",
    "TransactionCreationService",
]
