"""Transaction-related HTTP endpoints.

The transactions router owns the *read*, *edit* and *create* of
individual :class:`app.models.Transaction` rows:

* :func:`list_transactions` — filterable, paginated list.
* :func:`create_transaction` — JSON single creation (statement-linked).
* :func:`create_transaction_batch` — JSON batch creation (1-200 items).
* :func:`update_transaction` — patch a single transaction's category.

Statement creation (PDF upload) lives in :mod:`app.api.v1.statements`;
the boundary follows the aggregate root: a statement owns its
transactions, but reading, creating and editing individual rows does
not require loading the parent statement. Creation delegates all
business rules to :class:`TransactionCreationService` — the handlers
below are thin transport shims.
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Annotated, Final

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session
from app.models import Category, Transaction
from app.schemas.domain import (
    TransactionBatchCreate,
    TransactionBatchResponse,
    TransactionCreate,
    TransactionResponse,
)
from app.services.transaction_creation import (
    TransactionCreationError,
    TransactionCreationService,
)
from app.web.router import parse_optional_date, parse_optional_decimal

logger = logging.getLogger(__name__)

router: APIRouter = APIRouter(prefix="/transactions", tags=["transactions"])


# ---------------------------------------------------------------------------
# Sentinel for the empty-string ``category_id`` form value
# ---------------------------------------------------------------------------


class _ClearCategoryIdSentinel:
    """Marker returned by the ``category_id`` validator on an empty string.

    HTMX serialises the per-row ``<select>``'s blank "—" option
    as ``category_id=`` (an empty string). The handler must
    distinguish three states from the Pydantic model's
    perspective:

    * ``category_id`` not in the form              -> field absent
    * ``category_id=""`` in the form               -> user wants to clear
    * ``category_id="<uuid>"`` in the form         -> user picked a category

    Pydantic collapses the first two into ``None`` after UUID
    validation, so the handler cannot tell them apart. The
    validator instead returns this sentinel for the second
    case; the handler checks for it and clears the FK. Using
    a class instance (rather than a string literal) keeps the
    type signature ``uuid.UUID | None`` readable — only the
    handler has to know about the third "clear" state.
    """

    __slots__ = ()


_CLEAR_CATEGORY_ID: Final = _ClearCategoryIdSentinel()


# ---------------------------------------------------------------------------
# Shared template renderer (for the PATCH HTML branch)
# ---------------------------------------------------------------------------


#: Templates directory resolved relative to this file so the
#: router works regardless of the working directory the app is
#: launched from. The same pattern is used by ``app.web.router``.
#: The PATCH endpoint reuses the same partial template the web
#: router renders for ``GET /transactions/rows`` so the markup
#: stays single-sourced.
_TEMPLATES_DIR: Path = Path(__file__).parent.parent.parent / "web" / "templates"
_templates: Jinja2Templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


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

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

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
    category_id: uuid.UUID | None | _ClearCategoryIdSentinel = Field(
        default=None,
        description=(
            "UUID of a seeded Category row. When supplied, the FK and the "
            "denormalized label are written in a single transaction and the "
            "row is marked low_confidence=False. The empty string "
            "(``category_id=``) is the per-row ``<select>``'s blank '—' "
            "option and clears the FK without a 422."
        ),
    )

    @field_validator("category_id", mode="before")
    @classmethod
    def _coerce_empty_category_id(cls, value: object) -> object:
        """Map the empty-string form value to a clear-sentinel.

        The per-row ``<select>``'s blank "—" option
        serialises as ``category_id=`` (an empty string)
        when the user picks "no category". Pydantic would
        otherwise reject ``""`` as an invalid UUID with
        422; the handler needs a distinct "clear" state
        from "field absent" (which the 422 check guards).
        The sentinel is the bridge between the form's
        empty string and the handler's "clear the FK"
        branch.
        """
        if value == "":
            return _CLEAR_CATEGORY_ID
        return value


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
        str | None,
        Query(
            description=(
                "Inclusive lower bound on the posting date (ISO YYYY-MM-DD). "
                "An empty string means no filter."
            ),
        ),
    ] = None,
    date_to: Annotated[
        str | None,
        Query(
            description=(
                "Inclusive upper bound on the posting date (ISO YYYY-MM-DD). "
                "An empty string means no filter."
            ),
        ),
    ] = None,
    min_amount: Annotated[
        str | None,
        Query(
            description=(
                "Inclusive lower bound on the absolute amount. An empty string means no filter."
            ),
        ),
    ] = None,
    max_amount: Annotated[
        str | None,
        Query(
            description=(
                "Inclusive upper bound on the absolute amount. An empty string means no filter."
            ),
        ),
    ] = None,
    description: Annotated[
        str | None,
        Query(
            min_length=1,
            max_length=255,
            description="Partial, case-insensitive match against the description.",
        ),
    ] = None,
    category_id: Annotated[
        list[uuid.UUID] | None,
        Query(
            description=(
                "Repeatable filter — limit to transactions whose category_id "
                "matches any of the supplied UUIDs. Combine with `uncategorized=true` "
                "to also include untagged rows (NULL or low_confidence=True)."
            ),
        ),
    ] = None,
    uncategorized: Annotated[
        bool,
        Query(
            description=(
                "When true, also include transactions whose category_id is NULL or "
                "whose low_confidence flag is true (i.e. tagged with a free-form "
                "string by the legacy `category: str` field)."
            ),
        ),
    ] = False,
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
    # Fix 3: empty-string values (``date_from=``) mean "no filter" —
    # the HTML filter form serialises untouched fields that way —
    # while garbage non-empty values keep the strict 422 contract.
    parsed_date_from = parse_optional_date(date_from, field="date_from")
    parsed_date_to = parse_optional_date(date_to, field="date_to")
    parsed_min_amount = parse_optional_decimal(min_amount, field="min_amount")
    parsed_max_amount = parse_optional_decimal(max_amount, field="max_amount")

    # The query is built incrementally — every filter is a single
    # ``WHERE`` clause, and we only add the ``ORDER BY`` once.
    query = select(Transaction)
    if statement_id is not None:
        query = query.where(Transaction.statement_id == statement_id)
    if parsed_date_from is not None:
        query = query.where(Transaction.date >= parsed_date_from)
    if parsed_date_to is not None:
        query = query.where(Transaction.date <= parsed_date_to)
    if parsed_min_amount is not None:
        # ``amount`` is signed; bounding the *absolute* value
        # means a refund of $1.000 and a charge of $1.000 both
        # match ``min_amount=500``. ``InstrumentedAttribute`` does
        # not expose ``.abs()`` directly, so we use SQL's
        # ``func.abs`` and compare in SQL rather than Python.
        query = query.where(func.abs(Transaction.amount) >= parsed_min_amount)
    if parsed_max_amount is not None:
        query = query.where(func.abs(Transaction.amount) <= parsed_max_amount)
    if description is not None:
        # ``ilike`` provides case-insensitive PostgreSQL matching.
        # already case-insensitive for ASCII. We use ``func.lower``
        # on both sides so the SQL is portable.
        needle = f"%{description.lower()}%"
        query = query.where(func.lower(Transaction.description).like(needle))

    # Category filters — the closed-set ``category_id`` UUID and
    # the "untagged" sentinel compose as a parenthesized ``OR``:
    #
    # * both supplied  -> ``category_id IN (...) OR (IS NULL OR low_confidence=True)``
    # * only UUIDs     -> ``category_id IN (...)``
    # * only untagged  -> ``(category_id IS NULL OR low_confidence=True)``
    # * neither        -> no WHERE clause
    #
    # Wrapping each branch in a parenthesized ``or_`` keeps the
    # boolean precedence correct when combined with the AND
    # filters above (otherwise the second branch would silently
    # re-AND the closed-set UUIDs). Each branch is a single
    # ``where`` call so the SQL stays one statement.
    if category_id or uncategorized:
        # ``ColumnElement`` is the common supertype so the
        # ``.in_(...)`` and ``.is_(...)`` calls (which return
        # different SQL element types) compose into one
        # list. ``BinaryExpression`` in the type stub is too
        # narrow for the union.
        from sqlalchemy.sql.elements import ColumnElement

        clauses: list[ColumnElement[bool]] = []
        if category_id:
            clauses.append(Transaction.category_id.in_(category_id))
        if uncategorized:
            clauses.append(
                or_(
                    Transaction.category_id.is_(None),
                    Transaction.low_confidence.is_(True),
                )
            )
        query = query.where(or_(*clauses))

    # Stable order: oldest transaction first, with a tiebreaker
    # on the primary key so two rows with the same date do not
    # shift between pages.
    query = query.order_by(Transaction.date.asc(), Transaction.id.asc()).limit(limit).offset(offset)

    result = await session.execute(query)
    return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Creation: thin POST handlers over the creation service
# ---------------------------------------------------------------------------


#: Domain error code -> HTTP status. The service stays transport-agnostic;
#: this table is the single mapping point for the creation routes.
_CREATION_STATUS_BY_CODE: Final[dict[str, int]] = {
    "invalid_batch": status.HTTP_422_UNPROCESSABLE_CONTENT,
    "statement_metadata_required": status.HTTP_422_UNPROCESSABLE_CONTENT,
    "duplicate_statement_metadata": status.HTTP_422_UNPROCESSABLE_CONTENT,
    "statement_already_exists": status.HTTP_409_CONFLICT,
    "statement_creation_conflict": status.HTTP_409_CONFLICT,
    "credit_card_not_found": status.HTTP_404_NOT_FOUND,
    "category_not_found": status.HTTP_404_NOT_FOUND,
    "unsupported_currency": status.HTTP_400_BAD_REQUEST,
    "currency_mismatch": status.HTTP_400_BAD_REQUEST,
    "creation_failed": status.HTTP_500_INTERNAL_SERVER_ERROR,
}


def _raise_creation_failure(error: TransactionCreationError, *, include_index: bool) -> None:
    """Translate a domain creation failure into the documented HTTP envelope.

    The body is ``{"detail": {"code", "message", "field"?, "index"?}}``
    with ``field``/``index`` omitted when inapplicable. Single-route
    mapping omits the index (there is only one item); batch mapping keeps
    it so the client can locate the failing row. The service has already
    rolled back its outer transaction, so no request-owned writes exist
    when this runs.
    """
    detail: dict[str, object] = {"code": error.code, "message": error.message}
    if error.field is not None:
        detail["field"] = error.field
    if include_index and error.index is not None:
        detail["index"] = error.index
    raise HTTPException(status_code=_CREATION_STATUS_BY_CODE[error.code], detail=detail) from error


@router.post(
    "",
    response_model=TransactionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create one transaction linked to an existing or new statement",
    responses={
        status.HTTP_201_CREATED: {
            "description": "Transaction created and committed.",
            "model": TransactionResponse,
        },
        status.HTTP_400_BAD_REQUEST: {
            "description": "Unsupported currency or currency mismatch with the parent card.",
        },
        status.HTTP_404_NOT_FOUND: {
            "description": "Unknown card (nested metadata) or category_id.",
        },
        status.HTTP_409_CONFLICT: {
            "description": "Statement already exists (metadata supplied for an existing "
            "parent) or was created concurrently.",
        },
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "Invalid input or missing new-parent metadata.",
        },
    },
)
async def create_transaction(
    payload: TransactionCreate,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> JSONResponse:
    """Create one transaction atomically against its parent statement.

    Every item requires ``statement_id``: an existing parent is referenced
    by ID only (nested metadata is a 409 conflict), and a missing parent is
    created under that UUID from the nested ``statement`` metadata with
    ``source=api`` and ``status=completed``. Currency must match the parent
    card exactly (CLP or USD). The response is a single
    :class:`TransactionResponse`; duplicate-looking submissions are not
    deduplicated — every success appends a new row.
    """
    try:
        snapshots = await TransactionCreationService(session).create_many([payload])
    except TransactionCreationError as error:
        _raise_creation_failure(error, include_index=False)
    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content=snapshots[0].model_dump(mode="json"),
    )


@router.post(
    "/batch",
    response_model=TransactionBatchResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create 1-200 transactions in input order",
    responses={
        status.HTTP_201_CREATED: {
            "description": "Batch committed atomically; responses keep input order.",
            "model": TransactionBatchResponse,
        },
        status.HTTP_400_BAD_REQUEST: {
            "description": "Unsupported currency or currency mismatch with a parent card.",
        },
        status.HTTP_404_NOT_FOUND: {
            "description": "Unknown card (nested metadata) or category_id.",
        },
        status.HTTP_409_CONFLICT: {
            "description": "Statement already exists (metadata supplied for an existing "
            "parent) or was created concurrently.",
        },
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "Invalid input, batch bounds, or missing/duplicate new-parent metadata.",
        },
    },
)
async def create_transaction_batch(
    payload: TransactionBatchCreate,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> JSONResponse:
    """Create 1-200 transactions atomically in input order.

    Same rules as the single route; repeated items for one new statement
    share the single nested metadata object, additional objects are a 422
    at the second metadata-bearing index, and item-level failures carry
    their zero-based input index. ``count`` is constructed from the
    committed response list, never supplied by the client.
    """
    try:
        snapshots = await TransactionCreationService(session).create_many(payload.transactions)
    except TransactionCreationError as error:
        _raise_creation_failure(error, include_index=True)
    batch = TransactionBatchResponse(transactions=snapshots, count=len(snapshots))
    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content=batch.model_dump(mode="json"),
    )


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
    payload: Annotated[TransactionCategoryUpdate, Form()],
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Transaction | HTMLResponse:
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

    Phase 2 PR #3 — Accept header negotiation & form-encoded body
    -------------------------------------------------------------

    The endpoint reads the body as ``application/x-www-form-urlencoded``
    (``Form()``) rather than JSON, because the only first-party
    caller is the per-row ``<select>``'s ``hx-patch`` and HTMX
    serialises ``hx-patch`` bodies as form fields by default.
    The form fields match the Pydantic model field names
    (``category_id`` and ``category``), so the Pydantic
    ``extra="forbid"`` rule still applies: an unknown form
    field is rejected with 422.

    The endpoint returns one of two shapes depending on the
    ``Accept`` header:

    * ``text/html`` — returns an :class:`HTMLResponse` with the
      partial ``<tr>`` row (the same template the web router
      uses for ``GET /transactions/rows``). This is the HTMX
      swap path: the browser-side ``hx-patch`` triggers
      ``outerHTML`` and the server is the single source of
      truth for the new ``<select>`` state, so we do not have
      to mutate DOM state on the client.
    * everything else (the default; the JSON contract) —
      returns a :class:`TransactionResponse` as before.

    The ``response_model=TransactionResponse`` on the route
    decorator is still correct: FastAPI uses it to validate
    the *non-HTML* return value. The HTML branch returns
    ``HTMLResponse`` directly, which is a ``Response``
    subclass and is short-circuited by FastAPI without
    re-serialisation.

    Both branches execute the same write-through — the
    Accept header only changes the response shape, not the
    database mutation.
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

    if isinstance(payload.category_id, _ClearCategoryIdSentinel):
        # Empty-string form value — the per-row ``<select>``'s
        # blank "—" option. The user explicitly untagged the
        # row, so we clear the FK, drop the denormalized
        # string, and mark the row ``low_confidence=True``
        # (no category to be confident about). This is the
        # same end-state as the ingestion path for an
        # LLM-emitted ``category=None`` per PR #2 spec
        # scenario 6.
        transaction.category_id = None
        transaction.category = None
        transaction.low_confidence = True
    elif payload.category_id is not None:
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

    # Accept header dispatch (Phase 2 PR #3). The HTML branch
    # renders the partial template with the just-mutated row
    # plus the full category list (so the new <select> shows
    # the new "selected" option). The JSON branch is the
    # unchanged TransactionResponse contract.
    accept = request.headers.get("accept", "").lower()
    if "text/html" in accept:
        categories_result = await session.execute(
            select(Category).order_by(Category.sort_order.asc())
        )
        categories = list(categories_result.scalars().all())
        # The write-through above already committed, so the
        # mutation is durable. The render is the only step
        # that can still fail; if it does (missing context
        # key, a future template edit, etc.) we return a
        # generic 500 without leaking the traceback. The
        # user can refresh the page to see the new state via
        # the partial endpoint instead.
        try:
            return _templates.TemplateResponse(
                request=request,
                name="partials/transactions_table.html",
                context={
                    "transactions": [transaction],
                    "categories": categories,
                    "error": None,
                },
            )
        except Exception:
            logger.exception(
                "Failed to render PATCH HTML partial for transaction %s",
                transaction_id,
            )
            return HTMLResponse(
                content="<tr><td colspan='5'>Error rendering row</td></tr>",
                status_code=500,
            )

    return transaction


__all__ = [
    "TransactionCategoryUpdate",
    "create_transaction",
    "create_transaction_batch",
    "list_transactions",
    "router",
    "update_transaction",
]
