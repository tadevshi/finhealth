"""Deterministic, privacy-safe transaction candidate parsing.

This module deliberately consumes local text only.  Its output has enough
evidence to reconcile an LLM enrichment response without ever retaining the
source row text.
"""

from __future__ import annotations

import hashlib
import re
from contextlib import suppress
from dataclasses import replace
from datetime import date
from decimal import Decimal

from app.services.pdf.amount_parser import parse_amount
from app.services.pdf.sanitizer import sanitize_review_description
from app.services.pdf.strategies.base import ParsedCandidate

_DATE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{2,4})\b")
_AMOUNT = re.compile(r"(?:US\$|\$)\s*[+-]?[\d.,]+", re.IGNORECASE)
# These labels are statement-level headers or totals, not row-level evidence.
# Keep the allowlist deliberately narrow: unrecognised date/amount-shaped text
# becomes a sanitized parse-error ledger entry instead of disappearing.
_NON_TRANSACTION_BOILERPLATE = re.compile(
    r"\b(?:fecha de (?:facturaci[oó]n|vencimiento)|total(?:\s+\w+){0,2}|"
    r"saldo (?:anterior|actual)|pago m[ií]nimo|cup[oó]n de pago)\b",
    re.IGNORECASE,
)


def canonical_amount(value: Decimal) -> str:
    """Return a stable fixed-point representation for an identity payload."""
    return format(value.normalize(), "f")


def derive_row_identity(
    *,
    file_hash: str,
    variant: str,
    page_index: int,
    row_index: int,
    transaction_date: date,
    amount: Decimal,
    currency: str,
) -> str:
    """Return the versioned 64-character candidate identity specified by SDD."""
    parts = (
        "candidate-v1",
        file_hash.lower(),
        variant.upper(),
        str(page_index),
        str(row_index),
        transaction_date.isoformat(),
        canonical_amount(amount),
        currency.upper(),
    )
    return hashlib.sha256("\0".join(parts).encode()).hexdigest()


def _derive_unparsed_identity(
    *, file_hash: str, variant: str, page_index: int, row_index: int, currency: str
) -> str:
    """Derive a stable location identity without retaining malformed source text."""
    parts = (
        "candidate-v1",
        file_hash.lower(),
        variant.upper(),
        str(page_index),
        str(row_index),
        "",
        "",
        currency,
    )
    return hashlib.sha256("\0".join(parts).encode()).hexdigest()


def _parse_matched_rows(*, text: str, file_hash: str, variant: str) -> list[ParsedCandidate]:
    """Parse a row only after its bank/layout strategy has been selected."""
    currency = "CLP" if variant.upper() == "NACIONAL" else "USD"
    candidates: list[ParsedCandidate] = []
    for page_index, page in enumerate(text.split("\f")):
        for row_index, line in enumerate(page.splitlines()):
            if _NON_TRANSACTION_BOILERPLATE.search(line):
                continue
            match = _DATE.search(line)
            amount_match = _AMOUNT.search(line)
            if match is None and amount_match is None:
                continue
            transaction_date: date | None = None
            amount: Decimal | None = None
            if match is not None:
                try:
                    day, month, year = (int(value) for value in match.groups())
                    if year < 100:
                        year += 2000 if year < 70 else 1900
                    transaction_date = date(year, month, day)
                except ValueError:
                    pass
            if amount_match is not None:
                with suppress(ValueError, ArithmeticError):
                    amount = parse_amount(amount_match.group(), currency)
            if transaction_date is not None and amount is not None:
                identity = derive_row_identity(
                    file_hash=file_hash,
                    variant=variant,
                    page_index=page_index,
                    row_index=row_index,
                    transaction_date=transaction_date,
                    amount=amount,
                    currency=currency,
                )
                failure_code = None
            else:
                identity = _derive_unparsed_identity(
                    file_hash=file_hash,
                    variant=variant,
                    page_index=page_index,
                    row_index=row_index,
                    currency=currency,
                )
                failure_code = "parse_error"
            candidates.append(
                ParsedCandidate(
                    identity,
                    page_index,
                    row_index,
                    transaction_date,
                    amount,
                    currency,
                    failure_code,
                    sanitize_review_description(line),
                )
            )
    return mark_identity_collisions(candidates)


def parse_candidates(*, text: str, file_hash: str, variant: str) -> list[ParsedCandidate]:
    """Return candidates only for one supported, positively identified layout."""
    from app.services.pdf.strategies import DEFAULT_STRATEGIES, select_strategy

    selection = select_strategy(text, DEFAULT_STRATEGIES)
    if selection.strategy is None:
        return []
    return selection.strategy.parse(text=text, file_hash=file_hash)


def parse_failure_code(text: str) -> str | None:
    """Return the structured layout failure without manufacturing a candidate."""
    from app.services.pdf.strategies import DEFAULT_STRATEGIES, select_strategy

    return select_strategy(text, DEFAULT_STRATEGIES).reason_code


def mark_identity_collisions(candidates: list[ParsedCandidate]) -> list[ParsedCandidate]:
    """Quarantine every duplicate identity; never silently deduplicate rows."""
    counts: dict[str, int] = {}
    for candidate in candidates:
        counts[candidate.row_identity] = counts.get(candidate.row_identity, 0) + 1
    return [
        replace(candidate, failure_code="identity_collision")
        if counts[candidate.row_identity] > 1
        else candidate
        for candidate in candidates
    ]


__all__ = [
    "ParsedCandidate",
    "canonical_amount",
    "derive_row_identity",
    "mark_identity_collisions",
    "parse_candidates",
    "parse_failure_code",
]
