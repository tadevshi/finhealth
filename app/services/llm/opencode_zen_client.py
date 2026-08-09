"""OpenCode Zen client with model-aware wire contracts.

OpenCode Zen is a list of tested and verified models provided by
the OpenCode team. It exposes model-specific endpoints below
``https://opencode.ai/zen/v1``:

* Qwen and Claude models use the Anthropic-compatible
  ``/messages`` endpoint.
* DeepSeek, GLM, Kimi, MiniMax, and other OpenAI-compatible
  models use ``/chat/completions``.

This client selects the endpoint, request body, authentication
headers, and response parser from ``LLM_MODEL`` while keeping the
same retry and extraction-error behavior for both contracts.
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

# OpenCode Zen documents these model families on the Anthropic-compatible
# endpoint. All other models use its OpenAI-compatible chat endpoint.
_MESSAGE_COMPATIBLE_MODEL_PREFIXES: Final = ("qwen", "claude")
_ANTHROPIC_VERSION: Final = "2023-06-01"
_ANTHROPIC_MAX_TOKENS: Final = 4096


class OpenCodeZenClient(LLMProvider):
    """LLM client for OpenCode Zen (curated models, API-key auth).

    Selects the Anthropic-compatible ``/messages`` contract for
    Qwen and Claude models, and the OpenAI-compatible
    ``/chat/completions`` contract for other documented Zen models.
    The base URL is exposed by ``https://opencode.ai/zen/v1`` (or
    a self-hosted equivalent configured via ``LLM_API_ENDPOINT``).

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
        2. POST it to the endpoint selected from ``LLM_MODEL``. The
           OpenAI-compatible route uses
           ``response_format={"type": "json_object"}``; the
           Anthropic-compatible route relies on the prompt for JSON.
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
        """Return the model-specific Zen endpoint URL.

        The base URL already contains Zen's ``/v1`` path. Qwen and
        Claude use ``/messages``; all other models use
        ``/chat/completions``. A trailing slash is normalised.
        """
        base = self._settings.LLM_API_ENDPOINT.rstrip("/")
        suffix = "/messages" if self._uses_messages_endpoint() else "/chat/completions"
        return f"{base}{suffix}"

    def _build_payload(self, prompt: str) -> dict[str, Any]:
        """Build the request body for the selected Zen wire contract.

        The prompt is sent as a single ``user`` message. Anthropic
        models require ``max_tokens`` and do not accept the
        OpenAI-only ``temperature`` or ``response_format`` fields.
        OpenAI-compatible models retain the existing JSON response
        enforcement payload.
        """
        messages = [{"role": "user", "content": prompt}]
        if self._uses_messages_endpoint():
            return {
                "model": self._settings.LLM_MODEL,
                "max_tokens": _ANTHROPIC_MAX_TOKENS,
                "messages": messages,
            }
        return {
            "model": self._settings.LLM_MODEL,
            "messages": messages,
            "temperature": 0.0,
            "response_format": {"type": "json_object"},
        }

    def _build_headers(self) -> dict[str, str]:
        """Build HTTP headers for the request.

        The Anthropic-compatible route always advertises the
        pinned ``anthropic-version`` and sends ``x-api-key`` plus
        ``Authorization: Bearer`` when a key is configured. The
        OpenAI-compatible route sends only the Bearer header.
        Empty keys omit authentication, which keeps local and mock
        compatible providers usable.
        """
        headers: dict[str, str] = {}
        if self._uses_messages_endpoint():
            headers["anthropic-version"] = _ANTHROPIC_VERSION
        if self._settings.LLM_API_KEY:
            if self._uses_messages_endpoint():
                headers["x-api-key"] = self._settings.LLM_API_KEY
            headers["Authorization"] = f"Bearer {self._settings.LLM_API_KEY}"
        return headers

    def _parse_response(self, body: dict[str, Any]) -> ExtractionResponse:
        """Extract model output and validate it as :class:`ExtractionResponse`.

        The Anthropic-compatible route concatenates all text blocks
        from ``content`` before parsing JSON. The OpenAI-compatible
        route accepts these shapes:

        * OpenAI-style: ``{"choices": [{"message": {"content": "<json>"}}]}``
        * Flat: ``{"content": "<json>"}`` or ``{"transactions": [...], ...}``

        A message response without text blocks is a retryable typed
        error rather than a Pydantic validation failure.
        """
        if self._uses_messages_endpoint():
            text_blocks = _collect_text_blocks(body)
            if not text_blocks:
                raise LLMExtractionError(
                    f"OpenCode Zen response did not include text content blocks: {body!r}",
                    retryable=True,
                )
            content: Any = "".join(text_blocks)
        else:
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

        # Drop empty transaction rows and sanitise metadata
        # fields (None → "", bad currency → dropped). The
        # orchestrator fills empty metadata from the detected
        # variant.
        if isinstance(data, dict):
            data = _drop_empty_transactions(data)

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

    def _uses_messages_endpoint(self) -> bool:
        """Return whether ``LLM_MODEL`` uses Zen's Anthropic contract."""
        model = self._settings.LLM_MODEL.strip().lower().rsplit("/", 1)[-1]
        return model.startswith(_MESSAGE_COMPATIBLE_MODEL_PREFIXES)


def _collect_text_blocks(body: dict[str, Any]) -> list[str]:
    """Collect textual content from Anthropic-style response shapes.

    Non-text content blocks are ignored. Flat string content and
    already-unwrapped extraction payloads remain supported for local
    or mock-compatible providers.
    """
    content = body.get("content")
    if isinstance(content, list):
        return [
            block["text"]
            for block in content
            if isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        ]
    if isinstance(content, str):
        return [content]
    if "transactions" in body or "notes" in body:
        return [json.dumps(body)]
    return []


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


def _drop_empty_transactions(data: dict[str, Any]) -> dict[str, Any]:
    """Return ``data`` with rows that have empty fields removed
    and metadata sanitised.

    Small local models occasionally emit rows with empty fields
    when a chunk contains only the statement header, a fee
    block without a real transaction, or a section the model
    could not parse. Pydantic's
    :class:`ValidationError` would then fail the entire
    extraction even though the rest of the chunk is fine.

    We drop the bad rows here so the validation step sees a
    clean payload. Empty strings, whitespace-only strings, and
    ``None`` are all treated as "empty". The ``metadata`` block
    is also sanitised: any date / cardholder field that comes
    back as ``None`` is converted to an empty string, and a
    bad ``currency`` value (``"CLP or USD"`` from a model that
    copies the schema description into the value, or anything
    other than ``"CLP"`` / ``"USD"``) is dropped so the
    orchestrator fills it from the detected variant.

    The first chunk with valid metadata is kept canonical by
    the orchestrator.

    The function is a no-op for shapes that don't have a
    ``transactions`` key (e.g. an error envelope from the
    provider) so it cannot accidentally corrupt the input.
    """
    if not isinstance(data, dict):
        return data
    txns = data.get("transactions")
    if isinstance(txns, list):
        cleaned: list[dict[str, Any]] = []
        for row in txns:
            if not isinstance(row, dict):
                continue
            desc = (row.get("description") or "").strip()
            amount = (row.get("amount") or "").strip()
            if not desc or not amount:
                continue
            cleaned.append(row)
        data["transactions"] = cleaned

    # Sanitise metadata fields.
    meta = data.get("metadata")
    if isinstance(meta, dict):
        for key in (
            "period_start",
            "period_end",
            "statement_date",
            "cardholder",
            "card_number_masked",
        ):
            if meta.get(key) is None:
                meta[key] = ""
        currency = meta.get("currency")
        if not isinstance(currency, str) or currency not in ("CLP", "USD"):
            meta.pop("currency", None)
    return data
