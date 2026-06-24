"""OpenCode Zen client — curated LLM models with pay-as-you-go API key auth.

OpenCode Zen is the OpenCode team's curated list of LLM
models, exposed behind a single API key. The default models
(Claude, Qwen) are served through an
**Anthropic-compatible** ``/v1/messages`` endpoint, so this
client speaks the Anthropic request/response format rather
than the OpenAI chat-completions format used by the
:class:`OpenCodeGoClient`.

Why the Anthropic format
------------------------

Most of the recommended models for finhealth
(``claude-haiku-4-5``, ``qwen3.7-plus``, ``qwen3.7-max``)
are routed through OpenCode Zen's
``https://opencode.ai/zen/v1/messages`` endpoint, which
mirrors the Anthropic Messages API:

* The request body carries ``model``, ``max_tokens``, and a
  ``messages`` array with ``role`` + ``content`` entries.
* The response body returns a ``content`` array of typed
  blocks; we pick the ``"text"`` block and parse its
  ``text`` field as JSON.
* Authentication uses both the Anthropic ``x-api-key``
  header and the OpenAI-style ``Authorization: Bearer``
  header (Zen's gateway accepts both for compatibility).

The OpenAI/Google/DeepSeek models on Zen are served through
*different* paths (``/v1/responses``, ``/v1/models/{id}``,
``/v1/chat/completions``); this client targets the
Anthropic-style ``/v1/messages`` path only, since that is
what the recommended models use. Operators picking a Zen
model that is *not* on the Anthropic path will need a
different client (or a small change to this one to dispatch
on the model family) — see the OpenCode Zen docs.

Retry policy
------------

The retry policy, backoff, and error model are identical to
:class:`OpenCodeGoClient` — they live here too so the
behaviour matches across providers and so the test suite can
exercise them with a single set of helpers. The
:class:`~app.services.llm.schemas.LLMExtractionError` is
the only error type this client raises; the original cause
is preserved on ``__cause__``.

Configuration
-------------

The client is configured by the standard
:class:`~app.core.config.Settings` fields:

* ``LLM_PROVIDER`` — must be ``"opencode_zen"`` (or any of
  the accepted aliases handled by the factory).
* ``LLM_API_ENDPOINT`` — defaults to
  ``https://opencode.ai/zen/v1`` via ``.env.example``.
* ``LLM_API_KEY`` — required; get one from
  https://opencode.ai/auth.
* ``LLM_MODEL`` — the model id (e.g. ``"qwen3.7-plus"``).
* ``LLM_TIMEOUT`` — per-call timeout in seconds.
* ``LLM_MAX_RETRIES`` — retry budget (0 disables retries).
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


#: HTTP statuses that warrant a retry. 429 is rate limiting;
#: 5xx is a transient server error. Other 4xx codes are
#: configuration problems and propagate immediately.
_RETRYABLE_STATUSES: Final = frozenset({429, 500, 502, 503, 504})

#: HTTP statuses that should fail fast. Anything not in
#: :data:`_RETRYABLE_STATUSES` and not in the 2xx range is a
#: client error — the request itself is wrong, no amount of
#: retrying will help.
_NON_RETRYABLE_STATUSES: Final = frozenset({400, 401, 403, 404, 405, 415, 422})

#: Anthropic API version the request advertises. Pinned to
#: the stable 2023-06-01 release — newer versions are wire-
#: compatible for the request fields we use, and pinning
#: keeps the response shape predictable for the test suite.
_ANTHROPIC_VERSION: Final = "2023-06-01"

#: Default ``max_tokens`` for the extraction call. 4096 is
#: large enough for a few hundred transactions (the largest
#: statement we have seen stays well under 4K output tokens)
#: while staying inside the Anthropic per-request cap for
#: every model on the recommended list.
_DEFAULT_MAX_TOKENS: Final = 4096


class OpenCodeZenClient(LLMProvider):
    """LLM client for OpenCode Zen (curated models, API-key auth).

    Talks to the Anthropic-compatible ``/v1/messages``
    endpoint exposed by ``https://opencode.ai/zen/v1`` (or
    a self-hosted equivalent configured via
    ``LLM_API_ENDPOINT``).

    Parameters
    ----------
    settings:
        The application :class:`~app.core.config.Settings`.
        The client reads ``LLM_API_ENDPOINT``,
        ``LLM_API_KEY``, ``LLM_MODEL``, ``LLM_TIMEOUT``, and
        ``LLM_MAX_RETRIES`` at call time (not at
        construction), so a test that mutates the settings
        between calls sees the new values.
    http_client:
        Optional :class:`httpx.AsyncClient` to use. The
        client is responsible for its lifecycle — the test
        suite passes a short-lived one, while the
        application code can pass a long-lived one. When
        omitted, a fresh client is created per call.
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
        """Run a single LLM extraction, with retry on transient failures.

        The flow:

        1. Render the prompt via
           :func:`app.services.llm.prompts.build_extraction_prompt`.
        2. POST it to ``{settings.LLM_API_ENDPOINT}/messages``
           in Anthropic format (``model``, ``max_tokens``,
           ``messages``).
        3. Parse the first ``text`` content block and
           validate it as :class:`ExtractionResponse`.
        4. On retryable failure, sleep for ``2 ** attempt``
           seconds and try again, up to
           ``settings.LLM_MAX_RETRIES`` total attempts.

        Parameters
        ----------
        text:
            The full statement text. Non-empty.
        variant:
            ``"NACIONAL"`` or ``"INTERNACIONAL"``.

        Returns
        -------
        ExtractionResponse
            A validated envelope. Always non-``None`` —
            failures raise.

        Raises
        ------
        LLMExtractionError
            On any non-retryable failure, or after every
            retry has been exhausted. ``__cause__`` carries
            the underlying exception.
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
                        f"OpenCode Zen extraction failed after {attempt + 1} attempt(s)"
                    ) from exc
                backoff = 2**attempt
                logger.warning(
                    "OpenCode Zen extraction attempt %d/%d failed (%s); retrying in %ds",
                    attempt + 1,
                    max_retries + 1,
                    exc,
                    backoff,
                )
                await asyncio.sleep(backoff)

        # Unreachable: the loop either returns or raises.
        # The assignment to ``last_exc`` is a defensive
        # backstop that makes mypy happy about the
        # post-loop path.
        raise LLMExtractionError("OpenCode Zen extraction failed") from last_exc  # pragma: no cover

    async def _call_once(self, prompt: str) -> ExtractionResponse:
        """Make one HTTP call and validate the response.

        Split out from :meth:`extract_transactions` so the
        retry loop can call it without re-rendering the
        prompt.

        Raises
        ------
        LLMExtractionError
            On any failure. The ``retryable`` attribute
            distinguishes transient from terminal errors.
        """
        url = self._endpoint_url()
        payload = self._build_payload(prompt)
        headers = self._build_headers()
        timeout = self._settings.LLM_TIMEOUT

        try:
            client = self._get_client()
            response = await client.post(url, json=payload, timeout=timeout, headers=headers)
        except httpx.TimeoutException as exc:
            raise LLMExtractionError(
                f"OpenCode Zen timed out after {timeout}s: {exc}", retryable=True
            ) from exc
        except httpx.HTTPError as exc:
            raise LLMExtractionError(
                f"OpenCode Zen transport error: {exc}", retryable=True
            ) from exc

        if response.status_code in _RETRYABLE_STATUSES:
            raise LLMExtractionError(
                f"OpenCode Zen returned {response.status_code}: {response.text[:200]}",
                retryable=True,
            )
        if response.status_code in _NON_RETRYABLE_STATUSES or not response.is_success:
            raise LLMExtractionError(
                f"OpenCode Zen returned {response.status_code}: {response.text[:200]}",
                retryable=False,
            )

        try:
            body = response.json()
        except json.JSONDecodeError as exc:
            raise LLMExtractionError(
                f"OpenCode Zen returned non-JSON body: {exc}", retryable=True
            ) from exc

        return self._parse_response(body)

    def _endpoint_url(self) -> str:
        """Return the Anthropic-format ``/v1/messages`` URL.

        The base URL in settings is the Zen root (or a
        self-hosted equivalent); we append the standard
        ``/messages`` suffix used by Anthropic-compatible
        APIs. A trailing slash on the base URL is
        normalised to a single one.
        """
        base = self._settings.LLM_API_ENDPOINT.rstrip("/")
        return f"{base}/messages"

    def _build_payload(self, prompt: str) -> dict[str, Any]:
        """Build the Anthropic-format request body.

        The prompt is sent as a single ``user`` message; the
        system role would let the gateway inject its own
        system prompt and we want to keep behaviour
        deterministic. ``max_tokens`` is required by the
        Anthropic API, and ``anthropic-version`` is
        carried in the headers (see
        :meth:`_build_headers`).

        Unlike the OpenCode Go client, we do not send
        ``response_format={"type": "json_object"}`` — the
        Anthropic format does not support that field, and
        the prompt already instructs the model to emit
        valid JSON. The model is asked for structured
        output via the prompt alone; a regression is caught
        by the schema-validation retry path.
        """
        return {
            "model": self._settings.LLM_MODEL,
            "max_tokens": _DEFAULT_MAX_TOKENS,
            "messages": [{"role": "user", "content": prompt}],
        }

    def _build_headers(self) -> dict[str, str]:
        """Build HTTP headers for the request.

        Sends three authentication-related headers:

        * ``x-api-key`` — the Anthropic convention.
        * ``Authorization: Bearer <key>`` — the OpenAI
          convention, also accepted by Zen's gateway.
        * ``anthropic-version`` — pinned to ``2023-06-01``
          so the wire format stays predictable.

        The ``Content-Type`` is set automatically by
        :mod:`httpx` when ``json=payload`` is passed, so we
        do not set it here.

        When ``LLM_API_KEY`` is empty the auth headers are
        omitted — useful for a local Zen-compatible
        mock that does not require authentication.
        """
        headers: dict[str, str] = {
            "anthropic-version": _ANTHROPIC_VERSION,
        }
        if self._settings.LLM_API_KEY:
            headers["x-api-key"] = self._settings.LLM_API_KEY
            headers["Authorization"] = f"Bearer {self._settings.LLM_API_KEY}"
        return headers

    def _parse_response(self, body: dict[str, Any]) -> ExtractionResponse:
        """Extract the first text content block and validate it.

        The Anthropic response shape is::

            {
              "content": [
                {"type": "text", "text": "<json string>"},
                ...
              ],
              ...
            }

        The model may also return multiple text blocks (e.g.
        a reasoning block followed by the answer); we
        concatenate all ``"text"`` blocks before parsing, so
        a model that splits its output does not break the
        extraction.
        """
        text_chunks = _collect_text_blocks(body)
        if not text_chunks:
            raise LLMExtractionError(
                f"OpenCode Zen response did not include any text content blocks: {body!r}",
                retryable=True,
            )

        combined = "".join(text_chunks)
        try:
            data: Any = json.loads(combined)
        except json.JSONDecodeError as exc:
            raise LLMExtractionError(
                f"OpenCode Zen content was not valid JSON: {exc}", retryable=True
            ) from exc

        try:
            return ExtractionResponse.model_validate(data)
        except ValidationError as exc:
            raise LLMExtractionError(
                f"OpenCode Zen content did not match ExtractionResponse: {exc}",
                retryable=True,
            ) from exc

    def _get_client(self) -> httpx.AsyncClient:
        """Return the configured HTTP client, creating a one-shot if needed.

        A one-shot client is fine for tests; production code
        should pass a long-lived client for connection
        pooling.
        """
        if self._http_client is not None:
            return self._http_client
        return httpx.AsyncClient()

    async def aclose(self) -> None:
        """Close the owned HTTP client, if any.

        The orchestrator calls this on shutdown so a
        long-lived client (passed via ``http_client``) is
        not closed underneath it. Clients created internally
        are always closed.
        """
        if self._owns_http_client and self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None


def _collect_text_blocks(body: dict[str, Any]) -> list[str]:
    """Return every ``"text"`` block in an Anthropic response body.

    Handles three shapes:

    * Anthropic standard: ``{"content": [{"type": "text", "text": "..."}]}``
    * Flat: ``{"content": "<json string>"}`` — a single
      string instead of a list of blocks. Useful for
      tests and for proxies that flatten the response.
    * Bare payload: ``{"transactions": [...], ...}`` —
      no ``content`` wrapper at all. The body itself is
      returned as the only chunk.
    """
    content = body.get("content")
    if isinstance(content, list):
        chunks: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    chunks.append(text)
        return chunks
    if isinstance(content, str):
        return [content]
    if "transactions" in body or "notes" in body or "metadata" in body:
        return [json.dumps(body)]
    return []
