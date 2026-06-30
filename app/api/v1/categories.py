"""Category-related HTTP endpoints.

Exposes the two endpoints the Phase 2 categories foundation needs:

* :func:`list_categories` — ``GET /api/v1/categories`` returns the
  12 closed-set rows ordered by ``sort_order`` so the UI can
  populate a ``<select>`` (PR #3). The endpoint also serves the
  sanity-check surface for a future admin tool that wants to
  re-render the prompt's closed set.
* :func:`rename_category` — ``POST /api/v1/categories/{id}``
  renames a single category in a single transaction. The
  ``Category`` UPDATE and the cascade UPDATE on every
  :class:`app.models.Transaction` whose ``category_id`` matches
  are wrapped in the same ``session.commit()`` so the rename is
  atomic (per design decision #7). A 422 is returned when the
  proposed ``name`` collides with another row's ``name`` so the
  client surfaces a meaningful error before the user is left
  looking at half-applied state.

Why a single router for two endpoints
------------------------------------

The two endpoints share the same prefix, the same tags, and
the same dependency injection shape. Splitting them into two
files would mean two ``APIRouter`` instances and two ``include_router``
calls for a total of one line of net benefit; keeping them
together matches the rest of the project (banks, statements,
transactions are all single-router modules).
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session
from app.models.category import Category
from app.models.transaction import Transaction
from app.schemas.domain import CategoryRenameRequest, CategoryResponse

router: APIRouter = APIRouter(prefix="/categories", tags=["categories"])


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get(
    "",
    response_model=list[CategoryResponse],
    summary="List the closed-set categories in display order",
    responses={
        status.HTTP_200_OK: {
            "description": (
                "A list of 12 categories ordered by ``sort_order`` ascending. "
                "May be empty before the seed migration runs."
            ),
            "model": list[CategoryResponse],
        },
    },
)
async def list_categories(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[Category]:
    """Return every :class:`Category` row ordered by ``sort_order`` ascending.

    The closed-set is seeded at migration time and the API
    contract is a stable 12-row list. The endpoint does not
    paginate and does not filter — the entire taxonomy is
    tiny and the UI needs the full list to render a
    ``<select>`` in one round-trip.
    """
    result = await session.execute(select(Category).order_by(Category.sort_order.asc()))
    return list(result.scalars().all())


@router.post(
    "/{category_id}",
    response_model=CategoryResponse,
    summary="Rename a category and propagate the change to its transactions",
    responses={
        status.HTTP_200_OK: {
            "description": "Category renamed; transactions updated atomically.",
            "model": CategoryResponse,
        },
        status.HTTP_404_NOT_FOUND: {
            "description": "No category with that UUID.",
        },
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": (
                "The proposed ``name`` collides with another row's ``name``, "
                "or neither ``name`` nor ``display_name`` was supplied."
            ),
        },
    },
)
async def rename_category(
    category_id: uuid.UUID,
    payload: CategoryRenameRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Category:
    """Rename a :class:`Category` and propagate the change atomically.

    Both ``name`` and ``display_name`` are optional per design
    decision #6 — the endpoint rejects a body with neither
    field. The endpoint is *stateless* (per design decision
    #11): no audit row, no history table, the rename is
    immediate.

    The rename and the propagation share a single
    ``session.commit()`` so the operation is atomic (per
    design decision #7). The propagation updates every
    :class:`Transaction` whose ``category_id`` matches the
    renamed category's UUID, setting ``Transaction.category``
    to the new ``Category.name`` so any read-path that joins
    on the denormalized string sees the change immediately.
    The propagation is a filtered UPDATE — only matching
    ``category_id`` rows are touched.

    Collision check
    ---------------

    The proposed ``name`` (if supplied and different from
    the current value) is checked against every *other*
    row's ``name`` before the UPDATE runs. A collision
    returns 422 with a descriptive error so the client can
    surface it before the user sees a half-applied state.
    The explicit check gives a cleaner 422 than the
    ``UNIQUE`` constraint on ``categories.name`` would (it
    would surface as a 500 from the constraint violation).
    """
    if payload.name is None and payload.display_name is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="At least one of `name` or `display_name` must be supplied",
        )

    # 1. Look up the category to be renamed. 404 if the
    #    UUID does not match any row. The check happens
    #    *before* any write so a bad UUID is a clean 404,
    #    not a half-applied rename.
    result = await session.execute(select(Category).where(Category.id == category_id))
    category = result.scalar_one_or_none()
    if category is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Category {category_id} not found",
        )

    # 2. Collision check on the proposed ``name`` (only
    #    when the field is supplied *and* the value
    #    actually changes). Compare against every
    #    *other* row — the unique constraint is the
    #    second line of defence, but the explicit check
    #    gives a clean 422 instead of a 500 from a
    #    constraint violation.
    if payload.name is not None and payload.name != category.name:
        collision = await session.execute(
            select(Category.id).where(
                Category.name == payload.name,
                Category.id != category_id,
            )
        )
        if collision.scalar_one_or_none() is not None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=(f"Category name {payload.name!r} is already taken by another category"),
            )
        # 3a. Apply the new ``name`` to the Category row.
        category.name = payload.name

        # 3b. Propagate the new ``name`` to every
        #     transaction whose ``category_id`` matches.
        #     The UPDATE is scoped to the matching FK so
        #     non-matching rows are left alone.
        await session.execute(
            update(Transaction)
            .where(Transaction.category_id == category_id)
            .values(category=payload.name)
        )

    if payload.display_name is not None:
        category.display_name = payload.display_name

    # Single ``commit`` covers the Category update and the
    # transaction cascade. The commit implicitly flushes
    # the pending object mutations on ``category``, so the
    # explicit ``flush()`` the previous ``async with
    # session.begin()`` block had is no longer needed.
    await session.commit()
    # ``refresh`` pulls the post-commit state into the
    # in-memory object so the Pydantic response model sees
    # the new ``updated_at`` (set by the server-side
    # ``CURRENT_TIMESTAMP`` default) without a second
    # round-trip.
    await session.refresh(category)
    return category


__all__ = ["list_categories", "rename_category", "router"]
