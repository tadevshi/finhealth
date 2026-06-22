"""Structural interface every LLM provider implements.

We use :class:`typing.Protocol` (PEP 544) rather than an
abstract base class because:

* No inheritance is required — any class with the right
  method signatures satisfies the contract. That keeps the
  concrete clients small and mockable.
* The orchestrator depends on the protocol, never on a
  concrete class, so swapping providers is a one-line change
  in :mod:`app.services.llm.factory`.
* Static type checkers (mypy) treat the protocol as a real
  type and will flag a class that drops a method or changes
  a signature.

The protocol defines a *single* method,
:meth:`LLMProvider.extract_transactions`, because the LLM layer
has exactly one job: turn statement text into a list of
structured transactions. Everything else (prompt rendering,
HTTP, retries) is internal to each implementation.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.services.llm.schemas import ExtractionResponse


@runtime_checkable
class LLMProvider(Protocol):
    """A provider that turns statement text into structured transactions.

    The protocol is decorated with :func:`typing.runtime_checkable`
    so tests can do ``assert isinstance(client, LLMProvider)``
    without instantiating a fake. Static type checking works
    the usual way — mypy validates the call signatures against
    the annotations below.
    """

    async def extract_transactions(
        self,
        text: str,
        variant: str,
    ) -> ExtractionResponse:
        """Extract every transaction mentioned in ``text``.

        Parameters
        ----------
        text:
            The full statement text, as produced by
            :func:`app.services.pdf.extractor.extract_text`.
            Non-empty, UTF-8, with newlines preserved.
        variant:
            One of ``"NACIONAL"`` (CLP) or ``"INTERNACIONAL"``
            (USD). The provider uses it to pick the right
            prompt template — the amount format and the
            few-shot examples are variant-specific.

        Returns
        -------
        ExtractionResponse
            A validated envelope with at least ``transactions``
            and ``confidence``. An empty transaction list is
            a *valid* result for a $0 period.

        Raises
        ------
        LLMExtractionError
            If the provider cannot produce a valid response
            after exhausting its retries. The original cause
            (network, JSON parse, schema validation) is
            preserved on ``__cause__``.
        """
        ...
