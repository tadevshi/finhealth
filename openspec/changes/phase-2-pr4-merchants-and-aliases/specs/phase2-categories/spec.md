# Delta for phase2-categories

## Purpose

Phase 2 PR #4 extends the partial migration `0006_phase2_merchants_transactions_alter.py` (originally started in PR #2) with the `merchants` + `merchant_aliases` tables and the `transactions.merchant_id` FK. The capability's behaviour (9 requirements, 22 scenarios from PR #2 + PR #3) is unchanged; only the schema is extended. The new merchant-related behaviour (canonicalization, alias management, deterministic + LLM normalization, merchant API endpoints) is documented in the sibling `phase2-merchant-aliasing` spec — this delta exists only to mark the migration 0006 extension as part of the same change.

## ADDED Requirements

None for this delta. The new merchant behaviour is in the sibling `phase2-merchant-aliasing` spec; the existing `phase2-categories` capability does not gain any new requirements from PR #4.

## MODIFIED Requirements

None for this delta. The 9 existing requirements in the main `phase2-categories` spec (5 from PR #2 + 4 from PR #3) are unchanged. The `low_confidence` Boolean they stamp is now shared with the merchant normalizer (single column, no schema split) — that sharing is captured in the `phase2-merchant-aliasing` spec, not here.
