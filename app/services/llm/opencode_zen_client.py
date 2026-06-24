"""OpenCode Zen client — curated models with API-key authentication.

OpenCode Zen is a list of tested and verified models provided by
the OpenCode team. It is exposed at
``https://opencode.ai/zen/v1`` and is **OpenAI-compatible** —
the wire format is the same ``/v1/chat/completions`` endpoint
that ``OllamaClient`` and ``OpenCodeGoClient`` already speak,
just behind a different base URL and with a different
authentication header.

This client mirrors :class:`OllamaClient` and
:class:`OpenCodeGoClient` in shape: same retry policy, same
extraction-error model, same response parsing. The only
Zen-specific details are the URL (``/chat/completions``)
and the auth (``Authorization: Bearer <key>``).

Models that are *not* behind the ``/v1/messages`` Anthropic
shim are the ones we use here: most of the Zen catalogue
(DeepSeek, GLM, Kimi, Big Pickle, Grok, etc.) lives behind
the OpenAI-compat endpoint and is the one this client
targets.
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


class OpenCodeZenClient(LLMProvider):
    """LLM client for OpenCode Zen (curated models, API-key auth).

    Talks to the OpenAI-compatible ``/v1/chat/completions``
    endpoint exposed by ``https://opencode.ai/zen/v1`` (or a
    self-hosted equivalent configured via ``LLM_API_ENDPOINT``).

    Parameters
    ----------
    settings:
        The application :class:`~app.core.config.Settings`.
        The client reads ``LLM_API_ENDPOINT``, ``LLM_API_KEY``,
        ``LLM_MODEL``, ``LLM_TIMEOUT``, and ``LLM_MAX_RETRIES``
        at call time (not at construction), so a test that
        mutates the settings between calls sees the new values.
    http_client:
        Optional :class:`httpx.AsyncClient` to use. The client
        is responsible for its lifecycle — the test suite
        passes a short-lived one, while the application code
        can pass a long-lived one. When omitted, a fresh
        client is created per call (and closed immediately),
        which is fine for a request handler but wasteful for
        high-throughput use.
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
        2. POST it to ``{settings.LLM_API_ENDPOINT}/v1/chat/completions``
           with ``response_format={"type": "json_object"}`` so the
           daemon enforces JSON at the provider level, not just in
           the prompt.
        3. Parse the response and validate it as
           :class:`ExtractionResponse`.
        4. On retryable failure, sleep for ``2 ** attempt``
           seconds and try again, up to
           ``settings.LLM_MAX_RETRIES`` total attempts.
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

        raise LLMExtractionError("OpenCode Zen extraction failed") from last_exc  # pragma: no cover

    async def _call_once(self, prompt: str) -> ExtractionResponse:
        """Make one HTTP call and validate the response."""
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
        """Return the OpenAI-compat ``/v1/chat/completions`` URL.

        The base URL in settings is the Zen root (or a
        self-hosted equivalent); we append the standard
        ``/v1/chat/completions`` suffix used by OpenAI-compatible
        APIs. A trailing slash on the base URL is normalised
        to a single one.
        """
        base = self._settings.LLM_API_ENDPOINT.rstrip("/")
        return f"{base}/v1/chat/completions"

    def _build_payload(self, prompt: str) -> dict[str, Any]:
        """Build the OpenAI-compat request body.

        The prompt is sent as a single ``user`` message; the
        system role would let the daemon inject its own
        system prompt and we want to keep behaviour
        deterministic. ``response_format`` enforces JSON at
        the provider level — a guard against prompt-only
        enforcement failing.

        Note: ``max_tokens`` is intentionally omitted. The
        default cap on the OpenAI-compat side is large
        enough for a few hundred transactions; sending a
        lower cap on free models occasionally causes a
        400 ("max_tokens too large for this model"). We let
        the provider pick the ceiling.
        """
        return {
            "model": self._settings.LLM_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.0,
            "response_format": {"type": "json_object"},
        }

    def _build_headers(self) -> dict[str, str]:
        """Build HTTP headers for the request.

        The ``Authorization: Bearer <key>`` header is the
        OpenAI convention. Zen's gateway also accepts
        ``x-api-key`` (Anthropic convention) but ``Bearer``
        is the canonical choice for the OpenAI-compat
        endpoint.

        When ``LLM_API_KEY`` is empty the auth header is
        omitted — useful for a local Zen-compatible mock that
        does not require authentication.
        """
        headers: dict[str, str] = {}
        if self._settings.LLM_API_KEY:
            headers["Authorization"] = f"Bearer {self._settings.LLM_API_KEY}"
        return headers

    def _parse_response(self, body: dict[str, Any]) -> ExtractionResponse:
        """Extract the assistant message and validate it as :class:`ExtractionResponse`.

        Two shapes are accepted:

        * OpenAI-style: ``{"choices": [{"message": {"content": "<json>"}}]}``
        * Flat: ``{"content": "<json>"}`` or ``{"transactions": [...], ...}``

        The flat shape is convenient for local daemons (Ollama
        proxies, mocks) and keeps the test surface simple.
        """
        content = _extract_content(body)
        if content is None:
            raise LLMExtractionError(
                f"OpenCode Zen response did not include a content payload: {body!r}",
                retryable=True,
            )

        if isinstance(content, str):
            content = _strip_markdown_fences(content)

        try:
            data: Any = content if isinstance(content, (dict, list)) else json.loads(content)
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
        should pass a long-lived client for connection pooling.
        """
        if self._http_client is not None:
            return self._http_client
        return httpx.AsyncClient()

    async def aclose(self) -> None:
        """Close the owned HTTP client, if any."""
        if self._owns_http_client and self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None


def _extract_content(body: dict[str, Any]) -> Any:
    """Return the model's textual content from a chat-completions response.

    Handles the OpenAI shape (``choices[0].message.content``)
    and a flat shape where the body itself is the extraction
    payload. Returns ``None`` if no content is found, so the
    caller can raise a typed error.
    """
    choices = body.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0]
        if isinstance(first, dict):
            message = first.get("message")
            if isinstance(message, dict) and "content" in message:
                content = message["content"]
                # Some models return content=None when they only
                # used reasoning tokens. Treat that as no content
                # so the retry path can re-prompt with a higher
                # budget.
                if content is None:
                    return None
                return content
            if "content" in first:
                return first["content"]
    if "content" in body:
        return body["content"]
    if "transactions" in body or "notes" in body:
        return body
    return None


#: Regex that matches a Markdown code fence optionally tagged with
#: a language hint (``\`\`\`json``, ``\`\`\`JSON``, ``\`\`\` ``).
#: Captures the body of the fence (group 1). Used to peel the
#: fences off a model's reply before JSON parsing — needed
#: for free / small models that ignore the ``response_format``
#: constraint and wrap their output in markdown anyway.
_MARKDOWN_FENCE_RE = __import__("re").compile(
    r"^\s*```(?:json|JSON)?\s*\n?(.*?)\n?\s*```\s*$",
    __import__("re").DOTALL,
)


def _strip_markdown_fences(text: str) -> str:
    """Return ``text`` with a surrounding Markdown code fence removed."""
    match = _MARKDOWN_FENCE_RE.match(text)
    if match is not None:
        return match.group(1)
    return text
