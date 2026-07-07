# phase2-merchant-aliasing

## Purpose

Bank statements emit free-text `description` strings ("MCDONALDS SUC 12", "S.A. PARIS 03/06", "LIDER COM 3") with branch identifiers, legal suffixes, and installment markers. Without a canonical merchant entity, the user cannot answer "what did I spend at Lider?" without scanning every row manually.

PR #4 introduces the `merchants` and `merchant_aliases` tables, a `Transaction.merchant_id` FK, a deterministic normalizer that auto-creates canonical merchants on first sight (default path, zero LLM cost), an opt-in LLM helper for ambiguous descriptions, and two new endpoints. The deterministic path uses a single `re.sub` with `\b` anchors plus alias-table lookup, covers ~80% of cases via `KNOWN_MERCHANT_PATTERNS`, and treats unknown variants as separate merchants (decision #4). The opt-in LLM helper is gated by `LLM_MERCHANT_NORMALIZATION_ENABLED` (default `false`, decision #6) and is bounded to one LLM call per unique normalized text per ingestion (architecture pick C).

The `low_confidence` Boolean introduced by `phase2-categories` is reused as the unified "I can't confidently tag this row" signal for both the category miss and the new-merchant miss cases. On first sight of an unknown merchant, `default_category_id=NULL` and `low_confidence=True` (decision #9). The migration extends the existing `0006_phase2_merchants_transactions_alter.py` (single-file coordination point declared in PR #2); no new migration file is created. The LLM prompt, LLM clients, and the chunk loop in `_build_transactions` are untouched.

## ADDED Requirements

### Requirement: Known Patterns Auto-Merge with a Sensible Default Category

The merchant normalizer MUST look up the normalized alias text against `KNOWN_MERCHANT_PATTERNS` (a hardcoded dict in `app/services/merchants.py` mapping ~10-15 Chilean merchants to a `Category.name`). On hit, the auto-created `Merchant` row is stamped with `default_category_id=<cat.id>` and `low_confidence=False`. On miss, the auto-created `Merchant` is stamped with `default_category_id=NULL` and `low_confidence=True`. (Decisions #4, #9; architecture pick D)

#### Scenario: MCDONALDS pattern gets the "Dining Out" default

- GIVEN a transaction with `description="MCDONALDS SUC 12"` and `KNOWN_MERCHANT_PATTERNS` maps `"mcdonalds"` to `"Dining Out"`
- WHEN `_build_transactions` runs
- THEN the inserted `Transaction` has `merchant_id=<mcdonalds.id>`, and the MCDONALDS `Merchant` has `default_category_id=<dining_out.id>` and `low_confidence=False`

#### Scenario: Unknown pattern gets `low_confidence=True` and `default_category_id=NULL`

- GIVEN a transaction with `description="TIENDA ONLINE XYZ"` (not in `KNOWN_MERCHANT_PATTERNS`)
- WHEN `_build_transactions` runs
- THEN the inserted `Transaction` has `merchant_id=<tienda_online_xyz.id>` (auto-created) and the new `Merchant` has `default_category_id=NULL` and `low_confidence=True`

#### Scenario: LIDER pattern gets the "Groceries" default

- GIVEN a transaction with `description="LIDER COM 3"` and `KNOWN_MERCHANT_PATTERNS` maps `"lider"` to `"Groceries"`
- WHEN `_build_transactions` runs
- THEN the LIDER `Merchant` has `default_category_id=<groceries.id>` and `low_confidence=False`

### Requirement: Deterministic Normalization via Regex with `\b` Anchors

The `normalize(raw: str) -> str` function MUST lowercase the input, strip `SUC.*`/`com`/digits/accents/`/`/`;`/`.`/`,`/`S\.A\.`/`LTDA`/`CIA`/`SUCURSAL` using a single `re.sub` with `\b` anchors for the legal-entity tokens. The output is used as the alias lookup key. (Decision #4; architecture pick A)

#### Scenario: "MCDONALDS SUC 12" normalizes to "mcdonalds"

- GIVEN `raw="MCDONALDS SUC 12"`
- WHEN `normalize` runs
- THEN the result is `"mcdonalds"` (the `SUC`, digits, and whitespace are stripped)

#### Scenario: "S.A. PARIS 03/06" preserves the installment marker

- GIVEN `raw="S.A. PARIS 03/06"`
- WHEN `normalize` runs
- THEN the result is `"paris 03/06"` (the `S.A.` is stripped; the `03/06` installment marker is preserved)

#### Scenario: "LIDER COM 3" normalizes to "lider"

- GIVEN `raw="LIDER COM 3"`
- WHEN `normalize` runs
- THEN the result is `"lider"` (the `COM`, digits, and whitespace are stripped)

#### Scenario: "CINEMARK" is NOT over-stripped by the CIA rule

- GIVEN `raw="CINEMARK"`
- WHEN `normalize` runs
- THEN the result is `"cinemark"` (the `\b` anchor before `CIA` prevents stripping the `C` from `CINEMARK`)

#### Scenario: Accents and punctuation are stripped

- GIVEN inputs `"CAFÉ"` → `"cafe"`, `"AÉROPORT"` → `"aeroport"`, `"MCDONALDS/PARIS"` → `"mcdonaldsparis"`
- WHEN `normalize` runs
- THEN the result matches the expected normalized form in each case

### Requirement: Alias Table Lookup Is Hit-or-Create

The alias lookup MUST check `merchant_aliases` (indexed on `normalized`) for the normalized text. On hit, the new `Transaction` is bound to the existing `Merchant`. On miss, a new `Merchant` row plus a `MerchantAlias` row (with `source='auto'`) are auto-created in the same `commit()`. (Decision #4; architecture pick B)

#### Scenario: First upload with "MCDONALDS SUC 12" auto-creates Merchant + alias

- GIVEN the `merchant_aliases` table is empty
- WHEN `_build_transactions` runs with `description="MCDONALDS SUC 12"`
- THEN a new `Merchant` row is created (with `name="mcdonalds"`), a new `MerchantAlias` row is created (with `alias_text="MCDONALDS SUC 12"`, `normalized="mcdonalds"`, `source="auto"`), and the transaction has `merchant_id=<new_merchant.id>`

#### Scenario: Second upload hits the alias table and binds to existing Merchant

- GIVEN a previous upload created the MCDONALDS `Merchant` + alias
- WHEN a second upload runs with `description="MCDONALDS SUC 13"`
- THEN no new `Merchant` is created (the alias table is hit on `normalized="mcdonalds"`) and the transaction has `merchant_id=<existing_mcdonalds.id>`

#### Scenario: User-supplied alias is preserved verbatim with computed normalized form

- GIVEN a merchant with `id=<mcdonalds.id>` and a user POSTs `{"alias_text": "MAC DONALDS"}` to `/api/v1/merchants/<mcdonalds.id>/aliases`
- WHEN the alias is created
- THEN the new `MerchantAlias` row has `alias_text="MAC DONALDS"` (verbatim) and `normalized="macdonalds"` (Python-computed)

### Requirement: LLM Merchant Normalization Is Opt-In via Feature Flag

The `LLM_MERCHANT_NORMALIZATION_ENABLED` config flag MUST default to `false`. When off, the LLM helper is a no-op (zero extra LLM cost). When on, the helper is called first-occurrence-only (one LLM call per unique normalized text per ingestion), and the result is cached in `merchant_aliases` with `source='llm'` and `confidence=<llm_score>`. (Decision #6; architecture pick C)

#### Scenario: Flag off → no LLM calls for merchant normalization

- GIVEN `LLM_MERCHANT_NORMALIZATION_ENABLED=false`
- WHEN `_build_transactions` runs with various `description` values
- THEN the LLM call count for the upload equals the original per-chunk count (no extra merchant LLM calls are made)

#### Scenario: Flag on → first-occurrence-only LLM call, cached in alias table

- GIVEN `LLM_MERCHANT_NORMALIZATION_ENABLED=true`
- WHEN the first upload with `description="AMBIGUOUS MERCHANT"` runs
- THEN the LLM is called once to extract the canonical merchant name, the result is cached in `merchant_aliases` (with `source='llm'`, `confidence=<llm_score>`), and a subsequent upload with the same `description` does NOT call the LLM again (cache hit on `merchant_aliases.normalized`)

#### Scenario: LLM helper is bounded by unique normalized text per ingestion

- GIVEN a 30-row statement with 30 unique `description` values and the flag is on
- WHEN the ingestion runs
- THEN the LLM is called at most 30 times (one per unique normalized text); subsequent uploads with the same descriptions do NOT call the LLM

### Requirement: `GET /api/v1/merchants` Returns the Full Merchant List

The `GET /api/v1/merchants` endpoint MUST return all merchants ordered by `name` ascending. The response is a list of `MerchantResponse` objects (mirrors `CategoryResponse`). (Decision #4)

#### Scenario: Empty list when no merchants exist

- GIVEN the `merchants` table is empty
- WHEN `GET /api/v1/merchants` is called
- THEN the response is an empty list `[]`

#### Scenario: Sorted list returned after several uploads

- GIVEN the `merchants` table has 12+ rows from several uploads
- WHEN `GET /api/v1/merchants` is called
- THEN the response is a list of all merchants, ordered by `name` ascending

### Requirement: `POST /api/v1/merchants/{id}/aliases` Adds a User Alias Atomically

The endpoint MUST accept a body `{"alias_text": "..."}` and create a `MerchantAlias` row with `source='user'` in a single `commit()`. The endpoint MUST respond 200 with the new alias on success, 404 when the merchant UUID does not exist, and 422 when the `alias_text` duplicates an existing alias (the `UNIQUE(alias_text)` constraint enforces this). (Decision #4)

#### Scenario: Happy path returns 200 with the new alias

- GIVEN a merchant with `id=<mcdonalds.id>`
- WHEN the client POSTs `{"alias_text": "MAC DONALDS"}` to `/api/v1/merchants/<mcdonalds.id>/aliases`
- THEN the response is 200 with the new alias, and a `MerchantAlias` row is created with `alias_text="MAC DONALDS"` and `source="user"`

#### Scenario: 404 when the merchant does not exist

- GIVEN no merchant with `id=<unknown_id>`
- WHEN the client POSTs to `/api/v1/merchants/<unknown_id>/aliases`
- THEN the response is 404

#### Scenario: 422 when the `alias_text` is a duplicate

- GIVEN a `MerchantAlias` row exists with `alias_text="MCDONALDS SUC 12"`
- WHEN the client POSTs `{"alias_text": "MCDONALDS SUC 12"}` to `/api/v1/merchants/<mcdonalds.id>/aliases`
- THEN the response is 422 (the `UNIQUE(alias_text)` constraint blocks the duplicate)

### Requirement: `Transaction.merchant_id` Is Set by the Normalizer

After `_build_transactions` resolves the merchant via the normalizer, the inserted `Transaction` row MUST have `merchant_id` set to the resolved `Merchant.id`. The shared `low_confidence` Boolean MUST be flipped to `True` if the merchant is new (decision #9 — the same Boolean is reused for both category and merchant miss signals; no second flag is added). (Decisions #4, #9)

#### Scenario: Known-pattern transaction gets `merchant_id` set

- GIVEN a transaction with `description="MCDONALDS SUC 12"`
- WHEN `_build_transactions` runs
- THEN the inserted `Transaction` has `merchant_id=<mcdonalds.id>` (the existing or newly-created MCDONALDS `Merchant`)

#### Scenario: Unknown-pattern transaction gets `merchant_id` set and `low_confidence=True`

- GIVEN a transaction with `description="TIENDA ONLINE XYZ"` (not in `KNOWN_MERCHANT_PATTERNS`)
- WHEN `_build_transactions` runs
- THEN the inserted `Transaction` has `merchant_id=<tienda_online_xyz.id>` (auto-created `Merchant`) and `low_confidence=True`
