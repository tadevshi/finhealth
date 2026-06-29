"""Transaction-related HTTP endpoints.

The transactions router owns the *read* and *edit* of individual
:class:`app.models.Transaction` rows:

* :func:`list_transactions` — filterable, paginated list.
* :func:`update_transaction` — patch a single transaction's
  category.

Statement creation lives in :mod:`app.api.v1.statements`; the
boundary follows the aggregate root: a statement owns its
transactions, but reading and editing individual rows does not
require loading the parent statement.
"""

from __future__ import annotations

import logging
import uuid
from datetime import date
from decimal import Decimal
from typing import Annotated, Final

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session
from app.models import Category, Transaction
from app.schemas.domain import TransactionResponse

logger = logging.getLogger(__name__)

router: APIRouter = APIRouter(prefix="/transactions", tags=["transactions"])


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


#: Bounds on the page-size query parameter. ``1`` is the
#: minimum (asking for zero rows is a client bug), and ``200`` is
#: the maximum — large enough for an end-user's "show me
#: everything" tab, small enough that a single response stays
#: under a megabyte.
_MIN_PAGE_SIZE: Final = 1
_MAX_PAGE_SIZE: Final = 200
_DEFAULT_PAGE_SIZE: Final = 50

#: Bounds on the ``offset`` query parameter. ``0`` is the first
#: row; an upper bound prevents accidental deep-pagination
#: requests that the database will struggle to satisfy.
_MAX_OFFSET: Final = 10_000


# ---------------------------------------------------------------------------
# Request/response models
# ---------------------------------------------------------------------------


class TransactionCategoryUpdate(BaseModel):
    """Body of the ``PATCH /transactions/{id}`` endpoint.

    The endpoint accepts two fields:

    * ``category_id`` — a UUID from the seeded closed set. The
      endpoint writes the FK *and* the denormalized ``category``
      string (the LLM-readable label) in a single transaction,
      and marks the row ``low_confidence=False``. A 404 is
      returned if the UUID does not match any row.
    * ``category`` — a free-form string. **Deprecated**: the
      field is kept working for backward compatibility with
      clients that have not migrated to ``category_id`` yet.
      When ``category_id`` is ``None`` and ``category`` is
      supplied, the endpoint writes the string, leaves
      ``category_id`` as ``NULL``, sets ``low_confidence=True``,
      and emits exactly one ``logger.warning`` documenting
      the deprecation. The deprecation log fires at most once
      per request.

    Only one of the two fields is typically supplied. Supplying
    both with conflicting intent is rejected at the handler
    level (the ``category_id`` path wins, the ``category``
    string is ignored, and the deprecation log does not fire).
    """

    model_config = ConfigDict(extra="forbid")

    category: str | None = Field(
        default=None,
        min_length=1,
        max_length=50,
        description=(
            "Deprecated. Free-form category string. Use category_id instead. "
            "When supplied (and category_id is None) the row is marked "
            "low_confidence=True and a deprecation log is emitted."
        ),
    )
    category_id: uuid.UUID | None = Field(
        default=None,
        description=(
            "UUID of a seeded Category row. When supplied, the FK and the "
            "denormalized label are written in a single transaction and the "
            "row is marked low_confidence=False."
        ),
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get(
    "",
    response_model=list[TransactionResponse],
    summary="List transactions with filters and pagination",
    responses={
        status.HTTP_200_OK: {
            "description": "A page of transactions matching the filters. May be empty.",
            "model": list[TransactionResponse],
        },
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "Invalid query parameter (e.g. min_amount > max_amount).",
        },
    },
)
async def list_transactions(
    session: Annotated[AsyncSession, Depends(get_session)],
    statement_id: Annotated[
        uuid.UUID | None,
        Query(description="Filter to a single statement."),
    ] = None,
    date_from: Annotated[
        date | None,
        Query(description="Inclusive lower bound on the posting date."),
    ] = None,
    date_to: Annotated[
        date | None,
        Query(description="Inclusive upper bound on the posting date."),
    ] = None,
    min_amount: Annotated[
        Decimal | None,
        Query(description="Inclusive lower bound on the absolute amount."),
    ] = None,
    max_amount: Annotated[
        Decimal | None,
        Query(description="Inclusive upper bound on the absolute amount."),
    ] = None,
    description: Annotated[
        str | None,
        Query(
            min_length=1,
            max_length=255,
            description="Partial, case-insensitive match against the description.",
        ),
    ] = None,
    limit: Annotated[
        int,
        Query(
            ge=_MIN_PAGE_SIZE,
            le=_MAX_PAGE_SIZE,
            description="Maximum rows to return.",
        ),
    ] = _DEFAULT_PAGE_SIZE,
    offset: Annotated[
        int,
        Query(ge=0, le=_MAX_OFFSET, description="Rows to skip from the start."),
    ] = 0,
) -> list[Transaction]:
    """Return a page of transactions matching the supplied filters.

    Filters compose with ``AND``. A filter omitted from the
    query is not applied. ``description`` uses SQL ``ILIKE`` so
    the match is case-insensitive — the only sensible default
    for a free-text search box.

    Pagination is the simple ``limit/offset`` flavour. A future
    WU may add cursor pagination for stable scrolling; for
    Phase 1, the dataset is small enough that ``OFFSET`` is
    fine and the implementation is trivial.
    """
    # The query is built incrementally — every filter is a single
    # ``WHERE`` clause, and we only add the ``ORDER BY`` once.
    query = select(Transaction)
    if statement_id is not None:
        query = query.where(Transaction.statement_id == statement_id)
    if date_from is not None:
        query = query.where(Transaction.date >= date_from)
    if date_to is not None:
        query = query.where(Transaction.date <= date_to)
    if min_amount is not None:
        # ``amount`` is signed; bounding the *absolute* value
        # means a refund of $1.000 and a charge of $1.000 both
        # match ``min_amount=500``. ``InstrumentedAttribute`` does
        # not expose ``.abs()`` directly, so we use SQL's
        # ``func.abs`` and compare in SQL rather than Python.
        query = query.where(func.abs(Transaction.amount) >= min_amount)
    if max_amount is not None:
        query = query.where(func.abs(Transaction.amount) <= max_amount)
    if description is not None:
        # ``ilike`` is PostgreSQL-specific; SQLite's ``LIKE`` is
        # already case-insensitive for ASCII. We use ``func.lower``
        # on both sides so the SQL is portable.
        needle = f"%{description.lower()}%"
        query = query.where(func.lower(Transaction.description).like(needle))

    # Stable order: oldest transaction first, with a tiebreaker
    # on the primary key so two rows with the same date do not
    # shift between pages.
    query = query.order_by(Transaction.date.asc(), Transaction.id.asc()).limit(limit).offset(offset)

    result = await session.execute(query)
    return list(result.scalars().all())


@router.patch(
    "/{transaction_id}",
    response_model=TransactionResponse,
    summary="Update a single transaction's category",
    responses={
        status.HTTP_200_OK: {
            "description": "Category updated.",
            "model": TransactionResponse,
        },
        status.HTTP_404_NOT_FOUND: {
            "description": "No transaction with that UUID, or the supplied category_id is unknown.",
        },
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "The body is empty (neither category_id nor category supplied).",
        },
    },
)
async def update_transaction(
    transaction_id: uuid.UUID,
    payload: TransactionCategoryUpdate,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Transaction:
    """Update a single transaction's category.

    Phase 2 — write-through semantics
    ---------------------------------

    Two fields drive the update, with a precedence order:

    1. ``category_id`` (preferred). The endpoint looks up the
       :class:`Category` row, writes the FK *and* the
       denormalized ``category`` string in a single
       transaction, and marks the row ``low_confidence=False``.
       A 404 is returned if the UUID does not match any row.
    2. ``category`` (legacy / deprecated). When ``category_id``
       is ``None`` and ``category`` is set, the endpoint writes
       the string, leaves ``category_id`` as ``NULL``, sets
       ``low_confidence=True``, and emits exactly one
       ``logger.warning`` documenting the deprecation. The log
       fires at most once per request, so a noisy client
       cannot flood the log stream.

    Empty bodies (neither field supplied) are rejected with 422
    so the caller knows the call was a no-op. The other
    transaction fields are derived from the source PDF and the
    LLM extraction; letting the user edit them in the same
    endpoint would let a typo silently rewrite history, so the
    body schema is closed (extra fields are rejected with 422
    by the Pydantic layer).
    """
    if payload.category_id is None and payload.category is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="At least one of `category_id` or `category` must be supplied",
        )

    result = await session.execute(select(Transaction).where(Transaction.id == transaction_id))
    transaction = result.scalar_one_or_none()
    if transaction is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Transaction {transaction_id} not found",
        )

    if payload.category_id is not None:
        # Preferred path — closed-set tag with the canonical
        # name. Look up the Category row (404 if missing),
        # then write the FK and the denormalized string in
        # the same commit.
        category_result = await session.execute(
            select(Category).where(Category.id == payload.category_id)
        )
        category = category_result.scalar_one_or_none()
        if category is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Category {payload.category_id} not found",
            )
        transaction.category_id = category.id
        transaction.category = category.name
        transaction.low_confidence = False
    elif payload.category is not None:
        # Legacy path — free-form string. The user is
        # bypassing the taxonomy, so the row is flagged. A
        # single deprecation log line is emitted (per
        # apply's risk note in ``tasks.md``).
        transaction.category_id = None
        transaction.category = payload.category
        transaction.low_confidence = True
        logger.warning(
            "TransactionCategoryUpdate deprecation: legacy `category: str` field used; "
            "client %s should migrate to `category_id` to keep `low_confidence=False`",
            transaction_id,
        )

    await session.commit()
    await session.refresh(transaction)
    return transaction


__all__ = [
    "TransactionCategoryUpdate",
    "list_transactions",
    "router",
    "update_transaction",
]
