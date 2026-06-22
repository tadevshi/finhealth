"""Bank-related HTTP endpoints.

Exposes a read-only listing of every :class:`app.models.Bank` row the
UI needs to populate a dropdown. The endpoint is intentionally tiny:
no pagination, no filters, no POST. Banks are seeded by the
migration; operators do not add them at runtime in Phase 1.

The response is a small subset of the full bank row — ``id``,
``name``, ``display_name`` — so the front-end has everything it
needs to render ``<option value="<name>"><display_name></option>``
without a second round-trip. ``password_formula`` is *not* exposed;
it is an internal detail and has no place in a UI payload.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session
from app.models.bank import Bank

router: APIRouter = APIRouter(prefix="/banks", tags=["banks"])


class BankListItem(BaseModel):
    """One row in the ``GET /banks`` response.

    The model is deliberately narrow: only the fields the
    upload form needs. ``password_formula`` is internal and is
    never sent to the client.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    display_name: str


@router.get(
    "",
    response_model=list[BankListItem],
    summary="List the active banks available for statement upload",
    responses={
        status.HTTP_200_OK: {
            "description": "A list of active banks. May be empty before the seed migration runs.",
            "model": list[BankListItem],
        },
    },
)
async def list_banks(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[Bank]:
    """Return every active :class:`Bank` ordered by ``display_name``.

    Only ``is_active=True`` rows are returned so a bank that has
    been soft-disabled in the future does not appear in the
    upload dropdown. Ordering is by ``display_name`` (the
    human-readable label) so the dropdown reads alphabetically
    by what the user sees, not by the internal ``name`` slug.
    """
    result = await session.execute(
        select(Bank).where(Bank.is_active.is_(True)).order_by(Bank.display_name.asc())
    )
    return list(result.scalars().all())


__all__ = ["BankListItem", "list_banks", "router"]
