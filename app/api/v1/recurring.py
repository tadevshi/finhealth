"""Recurring-rule HTTP endpoints.

Exposes the two endpoints the Phase 2 PR #5 detector needs:

* :func:`list_recurring_rules` — ``GET /api/v1/recurring``
  returns every :class:`app.models.recurring_rule.RecurringRule`
  row where ``is_active=True``, ordered by ``last_seen_date``
  descending so the freshest patterns show first. Mirrors
  :func:`app.api.v1.merchants.list_merchants` and
  :func:`app.api.v1.categories.list_categories` in shape.
* :func:`update_recurring_rule` — ``PATCH /api/v1/recurring/{id}``
  flips the ``is_active`` flag on an existing rule. The
  endpoint is atomic: a single ``commit()`` covers the 404
  check (no rule), the body validation, and the row update.
  The ``recurring_rule_id`` FK on existing transactions is
  preserved (per design D) — the API filter excludes inactive
  rules from the read side, and the historical link remains
  for audit.

Why a single router for two endpoints
------------------------------------

The two endpoints share the same prefix, the same tags, and
the same dependency injection shape. Splitting them into two
files would mean two :class:`APIRouter` instances and two
``include_router`` calls for a total of one line of net
benefit; keeping them together matches the rest of the
project (banks, statements, transactions, categories,
merchants are all single-router modules).
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session
from app.models.recurring_rule import RecurringRule
from app.schemas.domain import RecurringRuleResponse, RecurringRuleUpdate

router: APIRouter = APIRouter(prefix="/recurring", tags=["recurring"])


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get(
    "",
    response_model=list[RecurringRuleResponse],
    summary="List the active recurring-transaction rules, freshest first",
    responses={
        status.HTTP_200_OK: {
            "description": (
                "A list of active rules ordered by ``last_seen_date`` "
                "descending. May be empty before the first ingest."
            ),
            "model": list[RecurringRuleResponse],
        },
    },
)
async def list_recurring_rules(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[RecurringRule]:
    """Return every active :class:`RecurringRule` ordered by ``last_seen_date`` desc.

    Inactive rules (``is_active=False``) are excluded — the
    user only sees the patterns the detector currently
    believes in. The detector's upsert path ignores the
    ``is_active`` flag, so a re-detected pattern on an
    inactive rule updates the same row (deactivation
    preserves the historical ``recurring_rule_id`` FK on
    the matched transactions).
    """
    result = await session.execute(
        select(RecurringRule)
        .where(RecurringRule.is_active.is_(True))
        .order_by(RecurringRule.last_seen_date.desc())
    )
    return list(result.scalars().all())


@router.patch(
    "/{rule_id}",
    response_model=RecurringRuleResponse,
    summary="Activate or deactivate an existing rule",
    responses={
        status.HTTP_200_OK: {
            "description": "Rule updated; the new state is returned.",
            "model": RecurringRuleResponse,
        },
        status.HTTP_404_NOT_FOUND: {
            "description": "No rule with that UUID.",
        },
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "The request body is missing ``is_active`` or has extra fields.",
        },
    },
)
async def update_recurring_rule(
    rule_id: uuid.UUID,
    payload: RecurringRuleUpdate,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> RecurringRule:
    """Set the ``is_active`` flag on the rule at ``rule_id``.

    The flow is a 404 check + a single ``commit()``:

    1. Look up the rule by UUID; 404 if it does not exist.
       The check happens *before* any write so a bad UUID is
       a clean 404, not a half-applied update.
    2. Set ``rule.is_active = payload.is_active`` and commit
       in a single round-trip so the response carries the
       post-commit state (the Pydantic model needs the
       server-side ``updated_at`` refresh).

    The detector's upsert path ignores the ``is_active``
    flag, so a deactivation here does NOT prevent the
    detector from updating the rule on the next ingest
    run. The flag is purely a read-side filter on
    :func:`list_recurring_rules` — the user toggles it to
    hide patterns they do not want to see, the detector
    keeps the rule's row current.
    """
    # 1. 404 check first so a bad UUID is a clean 404, not
    #    a 500 from a missing FK.
    rule_result = await session.execute(select(RecurringRule).where(RecurringRule.id == rule_id))
    rule = rule_result.scalar_one_or_none()
    if rule is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"RecurringRule {rule_id} not found",
        )

    # 2. Atomic single-commit update + refresh. The detector
    #    recomputes ``last_seen_date`` / ``occurrences`` /
    #    ``confidence`` on its own; this endpoint only flips
    #    the visibility flag.
    rule.is_active = payload.is_active
    await session.commit()
    await session.refresh(rule)
    return rule


__all__ = ["list_recurring_rules", "router", "update_recurring_rule"]
