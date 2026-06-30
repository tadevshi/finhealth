# phase2-recurring-detection

## Purpose

Stub acknowledged. This capability is implemented in PR #5 (recurring rules
table, `RecurringDetector` service, endpoints, log differentiation). The full
spec is authored after PR #4 lands and unblocks migration 0007.

## ADDED Requirements

### Requirement: Stub Acknowledged

The phase-2-classification change acknowledges that `phase2-recurring-detection`
is delivered in PR #5. The capability contract is finalised when PR #5's design
is written. (Reason: PR #5's `recurring_rule_id` FK depends on PR #4's
`merchant_id` FK; the full recurring spec can only be authored after both are
committed.)
