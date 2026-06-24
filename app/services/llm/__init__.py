"""LLM service layer for finhealth.

This subpackage implements the *non-deterministic* half of the
Phase 1 ingestion pipeline. The deterministic half
(:mod:`app.services.pdf`) turns a password-protected PDF into
clean text plus a :class:`decimal.Decimal` amount per line. The
LLM half turns that text into a structured list of transactions
that the orchestrator (added in WU 4) can persist.

Module map
----------

* :mod:`app.services.llm.schemas` — Pydantic models that mirror
  the JSON the LLM is asked to emit. Validated at the boundary
  so a malformed response never reaches the orchestrator.
* :mod:`app.services.llm.prompts` — Variant-specific prompt
  templates (``NACIONAL`` / ``INTERNACIONAL``) with few-shot
  examples calibrated against the CMF-mandated layout.
* :mod:`app.services.llm.protocol` — :class:`LLMProvider`
  :class:`~typing.Protocol` that every concrete client
  implements via structural subtyping (no ``ABC``).
* :mod:`app.services.llm.opencode_go_client` — Client for the
  OpenCode Go daemon, the default provider.
* :mod:`app.services.llm.ollama_client` — Client for a local
  Ollama daemon, used for offline / privacy-sensitive runs.
* :mod:`app.services.llm.opencode_zen_client` — Client for
  OpenCode Zen (curated cloud models with API key auth,
  served via the Anthropic-compatible ``/v1/messages``
  endpoint).
* :mod:`app.services.llm.factory` — Single entry point
  :func:`create_llm_client` that the orchestrator uses to
  construct the provider from :class:`~app.core.config.Settings`.

Why a Protocol, not an ABC
--------------------------

Structural subtyping means a client only has to expose the right
methods — there is no base class to inherit from. That keeps the
client implementations small (no ``super().__init__()`` ceremony)
and makes them trivially mockable in tests: any object with the
right shape satisfies the contract. The orchestrator depends on
the Protocol, never on a concrete class, so swapping providers
is a one-line change in :mod:`app.services.llm.factory`.

Retry policy
------------

All three concrete clients retry on transient failures
(network timeout, HTTP 429, malformed JSON) with
exponential backoff (1 s, 2 s, 4 s) up to
``Settings.LLM_MAX_RETRIES`` times. The ``max_retries=0``
case disables retries and any single failure propagates
immediately — useful for tests and for tight deadlines in
a request handler.
"""

from app.services.llm.factory import create_llm_client
from app.services.llm.ollama_client import OllamaClient
from app.services.llm.opencode_go_client import OpenCodeGoClient
from app.services.llm.opencode_zen_client import OpenCodeZenClient
from app.services.llm.protocol import LLMProvider
from app.services.llm.schemas import (
    ExtractionResponse,
    LLMExtractionError,
    StatementMetadata,
    TransactionExtraction,
)

__all__ = [
    "ExtractionResponse",
    "LLMExtractionError",
    "LLMProvider",
    "OllamaClient",
    "OpenCodeGoClient",
    "OpenCodeZenClient",
    "StatementMetadata",
    "TransactionExtraction",
    "create_llm_client",
]
