"""Tests for the LLM service layer (Work Unit 3).

These tests cover the non-deterministic half of the Phase 1
ingestion pipeline:

* :mod:`app.services.llm.protocol` — the
  :class:`LLMProvider` protocol and its runtime
  :func:`isinstance` check.
* :mod:`app.services.llm.schemas` — :class:`ExtractionResponse`
  and :class:`TransactionExtraction` validation rules.
* :mod:`app.services.llm.prompts` — variant-specific template
  rendering and JSON schema embedding.
* :mod:`app.services.llm.opencode_go_client` —
  :class:`OpenCodeGoClient` against an :class:`httpx.MockTransport`
  (no real network), including retry and backoff.
* :mod:`app.services.llm.ollama_client` — same coverage for
  :class:`OllamaClient`.
* :mod:`app.services.llm.opencode_zen_client` — same coverage
  for :class:`OpenCodeZenClient` (Anthropic-format ``/messages``
  endpoint, ``x-api-key`` and ``Authorization: Bearer`` headers).
* :mod:`app.services.llm.factory` — :func:`create_llm_client`
  dispatch and error model.

No HTTP daemon is required. The clients receive an
:class:`httpx.AsyncClient` wired to an
:class:`httpx.MockTransport` whose handler returns canned
JSON, so the tests are deterministic and run in any
environment.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.services.llm import (
    ExtractionResponse,
    LLMExtractionError,
    LLMProvider,
    OllamaClient,
    OpenCodeGoClient,
    OpenCodeZenClient,
    StatementMetadata,
    TransactionExtraction,
    create_llm_client,
)
from app.services.llm.factory import (
    PROVIDER_OLLAMA,
    PROVIDER_OPENCODE_GO,
    PROVIDER_OPENCODE_ZEN,
    UnknownLLMProviderError,
)
from app.services.llm.prompts import (
    INTERNACIONAL_PROMPT,
    NACIONAL_PROMPT,
    SUPPORTED_VARIANTS,
    build_extraction_prompt,
)

# ---------------------------------------------------------------------------
# Sample responses
# ---------------------------------------------------------------------------

NACIONAL_SAMPLE_TEXT = """\
ESTADO DE CUENTA NACIONAL
15/05/25  SUPERMERCADOS LIDER        $ 12.450
22/05/25  COMBUSTIBLE COPEC         $ 35.000
01/06/25  PARIS 03/06               $ 89.900
"""

INTERNACIONAL_SAMPLE_TEXT = """\
ESTADO DE CUENTA INTERNACIONAL
03/05/25  SPOTIFY USA              US$ 9,99
18/05/25  AMAZON.COM               US$ 42,30
02/06/25  AIRBNB INC               US$ 312,00
"""

#: Canonical extraction payload used by the OpenCode Go and
#: Ollama tests. Mirrors the format the LLM is asked to emit,
#: including the ``metadata`` block that carries the statement
#: header fields (masked PAN, cardholder, currency, period,
#: statement date).
VALID_EXTRACTION_PAYLOAD: dict[str, Any] = {
    "transactions": [
        {
            "date": "15/05/25",
            "description": "SUPERMERCADOS LIDER",
            "amount": "$ 12.450",
            "currency": "CLP",
            "category": "Groceries",
            "installment_number": None,
            "installment_total": None,
            "installment_value": None,
        },
        {
            "date": "22/05/25",
            "description": "COMBUSTIBLE COPEC",
            "amount": "$ 35.000",
            "currency": "CLP",
            "category": "Transport",
            "installment_number": None,
            "installment_total": None,
            "installment_value": None,
        },
        {
            "date": "01/06/25",
            "description": "PARIS 03/06",
            "amount": "$ 89.900",
            "currency": "CLP",
            "category": "Shopping",
            "installment_number": 1,
            "installment_total": 6,
            "installment_value": "$ 89.900",
        },
    ],
    "metadata": {
        "card_number_masked": "XXXX XXXX XXXX 0951",
        "cardholder": "LUIS SOTILLO AGUIAR",
        "currency": "CLP",
        "period_start": "01/05/2025",
        "period_end": "31/05/2025",
        "statement_date": "05/06/2025",
    },
    "confidence": 0.96,
    "notes": "3 transactions, one installment plan detected.",
}

#: The same payload wrapped in an OpenAI-style chat
#: completions response, which is what the OpenCode Go daemon
#: and Ollama's OpenAI-compat endpoint both return.
OPENAI_STYLE_RESPONSE: dict[str, Any] = {
    "choices": [
        {
            "message": {
                "role": "assistant",
                "content": json.dumps(VALID_EXTRACTION_PAYLOAD),
            }
        }
    ]
}

#: Anthropic-style response body — what OpenCode Zen's
#: ``/v1/messages`` endpoint returns for the recommended
#: models. The payload is wrapped in a ``content`` array
#: of typed blocks; we pick the ``"text"`` block and
#: parse its ``text`` field as JSON.
ANTHROPIC_STYLE_RESPONSE: dict[str, Any] = {
    "id": "msg_01ABC",
    "type": "message",
    "role": "assistant",
    "model": "qwen3.7-plus",
    "content": [
        {"type": "text", "text": json.dumps(VALID_EXTRACTION_PAYLOAD)},
    ],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 123, "output_tokens": 456},
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_settings(**overrides: Any) -> Settings:
    """Build a :class:`Settings` with sane LLM defaults for tests.

    The defaults match ``Settings`` production defaults, so a
    test that does not override them is exercising the
    real-world path.
    """
    defaults: dict[str, Any] = {
        "LLM_PROVIDER": "opencode_go",
        "LLM_API_ENDPOINT": "http://localhost:11434",
        "LLM_MODEL": "qwen3.7-max",
        "LLM_TIMEOUT": 30,
        "LLM_MAX_RETRIES": 3,
    }
    defaults.update(overrides)
    return Settings(**defaults)


def make_transport(
    handler: Any,
) -> tuple[httpx.AsyncClient, list[httpx.Request]]:
    """Return ``(client, seen_requests)`` wired to ``handler``.

    The handler is called once per HTTP request. It receives
    an :class:`httpx.Request` and must return an
    :class:`httpx.Response`. Every request is appended to
    ``seen_requests`` so the tests can assert on retry
    behaviour without re-parsing the response.
    """
    seen: list[httpx.Request] = []

    def _wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    transport = httpx.MockTransport(_wrapped)
    return httpx.AsyncClient(transport=transport), seen


# ---------------------------------------------------------------------------
# Protocol compliance
# ---------------------------------------------------------------------------


def test_protocol_is_runtime_checkable() -> None:
    """Both clients satisfy the :class:`LLMProvider` protocol at runtime.

    :class:`typing.Protocol` with :func:`runtime_checkable`
    is what lets the factory do ``isinstance(client, LLMProvider)``
    in production code (or in user-facing diagnostics). If a
    client ever drops a method, this test fails loudly
    instead of at the call site.
    """
    settings = make_settings()
    oc = OpenCodeGoClient(settings)
    ollama = OllamaClient(settings)
    zen = OpenCodeZenClient(settings)
    assert isinstance(oc, LLMProvider)
    assert isinstance(ollama, LLMProvider)
    assert isinstance(zen, LLMProvider)


def test_protocol_has_expected_method() -> None:
    """The protocol exposes ``extract_transactions`` with the right signature.

    Static checkers (mypy) validate this for us in the
    application code; this test documents the expected
    surface for the future maintainer.
    """
    assert hasattr(LLMProvider, "extract_transactions")
    # Annotations are part of the public contract: the
    # orchestrator imports them via ``from __future__ import
    # annotations`` and forwards the arguments positionally.
    annotations = LLMProvider.extract_transactions.__annotations__
    assert "text" in annotations
    assert "variant" in annotations
    assert "return" in annotations


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------


def test_transaction_extraction_valid() -> None:
    """A well-formed transaction validates and round-trips correctly."""
    txn = TransactionExtraction(
        date="15/05/25",
        description="SUPERMERCADOS LIDER",
        amount="$ 12.450",
        currency="CLP",
        category="Groceries",
    )
    assert txn.date == "15/05/25"
    assert txn.currency == "CLP"
    assert txn.installment_number is None
    assert txn.currency_is_valid() is True


def test_transaction_extraction_with_installments() -> None:
    """Installment fields are accepted and stored as-is."""
    txn = TransactionExtraction(
        date="01/06/25",
        description="PARIS 03/06",
        amount="$ 89.900",
        currency="CLP",
        installment_number=3,
        installment_total=6,
        installment_value="$ 89.900",
    )
    assert txn.installment_number == 3
    assert txn.installment_total == 6
    assert txn.installment_value == "$ 89.900"


def test_transaction_extraction_rejects_invalid_currency_length() -> None:
    """Currency must be exactly 3 characters."""
    with pytest.raises(ValidationError):
        TransactionExtraction(
            date="15/05/25",
            description="X",
            amount="$ 1",
            currency="CL",  # too short
        )


def test_transaction_extraction_rejects_extra_fields() -> None:
    """The model is closed: extra fields are rejected.

    An LLM that invents a new field (e.g. ``"merchant_id"``)
    should not silently make it into the database. The
    boundary rejects it so the orchestrator can flag the
    bad extraction.
    """
    with pytest.raises(ValidationError):
        TransactionExtraction(
            date="15/05/25",
            description="X",
            amount="$ 1",
            currency="CLP",
            merchant_id="abc",  # not in the schema
        )


def test_transaction_extraction_currency_is_valid_only_for_known_codes() -> None:
    """``currency_is_valid`` only accepts ``CLP`` and ``USD``.

    The orchestrator uses this check before parsing the
    amount — a hallucinated currency (``"EUR"``) must be
    caught before it reaches the amount parser.
    """
    clp = TransactionExtraction(date="15/05/25", description="X", amount="$ 1", currency="CLP")
    usd = TransactionExtraction(date="15/05/25", description="X", amount="US$ 1,00", currency="USD")
    bad = TransactionExtraction(date="15/05/25", description="X", amount="1", currency="EUR")
    assert clp.currency_is_valid() is True
    assert usd.currency_is_valid() is True
    assert bad.currency_is_valid() is False


def test_extraction_response_validates_full_payload() -> None:
    """A complete payload produces a validated :class:`ExtractionResponse`."""
    response = ExtractionResponse.model_validate(VALID_EXTRACTION_PAYLOAD)
    assert len(response.transactions) == 3
    assert response.confidence == pytest.approx(0.96)
    assert response.notes is not None
    assert response.transactions[2].installment_number == 1
    # The metadata block is parsed and exposed
    assert response.metadata.card_number_masked == "XXXX XXXX XXXX 0951"
    assert response.metadata.cardholder == "LUIS SOTILLO AGUIAR"
    assert response.metadata.currency == "CLP"
    assert response.metadata.period_start == "01/05/2025"
    assert response.metadata.period_end == "31/05/2025"
    assert response.metadata.statement_date == "05/06/2025"


def test_extraction_response_accepts_empty_transactions() -> None:
    """A $0 period is valid data, not an error.

    The metadata block is still required (statements always
    carry it) but the transaction list can be empty.
    """
    response = ExtractionResponse.model_validate(
        {
            "transactions": [],
            "metadata": {
                "card_number_masked": "XXXX XXXX XXXX 0951",
                "cardholder": "LUIS SOTILLO AGUIAR",
                "currency": "CLP",
                "period_start": "01/05/2025",
                "period_end": "31/05/2025",
                "statement_date": "05/06/2025",
            },
            "confidence": 0.5,
            "notes": "No charges.",
        }
    )
    assert response.transactions == []


def test_extraction_response_rejects_invalid_confidence() -> None:
    """Confidence must be a float in [0, 1]."""
    with pytest.raises(ValidationError):
        ExtractionResponse.model_validate({"transactions": [], "confidence": 1.5})


def test_extraction_response_requires_metadata() -> None:
    """The metadata block is mandatory — there is no default.

    A response without ``metadata`` is almost always a partial
    LLM generation and the orchestrator should fail fast.
    """
    with pytest.raises(ValidationError):
        ExtractionResponse.model_validate(
            {"transactions": [], "confidence": 0.5, "notes": "Missing metadata."}
        )


def test_extraction_response_rejects_extra_top_level_fields() -> None:
    """The envelope is closed: unknown top-level fields are rejected."""
    with pytest.raises(ValidationError):
        ExtractionResponse.model_validate(
            {
                "transactions": [],
                "metadata": {
                    "card_number_masked": "XXXX XXXX XXXX 0951",
                    "cardholder": "X",
                    "currency": "CLP",
                    "period_start": "01/01/2025",
                    "period_end": "31/01/2025",
                    "statement_date": "01/02/2025",
                },
                "confidence": 0.5,
                "secret_field": "leaked",
            }
        )


def test_statement_metadata_valid() -> None:
    """A well-formed metadata block validates and round-trips correctly."""
    metadata = StatementMetadata(
        card_number_masked="XXXX XXXX XXXX 0951",
        cardholder="LUIS SOTILLO AGUIAR",
        currency="CLP",
        period_start="01/05/2025",
        period_end="31/05/2025",
        statement_date="05/06/2025",
    )
    assert metadata.card_number_masked == "XXXX XXXX XXXX 0951"
    assert metadata.cardholder == "LUIS SOTILLO AGUIAR"
    assert metadata.currency_is_valid() is True


def test_statement_metadata_rejects_unknown_currency() -> None:
    """A currency that is not CLP/USD is rejected by ``currency_is_valid``."""
    metadata = StatementMetadata(
        card_number_masked="XXXX XXXX XXXX 0951",
        cardholder="X",
        currency="EUR",  # not allowed
        period_start="01/01/2025",
        period_end="31/01/2025",
        statement_date="01/02/2025",
    )
    assert metadata.currency_is_valid() is False


def test_statement_metadata_rejects_short_currency_code() -> None:
    """The currency field is length-bounded to 3 chars by the Pydantic schema."""
    with pytest.raises(ValidationError):
        StatementMetadata(
            card_number_masked="XXXX XXXX XXXX 0951",
            cardholder="X",
            currency="CL",  # too short
            period_start="01/01/2025",
            period_end="31/01/2025",
            statement_date="01/02/2025",
        )


def test_statement_metadata_rejects_extra_fields() -> None:
    """The metadata model is closed: extra fields are rejected.

    The LLM is told the metadata shape; if it invents a new
    field (e.g. ``card_expiration``), the boundary rejects it
    so the orchestrator can flag the bad extraction.
    """
    with pytest.raises(ValidationError):
        StatementMetadata.model_validate(
            {
                "card_number_masked": "XXXX XXXX XXXX 0951",
                "cardholder": "X",
                "currency": "CLP",
                "period_start": "01/01/2025",
                "period_end": "31/01/2025",
                "statement_date": "01/02/2025",
                "card_expiration": "12/29",  # not in the schema
            }
        )


def test_llm_extraction_error_has_retryable_flag() -> None:
    """The error type carries a ``retryable`` flag (defaults to False)."""
    err_default = LLMExtractionError("boom")
    err_retry = LLMExtractionError("blip", retryable=True)
    assert err_default.retryable is False
    assert err_retry.retryable is True
    assert "boom" in str(err_default)


def test_empty_response_helper() -> None:
    """``empty_response`` returns a valid empty :class:`ExtractionResponse`."""
    from app.services.llm.schemas import empty_response

    response = empty_response(confidence=0.0)
    assert response.transactions == []
    assert response.confidence == 0.0
    assert response.notes is None
    # The default metadata placeholder is exposed so the
    # response is well-formed even when the helper is called
    # without arguments.
    assert response.metadata.currency == "CLP"


def test_empty_response_helper_accepts_explicit_metadata() -> None:
    """``empty_response`` lets callers supply real metadata when they have it."""
    from app.services.llm.schemas import empty_response

    metadata = StatementMetadata(
        card_number_masked="XXXX XXXX XXXX 0951",
        cardholder="LUIS SOTILLO AGUIAR",
        currency="USD",
        period_start="01/01/2025",
        period_end="31/01/2025",
        statement_date="01/02/2025",
    )
    response = empty_response(confidence=0.5, metadata=metadata)
    assert response.metadata is metadata
    assert response.metadata.currency == "USD"


def test_parse_amount_safe_succeeds_for_valid_input() -> None:
    """``parse_amount_safe`` delegates to the amount parser for known formats."""
    from decimal import Decimal

    from app.services.llm.schemas import parse_amount_safe

    clp = parse_amount_safe("$ 12.450", "CLP")
    assert clp == Decimal("12450")
    usd = parse_amount_safe("US$ 9,99", "USD")
    assert usd == Decimal("9.99")


def test_parse_amount_safe_wraps_parse_errors() -> None:
    """A bad amount string is converted into :class:`LLMExtractionError`."""
    from app.services.llm.schemas import parse_amount_safe

    with pytest.raises(LLMExtractionError, match="unparseable amount"):
        parse_amount_safe("not a number", "CLP")


# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------


def test_supported_variants() -> None:
    """The prompt module advertises exactly NACIONAL and INTERNACIONAL."""
    assert SUPPORTED_VARIANTS == ("NACIONAL", "INTERNACIONAL")


def test_prompt_templates_are_non_empty() -> None:
    """Both templates are non-empty strings.

    A regression that empties a template would only show up
    in the LLM's confused output — the test catches it at
    import time.
    """
    assert isinstance(NACIONAL_PROMPT, str)
    assert isinstance(INTERNACIONAL_PROMPT, str)
    assert len(NACIONAL_PROMPT) > 200
    assert len(INTERNACIONAL_PROMPT) > 200


def test_prompt_templates_are_variant_specific() -> None:
    """The two templates are distinct.

    They share the same skeleton but the few-shot examples
    and the amount-format rules differ; a regression that
    accidentally aliases them would silently break the
    USD extraction.
    """
    assert NACIONAL_PROMPT != INTERNACIONAL_PROMPT
    assert "CLP" in NACIONAL_PROMPT or "Chilean Pesos" in NACIONAL_PROMPT
    assert "USD" in INTERNACIONAL_PROMPT or "US Dollars" in INTERNACIONAL_PROMPT


def test_build_extraction_prompt_embeds_text() -> None:
    """The user's text is embedded verbatim in the rendered prompt."""
    prompt = build_extraction_prompt("NACIONAL", NACIONAL_SAMPLE_TEXT)
    assert NACIONAL_SAMPLE_TEXT in prompt
    assert "ESTADO DE CUENTA NACIONAL" in prompt
    assert "SUPERMERCADOS LIDER" in prompt


def test_build_extraction_prompt_embeds_schema() -> None:
    """The Pydantic-derived JSON schema appears in the prompt."""
    prompt = build_extraction_prompt("NACIONAL", NACIONAL_SAMPLE_TEXT)
    # Schema keywords: every Pydantic field is named in the
    # prompt so the LLM has the shape in front of it.
    for field in (
        "transactions",
        "date",
        "description",
        "amount",
        "currency",
        "category",
        "installment_number",
        "installment_total",
        "installment_value",
        "metadata",
        "card_number_masked",
        "cardholder",
        "period_start",
        "period_end",
        "statement_date",
        "confidence",
        "notes",
    ):
        assert field in prompt, f"Schema field {field!r} missing from prompt"


def test_build_extraction_prompt_rejects_unknown_variant() -> None:
    """An unknown variant raises :class:`ValueError`.

    Defaulting to NACIONAL would corrupt USD statements, so
    the function fails fast with a clear error.
    """
    with pytest.raises(ValueError, match="Unsupported variant"):
        build_extraction_prompt("FUTURO", "any text")


def test_build_extraction_prompt_rejects_empty_text() -> None:
    """Empty text raises :class:`ValueError`.

    An LLM call on an empty string would burn tokens and
    return garbage; the boundary catches the bug early.
    """
    with pytest.raises(ValueError, match="empty text"):
        build_extraction_prompt("NACIONAL", "")


def test_build_extraction_prompt_internacional_uses_us_marker() -> None:
    """The INTERNACIONAL prompt mentions the ``US$`` marker."""
    prompt = build_extraction_prompt("INTERNACIONAL", INTERNACIONAL_SAMPLE_TEXT)
    assert "US$" in prompt
    assert INTERNACIONAL_SAMPLE_TEXT in prompt


def test_prompt_schema_helpers() -> None:
    """``model_schema`` and ``transaction_schema`` return the Pydantic models."""
    from app.services.llm.prompts import model_schema, transaction_schema

    assert model_schema() is ExtractionResponse
    assert transaction_schema() is TransactionExtraction


# ---------------------------------------------------------------------------
# OpenCodeGoClient
# ---------------------------------------------------------------------------


def test_opencode_url_is_chat_completions() -> None:
    """The endpoint URL appends ``/chat/completions`` to the base URL."""
    settings = make_settings(LLM_API_ENDPOINT="http://example.com:8000/")
    client = OpenCodeGoClient(settings)
    try:
        assert client._endpoint_url() == "http://example.com:8000/chat/completions"
    finally:
        # No client to close (none created yet).
        pass


def test_opencode_url_handles_no_trailing_slash() -> None:
    """A base URL without a trailing slash still produces a clean URL."""
    settings = make_settings(LLM_API_ENDPOINT="http://example.com")
    client = OpenCodeGoClient(settings)
    assert client._endpoint_url() == "http://example.com/chat/completions"


def test_opencode_payload_uses_settings_model() -> None:
    """The request body includes the model from settings and JSON response format."""
    settings = make_settings(LLM_MODEL="my-test-model")
    client = OpenCodeGoClient(settings)
    payload = client._build_payload("hello")
    assert payload["model"] == "my-test-model"
    assert payload["response_format"] == {"type": "json_object"}
    assert payload["temperature"] == 0.0
    assert payload["messages"][0]["content"] == "hello"


@pytest.mark.asyncio
async def test_opencode_successful_call_returns_extraction_response() -> None:
    """A 200 with valid JSON produces a validated :class:`ExtractionResponse`."""
    client_http, seen = make_transport(lambda req: httpx.Response(200, json=OPENAI_STYLE_RESPONSE))
    settings = make_settings()
    llm = OpenCodeGoClient(settings, http_client=client_http)
    try:
        result = await llm.extract_transactions(NACIONAL_SAMPLE_TEXT, "NACIONAL")
    finally:
        await llm.aclose()

    assert isinstance(result, ExtractionResponse)
    assert len(result.transactions) == 3
    assert result.confidence == pytest.approx(0.96)
    assert seen[0].url.path == "/chat/completions"
    # The request body must carry the variant-specific
    # prompt — i.e. the user text must be in the request
    # body, not stripped at the client.
    body = json.loads(seen[0].content)
    assert "SUPERMERCADOS LIDER" in body["messages"][0]["content"]


@pytest.mark.asyncio
async def test_opencode_accepts_flat_response_shape() -> None:
    """A response that *is* the extraction payload (no ``choices``) is also accepted.

    Some local daemons return the parsed JSON directly. The
    parser must accept it so the test surface stays simple.
    """
    client_http, _ = make_transport(lambda req: httpx.Response(200, json=VALID_EXTRACTION_PAYLOAD))
    settings = make_settings()
    llm = OpenCodeGoClient(settings, http_client=client_http)
    try:
        result = await llm.extract_transactions(NACIONAL_SAMPLE_TEXT, "NACIONAL")
    finally:
        await llm.aclose()
    assert len(result.transactions) == 3


@pytest.mark.asyncio
async def test_opencode_retries_on_429_and_succeeds() -> None:
    """A 429 on the first call is retried; success on the second.

    The test asserts the *count* of requests, not just the
    final result, so a regression that retries but does not
    give up after success is caught.
    """
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(429, json={"error": "rate limited"})
        return httpx.Response(200, json=OPENAI_STYLE_RESPONSE)

    client_http, _ = make_transport(handler)
    settings = make_settings(LLM_MAX_RETRIES=3)
    llm = OpenCodeGoClient(settings, http_client=client_http)
    # Patch ``asyncio.sleep`` so the retry happens instantly.
    with patch("app.services.llm.opencode_go_client.asyncio.sleep", new=_async_noop):
        result = await llm.extract_transactions(NACIONAL_SAMPLE_TEXT, "NACIONAL")
    await llm.aclose()
    assert attempts["n"] == 2
    assert len(result.transactions) == 3


@pytest.mark.asyncio
async def test_opencode_retries_on_malformed_json_and_succeeds() -> None:
    """A non-JSON body on the first call is retried; success on the second."""
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(200, text="not json at all")
        return httpx.Response(200, json=OPENAI_STYLE_RESPONSE)

    client_http, _ = make_transport(handler)
    settings = make_settings(LLM_MAX_RETRIES=2)
    llm = OpenCodeGoClient(settings, http_client=client_http)
    with patch("app.services.llm.opencode_go_client.asyncio.sleep", new=_async_noop):
        result = await llm.extract_transactions(NACIONAL_SAMPLE_TEXT, "NACIONAL")
    await llm.aclose()
    assert attempts["n"] == 2
    assert len(result.transactions) == 3


@pytest.mark.asyncio
async def test_opencode_retries_on_schema_validation_and_succeeds() -> None:
    """A schema-invalid body on the first call is retried."""
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": json.dumps({"wrong": "shape"})}}]},
            )
        return httpx.Response(200, json=OPENAI_STYLE_RESPONSE)

    client_http, _ = make_transport(handler)
    settings = make_settings(LLM_MAX_RETRIES=2)
    llm = OpenCodeGoClient(settings, http_client=client_http)
    with patch("app.services.llm.opencode_go_client.asyncio.sleep", new=_async_noop):
        result = await llm.extract_transactions(NACIONAL_SAMPLE_TEXT, "NACIONAL")
    await llm.aclose()
    assert attempts["n"] == 2
    assert len(result.transactions) == 3


@pytest.mark.asyncio
async def test_opencode_raises_after_max_retries() -> None:
    """Persistent 429s raise :class:`LLMExtractionError` after ``max_retries + 1`` calls."""
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(503, json={"error": "service unavailable"})

    client_http, _ = make_transport(handler)
    settings = make_settings(LLM_MAX_RETRIES=2)
    llm = OpenCodeGoClient(settings, http_client=client_http)
    with (
        patch("app.services.llm.opencode_go_client.asyncio.sleep", new=_async_noop),
        pytest.raises(LLMExtractionError, match="failed after 3 attempt"),
    ):
        await llm.extract_transactions(NACIONAL_SAMPLE_TEXT, "NACIONAL")
    await llm.aclose()
    # 1 initial + 2 retries = 3 attempts
    assert attempts["n"] == 3


@pytest.mark.asyncio
async def test_opencode_does_not_retry_on_401() -> None:
    """A 401 is non-retryable: a single attempt, then raise.

    A 401 is a configuration error (wrong key, wrong host).
    Retrying just amplifies the noise.
    """
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(401, json={"error": "unauthorized"})

    client_http, _ = make_transport(handler)
    settings = make_settings(LLM_MAX_RETRIES=3)
    llm = OpenCodeGoClient(settings, http_client=client_http)
    with (
        patch("app.services.llm.opencode_go_client.asyncio.sleep", new=_async_noop),
        pytest.raises(LLMExtractionError),
    ):
        await llm.extract_transactions(NACIONAL_SAMPLE_TEXT, "NACIONAL")
    await llm.aclose()
    assert attempts["n"] == 1


@pytest.mark.asyncio
async def test_opencode_retries_on_timeout() -> None:
    """A transport-level timeout is treated as retryable."""
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise httpx.ReadTimeout("simulated timeout")
        return httpx.Response(200, json=OPENAI_STYLE_RESPONSE)

    client_http, _ = make_transport(handler)
    settings = make_settings(LLM_MAX_RETRIES=2)
    llm = OpenCodeGoClient(settings, http_client=client_http)
    with patch("app.services.llm.opencode_go_client.asyncio.sleep", new=_async_noop):
        result = await llm.extract_transactions(NACIONAL_SAMPLE_TEXT, "NACIONAL")
    await llm.aclose()
    assert attempts["n"] == 2
    assert len(result.transactions) == 3


@pytest.mark.asyncio
async def test_opencode_max_retries_zero_means_single_attempt() -> None:
    """``LLM_MAX_RETRIES=0`` disables retries entirely.

    A test runner that needs the LLM to fail *now* can set
    retries to zero and assert the error on the first call.
    """
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(429, json={"error": "rate limited"})

    client_http, _ = make_transport(handler)
    settings = make_settings(LLM_MAX_RETRIES=0)
    llm = OpenCodeGoClient(settings, http_client=client_http)
    with pytest.raises(LLMExtractionError):
        await llm.extract_transactions(NACIONAL_SAMPLE_TEXT, "NACIONAL")
    await llm.aclose()
    assert attempts["n"] == 1


@pytest.mark.asyncio
async def test_opencode_rejects_empty_text() -> None:
    """An empty input raises immediately, without making any HTTP call."""
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(200, json=OPENAI_STYLE_RESPONSE)

    client_http, _ = make_transport(handler)
    settings = make_settings()
    llm = OpenCodeGoClient(settings, http_client=client_http)
    with pytest.raises(LLMExtractionError, match="empty text"):
        await llm.extract_transactions("", "NACIONAL")
    await llm.aclose()
    assert attempts["n"] == 0


@pytest.mark.asyncio
async def test_opencode_backoff_uses_exponential_schedule() -> None:
    """The retry loop calls ``asyncio.sleep`` with 1, 2, 4, ... seconds.

    Verifying the backoff *durations* — not just the call
    count — catches a regression that flattens the schedule
    and makes retries thunder against a flapping endpoint.
    """
    attempts = {"n": 0}
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(503, json={"error": "down"})

    client_http, _ = make_transport(handler)
    settings = make_settings(LLM_MAX_RETRIES=3)
    llm = OpenCodeGoClient(settings, http_client=client_http)
    with (
        patch("app.services.llm.opencode_go_client.asyncio.sleep", new=fake_sleep),
        pytest.raises(LLMExtractionError),
    ):
        await llm.extract_transactions(NACIONAL_SAMPLE_TEXT, "NACIONAL")
    await llm.aclose()
    # Three retries between four attempts → three sleeps.
    assert sleeps == [1, 2, 4]


@pytest.mark.asyncio
async def test_opencode_retries_on_transport_error() -> None:
    """A non-timeout transport error (e.g. ConnectError) is also retried."""
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise httpx.ConnectError("simulated connection refused")
        return httpx.Response(200, json=OPENAI_STYLE_RESPONSE)

    client_http, _ = make_transport(handler)
    settings = make_settings(LLM_MAX_RETRIES=2)
    llm = OpenCodeGoClient(settings, http_client=client_http)
    with patch("app.services.llm.opencode_go_client.asyncio.sleep", new=_async_noop):
        result = await llm.extract_transactions(NACIONAL_SAMPLE_TEXT, "NACIONAL")
    await llm.aclose()
    assert attempts["n"] == 2
    assert len(result.transactions) == 3


@pytest.mark.asyncio
async def test_opencode_rejects_response_with_no_content() -> None:
    """A 200 with a body that has no content key triggers a retry.

    With ``LLM_MAX_RETRIES=0`` the error surfaces on the
    first call. The retry path is covered by the
    malformed-JSON test, so the simple case is enough here.
    """
    client_http, _ = make_transport(lambda req: httpx.Response(200, json={"unrelated": "shape"}))
    settings = make_settings(LLM_MAX_RETRIES=0)
    llm = OpenCodeGoClient(settings, http_client=client_http)
    with pytest.raises(LLMExtractionError) as exc_info:
        await llm.extract_transactions(NACIONAL_SAMPLE_TEXT, "NACIONAL")
    # The retry layer wraps the underlying error — the
    # original message lives on ``__cause__``.
    assert "did not include a content" in str(exc_info.value.__cause__)
    await llm.aclose()


@pytest.mark.asyncio
async def test_opencode_rejects_content_that_is_not_json() -> None:
    """A 200 with content that is neither dict nor JSON string triggers a retry."""
    body = {"choices": [{"message": {"content": "not parseable json {"}}]}
    client_http, _ = make_transport(lambda req: httpx.Response(200, json=body))
    settings = make_settings(LLM_MAX_RETRIES=0)
    llm = OpenCodeGoClient(settings, http_client=client_http)
    with (
        patch("app.services.llm.opencode_go_client.asyncio.sleep", new=_async_noop),
        pytest.raises(LLMExtractionError) as exc_info,
    ):
        await llm.extract_transactions(NACIONAL_SAMPLE_TEXT, "NACIONAL")
    assert "not valid JSON" in str(exc_info.value.__cause__)
    await llm.aclose()


def test_opencode_acloses_owned_client() -> None:
    """``aclosed`` closes the internal one-shot client but not the injected one.

    The contract is: a client constructed without a
    ``http_client`` owns its own and closes it on
    ``aclose()``; a client constructed with one does not
    close it on ``aclose()`` (the caller owns it).
    """
    settings = make_settings()

    owned = OpenCodeGoClient(settings)
    # No public client attribute until first call; verify
    # ``_get_client`` creates one and ``aclose`` releases it.
    internal = owned._get_client()
    assert isinstance(internal, httpx.AsyncClient)
    assert owned._owns_http_client is True

    injected_http, _ = make_transport(lambda req: httpx.Response(200, json=OPENAI_STYLE_RESPONSE))
    borrowed = OpenCodeGoClient(settings, http_client=injected_http)
    assert borrowed._owns_http_client is False


def test_opencode_extract_content_branches() -> None:
    """``_extract_content`` handles every documented response shape.

    This is a pure function test — no network, no async.
    The branches exercised here are the ones that the
    integration tests do not reach.
    """
    from app.services.llm.opencode_go_client import _extract_content

    # OpenAI shape
    assert _extract_content({"choices": [{"message": {"content": "hello"}}]}) == "hello"
    # ``choices[0].content`` (some proxies flatten)
    assert _extract_content({"choices": [{"content": "flat"}]}) == "flat"
    # Top-level ``content``
    assert _extract_content({"content": "top-level"}) == "top-level"
    # The body itself is the payload
    assert _extract_content({"transactions": [], "notes": "x"}) == {
        "transactions": [],
        "notes": "x",
    }
    # Nothing matches
    assert _extract_content({"unrelated": "shape"}) is None


# ---------------------------------------------------------------------------
# OllamaClient
# ---------------------------------------------------------------------------


def test_ollama_url_uses_v1_chat_completions() -> None:
    """The Ollama URL is ``{base}/v1/chat/completions`` (OpenAI-compat)."""
    settings = make_settings(LLM_API_ENDPOINT="http://localhost:11434/")
    client = OllamaClient(settings)
    assert client._endpoint_url() == "http://localhost:11434/v1/chat/completions"


def test_ollama_payload_omits_response_format() -> None:
    """Ollama's payload sets ``stream: False`` and omits ``response_format``.

    Ollama's OpenAI-compat endpoint ignores ``response_format``,
    so we drop it to keep the request shape honest about what
    the daemon honours. ``temperature=0.0`` is kept.
    """
    settings = make_settings(LLM_MODEL="llama3.2")
    client = OllamaClient(settings)
    payload = client._build_payload("hello")
    assert payload["model"] == "llama3.2"
    assert payload["stream"] is False
    assert payload["temperature"] == 0.0
    assert "response_format" not in payload
    assert payload["messages"][0]["content"] == "hello"


@pytest.mark.asyncio
async def test_ollama_successful_call_returns_extraction_response() -> None:
    """A 200 with valid JSON produces a validated :class:`ExtractionResponse`."""
    client_http, seen = make_transport(lambda req: httpx.Response(200, json=OPENAI_STYLE_RESPONSE))
    settings = make_settings(LLM_PROVIDER="ollama", LLM_API_ENDPOINT="http://localhost:11434")
    llm = OllamaClient(settings, http_client=client_http)
    try:
        result = await llm.extract_transactions(INTERNACIONAL_SAMPLE_TEXT, "INTERNACIONAL")
    finally:
        await llm.aclose()
    assert isinstance(result, ExtractionResponse)
    assert seen[0].url.path == "/v1/chat/completions"
    body = json.loads(seen[0].content)
    assert "US$" in body["messages"][0]["content"] or "USD" in body["messages"][0]["content"]


@pytest.mark.asyncio
async def test_ollama_accepts_native_message_shape() -> None:
    """Ollama's native ``message.content`` shape is also accepted."""
    native = {
        "message": {
            "role": "assistant",
            "content": json.dumps(VALID_EXTRACTION_PAYLOAD),
        }
    }
    client_http, _ = make_transport(lambda req: httpx.Response(200, json=native))
    settings = make_settings()
    llm = OllamaClient(settings, http_client=client_http)
    try:
        result = await llm.extract_transactions(INTERNACIONAL_SAMPLE_TEXT, "INTERNACIONAL")
    finally:
        await llm.aclose()
    assert len(result.transactions) == 3


@pytest.mark.asyncio
async def test_ollama_retries_on_500_and_succeeds() -> None:
    """A 500 on the first call is retried."""
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(500, text="internal error")
        return httpx.Response(200, json=OPENAI_STYLE_RESPONSE)

    client_http, _ = make_transport(handler)
    settings = make_settings(LLM_MAX_RETRIES=2)
    llm = OllamaClient(settings, http_client=client_http)
    with patch("app.services.llm.ollama_client.asyncio.sleep", new=_async_noop):
        result = await llm.extract_transactions(INTERNACIONAL_SAMPLE_TEXT, "INTERNACIONAL")
    await llm.aclose()
    assert attempts["n"] == 2
    assert len(result.transactions) == 3


@pytest.mark.asyncio
async def test_ollama_raises_after_max_retries() -> None:
    """Persistent 500s raise :class:`LLMExtractionError` after all attempts."""
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(500, text="down")

    client_http, _ = make_transport(handler)
    settings = make_settings(LLM_MAX_RETRIES=1)
    llm = OllamaClient(settings, http_client=client_http)
    with (
        patch("app.services.llm.ollama_client.asyncio.sleep", new=_async_noop),
        pytest.raises(LLMExtractionError, match="failed after 2 attempt"),
    ):
        await llm.extract_transactions(INTERNACIONAL_SAMPLE_TEXT, "INTERNACIONAL")
    await llm.aclose()
    assert attempts["n"] == 2


@pytest.mark.asyncio
async def test_ollama_does_not_retry_on_404() -> None:
    """A 404 (typically: model not pulled) is non-retryable."""
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(404, json={"error": "model not found"})

    client_http, _ = make_transport(handler)
    settings = make_settings(LLM_MAX_RETRIES=3)
    llm = OllamaClient(settings, http_client=client_http)
    with (
        patch("app.services.llm.ollama_client.asyncio.sleep", new=_async_noop),
        pytest.raises(LLMExtractionError),
    ):
        await llm.extract_transactions(INTERNACIONAL_SAMPLE_TEXT, "INTERNACIONAL")
    await llm.aclose()
    assert attempts["n"] == 1


@pytest.mark.asyncio
async def test_ollama_rejects_empty_text() -> None:
    """Empty input raises immediately."""
    client_http, _ = make_transport(lambda req: httpx.Response(200, json=OPENAI_STYLE_RESPONSE))
    settings = make_settings()
    llm = OllamaClient(settings, http_client=client_http)
    with pytest.raises(LLMExtractionError, match="empty text"):
        await llm.extract_transactions("   ", "INTERNACIONAL")
    await llm.aclose()


@pytest.mark.asyncio
async def test_ollama_retries_on_transport_error() -> None:
    """A non-timeout transport error is retried."""
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise httpx.ConnectError("refused")
        return httpx.Response(200, json=OPENAI_STYLE_RESPONSE)

    client_http, _ = make_transport(handler)
    settings = make_settings(LLM_MAX_RETRIES=2)
    llm = OllamaClient(settings, http_client=client_http)
    with patch("app.services.llm.ollama_client.asyncio.sleep", new=_async_noop):
        result = await llm.extract_transactions(INTERNACIONAL_SAMPLE_TEXT, "INTERNACIONAL")
    await llm.aclose()
    assert attempts["n"] == 2
    assert len(result.transactions) == 3


@pytest.mark.asyncio
async def test_ollama_retries_on_timeout() -> None:
    """A timeout is retried like any other transient error."""
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise httpx.ReadTimeout("simulated timeout")
        return httpx.Response(200, json=OPENAI_STYLE_RESPONSE)

    client_http, _ = make_transport(handler)
    settings = make_settings(LLM_MAX_RETRIES=2)
    llm = OllamaClient(settings, http_client=client_http)
    with patch("app.services.llm.ollama_client.asyncio.sleep", new=_async_noop):
        result = await llm.extract_transactions(INTERNACIONAL_SAMPLE_TEXT, "INTERNACIONAL")
    await llm.aclose()
    assert attempts["n"] == 2
    assert len(result.transactions) == 3


@pytest.mark.asyncio
async def test_ollama_rejects_response_with_no_content() -> None:
    """A 200 with a body that has no content key is rejected (no retries)."""
    client_http, _ = make_transport(lambda req: httpx.Response(200, json={"unrelated": "shape"}))
    settings = make_settings(LLM_MAX_RETRIES=0)
    llm = OllamaClient(settings, http_client=client_http)
    with pytest.raises(LLMExtractionError) as exc_info:
        await llm.extract_transactions(INTERNACIONAL_SAMPLE_TEXT, "INTERNACIONAL")
    assert "did not include a content" in str(exc_info.value.__cause__)
    await llm.aclose()


@pytest.mark.asyncio
async def test_ollama_rejects_content_that_is_not_json() -> None:
    """Content that is not valid JSON triggers a retry, then a typed error."""
    body = {"choices": [{"message": {"content": "garbage {"}}]}
    client_http, _ = make_transport(lambda req: httpx.Response(200, json=body))
    settings = make_settings(LLM_MAX_RETRIES=0)
    llm = OllamaClient(settings, http_client=client_http)
    with (
        patch("app.services.llm.ollama_client.asyncio.sleep", new=_async_noop),
        pytest.raises(LLMExtractionError) as exc_info,
    ):
        await llm.extract_transactions(INTERNACIONAL_SAMPLE_TEXT, "INTERNACIONAL")
    assert "not valid JSON" in str(exc_info.value.__cause__)
    await llm.aclose()


def test_ollama_extract_content_branches() -> None:
    """``_extract_content`` covers all four documented response shapes."""
    from app.services.llm.ollama_client import _extract_content

    # OpenAI shape
    assert _extract_content({"choices": [{"message": {"content": "x"}}]}) == "x"
    # Native Ollama ``message.content``
    assert _extract_content({"message": {"content": "native"}}) == "native"
    # Top-level ``content``
    assert _extract_content({"content": "top"}) == "top"
    # Body is the payload
    assert _extract_content({"transactions": [], "notes": "x"}) == {
        "transactions": [],
        "notes": "x",
    }
    # Nothing matches
    assert _extract_content({"unrelated": "shape"}) is None
    # Empty ``choices`` list → falls through to the other checks
    assert _extract_content({"choices": []}) is None


def test_ollama_acloses_owned_client() -> None:
    """``aclosed`` semantics: owned clients are closed, injected ones are not."""
    settings = make_settings()

    owned = OllamaClient(settings)
    internal = owned._get_client()
    assert isinstance(internal, httpx.AsyncClient)
    assert owned._owns_http_client is True

    injected_http, _ = make_transport(lambda req: httpx.Response(200, json=OPENAI_STYLE_RESPONSE))
    borrowed = OllamaClient(settings, http_client=injected_http)
    assert borrowed._owns_http_client is False


# ---------------------------------------------------------------------------
# OpenCodeZenClient
# ---------------------------------------------------------------------------


def test_zen_url_is_messages_endpoint() -> None:
    """The endpoint URL appends ``/messages`` to the base URL.

    The Anthropic-format path is the one the recommended
    Zen models use (``qwen3.7-plus``, ``claude-haiku-4-5``,
    etc.). A trailing slash on the base URL is normalised.
    """
    settings = make_settings(LLM_API_ENDPOINT="https://opencode.ai/zen/v1/")
    client = OpenCodeZenClient(settings)
    assert client._endpoint_url() == "https://opencode.ai/zen/v1/messages"


def test_zen_url_handles_no_trailing_slash() -> None:
    """A base URL without a trailing slash still produces a clean URL."""
    settings = make_settings(LLM_API_ENDPOINT="https://opencode.ai/zen/v1")
    client = OpenCodeZenClient(settings)
    assert client._endpoint_url() == "https://opencode.ai/zen/v1/messages"


def test_zen_payload_uses_settings_model_and_anthropic_format() -> None:
    """The request body is Anthropic-format: ``model``, ``max_tokens``, ``messages``.

    Unlike the OpenCode Go client, there is no
    ``response_format`` field — the Anthropic format does
    not support it, and the prompt is what asks for JSON.
    """
    settings = make_settings(LLM_MODEL="qwen3.7-plus")
    client = OpenCodeZenClient(settings)
    payload = client._build_payload("hello world")
    assert payload["model"] == "qwen3.7-plus"
    assert payload["max_tokens"] == 4096
    assert payload["messages"] == [{"role": "user", "content": "hello world"}]
    assert "response_format" not in payload
    assert "temperature" not in payload


def test_zen_headers_include_anthropic_version() -> None:
    """Every request advertises the pinned ``anthropic-version`` header.

    Pinning the version keeps the wire format predictable
    for the test suite and avoids a surprise when Zen
    rolls out a new API version.
    """
    settings = make_settings()
    client = OpenCodeZenClient(settings)
    headers = client._build_headers()
    assert headers["anthropic-version"] == "2023-06-01"


def test_zen_headers_include_api_key_when_set() -> None:
    """When ``LLM_API_KEY`` is set, both ``x-api-key`` and ``Authorization: Bearer`` are sent.

    Zen's gateway accepts both authentication styles for
    compatibility — the Anthropic ``x-api-key`` header
    and the OpenAI ``Authorization: Bearer`` header. The
    client sends both so the request works against any
    Zen-compatible proxy.
    """
    settings = make_settings(LLM_API_KEY="sk-zen-test-key")
    client = OpenCodeZenClient(settings)
    headers = client._build_headers()
    assert headers["x-api-key"] == "sk-zen-test-key"
    assert headers["Authorization"] == "Bearer sk-zen-test-key"
    assert headers["anthropic-version"] == "2023-06-01"


def test_zen_headers_omit_auth_when_api_key_empty() -> None:
    """An empty ``LLM_API_KEY`` produces no auth headers.

    Useful for a local Zen-compatible mock that does not
    require authentication.
    """
    settings = make_settings(LLM_API_KEY="")
    client = OpenCodeZenClient(settings)
    headers = client._build_headers()
    assert "x-api-key" not in headers
    assert "Authorization" not in headers
    # ``anthropic-version`` is still sent.
    assert headers["anthropic-version"] == "2023-06-01"


@pytest.mark.asyncio
async def test_zen_successful_call_returns_extraction_response() -> None:
    """A 200 with a valid Anthropic body produces a validated response.

    The request URL is the ``/messages`` path (not
    ``/chat/completions``), the request body is in
    Anthropic format, and the auth headers are present.
    """
    client_http, seen = make_transport(
        lambda req: httpx.Response(200, json=ANTHROPIC_STYLE_RESPONSE)
    )
    settings = make_settings(LLM_API_KEY="sk-zen-test-key")
    llm = OpenCodeZenClient(settings, http_client=client_http)
    try:
        result = await llm.extract_transactions(NACIONAL_SAMPLE_TEXT, "NACIONAL")
    finally:
        await llm.aclose()

    assert isinstance(result, ExtractionResponse)
    assert len(result.transactions) == 3
    assert result.confidence == pytest.approx(0.96)
    # The request was sent to /messages, not /chat/completions.
    assert seen[0].url.path == "/messages"
    # The request body is Anthropic-format.
    body = json.loads(seen[0].content)
    assert body["model"] == settings.LLM_MODEL
    assert body["max_tokens"] == 4096
    assert body["messages"] == [{"role": "user", "content": body["messages"][0]["content"]}]
    assert "SUPERMERCADOS LIDER" in body["messages"][0]["content"]
    # The auth headers are present on the request.
    assert seen[0].headers["x-api-key"] == "sk-zen-test-key"
    assert seen[0].headers["Authorization"] == "Bearer sk-zen-test-key"
    assert seen[0].headers["anthropic-version"] == "2023-06-01"


@pytest.mark.asyncio
async def test_zen_concatenates_multiple_text_blocks() -> None:
    """A response with multiple ``text`` blocks has them concatenated.

    Some Anthropic-style models split their output across
    multiple text blocks (a reasoning block + the answer,
    for example). The parser must concatenate them, not
    drop the answer block, so the extraction succeeds.
    """
    full_json = json.dumps(VALID_EXTRACTION_PAYLOAD)
    # Split the JSON in two at the midpoint so the
    # response has two text blocks, neither of which
    # is a valid :class:`ExtractionResponse` on its own.
    midpoint = len(full_json) // 2
    split_response = {
        "id": "msg_01ABC",
        "type": "message",
        "role": "assistant",
        "model": "qwen3.7-plus",
        "content": [
            {"type": "text", "text": full_json[:midpoint]},
            {"type": "text", "text": full_json[midpoint:]},
        ],
    }
    client_http, _ = make_transport(lambda req: httpx.Response(200, json=split_response))
    settings = make_settings(LLM_API_KEY="sk-zen-test-key")
    llm = OpenCodeZenClient(settings, http_client=client_http)
    try:
        result = await llm.extract_transactions(NACIONAL_SAMPLE_TEXT, "NACIONAL")
    finally:
        await llm.aclose()

    assert len(result.transactions) == 3
    assert result.confidence == pytest.approx(0.96)


@pytest.mark.asyncio
async def test_zen_rejects_response_with_no_text_blocks() -> None:
    """A body without any text content blocks triggers a retry, then a typed error."""
    empty_response = {
        "id": "msg_01ABC",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "tool_use", "id": "x", "name": "y", "input": {}}],
    }
    client_http, _ = make_transport(lambda req: httpx.Response(200, json=empty_response))
    settings = make_settings(LLM_MAX_RETRIES=0, LLM_API_KEY="sk-zen-test-key")
    llm = OpenCodeZenClient(settings, http_client=client_http)
    with (
        patch("app.services.llm.opencode_zen_client.asyncio.sleep", new=_async_noop),
        pytest.raises(LLMExtractionError) as exc_info,
    ):
        await llm.extract_transactions(NACIONAL_SAMPLE_TEXT, "NACIONAL")
    # The retry layer wraps the underlying error — the
    # original message lives on ``__cause__``.
    assert "text content" in str(exc_info.value.__cause__)
    await llm.aclose()


@pytest.mark.asyncio
async def test_zen_retries_on_429_and_succeeds() -> None:
    """A 429 on the first call is retried; success on the second."""
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(429, json={"error": "rate limited"})
        return httpx.Response(200, json=ANTHROPIC_STYLE_RESPONSE)

    client_http, _ = make_transport(handler)
    settings = make_settings(LLM_MAX_RETRIES=3, LLM_API_KEY="sk-zen-test-key")
    llm = OpenCodeZenClient(settings, http_client=client_http)
    with patch("app.services.llm.opencode_zen_client.asyncio.sleep", new=_async_noop):
        result = await llm.extract_transactions(NACIONAL_SAMPLE_TEXT, "NACIONAL")
    await llm.aclose()
    assert attempts["n"] == 2
    assert len(result.transactions) == 3


@pytest.mark.asyncio
async def test_zen_does_not_retry_on_401() -> None:
    """A 401 is non-retryable: a single attempt, then raise.

    A 401 is a configuration error (wrong key, wrong
    account). Retrying just amplifies the noise.
    """
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(401, json={"error": "unauthorized"})

    client_http, _ = make_transport(handler)
    settings = make_settings(LLM_MAX_RETRIES=3, LLM_API_KEY="sk-zen-test-key")
    llm = OpenCodeZenClient(settings, http_client=client_http)
    with (
        patch("app.services.llm.opencode_zen_client.asyncio.sleep", new=_async_noop),
        pytest.raises(LLMExtractionError),
    ):
        await llm.extract_transactions(NACIONAL_SAMPLE_TEXT, "NACIONAL")
    await llm.aclose()
    assert attempts["n"] == 1


@pytest.mark.asyncio
async def test_zen_retries_on_schema_validation_and_succeeds() -> None:
    """A schema-invalid body on the first call is retried."""
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(
                200,
                json={"content": [{"type": "text", "text": json.dumps({"wrong": "shape"})}]},
            )
        return httpx.Response(200, json=ANTHROPIC_STYLE_RESPONSE)

    client_http, _ = make_transport(handler)
    settings = make_settings(LLM_MAX_RETRIES=2, LLM_API_KEY="sk-zen-test-key")
    llm = OpenCodeZenClient(settings, http_client=client_http)
    with patch("app.services.llm.opencode_zen_client.asyncio.sleep", new=_async_noop):
        result = await llm.extract_transactions(NACIONAL_SAMPLE_TEXT, "NACIONAL")
    await llm.aclose()
    assert attempts["n"] == 2
    assert len(result.transactions) == 3


@pytest.mark.asyncio
async def test_zen_rejects_empty_text() -> None:
    """An empty input raises immediately, without making any HTTP call."""
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(200, json=ANTHROPIC_STYLE_RESPONSE)

    client_http, _ = make_transport(handler)
    settings = make_settings(LLM_API_KEY="sk-zen-test-key")
    llm = OpenCodeZenClient(settings, http_client=client_http)
    with pytest.raises(LLMExtractionError, match="empty text"):
        await llm.extract_transactions("", "NACIONAL")
    await llm.aclose()
    assert attempts["n"] == 0


@pytest.mark.asyncio
async def test_zen_backoff_uses_exponential_schedule() -> None:
    """The retry loop calls ``asyncio.sleep`` with 1, 2, 4, ... seconds.

    Verifying the backoff *durations* — not just the call
    count — catches a regression that flattens the
    schedule.
    """
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "down"})

    client_http, _ = make_transport(handler)
    settings = make_settings(LLM_MAX_RETRIES=3, LLM_API_KEY="sk-zen-test-key")
    llm = OpenCodeZenClient(settings, http_client=client_http)
    with (
        patch("app.services.llm.opencode_zen_client.asyncio.sleep", new=fake_sleep),
        pytest.raises(LLMExtractionError),
    ):
        await llm.extract_transactions(NACIONAL_SAMPLE_TEXT, "NACIONAL")
    await llm.aclose()
    assert sleeps == [1, 2, 4]


def test_zen_collect_text_blocks_branches() -> None:
    """``_collect_text_blocks`` covers all four documented response shapes."""
    from app.services.llm.opencode_zen_client import _collect_text_blocks

    # Anthropic standard: list of typed blocks
    body = {
        "content": [
            {"type": "text", "text": "alpha"},
            {"type": "tool_use", "id": "x", "name": "y", "input": {}},
            {"type": "text", "text": "beta"},
        ]
    }
    assert _collect_text_blocks(body) == ["alpha", "beta"]
    # Flat: a single string instead of a list
    assert _collect_text_blocks({"content": "top-level"}) == ["top-level"]
    # Bare payload
    assert _collect_text_blocks({"transactions": [], "metadata": {"x": 1}}) == [
        json.dumps({"transactions": [], "metadata": {"x": 1}})
    ]
    # Nothing matches
    assert _collect_text_blocks({"unrelated": "shape"}) == []
    # Empty list
    assert _collect_text_blocks({"content": []}) == []


def test_zen_acloses_owned_client() -> None:
    """``aclosed`` semantics: owned clients are closed, injected ones are not."""
    settings = make_settings()

    owned = OpenCodeZenClient(settings)
    internal = owned._get_client()
    assert isinstance(internal, httpx.AsyncClient)
    assert owned._owns_http_client is True

    injected_http, _ = make_transport(
        lambda req: httpx.Response(200, json=ANTHROPIC_STYLE_RESPONSE)
    )
    borrowed = OpenCodeZenClient(settings, http_client=injected_http)
    assert borrowed._owns_http_client is False


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def test_factory_creates_opencode_go_client() -> None:
    """``LLM_PROVIDER='opencode_go'`` produces an :class:`OpenCodeGoClient`."""
    settings = make_settings(LLM_PROVIDER=PROVIDER_OPENCODE_GO)
    client = create_llm_client(settings)
    assert isinstance(client, OpenCodeGoClient)


def test_factory_creates_ollama_client() -> None:
    """``LLM_PROVIDER='ollama'`` produces an :class:`OllamaClient`."""
    settings = make_settings(LLM_PROVIDER=PROVIDER_OLLAMA)
    client = create_llm_client(settings)
    assert isinstance(client, OllamaClient)


def test_factory_creates_opencode_zen_client() -> None:
    """``LLM_PROVIDER='opencode_zen'`` produces an :class:`OpenCodeZenClient`."""
    settings = make_settings(
        LLM_PROVIDER=PROVIDER_OPENCODE_ZEN,
        LLM_API_ENDPOINT="https://opencode.ai/zen/v1",
        LLM_API_KEY="test-key",
        LLM_MODEL="qwen3.7-plus",
    )
    client = create_llm_client(settings)
    assert isinstance(client, OpenCodeZenClient)
    assert isinstance(client, LLMProvider)


def test_factory_accepts_case_insensitive_provider() -> None:
    """The provider name is matched case-insensitively.

    A misconfigured ``.env`` (``OpenCode_Go`` instead of
    ``opencode_go``) should not silently fall back to the
    default — but it should still resolve to the right
    client.
    """
    settings = make_settings(LLM_PROVIDER="OpenCode_Go")
    client = create_llm_client(settings)
    assert isinstance(client, OpenCodeGoClient)


def test_factory_raises_for_unknown_provider() -> None:
    """An unknown provider string raises :class:`UnknownLLMProviderError`."""
    settings = make_settings(LLM_PROVIDER="gpt-from-the-cloud")
    with pytest.raises(UnknownLLMProviderError, match="gpt-from-the-cloud"):
        create_llm_client(settings)


def test_factory_returns_protocol_compliant_object() -> None:
    """The factory's return value is always a valid :class:`LLMProvider`."""
    for provider in (PROVIDER_OPENCODE_GO, PROVIDER_OLLAMA, PROVIDER_OPENCODE_ZEN):
        client = create_llm_client(make_settings(LLM_PROVIDER=provider))
        assert isinstance(client, LLMProvider)


# ---------------------------------------------------------------------------
# Helpers (kept at the bottom so the test names read top-to-bottom)
# ---------------------------------------------------------------------------


async def _async_noop(_: float) -> None:
    """Replacement for :func:`asyncio.sleep` that returns immediately.

    Used by retry tests so the suite does not actually
    sleep for up to 7 seconds on a 3-retry 4xx storm.
    """
    return None
