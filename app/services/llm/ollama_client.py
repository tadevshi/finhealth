"""Ollama client — local LLM provider for offline / privacy-sensitive runs.

Ollama exposes an OpenAI-compatible ``/v1/chat/completions``
endpoint since v0.1.14, so the request shape is the same as
the OpenCode Go client. The differences live elsewhere:

* **No API key.** Ollama is a local daemon; auth is not
  needed.
* **Default endpoint** is ``http://localhost:11434`` (the
  Ollama default listen address), overridable via
  ``Settings.LLM_API_ENDPOINT``.
* **Model name** is whatever the user has ``ollama pull``-ed
  (e.g. ``"llama3"``, ``"qwen2.5"``). The default in
  settings is a placeholder; users override it via env.

The client reuses the prompt templates and the response
parsing from the OpenCode Go client. The retry policy,
backoff, and error model are identical — there is no
provider-specific behaviour to fork on. The two clients are
intentionally near-duplicates, and that is a *good* thing:
when the prompts change, both clients pick up the change
without coordination.

Why not just have one client with a ``provider="ollama"`` flag?
The HTTP shape is the same but the default endpoint and the
model selection differ. A flag would push the per-provider
defaults into runtime branching, which is harder to test and
harder to read. Two small classes with a shared protocol is
the simpler factoring.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Final

import httpx
from pydantic import ValidationError

from app.core.config import Settings
from app.services.llm.prompts import build_extraction_prompt
from app.services.llm.protocol import LLMProvider
from app.services.llm.schemas import ExtractionResponse, LLMExtractionError

logger = logging.getLogger(__name__)


#: HTTP statuses that warrant a retry. Same rationale as in
#: the OpenCode Go client — 429 is rate limiting, 5xx is a
#: transient server error.
_RETRYABLE_STATUSES: Final = frozenset({429, 500, 502, 503, 504})

#: HTTP statuses that should fail fast. A 404 here typically
#: means the model is not pulled yet — retrying will not fix
#: that. 401/403 are not relevant for Ollama (no auth) but
#: are kept for forward compatibility.
_NON_RETRYABLE_STATUSES: Final = frozenset({400, 401, 403, 404, 405, 415, 422})


class OllamaClient(LLMProvider):
    """LLM client that talks to a local Ollama daemon.

    Parameters
    ----------
    settings:
        The application :class:`~app.core.config.Settings`. The
        client reads ``LLM_API_ENDPOINT`` (default
        ``http://localhost:11434``), ``LLM_MODEL``,
        ``LLM_TIMEOUT``, and ``LLM_MAX_RETRIES`` at call time.
    http_client:
        Optional :class:`httpx.AsyncClient`. When omitted, a
        one-shot client is created per call.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings
        self._http_client = http_client
        self._owns_http_client = http_client is None

    async def extract_transactions(
        self,
        text: str,
        variant: str,
    ) -> ExtractionResponse:
        """Run a single Ollama extraction, with retry on transient failures.

        The flow is identical to :class:`OpenCodeGoClient` —
        same prompt, same response shape, same backoff
        schedule. The two clients diverge only in the HTTP
        URL and the model field.
        """
        if not text or not text.strip():
            raise LLMExtractionError("Cannot extract from empty text")

        prompt = build_extraction_prompt(variant, text)
        max_retries = self._settings.LLM_MAX_RETRIES
        last_exc: Exception | None = None

        for attempt in range(max_retries + 1):
            try:
                return await self._call_once(prompt)
            except LLMExtractionError as exc:
                last_exc = exc
                if not exc.retryable or attempt >= max_retries:
                    raise LLMExtractionError(
                        f"Ollama extraction failed after {attempt + 1} attempt(s)"
                    ) from exc
                backoff = 2**attempt
                logger.warning(
                    "Ollama extraction attempt %d/%d failed (%s); retrying in %ds",
                    attempt + 1,
                    max_retries + 1,
                    exc,
                    backoff,
                )
                await asyncio.sleep(backoff)

        raise LLMExtractionError("Ollama extraction failed") from last_exc  # pragma: no cover

    async def _call_once(self, prompt: str) -> ExtractionResponse:
        """Make one HTTP call to Ollama and validate the response."""
        url = self._endpoint_url()
        payload = self._build_payload(prompt)
        timeout = self._settings.LLM_TIMEOUT

        try:
            client = self._get_client()
            response = await client.post(url, json=payload, timeout=timeout)
        except httpx.TimeoutException as exc:
            raise LLMExtractionError(
                f"Ollama timed out after {timeout}s: {exc}", retryable=True
            ) from exc
        except httpx.HTTPError as exc:
            raise LLMExtractionError(f"Ollama transport error: {exc}", retryable=True) from exc

        if response.status_code in _RETRYABLE_STATUSES:
            raise LLMExtractionError(
                f"Ollama returned {response.status_code}: {response.text[:200]}",
                retryable=True,
            )
        if response.status_code in _NON_RETRYABLE_STATUSES or not response.is_success:
            raise LLMExtractionError(
                f"Ollama returned {response.status_code}: {response.text[:200]}",
                retryable=False,
            )

        try:
            body = response.json()
        except json.JSONDecodeError as exc:
            raise LLMExtractionError(
                f"Ollama returned non-JSON body: {exc}", retryable=True
            ) from exc

        return self._parse_response(body)

    def _endpoint_url(self) -> str:
        """Return the chat-completions URL.

        Ollama exposes the OpenAI-compatible endpoint at
        ``/v1/chat/completions`` since v0.1.14. Users with
        older Ollama versions get a clear 404, which the
        non-retryable list converts into an
        :class:`LLMExtractionError`.
        """
        base = self._settings.LLM_API_ENDPOINT.rstrip("/")
        return f"{base}/v1/chat/completions"

    def _build_payload(self, prompt: str) -> dict[str, Any]:
        """Build the OpenAI-compatible request body for Ollama.

        Ollama ignores ``response_format`` on the OpenAI-compat
        endpoint (it relies on the prompt), but we still send
        it so the request shape is identical to the OpenCode
        Go client. ``temperature=0.0`` pins the model to
        deterministic output — a good default for a parsing
        task.
        """
        return {
            "model": self._settings.LLM_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.0,
            "stream": False,
        }

    def _parse_response(self, body: dict[str, Any]) -> ExtractionResponse:
        """Extract the assistant message and validate it as :class:`ExtractionResponse`.

        Same shape handling as the OpenCode Go client — see
        :meth:`OpenCodeGoClient._parse_response` for the
        rationale.
        """
        content = _extract_content(body)
        if content is None:
            raise LLMExtractionError(
                f"Ollama response did not include a content payload: {body!r}",
                retryable=True,
            )

        try:
            data: Any = content if isinstance(content, (dict, list)) else json.loads(content)
        except json.JSONDecodeError as exc:
            raise LLMExtractionError(
                f"Ollama content was not valid JSON: {exc}", retryable=True
            ) from exc

        try:
            return ExtractionResponse.model_validate(data)
        except ValidationError as exc:
            raise LLMExtractionError(
                f"Ollama content did not match ExtractionResponse: {exc}",
                retryable=True,
            ) from exc

    def _get_client(self) -> httpx.AsyncClient:
        """Return the configured HTTP client, creating a one-shot if needed."""
        if self._http_client is not None:
            return self._http_client
        return httpx.AsyncClient()

    async def aclose(self) -> None:
        """Close the owned HTTP client, if any."""
        if self._owns_http_client and self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None


def _extract_content(body: dict[str, Any]) -> Any:
    """Return the model's textual content from an Ollama response.

    Ollama's OpenAI-compat endpoint mirrors the OpenAI shape,
    but its native ``/api/chat`` endpoint uses
    ``message.content`` at the top level. We accept both for
    flexibility.
    """
    choices = body.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0]
        if isinstance(first, dict):
            message = first.get("message")
            if isinstance(message, dict) and "content" in message:
                return message["content"]
            if "content" in first:
                return first["content"]
    message = body.get("message")
    if isinstance(message, dict) and "content" in message:
        return message["content"]
    if "content" in body:
        return body["content"]
    if "transactions" in body or "notes" in body:
        return body
    return None
