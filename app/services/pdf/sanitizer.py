"""Build bounded local review text without retaining identifier-like tokens."""

from __future__ import annotations

import re
import unicodedata

_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_DATE = re.compile(r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b")
_AMOUNT = re.compile(r"(?:US\$|\$)\s*[+-]?[\d.,]+", re.IGNORECASE)
_DELIMITER = re.compile(r"[|;]+")
_PAN = re.compile(r"\b(?:\d[ -]?){13,19}\b")
_RUT = re.compile(r"\b\d{1,2}[.]?\d{3}[.]?\d{3}-?[\dkK]\b")
_LABELED_IDENTIFIER = re.compile(
    r"\b(?:n[úu]mero\s+de\s+)?(?:cuenta|account|acct|a/c|"
    r"nro\.?\s*(?:cta|cuenta)|ref(?:erencia)?|referencia)"
    r"\s*(?:n[°ºo]\.?|no\.?)?\s*[:#-]?\s*[\w/-]{4,}\b",
    re.IGNORECASE,
)
_SPACE = re.compile(r"\s+")


def sanitize_review_description(value: str) -> str | None:
    """Return bounded, locally derived review text with identifier-like values masked."""

    normalized = unicodedata.normalize("NFKC", value)
    normalized = _CONTROL.sub(" ", normalized)
    normalized = _DATE.sub(" ", normalized)
    normalized = _AMOUNT.sub(" ", normalized)
    normalized = _DELIMITER.sub(" ", normalized)
    normalized = _PAN.sub("[REDACTED]", normalized)
    normalized = _RUT.sub("[REDACTED]", normalized)
    normalized = _LABELED_IDENTIFIER.sub("[REDACTED]", normalized)
    normalized = _SPACE.sub(" ", normalized).strip()
    return normalized[:255] or None
