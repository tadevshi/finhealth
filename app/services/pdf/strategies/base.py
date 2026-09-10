"""Contracts shared by deterministic statement-layout parsers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Protocol


@dataclass(frozen=True, slots=True)
class ParsedCandidate:
    """Local deterministic evidence for one statement row."""

    row_identity: str
    page_index: int
    row_index: int
    date: date | None
    amount: Decimal | None
    currency: str | None
    failure_code: str | None = None
    review_description: str | None = None


class ParserStrategy(Protocol):
    """A supported bank/layout parser with explicit variant detection."""

    bank_name: str
    variant_signatures: tuple[str, ...]

    def matches(self, text: str) -> bool:
        """Return whether this bank and NACIONAL/INTERNACIONAL layout matches."""

    def parse(self, *, text: str, file_hash: str) -> list[ParsedCandidate]:
        """Produce only deterministic candidates for an already-matched layout."""
