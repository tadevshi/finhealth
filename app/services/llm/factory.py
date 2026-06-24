"""Factory that constructs the LLM client from :class:`Settings`.

The orchestrator depends on the :class:`LLMProvider` protocol,
not on a concrete class. This module is the *only* place in
the application that knows which provider string maps to
which class. Adding a new provider is a one-line change here
plus a new client module — no orchestrator edits, no schema
migrations.

Why a string and not an enum
---------------------------

The :class:`Settings` field is a plain string so adding a
new provider is a code change, not a config-schema
migration. Users do not need to update their ``.env`` to a
newer enum value just to keep the app booting — unknown
values fail fast at construction time, which is the right
place for a "you configured it wrong" error to live.
"""

from __future__ import annotations

import logging

from app.core.config import Settings
from app.services.llm.ollama_client import OllamaClient
from app.services.llm.opencode_go_client import OpenCodeGoClient
from app.services.llm.opencode_zen_client import OpenCodeZenClient
from app.services.llm.protocol import LLMProvider

logger = logging.getLogger(__name__)


#: Provider identifiers the factory knows how to build. The
#: values match the strings accepted in ``Settings.LLM_PROVIDER``.
PROVIDER_OPENCODE_GO: str = "opencode_go"
PROVIDER_OLLAMA: str = "ollama"
PROVIDER_OPENCODE_ZEN: str = "opencode_zen"

#: Canonical list of supported providers. Exposed so the test
#: suite can iterate over every value without hardcoding
#: strings in two places.
SUPPORTED_PROVIDERS: tuple[str, ...] = (
    PROVIDER_OPENCODE_GO,
    PROVIDER_OLLAMA,
    PROVIDER_OPENCODE_ZEN,
)


class UnknownLLMProviderError(ValueError):
    """Raised when ``Settings.LLM_PROVIDER`` does not name a known provider.

    The error includes the list of supported providers so
    the operator can see at a glance which string to use.
    """


def create_llm_client(settings: Settings) -> LLMProvider:
    """Return the LLM client that matches ``settings.LLM_PROVIDER``.

    The function is the single integration point for the
    orchestrator. Tests that want to swap in a mock pass
    their own client; production code calls this function
    exactly once at startup.

    Parameters
    ----------
    settings:
        The application settings. ``LLM_PROVIDER`` is read
        case-insensitively (``opencode_go`` and
        ``OpenCode_Go`` both work) so a misconfigured
        ``.env`` does not silently fall back to the default.

    Returns
    -------
    LLMProvider
        A concrete client that satisfies the protocol.

    Raises
    ------
    UnknownLLMProviderError
        If ``settings.LLM_PROVIDER`` does not match any
        known provider. The message lists the supported
        values.
    """
    provider = settings.LLM_PROVIDER.strip().lower()

    if provider == PROVIDER_OPENCODE_GO:
        logger.info("Creating OpenCodeGoClient (endpoint=%s)", settings.LLM_API_ENDPOINT)
        return OpenCodeGoClient(settings)

    if provider == PROVIDER_OLLAMA:
        logger.info("Creating OllamaClient (endpoint=%s)", settings.LLM_API_ENDPOINT)
        return OllamaClient(settings)

    if provider == PROVIDER_OPENCODE_ZEN:
        logger.info("Creating OpenCodeZenClient (endpoint=%s)", settings.LLM_API_ENDPOINT)
        return OpenCodeZenClient(settings)

    raise UnknownLLMProviderError(
        f"Unknown LLM provider {settings.LLM_PROVIDER!r}. "
        f"Supported providers: {', '.join(SUPPORTED_PROVIDERS)}."
    )
