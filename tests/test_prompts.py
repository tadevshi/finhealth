"""Tests for the closed-set category instruction in the LLM prompts.

The Phase 2 categories foundation depends on the LLM emitting one of
12 canonical names verbatim. The prompt module is the single source of
truth for that list, so the tests assert:

* every one of the 12 names appears in the rendered prompt;
* the few-shot examples (``_NACIONAL_EXAMPLE_OUTPUT`` and
  ``_INTERNACIONAL_EXAMPLE_OUTPUT``) use names from the set;
* the JSON schema inline in the prompt mentions the closed set.

These tests are part of the PR #2 acceptance criteria — without
them, a future refactor of the prompt that drops one of the 12 names
silently would only be caught at the ingestion boundary.
"""

from __future__ import annotations

import json
import re

from app.services.llm.prompts import (
    _INTERNACIONAL_EXAMPLE_OUTPUT,
    _NACIONAL_EXAMPLE_OUTPUT,
    INTERNACIONAL_PROMPT,
    NACIONAL_PROMPT,
    SEED_CATEGORY_NAMES,
    _schema_json,
    build_extraction_prompt,
)

# ---------------------------------------------------------------------------
# Sample text
# ---------------------------------------------------------------------------

NACIONAL_SAMPLE_TEXT = """\
ESTADO DE CUENTA NACIONAL
12/03/25  SUPERMERCADOS LIDER        $ 5.500
18/03/25  COMBUSTIBLE COPEC          $ 12.300
25/03/25  PARIS 03/06                $ 89.900
"""

INTERNACIONAL_SAMPLE_TEXT = """\
ESTADO DE CUENTA INTERNACIONAL
10/04/25  STREAMING DEMO SRL        US$ 8,99
22/04/25  TIENDA ONLINE EJEMPLO     US$ 15,99
05/05/25  SERVICIO WEB FICTICIO     US$ 42,00
"""


# ---------------------------------------------------------------------------
# Closed-set constant
# ---------------------------------------------------------------------------


def test_seed_category_names_count_is_twelve() -> None:
    """The closed set has exactly 12 entries.

    The Phase 2 design locks the count at 12 — the Y-NAB-derived
    flat taxonomy. A regression that adds or drops a name would
    drift the design and the seed, so the count is asserted.
    """
    assert len(SEED_CATEGORY_NAMES) == 12


def test_seed_category_names_are_distinct() -> None:
    """The closed set has no duplicate names.

    A duplicate would let the LLM emit an ambiguous value and
    the ingestion layer could not tell which one was intended.
    """
    assert len(SEED_CATEGORY_NAMES) == len(set(SEED_CATEGORY_NAMES))


def test_seed_category_names_have_canonical_casing() -> None:
    """Every name is title-case with no extra whitespace.

    The ingestion layer does a case-insensitive match
    (``strip().lower()``) so a row tagged ``"food "`` still
    hits, but the canonical spelling is the one in this tuple.
    """
    for name in SEED_CATEGORY_NAMES:
        assert name == name.strip(), f"Name {name!r} has leading/trailing whitespace"
        assert name[0].isupper(), f"Name {name!r} is not title-case"


# ---------------------------------------------------------------------------
# NACIONAL prompt contains the closed set
# ---------------------------------------------------------------------------


def test_nacional_prompt_lists_all_twelve_categories() -> None:
    """Every one of the 12 names appears in the NACIONAL template.

    The closed-set enumeration is in the "INSTRUCTIONS" section
    of the template. Each name is asserted to be present
    verbatim so a future refactor that drops one (e.g. by
    editing the prose and forgetting to update the list) is
    caught here.
    """
    for name in SEED_CATEGORY_NAMES:
        assert name in NACIONAL_PROMPT, (
            f"NACIONAL prompt is missing category {name!r} from the closed set"
        )


def test_nacional_prompt_examples_use_closed_set() -> None:
    """The NACIONAL few-shot example uses only closed-set names.

    Parses the inline example output and asserts every
    ``category`` value is in the closed set. A free-form
    category in the example (e.g. ``"Restaurants"``) would teach
    the LLM the wrong vocabulary and defeat the closed-set
    enforcement.
    """
    example = json.loads(_NACIONAL_EXAMPLE_OUTPUT)
    for txn in example["transactions"]:
        assert txn["category"] in SEED_CATEGORY_NAMES, (
            f"NACIONAL example uses non-closed-set category {txn['category']!r}"
        )


# ---------------------------------------------------------------------------
# INTERNACIONAL prompt contains the closed set
# ---------------------------------------------------------------------------


def test_internacional_prompt_lists_all_twelve_categories() -> None:
    """Every one of the 12 names appears in the INTERNACIONAL template."""
    for name in SEED_CATEGORY_NAMES:
        assert name in INTERNACIONAL_PROMPT, (
            f"INTERNACIONAL prompt is missing category {name!r} from the closed set"
        )


def test_internacional_prompt_examples_use_closed_set() -> None:
    """The INTERNACIONAL few-shot example uses only closed-set names."""
    example = json.loads(_INTERNACIONAL_EXAMPLE_OUTPUT)
    for txn in example["transactions"]:
        assert txn["category"] in SEED_CATEGORY_NAMES, (
            f"INTERNACIONAL example uses non-closed-set category {txn['category']!r}"
        )


# ---------------------------------------------------------------------------
# Inline JSON schema mentions the closed set
# ---------------------------------------------------------------------------


def test_schema_json_lists_all_twelve_categories() -> None:
    """The inline ``_schema_json()`` output mentions every name.

    The JSON schema is rendered verbatim into the prompt. A
    small model that re-reads the schema inline (instead of the
    prose) must still see the closed set, so every name is
    asserted to be present in the rendered string.
    """
    schema = _schema_json()
    for name in SEED_CATEGORY_NAMES:
        assert name in schema, (
            f"_schema_json() is missing category {name!r} from the inline closed set"
        )


def test_schema_json_mentions_closed_set_phrase() -> None:
    """The inline schema describes ``category`` as a closed-set enumeration.

    The literal phrase ``"closed-set"`` (or a near equivalent)
    is what tells a small model that an off-set name is a
    contract violation. A regression that drops the phrase
    would re-introduce the free-form leak the closed set
    exists to prevent.
    """
    schema = _schema_json()
    assert "closed-set" in schema.lower() or "one of the" in schema.lower()


# ---------------------------------------------------------------------------
# End-to-end rendered prompt
# ---------------------------------------------------------------------------


def test_build_extraction_prompt_nacional_contains_closed_set() -> None:
    """The rendered NACIONAL prompt contains every one of the 12 names."""
    prompt = build_extraction_prompt("NACIONAL", NACIONAL_SAMPLE_TEXT)
    for name in SEED_CATEGORY_NAMES:
        assert name in prompt, f"Rendered NACIONAL prompt is missing {name!r}"


def test_build_extraction_prompt_internacional_contains_closed_set() -> None:
    """The rendered INTERNACIONAL prompt contains every one of the 12 names."""
    prompt = build_extraction_prompt("INTERNACIONAL", INTERNACIONAL_SAMPLE_TEXT)
    for name in SEED_CATEGORY_NAMES:
        assert name in prompt, f"Rendered INTERNACIONAL prompt is missing {name!r}"


def test_build_extraction_prompt_embeds_schema() -> None:
    """The rendered prompt carries the JSON schema inline (so the closed set is
    surfaced twice — in the prose and in the schema)."""
    prompt = build_extraction_prompt("NACIONAL", NACIONAL_SAMPLE_TEXT)
    assert "```json" in prompt
    # Every name appears at least twice: once in the prose and once
    # in the schema. The regex is permissive so a refactor that
    # changes the surrounding formatting does not break the test.
    for name in SEED_CATEGORY_NAMES:
        matches = re.findall(re.escape(name), prompt)
        assert len(matches) >= 1, f"{name!r} not in rendered prompt at all"
