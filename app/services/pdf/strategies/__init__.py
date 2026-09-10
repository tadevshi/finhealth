"""Bank-layout parser strategy contracts and detection registry."""

from app.services.pdf.strategies.base import ParsedCandidate, ParserStrategy
from app.services.pdf.strategies.registry import LayoutSelection, select_strategy
from app.services.pdf.strategies.santander import SantanderStrategy

DEFAULT_STRATEGIES: tuple[ParserStrategy, ...] = (SantanderStrategy(),)

__all__ = [
    "DEFAULT_STRATEGIES",
    "LayoutSelection",
    "ParsedCandidate",
    "ParserStrategy",
    "SantanderStrategy",
    "select_strategy",
]
