# demo-data-seeding Specification

## Purpose

Repeat-safe, deterministic demo seeding. Seed rows are provenance-scoped, category aliases resolve to canonical closed-set names through case/Unicode normalization, and re-running the seed never destroys rows the seed did not create.

## Requirements

### Requirement: Category Alias Normalization

The seed MUST resolve plan keys (`dining`, `transport`, `services`, …) to canonical closed-set display names (`Dining Out`, `Transportation`, `Bills`, …) through a case-folded, Unicode-normalized alias table. Resolution MUST be deterministic. Unknown plan keys MUST map to `Uncategorized`.

#### Scenario: Normalized alias resolves

- **GIVEN** plan key `"dining"` and alias `"dining" → "Dining Out"`
- **WHEN** seed resolves the key
- **THEN** result is `Dining Out`

#### Scenario: Unknown key falls back

- **GIVEN** plan key `"something_new"` absent from alias table
- **WHEN** seed resolves the key
- **THEN** result is `Uncategorized`

### Requirement: Deterministic Seed Provenance

Every row the seed creates MUST carry a stable seed-provenance marker. The marker MUST be identical across runs given the same input. The seed MUST NOT mark rows it did not create.

#### Scenario: Seed rows carry the marker

- **GIVEN** a fresh database
- **WHEN** seed completes
- **THEN** every inserted seed row has the marker set

#### Scenario: Non-seed rows stay unmarked

- **GIVEN** a user-inserted transaction predating the run
- **WHEN** seed runs
- **THEN** the user transaction's marker remains unset

### Requirement: Repeat-Safe Non-Destructive Upsert

Re-running the seed MUST yield seed-owned rows byte-identical (modulo allowed timestamps) to the first run. The seed MUST NOT delete, truncate, or overwrite rows not provably seed-owned. Table-wide deletes are FORBIDDEN.

#### Scenario: Two runs yield identical seed-owned rows

- **GIVEN** a seeded database
- **WHEN** seed runs a second time
- **THEN** seed-owned rows by stable key and non-timestamp values are identical

#### Scenario: User rows survive

- **GIVEN** a user transaction `T_user` on a pre-existing statement
- **WHEN** seed runs
- **THEN** `T_user` remains unchanged

#### Scenario: Seed never attaches to user statements

- **GIVEN** a user-owned statement `S_user`
- **WHEN** seed runs
- **THEN** no seed-owned transaction is attached to `S_user`

### Requirement: Seed-Owned Statements and Transactions

Seed-created statements and transactions MUST reconcile only against statements that are themselves seed-owned, using a stable deterministic key (file-hash or seeded UUID). Reconciliation MUST NOT match on mutable attributes (card + period) alone.

#### Scenario: Stable key lookup

- **GIVEN** a deterministic key derived from seed input
- **WHEN** seed looks up an existing seed statement
- **THEN** only the statement with that exact key matches

#### Scenario: Mutable attributes alone do not reconcile

- **GIVEN** a user statement on the same card/period
- **WHEN** seed runs
- **THEN** user statement untouched; new seed statement created with own provenance

### Requirement: Uncategorized Category Behavior

The `Uncategorized` closed-set category MUST be present in the seeded set. Seed transactions resolved to it MUST count under it like any other category. When no seed transaction resolves there, its period total MUST be `{}` and count `0`.

#### Scenario: Uncategorized with zero spend

- **GIVEN** no seed transaction resolves to `Uncategorized`
- **WHEN** `DashboardService.categories` called
- **THEN** row present with `total_per_currency == {}`, `transaction_count == 0`

#### Scenario: Uncategorized with seed spend

- **GIVEN** 2 seed transactions resolve to `Uncategorized`
- **WHEN** `DashboardService.categories` called
- **THEN** row reflects those 2 transactions
