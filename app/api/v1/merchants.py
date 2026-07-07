"""Merchant-related HTTP endpoints.

Exposes the two endpoints the Phase 2 PR #4 merchant foundation
needs:

* :func:`list_merchants` — ``GET /api/v1/merchants`` returns
  every :class:`app.models.merchant.Merchant` row ordered by
  ``name`` ascending so the UI can populate a ``<select>`` in
  one round-trip (mirrors :func:`app.api.v1.categories.list_categories`).
* :func:`add_alias` — ``POST /api/v1/merchants/{id}/aliases``
  binds a user-supplied ``alias_text`` to an existing
  :class:`Merchant`. The endpoint is atomic: a single
  ``commit()`` covers the 404 check (no merchant), the
  422 check (duplicate alias), and the row insert.

Why a single router for two endpoints
------------------------------------

The two endpoints share the same prefix, the same tags, and
the same dependency injection shape. Splitting them into two
files would mean two ``APIRouter`` instances and two
``include_router`` calls for a total of one line of net
benefit; keeping them together matches the rest of the
project (banks, statements, transactions, categories are
all single-router modules).
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session
from app.models.merchant import Merchant, MerchantAlias, MerchantAliasSource
from app.schemas.domain import (
    MerchantAliasCreate,
    MerchantAliasResponse,
    MerchantResponse,
)
from app.services.merchants import normalize

router: APIRouter = APIRouter(prefix="/merchants", tags=["merchants"])


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get(
    "",
    response_model=list[MerchantResponse],
    summary="List the canonical merchants in alphabetical order",
    responses={
        status.HTTP_200_OK: {
            "description": (
                "A list of merchants ordered by ``name`` ascending. "
                "May be empty before the first upload."
            ),
            "model": list[MerchantResponse],
        },
    },
)
async def list_merchants(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[Merchant]:
    """Return every :class:`Merchant` row ordered by ``name`` ascending.

    The list is intentionally unfiltered — the UI renders the
    full set in a ``<select>`` so the user can pick a merchant
    to inspect. Active-flag filtering is a future enhancement
    (the schema already has ``is_active`` for soft-delete
    semantics, but the v1 endpoint returns all rows).
    """
    result = await session.execute(select(Merchant).order_by(Merchant.name.asc()))
    return list(result.scalars().all())


@router.post(
    "/{merchant_id}/aliases",
    response_model=MerchantAliasResponse,
    summary="Bind a user-supplied alias to an existing merchant",
    responses={
        status.HTTP_200_OK: {
            "description": "Alias created; the new row is returned.",
            "model": MerchantAliasResponse,
        },
        status.HTTP_404_NOT_FOUND: {
            "description": "No merchant with that UUID.",
        },
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": (
                "The ``alias_text`` collides with an existing "
                "alias (the ``UNIQUE(alias_text)`` constraint on "
                "``merchant_aliases`` blocks the duplicate)."
            ),
        },
    },
)
async def add_alias(
    merchant_id: uuid.UUID,
    payload: MerchantAliasCreate,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> MerchantAlias:
    """Bind ``payload.alias_text`` to the merchant at ``merchant_id``.

    The flow is a three-step check + write:

    1. Look up the merchant by UUID; 404 if it does not exist.
       The check happens *before* any write so a bad UUID is
       a clean 404, not a half-applied alias.
    2. Compute the canonical ``normalized`` form via
       :func:`app.services.merchants.normalize` server-side —
       the user does not have to know the canonicalisation
       rules.
    3. Insert the :class:`MerchantAlias` row with
       ``source='user'``. A duplicate ``alias_text`` raises
       :class:`sqlalchemy.exc.IntegrityError`; the handler
       catches it and returns 422 with a clear message so
       the client can surface the conflict to the user.

    The single ``commit()`` at the end covers the insert
    and the post-commit ``refresh`` so the Pydantic response
    model sees the database-set ``created_at`` /
    ``updated_at`` values.
    """
    # 1. 404 check first so a bad UUID is a clean 404, not
    #    a 500 from a missing FK.
    merchant_result = await session.execute(select(Merchant).where(Merchant.id == merchant_id))
    merchant = merchant_result.scalar_one_or_none()
    if merchant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Merchant {merchant_id} not found",
        )

    # 2. Canonical form (server-side). Empty ``normalize``
    #    result means the alias is whitespace-only or
    #    otherwise non-canonicalisable — a 422 lets the
    #    client surface the error before the user sees a
    #    half-applied alias.
    canonical = normalize(payload.alias_text)
    if not canonical:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                f"alias_text {payload.alias_text!r} normalises to an "
                "empty string; supply a non-blank alias"
            ),
        )

    # 3. Insert with the user source. The IntegrityError
    #    catch handles ``UNIQUE(alias_text)`` collisions —
    #    a defensive check (the canonical form could
    #    legitimately collide with an existing alias that
    #    happened to come from a different raw string).
    alias = MerchantAlias(
        merchant_id=merchant.id,
        alias_text=payload.alias_text,
        normalized=canonical,
        source=MerchantAliasSource.USER,
        confidence=None,
    )
    session.add(alias)
    try:
        await session.commit()
    except IntegrityError as exc:
        # Roll back the failed insert before raising so the
        # session is clean for the next request.
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                f"alias_text {payload.alias_text!r} is already bound to "
                "an existing merchant (UNIQUE(alias_text) collision)"
            ),
        ) from exc

    # ``refresh`` pulls the post-commit state into the
    # in-memory object so the Pydantic response model sees
    # the new ``created_at`` / ``updated_at`` (set by the
    # server-side ``CURRENT_TIMESTAMP`` default) without a
    # second round-trip.
    await session.refresh(alias)
    return alias


__all__ = ["add_alias", "list_merchants", "router"]
