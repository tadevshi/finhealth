# phase2-merchant-aliasing

## Purpose

Stub acknowledged. This capability is implemented across PRs #3 (UI hooks) and
#4 (merchants tables, normalization, LLM helper, endpoints). The full spec is
authored after PR #2 lands and unblocks the migration 0006 ALTER.

## ADDED Requirements

### Requirement: Stub Acknowledged

The phase-2-classification change acknowledges that `phase2-merchant-aliasing`
is delivered across later PRs. The capability contract is finalised when PR
#4's design is written. (Reason: PR #4 depends on PR #2's `category_id` ALTER
in migration 0006; the full merchant spec can only be authored after that
coordinating migration is committed.)
