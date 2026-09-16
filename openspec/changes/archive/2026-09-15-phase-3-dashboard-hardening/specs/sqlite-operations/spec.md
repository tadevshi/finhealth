# sqlite-operations Specification

## Purpose

Canonical SQLite path, bind-mount persistence, and verified backup/restore procedures. PostgreSQL migration is explicitly out of scope for this change.

## Requirements

### Requirement: Canonical SQLite Path

The application MUST use a single canonical SQLite database location `data/finhealth.db` for both local development and Docker execution. Configuration (`app/core/config.py`, `.env.example`) MUST default to this path. Any prior alternative local path (e.g. `./finhealth.db` at repo root) MUST be removed or redirected.

#### Scenario: Local default uses data/finhealth.db

- **GIVEN** no `DATABASE_URL` override
- **WHEN** the application starts locally
- **THEN** SQLite opens `data/finhealth.db` (relative to the working directory)

#### Scenario: Docker default uses data/finhealth.db

- **GIVEN** the container runs with the standard compose file
- **WHEN** the application starts inside the container
- **THEN** SQLite opens `/app/data/finhealth.db`, which corresponds to the host `./data/finhealth.db` via bind mount

### Requirement: Bind-Mount Persistence

Docker Compose MUST bind-mount the host `./data` directory to `/app/data` in the application container, so `finhealth.db`, its WAL (`-wal`), and SHM (`-shm`) files persist across container restarts. The compose file MUST NOT declare `./data` as a named volume.

#### Scenario: Database persists across container restart

- **GIVEN** the container has written rows to `finhealth.db`
- **WHEN** `docker compose down` is run (without `-v`) and `docker compose up` is run again
- **THEN** the host `./data/finhealth.db` retains all rows written before the restart

#### Scenario: `-v` flag does not remove bind-mounted host data

- **GIVEN** the compose file uses a bind mount for `./data`
- **WHEN** `docker compose down -v` is run
- **THEN** the host `./data/` directory and its contents are unaffected (named volumes only are removed)

### Requirement: Verified Backup Procedure

The documentation MUST provide a verified backup procedure that produces a consistent SQLite snapshot. The procedure MUST use one of: SQLite's backup API, OR a stopped-container (or checkpointed) copy of the main file together with WAL handling. The procedure MUST verify the backup's integrity after creation (e.g. `PRAGMA integrity_check` against the backup).

#### Scenario: Backup via SQLite backup API

- **GIVEN** a running application with active writes
- **WHEN** the documented backup command runs
- **THEN** a backup file is produced that passes `PRAGMA integrity_check`

#### Scenario: Backup via stopped container

- **GIVEN** the container is stopped
- **WHEN** the host copies `./data/finhealth.db` (plus `-wal` and `-shm` if present)
- **THEN** the copied file passes `PRAGMA integrity_check`

### Requirement: Verified Restore Procedure

The documentation MUST provide a verified restore procedure. Restore MUST stop the application, replace `data/finhealth.db` (and remove stale `-wal`/`-shm`), restart the application, and then run a health check that confirms row counts match the backup's recorded counts.

#### Scenario: Restore preserves row counts

- **GIVEN** a backup with recorded counts for `transactions`, `statements`, `credit_cards`, `banks`
- **WHEN** the documented restore procedure runs
- **THEN** post-restore row counts match the recorded counts and the dashboard endpoints return HTTP 200

#### Scenario: Restore fails fast on integrity error

- **GIVEN** a corrupted backup file
- **WHEN** `PRAGMA integrity_check` runs during restore verification
- **THEN** the procedure aborts before the application is restarted

### Requirement: Documentation Accuracy

`README.md` MUST accurately describe the persistence model: bind-mount semantics, the single canonical path `data/finhealth.db`, the correct effect of `docker compose down -v`, and the verified backup/restore runbook. Claims that `down -v` deletes bind-mounted host directories MUST be removed.

#### Scenario: README does not claim `-v` deletes host data

- **GIVEN** the README is rendered
- **WHEN** the reader inspects the Docker persistence section
- **THEN** no statement claims that `docker compose down -v` deletes the host `./data/` directory

#### Scenario: README documents backup/restore runbook

- **GIVEN** the README is rendered
- **WHEN** the reader inspects the operations section
- **THEN** a backup procedure and a restore procedure with verification steps are present
