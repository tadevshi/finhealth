"""Deterministic parser for the supported Santander statement layouts."""

from __future__ import annotations

from app.services.pdf.strategies.base import ParsedCandidate, ParserStrategy


class SantanderStrategy(ParserStrategy):
    """Recognise Santander NACIONAL or INTERNACIONAL statement headers."""

    bank_name = "santander"
    variant_signatures = ("ESTADO DE CUENTA", "TARJETA DE CRÉDITO")

    def matches(self, text: str) -> bool:
        normalized = text.casefold()
        return "santander" in normalized and all(
            signature.casefold() in normalized for signature in self.variant_signatures
        )

    def parse(self, *, text: str, file_hash: str) -> list[ParsedCandidate]:
        """Parse only rows from a positively identified Santander layout."""
        # Imported lazily to keep the public candidate facade free of an
        # import-time cycle while keeping row identity logic shared.
        from app.services.pdf.candidates import _parse_matched_rows
        from app.services.pdf.variant_detector import detect_variant

        return _parse_matched_rows(text=text, file_hash=file_hash, variant=detect_variant(text))
