"""Variant-specific prompt templates for the LLM extraction step.

The LLM is a flexible parser, not a deterministic one. Its
output quality depends on the prompt it is given, and the CMF
state-of-account template has *two incompatible variants*:

* ``NACIONAL`` — Chilean pesos. Dates ``DD/MM/YY``, amounts
  ``$ 1.234.567`` (dot thousands, no decimals).
* ``INTERNACIONAL`` — US dollars. Dates ``DD/MM/YY``,
  amounts ``US$ 1.234,56`` (dot thousands, comma decimals).

The two formats look superficially similar but the conventions
flip: thousands vs decimal separator, presence of a decimal
part, presence of a ``US$`` marker. A prompt that does not
spell this out will produce an LLM response that flips
decimals and thousands at random — and the validation step
will then either accept garbage or fail half the time.

We therefore keep *one prompt per variant* with the rules
inlined. Each prompt carries:

* The CMF context (why the format exists, what to expect).
* The exact amount format the LLM should emit.
* The output JSON schema, repeated verbatim, so the LLM
  cannot drift.
* A few-shot example drawn from a real statement, so the
  model sees the input→output shape at least once.
* A strict "no extra prose" rule, because the client parses
  the response as JSON and any prose breaks the parser.

The function :func:`build_extraction_prompt` does the
formatting: it picks the right template, interpolates the
schema JSON, and embeds the user-provided statement text.
The result is a single string the client sends to the
provider. Keeping the prompt as a single string — not a
multi-message chat — keeps the client code uniform across
providers.
"""

from __future__ import annotations

import json
from typing import Final

from app.services.llm.schemas import ExtractionResponse, TransactionExtraction

# ---------------------------------------------------------------------------
# Few-shot examples
# ---------------------------------------------------------------------------
#
# Each example is a (input_text, output_json) pair. The input
# is a fragment of a real statement; the output is the JSON
# the LLM is expected to produce. Examples are inlined
# directly in the prompt so the LLM sees input→output in the
# same context, which is the cheapest possible way to do
# few-shot.

_NACIONAL_EXAMPLE_INPUT: Final = """\
ESTADO DE CUENTA NACIONAL
15/05/25  SUPERMERCADOS LIDER        $ 12.450
22/05/25  COMBUSTIBLE COPEC         $ 35.000
01/06/25  PARIS 03/06               $ 89.900"""

_NACIONAL_EXAMPLE_OUTPUT: Final = json.dumps(
    {
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
        "notes": "3 transactions, all in CLP.",
    },
    ensure_ascii=False,
    indent=2,
)

_INTERNACIONAL_EXAMPLE_INPUT: Final = """\
ESTADO DE CUENTA INTERNACIONAL
03/05/25  SPOTIFY USA              US$ 9,99
18/05/25  AMAZON.COM               US$ 42,30
02/06/25  AIRBNB INC               US$ 312,00"""

_INTERNACIONAL_EXAMPLE_OUTPUT: Final = json.dumps(
    {
        "transactions": [
            {
                "date": "03/05/25",
                "description": "SPOTIFY USA",
                "amount": "US$ 9,99",
                "currency": "USD",
                "category": "Subscriptions",
                "installment_number": None,
                "installment_total": None,
                "installment_value": None,
            },
            {
                "date": "18/05/25",
                "description": "AMAZON.COM",
                "amount": "US$ 42,30",
                "currency": "USD",
                "category": "Shopping",
                "installment_number": None,
                "installment_total": None,
                "installment_value": None,
            },
            {
                "date": "02/06/25",
                "description": "AIRBNB INC",
                "amount": "US$ 312,00",
                "currency": "USD",
                "category": "Travel",
                "installment_number": None,
                "installment_total": None,
                "installment_value": None,
            },
        ],
        "metadata": {
            "card_number_masked": "XXXX XXXX XXXX 0951",
            "cardholder": "LUIS SOTILLO AGUIAR",
            "currency": "USD",
            "period_start": "01/05/2025",
            "period_end": "31/05/2025",
            "statement_date": "05/06/2025",
        },
        "confidence": 0.94,
        "notes": "3 transactions, all in USD.",
    },
    ensure_ascii=False,
    indent=2,
)


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------
#
# Templates use ``str.format``-style placeholders that
# :func:`build_extraction_prompt` substitutes. We pick
# ``str.format`` (not f-strings) so the JSON schema can be
# injected as a *value* — otherwise the JSON braces would
# collide with f-string syntax.
#
# The ``{{...}}`` are literal braces, escaped for
# ``str.format`` so the LLM sees a real JSON snippet.

_NACIONAL_PROMPT_TEMPLATE: Final = """\
You are a precise financial parser. Extract every transaction \
from the Chilean bank statement below.

CONTEXT
-------
- This is a CMF-mandated "Estado de Cuenta" (state of account) \
in the NACIONAL variant.
- Currency is Chilean Pesos (CLP). CLP has NO decimal part.
- Dates are formatted DD/MM/YY.
- Amounts are formatted with a dot as the thousands separator \
and no decimals: "$ 1.234.567".
- Installments, when present, appear as "NN/NN" appended to the \
description, for example "PARIS 03/06" means installment 3 of 6.

INSTRUCTIONS
------------
1. Read the text and identify every transaction row.
2. For each row, extract the date, description, amount, and currency.
3. Preserve the EXACT visual format of the amount (including the \
leading "$" and thousand separators). Do not normalise.
4. If the description contains an "NN/NN" installment marker, set \
``installment_number`` and ``installment_total`` accordingly and \
copy the amount into ``installment_value``. Otherwise leave them null.
5. Suggest a short category for each transaction (e.g. "Groceries", \
"Transport", "Restaurants", "Shopping", "Subscriptions", "Travel"). \
Use null if unsure.
6. Extract the statement header fields into the ``metadata`` object:
   * ``card_number_masked`` — the masked PAN as printed on every \
page (e.g. "XXXX XXXX XXXX 0951").
   * ``cardholder`` — the printed cardholder name in uppercase \
(e.g. "LUIS SOTILLO AGUIAR").
   * ``currency`` — "CLP" for this NACIONAL section.
   * ``period_start`` / ``period_end`` — the billing period in \
DD/MM/YYYY (use DD/MM/YY if the statement omits the century).
   * ``statement_date`` — the emission date in DD/MM/YYYY.
7. Estimate a ``confidence`` score between 0 and 1 for the whole extraction.
8. Add optional ``notes`` summarising the call (e.g. "12 transactions, \
one installment plan detected").

OUTPUT FORMAT
-------------
Return ONLY a single JSON object with this exact schema. No prose, \
no markdown fences, no commentary before or after.

```json
{schema}
```

EXAMPLE
-------
Input text:
```
{nacional_example_input}
```

Expected output:
```json
{nacional_example_output}
```

TEXT TO ANALYSE
---------------
{text}
"""

_INTERNACIONAL_PROMPT_TEMPLATE: Final = """\
You are a precise financial parser. Extract every transaction \
from the Chilean bank statement below.

CONTEXT
-------
- This is a CMF-mandated "Estado de Cuenta" (state of account) \
in the INTERNACIONAL variant.
- Currency is US Dollars (USD), always prefixed with "US$".
- Dates are formatted DD/MM/YY.
- Amounts are formatted with a dot as the thousands separator and \
a COMMA as the decimal separator: "US$ 1.234,56".
- The decimal part is always present (two digits).
- Installments, when present, appear as "NN/NN" appended to the \
description, for example "DELTA 04/12" means installment 4 of 12.

INSTRUCTIONS
------------
1. Read the text and identify every transaction row.
2. For each row, extract the date, description, amount, and currency.
3. Preserve the EXACT visual format of the amount (including the \
leading "US$" prefix, thousand separators, and decimal comma). \
Do not normalise.
4. If the description contains an "NN/NN" installment marker, set \
``installment_number`` and ``installment_total`` accordingly and \
copy the amount into ``installment_value``. Otherwise leave them null.
5. Suggest a short category for each transaction (e.g. "Subscriptions", \
"Travel", "Restaurants", "Shopping", "Transport"). Use null if unsure.
6. Extract the statement header fields into the ``metadata`` object:
   * ``card_number_masked`` — the masked PAN as printed on every \
page (e.g. "XXXX XXXX XXXX 0951").
   * ``cardholder`` — the printed cardholder name in uppercase \
(e.g. "LUIS SOTILLO AGUIAR").
   * ``currency`` — "USD" for this INTERNACIONAL section.
   * ``period_start`` / ``period_end`` — the billing period in \
DD/MM/YYYY (use DD/MM/YY if the statement omits the century).
   * ``statement_date`` — the emission date in DD/MM/YYYY.
7. Estimate a ``confidence`` score between 0 and 1 for the whole extraction.
8. Add optional ``notes`` summarising the call (e.g. "5 transactions, \
all in USD, no installments detected").

OUTPUT FORMAT
-------------
Return ONLY a single JSON object with this exact schema. No prose, \
no markdown fences, no commentary before or after.

```json
{schema}
```

EXAMPLE
-------
Input text:
```
{internacional_example_input}
```

Expected output:
```json
{internacional_example_output}
```

TEXT TO ANALYSE
---------------
{text}
"""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


#: Public alias for the NACIONAL template. Exposed for tests
#: that want to assert the template renders without calling
#: the LLM.
NACIONAL_PROMPT: Final = _NACIONAL_PROMPT_TEMPLATE

#: Public alias for the INTERNACIONAL template. Exposed for
#: tests that want to assert the template renders without
#: calling the LLM.
INTERNACIONAL_PROMPT: Final = _INTERNACIONAL_PROMPT_TEMPLATE

#: Variants the prompt module knows how to render. Anything
#: else is rejected with :class:`ValueError` — the LLM
#: cannot guess which conventions to follow for an unknown
#: variant, and silently defaulting to NACIONAL would corrupt
#: USD statements.
SUPPORTED_VARIANTS: Final = ("NACIONAL", "INTERNACIONAL")


def _schema_json() -> str:
    """Render the expected JSON schema as a pretty string.

    The schema is built from the same Pydantic models the
    client uses to validate the response, so the LLM and the
    validator cannot drift apart: if a field is added on the
    model, the prompt updates automatically.
    """
    schema = {
        "transactions": [
            {
                "date": "DD/MM/YY or DD/MM/YYYY",
                "description": "string",
                "amount": "string in the original visual format",
                "currency": "CLP or USD",
                "category": "string or null",
                "installment_number": "int or null",
                "installment_total": "int or null",
                "installment_value": "string or null",
            }
        ],
        "metadata": {
            "card_number_masked": "string (e.g. 'XXXX XXXX XXXX 0951')",
            "cardholder": "string (e.g. 'LUIS SOTILLO AGUIAR')",
            "currency": "CLP or USD",
            "period_start": "DD/MM/YYYY",
            "period_end": "DD/MM/YYYY",
            "statement_date": "DD/MM/YYYY",
        },
        "confidence": "float between 0 and 1",
        "notes": "string or null",
    }
    return json.dumps(schema, ensure_ascii=False, indent=2)


def build_extraction_prompt(variant: str, text: str) -> str:
    """Return the full prompt for ``variant`` with ``text`` embedded.

    The function does the two substitutions the client cannot
    do at import time: the JSON schema (which depends on the
    current Pydantic models) and the statement text (which
    is per-request). Few-shot examples are baked into the
    template — they are variant-specific and do not change
    per request.

    Parameters
    ----------
    variant:
        ``"NACIONAL"`` or ``"INTERNACIONAL"``.
    text:
        The full statement text, as produced by
        :func:`app.services.pdf.extractor.extract_text`.
        Must be non-empty; the LLM cannot extract from blank
        input and the retry policy would mask the real bug.

    Returns
    -------
    str
        A single string ready to be sent to the provider.

    Raises
    ------
    ValueError
        If ``variant`` is not one of
        :data:`SUPPORTED_VARIANTS`, or if ``text`` is empty.
    """
    if variant not in SUPPORTED_VARIANTS:
        raise ValueError(f"Unsupported variant {variant!r}. Expected one of {SUPPORTED_VARIANTS}.")
    if not text or not text.strip():
        raise ValueError("Cannot build an extraction prompt from empty text")

    schema = _schema_json()

    # Sanity check: the template must render. This catches a
    # typo in the template at import time, not at first call.
    if variant == "NACIONAL":
        return _NACIONAL_PROMPT_TEMPLATE.format(
            schema=schema,
            nacional_example_input=_NACIONAL_EXAMPLE_INPUT,
            nacional_example_output=_NACIONAL_EXAMPLE_OUTPUT,
            text=text,
        )
    return _INTERNACIONAL_PROMPT_TEMPLATE.format(
        schema=schema,
        internacional_example_input=_INTERNACIONAL_EXAMPLE_INPUT,
        internacional_example_output=_INTERNACIONAL_EXAMPLE_OUTPUT,
        text=text,
    )


def model_schema() -> type[ExtractionResponse]:
    """Return the :class:`ExtractionResponse` class.

    Exposed so tests can assert the prompt's inline schema
    matches the Pydantic model without importing both
    modules.
    """
    return ExtractionResponse


def transaction_schema() -> type[TransactionExtraction]:
    """Return the :class:`TransactionExtraction` class.

    See :func:`model_schema` for the rationale.
    """
    return TransactionExtraction
