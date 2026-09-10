"""Explicit layout detection without an LLM or generic-format fallback."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from app.services.pdf.strategies.base import ParserStrategy


@dataclass(frozen=True, slots=True)
class LayoutSelection:
    """A parser selection or an auditable reason why one is unavailable."""

    strategy: ParserStrategy | None
    reason_code: str | None


def select_strategy(text: str, strategies: Iterable[ParserStrategy]) -> LayoutSelection:
    """Select exactly one matching layout; ambiguity never falls back to an LLM."""

    matches = [strategy for strategy in strategies if strategy.matches(text)]
    if len(matches) == 1:
        return LayoutSelection(strategy=matches[0], reason_code=None)
    if not matches:
        return LayoutSelection(strategy=None, reason_code="unsupported_format")
    return LayoutSelection(strategy=None, reason_code="ambiguous_layout")
