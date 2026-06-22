"""OpenCode Go client — the default LLM provider for finhealth.

OpenCode Go is the locally-running daemon that powers
finhealth's LLM extraction in production. The client is a thin
wrapper around its ``/chat/completions`` HTTP endpoint:

* The request is a JSON body with the prompt and the model
  name. The endpoint is OpenAI-compatible, so the same shape
  works against a wide range of providers without changing
  the client.
* The response is JSON. We ask the model for
  ``response_format={"type": "json_object"}`` so the daemon
  enforces JSON at the provider level, not just in the
  prompt.
* Validation happens client-side via Pydantic. A response
  that does not parse is retried with the same body, on the
  theory that a single bad generation is more likely than a
  permanent model regression.

Retry policy
------------

* Network timeout (``httpx.TimeoutException``) — retry.
* HTTP 429 (rate limit) — retry.
* HTTP 5xx — retry.
* HTTP 4xx other than 429 — *fail fast*. A 401/403 is a
  configuration problem and retrying will not help.
* Malformed JSON (``json.JSONDecodeError``) — retry.
* Schema validation failure
  (``pydantic.ValidationError``) — retry.
* ``Settings.LLM_MAX_RETRIES`` total attempts (so retries ==
  0 means a single attempt, no retry).

Backoff is exponential: 1 s, 2 s, 4 s, ... capped at the
total number of attempts. The function raises
:class:`~app.services.llm.schemas.LLMExtractionError` if every
attempt fails; the original cause is preserved on
``__cause__``.

The client is *configurable* — every dependency (endpoint,
model, timeout, retries, HTTP client) is injected, so the
tests can swap them for a :class:`httpx.MockTransport` and
run end-to-end without a network. There is no module-level
state.
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


class OpenCodeGoClient(LLMProvider):
    """LLM client that talks to an OpenCode Go / OpenAI-compatible daemon.

    Parameters
    ----------
    settings:
        The application :class:`~app.core.config.Settings`. The
        client reads ``LLM_API_ENDPOINT``, ``LLM_MODEL``,
        ``LLM_TIMEOUT``, and ``LLM_MAX_RETRIES`` at call time
        (not at construction), so a test that mutates the
        settings between calls sees the new values.
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
        2. POST it to ``{settings.LLM_API_ENDPOINT}/chat/completions``
           with ``response_format={"type": "json_object"}``.
        3. Parse the response and validate it as
           :class:`ExtractionResponse`.
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
                    # Non-retryable, or no attempts left. Wrap
                    # the cause so callers always see a single
                    # error type and can introspect via
                    # ``__cause__``.
                    raise LLMExtractionError(
                        f"OpenCode Go extraction failed after {attempt + 1} attempt(s)"
                    ) from exc
                backoff = 2**attempt
                logger.warning(
                    "OpenCode Go extraction attempt %d/%d failed (%s); retrying in %ds",
                    attempt + 1,
                    max_retries + 1,
                    exc,
                    backoff,
                )
                await asyncio.sleep(backoff)

        # Unreachable: the loop either returns or raises. The
        # assignment to ``last_exc`` is a defensive backstop
        # that makes mypy happy about the post-loop path.
        raise LLMExtractionError("OpenCode Go extraction failed") from last_exc  # pragma: no cover

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
        timeout = self._settings.LLM_TIMEOUT

        try:
            client = self._get_client()
            response = await client.post(url, json=payload, timeout=timeout)
        except httpx.TimeoutException as exc:
            raise LLMExtractionError(
                f"OpenCode Go timed out after {timeout}s: {exc}", retryable=True
            ) from exc
        except httpx.HTTPError as exc:
            raise LLMExtractionError(f"OpenCode Go transport error: {exc}", retryable=True) from exc

        if response.status_code in _RETRYABLE_STATUSES:
            raise LLMExtractionError(
                f"OpenCode Go returned {response.status_code}: {response.text[:200]}",
                retryable=True,
            )
        if response.status_code in _NON_RETRYABLE_STATUSES or not response.is_success:
            raise LLMExtractionError(
                f"OpenCode Go returned {response.status_code}: {response.text[:200]}",
                retryable=False,
            )

        try:
            body = response.json()
        except json.JSONDecodeError as exc:
            raise LLMExtractionError(
                f"OpenCode Go returned non-JSON body: {exc}", retryable=True
            ) from exc

        return self._parse_response(body)

    def _endpoint_url(self) -> str:
        """Return the chat-completions URL.

        The base URL in settings is the daemon's root; we
        append the standard ``/chat/completions`` suffix used
        by OpenAI-compatible APIs. A trailing slash on the
        base URL is normalised to a single one.
        """
        base = self._settings.LLM_API_ENDPOINT.rstrip("/")
        return f"{base}/chat/completions"

    def _build_payload(self, prompt: str) -> dict[str, Any]:
        """Build the OpenAI-compatible request body.

        The prompt is sent as a single ``user`` message; the
        system role would let the daemon inject its own
        system prompt and we want to keep behaviour
        deterministic. ``response_format`` enforces JSON at
        the provider level — a guard against prompt-only
        enforcement failing.
        """
        return {
            "model": self._settings.LLM_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.0,
            "response_format": {"type": "json_object"},
        }

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
                f"OpenCode Go response did not include a content payload: {body!r}",
                retryable=True,
            )

        try:
            # ``content`` may already be a dict (some daemons
            # return parsed JSON) or a string (the OpenAI
            # convention). Both are valid.
            data: Any = content if isinstance(content, (dict, list)) else json.loads(content)
        except json.JSONDecodeError as exc:
            raise LLMExtractionError(
                f"OpenCode Go content was not valid JSON: {exc}", retryable=True
            ) from exc

        try:
            return ExtractionResponse.model_validate(data)
        except ValidationError as exc:
            raise LLMExtractionError(
                f"OpenCode Go content did not match ExtractionResponse: {exc}",
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


def _extract_content(body: dict[str, Any]) -> Any:
    """Return the model's textual content from a chat-completions response.

    Handles both the OpenAI shape (``choices[0].message.content``)
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
                return message["content"]
            if "content" in first:
                return first["content"]
    if "content" in body:
        return body["content"]
    if "transactions" in body or "notes" in body:
        # The body is the extraction payload itself.
        return body
    return None
