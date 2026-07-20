# finhealth

A personal financial analyzer and spend control. Self-hosted, single-user,
opinionated: ingest bank statement PDFs, parse them with LLMs, and surface where
the money actually goes.

> **Status:** Phase 1 + Phase 2 are live. PDF ingestion with LLM extraction,
> closed-set categorization, merchant resolution, and deterministic
> recurring-transaction detection ship together. Upload a Chilean bank
> statement (Santander, Itaú, Banco de Chile), the pipeline categorises
> every line item, binds it to a canonical merchant, and flags recurring
> patterns (subscriptions, utility bills, monthly groceries). The spend
> dashboard lands in Phase 3+.

---

## Features

### Available now (Phase 1)

- **Async FastAPI** application with a clean factory pattern (`create_app`)
- **SQLite + SQLAlchemy 2.x (async)** with session-scoped dependency injection
- **Alembic** async migrations with up/down round-trip tests
- **Server-rendered web shell** (Jinja2 + HTMX + Alpine.js + Tailwind CSS)
  with a dark-mode toggle
- **PDF statement ingestion** — drop a Chilean bank statement PDF, the
  pipeline decrypts (pikepdf), converts the PDF to structured
  Markdown (markitdown), detects the CMF NACIONAL / INTERNACIONAL
  variant, and parses transactions with the configured LLM
- **Provider-agnostic LLM** — `opencode_go` (default, OpenAI-compatible),
  `ollama`, or `opencode_zen` (curated cloud models via the
  Anthropic-compatible endpoint). Selected by `LLM_PROVIDER`
- **Idempotent uploads** — re-uploading the same PDF for the same card is
  a no-op (SHA-256 dedup at the `(card, file_hash)` level)
- **Filterable transactions list** — date range, amount range, description
  search, currency; HTMX-powered filtering without page reloads
- **Manual category assignment** — PATCH a row's category in-place from
  the list page
- **Health endpoint** at `GET /api/v1/health` for liveness probes
- **Strict typing** (`mypy --strict`) and **linting** (`ruff check` +
  `ruff format`) wired in `pyproject.toml`
- **Test suite** with `pytest`, `pytest-asyncio`, and coverage

### Available now (Phase 2 — Classification, Merchant Resolution & Recurring Detection)

- **Closed-set LLM categorization** (`phase2-categories`,
  [spec](openspec/specs/phase2-categories/spec.md)) — the ingestion
  pipeline tells the LLM to emit one of 12 seeded Y-NAB category
  names verbatim, resolves the emitted string against the seed
  in a single SELECT + in-memory dict cache, and stamps the
  ``category_id`` FK + ``low_confidence=False`` on every hit.
  A miss keeps the LLM string with ``low_confidence=True`` so
  the user can re-tag the row by hand via the existing
  ``PATCH /api/v1/transactions/{id}`` endpoint. The
  ``POST /api/v1/categories/{id}`` rename endpoint propagates
  the change to every transaction's denormalized ``category``
  string atomically (single ``commit()`` covers the Category
  UPDATE + the cascade UPDATE on ``transactions``); a name
  collision is rejected with 422 *before* any write.
- **Merchant resolution & aliasing** (`phase2-merchant-aliasing`,
  [spec](openspec/specs/phase2-merchant-aliasing/spec.md)) — the
  deterministic normaliser in
  :mod:`app.services.merchants` strips Chilean legal-entity
  suffixes (``S.A.``, ``S.A.C.``, ``SpA``), branch identifiers
  (``SUC 12``, ``COM 3``), punctuation, and diacritics so two
  bank descriptions that refer to the same merchant land on the
  same canonical key. The :class:`MerchantNormalizer` does an
  alias-table hit-or-create: ~80% of the way is covered by
  :data:`KNOWN_MERCHANT_PATTERNS` (12 hardcoded canonical
  merchants → seeded categories); the remaining 20% auto-create
  a :class:`Merchant` row on first sight and bind the bank
  description as a ``MerchantAlias`` with ``source='auto'``.
  The user can extend the table via
  ``POST /api/v1/merchants/{id}/aliases``; the canonical
  ``normalized`` form is computed server-side and the row is
  stamped ``source='user'``.
- **Deterministic recurring-transaction detection**
  (`phase2-recurring-detection`,
  [spec](openspec/changes/phase-2-pr5-recurring-detection/specs/phase2-recurring-detection/spec.md)
  — will live at `openspec/specs/phase2-recurring-detection/spec.md`
  after PR #5's archive step) — :class:`RecurringDetector`
  runs at the end of every successful ingest. It scans the
  last 90 days of transactions on the same ``credit_card_id``,
  groups by ``(merchant_id, currency)``, drops amounts
  outside the ±15% median band, and requires ≥3 in-band
  occurrences to qualify. The median interval between
  consecutive in-band dates drives the period label
  (``weekly`` / ``biweekly`` / ``monthly`` / ``quarterly`` /
  ``yearly``); a 0.0–1.0 ``confidence`` score combines an
  occurrence-count factor with an amount-consistency factor
  (``min(1.0, occurrences/5) * max(0.0, 1.0 - (max-min)/median)``,
  rounded to 4 decimals). The detector upserts by a composite
  key ``(merchant_id, amount_min, amount_max, currency,
  period_days)`` and back-fills the ``recurring_rule_id`` FK
  on the just-ingested statement's transactions in the same
  ``commit()``. The user toggles a rule's visibility via
  ``PATCH /api/v1/recurring/{id}`` with ``{"is_active":
  false}``; the FK is preserved on deactivation (design D)
  so the historical audit trail survives.

### Available now (Phase 3 — Dashboard)

- **Read-side aggregation service** (`phase3-dashboard`,
  [spec](openspec/changes/phase-3-dashboard/specs/phase3-dashboard/spec.md))
  — :class:`app.services.dashboard.DashboardService` ships
  five pure-SQL aggregation methods (summary / categories /
  merchants / monthly / recurring) that all return
  per-currency sub-rollups (``{"CLP": ..., "USD": ...}``).
  The service is the single source of truth for the
  per-currency contract, the 12-row categories guarantee,
  the card-filter (``UUID | "all"``) semantics, and the
  monthly bar-chart time series. Multi-currency is honest
  about no FX: the app has no rate table, so the dashboard
  never sums across currencies — every aggregate is a
  per-currency dict the UI can render side-by-side.
- **JSON API endpoints** (``/api/v1/dashboard/*``, PR #9) —
  five thin HTTP wrappers over the service that validate
  the query params (``period`` regex, ``range`` ∈
  ``{0, 3, 6, 12}``, ``card_id`` UUID or ``"all"``) and
  return ``400`` for any other shape. The endpoints are
  the wire-format surface; the service is the business
  logic.
- **Server-rendered dashboard page** (`/dashboard`, PR #10) —
  a single page with two Alpine.js pickers (card + period)
  and five HTMX-loaded sections (KPIs, categories, top
  merchants, monthly bars, recurring list). The page is
  a pure server-render — **no JS chart library** (no
  Chart.js, ApexCharts, Plotly, ECharts, or D3). Every
  bar is a Tailwind ``<div>`` with
  ``style="width: {pct_of_max}%"`` computed server-side
  from the time-series max; the spec scenario "Each month
  renders a Tailwind bar with ``style="width: X%"``" is
  the contract. The first paint is meaningful with
  JavaScript disabled; HTMX + Alpine are a progressive
  enhancement.
- **Multi-currency side-by-side** — the KPI grid renders
  two sub-grids (CLP and USD) when both currencies are
  present in the period, and a single sub-grid otherwise.
  The monthly bar chart renders two sub-bars per month
  (indigo for CLP, emerald for USD) when both are
  present. No FX conversion is applied anywhere; the
  per-currency sub-rollup is the v1 contract.
- **Card picker** — "Todas las cards" (default) + every
  active :class:`CreditCard` row from the database,
  labelled ``"<bank.display_name> - <card_number_masked>"``.
  Inactive cards are excluded so the picker never shows
  cards the user has retired.
- **Period picker** — "Mes actual" (default), "Últimos
  3 / 6 / 12 meses", and "Todo el historial". The
  "all-time" option is the ``range=0`` sentinel the
  service treats as "every distinct month in the
  dataset". The current-month default matches the spec
  scenario "Period picker defaults to the current month".
- **Honest "top spenders" label** — the dashboard surfaces
  the top categories and merchants per period (per the
  categories and merchants endpoints). This is *not* an
  anomaly detector — there is no model, no flag, no alert.
  A future Phase 4 capability may add real anomaly
  detection; for v1 the top-N lists are honestly labelled
  as "top spenders" / "categorías principales".

### Coming next

- **Phase 4 — Insights:** budgeting rules, alerts, exports

---

- **Python 3.12+** (`python --version` should report 3.12 or newer)
- **pip** (bundled with Python 3.12)
- **git**
- A POSIX shell (bash, zsh). Windows works under WSL.

> No database server is required: finhealth uses a local SQLite file
> (`data/finhealth.db`) created on first migration.

### LLM endpoint

Ingestion needs an OpenAI-compatible chat-completions endpoint reachable
from the app process. The default `opencode_go` provider targets a local
daemon; `ollama` is the easiest self-hosted alternative. See the
[Configuration reference](#configuration-reference) for the full list.

---

## Installation

### 1. Clone the repository

```bash
git clone https://github.com/tadevshi/finhealth.git
cd finhealth
```

### 2. Create a virtual environment

```bash
python3.12 -m venv .venv
source .venv/bin/activate
```

On Windows (PowerShell):

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
```

### 3. Install dependencies (with dev extras)

```bash
pip install -e ".[dev]"
```

### 4. Configure environment variables

```bash
cp .env.example .env
```

Open `.env` in your editor. At minimum:

- Set `LLM_PROVIDER` and the matching endpoint / model.
- Replace `SECRET_KEY` with a real random value for any non-development
  use: `python -c "import secrets; print(secrets.token_urlsafe(32))"`.

### 5. Run database migrations

```bash
alembic upgrade head
```

The SQLite database (`data/finhealth.db`) is created on first run. The
Phase 1 migration seeds the three known banks (Santander, Itaú, Banco
de Chile) with the right password formulas.

---

## Usage

### Start the development server

```bash
uvicorn app.main:app --reload
```

The server listens on `http://127.0.0.1:8000` by default.

### Web UI

| URL                              | Purpose                                     |
| -------------------------------- | ------------------------------------------- |
| `http://127.0.0.1:8000/`         | Redirects to `/upload`                      |
| `http://127.0.0.1:8000/upload`   | Drag-and-drop statement upload form         |
| `http://127.0.0.1:8000/transactions` | Filterable, paginated transaction list |
| `http://127.0.0.1:8000/dashboard` | Phase 3 dashboard (KPIs + Tailwind bars + HTMX partials) |
| `http://127.0.0.1:8000/docs`     | Interactive OpenAPI / Swagger UI            |
| `http://127.0.0.1:8000/redoc`    | ReDoc API reference                         |

The `/dashboard` page renders the read-side aggregation
layer with two pickers (card + period) and five HTMX-loaded
sections. The first paint is meaningful with JavaScript
disabled; the picker change triggers an HTMX partial
refresh (no full page reload) and the per-section endpoint
hits the same :class:`app.services.dashboard.DashboardService`
the JSON API uses. See the
[Phase 3 spec](openspec/changes/phase-3-dashboard/specs/phase3-dashboard/spec.md)
for the per-section contract.

### API endpoints (v1)

| Method | Path                                  | Purpose                                       |
| ------ | ------------------------------------- | --------------------------------------------- |
| GET    | `/api/v1/health`                      | Liveness probe (DB round-trip)                |
| GET    | `/api/v1/banks`                       | List active banks (for the upload dropdown)   |
| POST   | `/api/v1/statements/upload`           | Upload a PDF + run the ingestion pipeline     |
| GET    | `/api/v1/statements/{statement_id}`   | Read a single statement (with transactions)   |
| GET    | `/api/v1/transactions`                | List transactions with filters                |
| PATCH  | `/api/v1/transactions/{transaction_id}` | Update a single transaction's category      |
| GET    | `/api/v1/categories`                  | List the 12 closed-set Y-NAB categories (PR #2) |
| POST   | `/api/v1/categories/{id}`             | Rename a category + propagate to its transactions (PR #2) |
| GET    | `/api/v1/merchants`                   | List canonical merchants (PR #4)              |
| POST   | `/api/v1/merchants/{id}/aliases`      | Bind a user-supplied alias to a merchant (PR #4) |
| GET    | `/api/v1/recurring`                   | List active recurring-transaction rules, freshest first (PR #5) |
| PATCH  | `/api/v1/recurring/{id}`              | Activate or deactivate a recurring rule (PR #5) |
| GET    | `/api/v1/dashboard/summary`           | Phase 3 KPI tile payload for a single month (PR #9) |
| GET    | `/api/v1/dashboard/categories`        | Phase 3 12 closed-set category rows for a month (PR #9) |
| GET    | `/api/v1/dashboard/merchants`         | Phase 3 top-N merchants for a month (PR #9) |
| GET    | `/api/v1/dashboard/monthly`           | Phase 3 monthly time series for the bar chart (PR #9) |
| GET    | `/api/v1/dashboard/recurring`         | Phase 3 active recurring rules with an in-band occurrence (PR #9) |

### Quick health check

```bash
curl http://127.0.0.1:8000/api/v1/health
```

Expected response:

```json
{"status": "ok", "app": "finhealth", "version": "0.1.0"}
```

### Upload workflow

1. Open `http://127.0.0.1:8000/upload` in your browser.
2. Pick or drop a Chilean credit-card statement PDF (Santander, Itaú,
   Banco de Chile are the three supported banks).
3. Choose the issuing bank from the dropdown.
4. Enter the cardholder's RUT (the bank password is derived from it).
5. Fill in the masked card number, cardholder name, and the currency
   that matches the statement.
6. Click **Upload statement**. The pipeline:
   - Decrypts the PDF with the bank-specific formula
   - Converts the PDF to structured Markdown with `markitdown`
     (Microsoft) — preserves table structure so small LLMs parse
     it in a fraction of the time they need for raw text
   - Detects the CMF NACIONAL / INTERNACIONAL variant
    - **Chunks the text into overlapping windows of `LLM_MAX_INPUT_CHARS`
      chars (5000 by default) and calls the LLM once per chunk.** A
      full CMF statement is ~18k chars of Markdown via markitdown,
      but small local models (qwen2.5:1.5b) produce malformed JSON
      on long prompts. The chunker walks the document with a sliding
      window of `LLM_MAX_INPUT_CHARS` and `LLM_CHUNK_OVERLAP_CHARS`
      (200 by default) so a transaction that straddles a chunk
      boundary is still present in full in at least one chunk.
      A single Santander upload typically produces 3-4 LLM calls;
      the orchestrator dedupes the merged transaction list on
      `(date, description, amount)`.
    - Calls the configured LLM with bank+variant context
    - Parses amounts to `Decimal` and dates to the statement period
    - Persists the statement (status `completed`) + every transaction
7. The success banner links to `/transactions` where the rows are
   filterable and each row's category can be edited in place.

### Transaction filters

The `/transactions` page supports filtering by:

- **Date range** — `date_from` and `date_to` (inclusive, ISO `YYYY-MM-DD`)
- **Amount range** — `min_amount` and `max_amount` (absolute value bounds)
- **Description substring** — case-insensitive `ILIKE` match
- **Currency** — `CLP` or `USD`
- **Statement** — `statement_id` UUID to scope to a single statement

Filters compose with AND. Pagination and cursor scrolling arrive in a
later WU; for now the page returns up to 200 rows.

---

## Development

### Run the test suite

```bash
pytest
```

The suite is async-mode auto and includes a coverage report by default. To
skip coverage:

```bash
pytest --no-cov
```

For Phase 3 dashboard hardening verification, run the focused harness:

```bash
./scripts/verify.sh
```

The script runs the dashboard/seed/SQLite/documentation/Docker lifecycle
focused pytest suite, Ruff, `compileall`, and the disposable Docker checks.
The Docker lifecycle test uses an isolated Compose project and xfails cleanly
when Docker is unavailable.

The Phase 1 E2E test (`tests/test_e2e_phase1.py`) requires the sample
PDFs in `shared/account-state-examples/`. The repo does not commit
those (they contain real card numbers); drop the bank's own statement
PDFs into that directory to exercise the pipeline locally.

#### Running the integration tests (real PDFs)

Integration and E2E tests that decrypt the real sample PDFs need the
cardholder's RUT to derive the per-bank password. The RUT is read
from the `TEST_RUT` environment variable so the real identifier never
has to be committed:

```bash
TEST_RUT=12.345.678-9 pytest tests/test_e2e_phase1.py
```

Without `TEST_RUT`, those tests are skipped automatically — the
deterministic unit tests (password deriver, amount parser, variant
detector) and the rest of the suite still run. Any standard Chilean
RUT format is accepted (with or without the verification digit,
dots, spaces). See `.env.example` for the canonical entry.

### Lint and format

```bash
ruff check .        # lint
ruff format .       # auto-format
```

### Static type checking

```bash
mypy --strict app/
```

The `app/` package is checked under `--strict`. `alembic/versions/` and
`tests/` are intentionally excluded — see `pyproject.toml` for the exact
configuration.

### Database migrations

```bash
# Apply all pending migrations
alembic upgrade head

# Roll back one migration
alembic downgrade -1

# Create a new migration (autogenerate)
alembic revision --autogenerate -m "describe change"

# Show current revision
alembic current

# Show migration history
alembic history
```

The migration history is small and intentionally append-only:

| Revision                                            | Phase | Purpose                                                              |
| --------------------------------------------------- | ----- | -------------------------------------------------------------------- |
| `0001_initial`                                      | 1     | Initial schema (banks, credit cards, statements, transactions)        |
| `0002_phase1_ingestion`                             | 1     | Phase 1 ingestion columns + indexes                                  |
| `0003_statement_error_message`                      | 1     | Surface ingestion failures on the statement row                      |
| `0004_timestamp_server_defaults`                    | 1     | Server-side `CURRENT_TIMESTAMP` defaults for `created_at`/`updated_at` |
| `0005_phase2_categories`                            | 2     | `categories` table + seed of 12 Y-NAB rows |
| `0006_phase2_merchants_transactions_alter`          | 2     | `merchants` + `merchant_aliases` tables; `category_id` + `merchant_id` FKs + `low_confidence` on `transactions` |
| `0007_phase2_recurring_rules`                       | 2     | `recurring_rules` table; `recurring_rule_id` FK on `transactions`    |

`alembic upgrade head` runs all seven in order and is invoked
automatically on every container start (see
[Docker deployment](#docker-deployment) below), so a fresh
clone + `docker compose up` is enough to land on the latest
schema with no manual steps.

### Pre-commit hooks (optional)

The repository ships with a `.pre-commit-config.yaml` that runs `ruff`,
`ruff-format`, and `mypy` on staged files. To enable it:

```bash
pip install pre-commit
pre-commit install
```

Now every `git commit` will run the hooks on changed files. To run them
manually across the whole repo:

```bash
pre-commit run --all-files
```

---

## Project structure

```
finhealth/
├── app/                     # Application package
│   ├── api/                 #   API routes (versioned)
│   │   └── v1/              #     v1 router (banks, health, statements, transactions)
│   ├── core/                #   Cross-cutting (settings, lifespan)
│   ├── db/                  #   Async engine + session factory
│   ├── models/              #   SQLAlchemy ORM models (Bank, CreditCard, Statement, Transaction)
│   ├── schemas/             #   Pydantic request/response models
│   ├── services/            #   Domain services
│   │   ├── pdf/             #     PDF pipeline (decrypt, extract, variant, amount, chunk)
│   │   ├── llm/             #     LLM provider abstraction (opencode_go, ollama, opencode_zen)
│   │   └── ingestion.py     #     Orchestrator (PDF + LLM + DB)
│   ├── static/              #   Static assets served at /static
│   ├── web/                 #   Server-rendered HTML routes
│   │   └── templates/       #     Jinja2 templates (base, upload, transactions, partials)
│   └── main.py              #   FastAPI factory + module-level `app`
├── alembic/                 #   Database migrations
│   ├── env.py               #     Async-aware env config
│   └── versions/            #     Auto-generated migration scripts
├── tests/                   #   pytest test suite (async)
├── shared/                  #   Local-only data samples (gitignored — drop your bank PDFs here)
├── .env.example             # Environment template
├── .pre-commit-config.yaml  # Optional pre-commit hooks
├── alembic.ini              # Alembic config
├── pyproject.toml           # Project + tooling config
└── README.md
```

---

## Configuration reference

All settings are read from environment variables (or a `.env` file in the
project root). See `.env.example` for the full list. Key entries:

| Variable            | Default                                | Purpose                                       |
| ------------------- | -------------------------------------- | --------------------------------------------- |
| `APP_NAME`          | `finhealth`                            | Display name + OpenAPI title                  |
| `DEBUG`             | `false`                                | Verbose errors / autoreload hint              |
| `SECRET_KEY`        | `change-me-in-production`              | Secret used for signing tokens                |
| `DATABASE_URL`      | `sqlite+aiosqlite:///data/finhealth.db` | Async SQLAlchemy URL                          |
| `CORS_ORIGINS`      | `["http://localhost:8000", ...]`       | Allowed CORS origins                          |
| `LLM_PROVIDER`      | `opencode_go`                          | LLM provider identifier                       |
| `LLM_API_ENDPOINT`  | `http://localhost:11434`               | Base URL for the LLM provider's HTTP API      |
| `LLM_MODEL`         | `qwen3.7-max`                          | Model name sent to the LLM provider           |
| `LLM_TIMEOUT`       | `60`                                   | Timeout (seconds) for one LLM call            |
| `LLM_MAX_RETRIES`   | `3`                                    | Automatic retries on transient LLM errors     |
| `LLM_MAX_INPUT_CHARS` | `5000`                               | Max chars of PDF text per chunk sent to the LLM |
| `LLM_CHUNK_OVERLAP_CHARS` | `200`                            | Overlap between consecutive PDF chunks         |
| `PDF_UPLOAD_DIR`    | `shared`                               | Where uploaded PDFs are stored                |
| `MAX_FILE_SIZE_MB`  | `10`                                   | Upload size cap (413 over the cap)            |

---

## License

[MIT](./LICENSE)

---

## Deployment

finhealth ships with two docker-compose profiles. Pick the one
that matches your environment:

### Option 1: OpenCode Zen (recommended, pay-as-you-go)

[OpenCode Zen](https://opencode.ai/zen) is a curated list of
LLM models (Claude, Qwen, Gemini, etc.) with transparent
per-token pricing and a single API key. Most recommended
models for finhealth are served through Zen's
Anthropic-compatible `/v1/messages` endpoint.

1. Sign up at <https://opencode.ai/auth> and copy your API
   key.
2. Set the following in `.env`:

   ```bash
   LLM_PROVIDER=opencode_zen
   LLM_API_ENDPOINT=https://opencode.ai/zen/v1
   LLM_API_KEY=your-key-here
   LLM_MODEL=qwen3.7-plus
   ```

3. Start the app:

   ```bash
   docker compose up -d
   ```

**Recommended models for PDF parsing**:

| Model              | Cost (1M tokens in/out) | Quality | Use case                          |
| ------------------ | ----------------------- | ------- | --------------------------------- |
| `qwen3.7-plus`     | $0.40 / $1.60           | Good    | Default, best value               |
| `gemini-3-flash`   | $0.50 / $3.00           | Good    | Cheapest, good for simple statements |
| `claude-haiku-4-5` | $1.00 / $5.00           | Better  | Complex layouts                    |
| `qwen3.7-max`      | $2.50 / $7.50           | Best    | Premium quality                    |

Start with `qwen3.7-plus` and switch up if you see extraction
mistakes. The pricing is per-token and the model is
re-evaluated on every call, so switching is a one-line
`.env` change and a container restart.

See the [Docker deployment](#docker-deployment) section below
for the full operational reference (volumes, health checks,
logs, updating).

### Option 2: Self-hosted with Ollama (CPU-friendly, no API costs)

### Option 2: Self-hosted with Ollama (CPU-friendly, no API costs)

For servers without a GPU or a budget for API costs. Runs Ollama
as a sidecar container; the app talks to it over Docker's
internal network. Optimized for CPU-only inference with limited
RAM (2-4 GB).

```bash
# 1. Start the Ollama sidecar (waits for healthy state before
#    finhealth starts)
docker compose -f docker-compose.self-hosted.yml up -d ollama

# 2. Pull a CPU-friendly model (one-time, ~1-3 GB download)
./scripts/pull-ollama-model.sh qwen2.5:1.5b

# 3. Start finhealth
docker compose -f docker-compose.self-hosted.yml up -d
```

The compose file pins the LLM provider to `ollama` and points
`LLM_API_ENDPOINT` at `http://ollama:11434` (Docker's internal DNS
for the sidecar). Override `LLM_MODEL` in `.env` to pick a
different model — the script reads it as the default.

**Recommended models for CPU-only servers**:

| Model | RAM | Speed | Quality | Use case |
|-------|-----|-------|---------|----------|
| `qwen2.5:1.5b` | ~1GB | Fast | Good | Default, JSON/structured output |
| `llama3.2:3b` | ~2GB | Medium | Better | General purpose, good Spanish |
| `phi3.5:3.8b` | ~3GB | Slow | Best | Complex parsing, highest accuracy |

**Memory considerations**:
- Each model needs RAM for weights + KV cache
- 1.5b models: ~2GB total
- 3b models: ~3-4GB total
- Set `deploy.resources.limits.memory` in docker-compose accordingly

Pulled models are persisted in the `ollama_data` named volume, so
they survive `docker compose down` and container restarts. Use
`docker compose -f docker-compose.self-hosted.yml down -v` only
when you want a clean slate (it wipes the model cache too).

---

## Docker deployment

A self-hosted single-user app is the natural fit for a single
container, so finhealth ships a multi-stage `Dockerfile` and a
`docker-compose.yml` that mount the SQLite database and the
upload directory as host bind mounts for persistence. The
`docker-compose.yml` in this directory targets **cloud LLM
providers**; for self-hosted Ollama see
[Option 2](#option-2-self-hosted-with-ollama-cpu-friendly-no-api-costs)
above.

### Quick start

1. Create a `.env` file from the template (see the configuration
   section below for the relevant keys):

   ```bash
   cp .env.example .env
   # Edit .env — at minimum set SECRET_KEY, LLM_API_ENDPOINT,
   # and LLM_API_KEY (for cloud providers).
   ```

2. Build the image and start the container in the background:

   ```bash
   docker compose build
   docker compose up -d
   ```

3. Open <http://localhost:8000>. The upload page is at
   `/upload`, the transactions list at `/transactions`, the
   interactive API docs at `/docs`.

The first `up -d` runs `alembic upgrade head` on container start,
so the SQLite schema is created and the three supported banks
(Santander, Itaú, Banco de Chile) are seeded automatically.

### Configuration

All settings come from environment variables, which the compose
file reads from your local `.env`. The most relevant keys for a
Docker deployment are:

| Variable            | Default (Docker)                              | Purpose                                       |
| ------------------- | --------------------------------------------- | --------------------------------------------- |
| `DATABASE_URL`      | `sqlite+aiosqlite:////app/data/finhealth.db`  | Async SQLAlchemy URL. The path is *inside* the container — the host bind mount on `./data` makes it persistent. |
| `SECRET_KEY`        | `change-me-in-production`                     | Set a real random value in `.env` for production. |
| `LLM_PROVIDER`      | `opencode_go`                                 | LLM provider identifier (`opencode_go`, `ollama`, `opencode_zen`). |
| `LLM_API_ENDPOINT`  | *(unset — required)*                          | Base URL for the LLM provider's HTTP API. No default in the cloud compose file; the app fails fast if it is unset. For self-hosted Ollama, use `docker-compose.self-hosted.yml` which points at `http://ollama:11434`. |
| `LLM_API_KEY`       | *(unset)*                                     | Required for cloud providers. Optional for local providers like Ollama. |
| `LLM_MODEL`         | `qwen3.7-max`                                 | Model name sent to the LLM provider. |
| `LLM_TIMEOUT`       | `60`                                          | Timeout (seconds) for one LLM call. |
| `LLM_MAX_RETRIES`   | `3`                                           | Automatic retries on transient LLM errors. |
| `LLM_MAX_INPUT_CHARS` | `5000`                                     | Max chars of PDF text per chunk sent to the LLM. |
| `LLM_CHUNK_OVERLAP_CHARS` | `200`                                  | Overlap between consecutive PDF chunks. |
| `PDF_UPLOAD_DIR`    | `/app/shared`                                 | Where uploaded PDFs are stored *inside* the container. |
| `MAX_FILE_SIZE_MB`  | `10`                                          | Upload size cap (returns 413 over the cap). |

### Volumes

The compose file bind-mounts two host directories into the
container. They survive `docker compose down` and any number of
container restarts. `docker compose down -v` removes Docker named
volumes, but it does **not** delete bind-mounted host directories
such as `./data` or `./shared`.

| Host path   | Container path  | Purpose                                                                 |
| ----------- | --------------- | ----------------------------------------------------------------------- |
| `./shared`  | `/app/shared`   | PDF uploads. Drop a statement PDF in `./shared/` and it is immediately visible to the running app. |
| `./data`    | `/app/data`     | SQLite database + WAL files. The schema, the main DB, and the write-ahead log all live in one directory so atomic commits survive a restart. |

Both directories are tracked as empty in git via `.gitkeep`
files, so a fresh `git clone` produces the directories Docker
needs to bind-mount. Anything you drop in there is ignored by
git (see `.gitignore`).

### SQLite backup and restore

The canonical database path is `data/finhealth.db` locally and
`/app/data/finhealth.db` inside Docker. Because SQLite may use WAL
files, do not copy a live database file blindly. Use the verified
helper, which uses SQLite's backup API, runs `PRAGMA integrity_check`,
and writes a row-count manifest for `transactions`, `statements`,
`credit_cards`, and `banks`:

```bash
python -m app.cli.sqlite_ops backup \
  sqlite:///data/finhealth.db \
  backups/finhealth-$(date +%F).db
```

To restore, stop the app first, validate and atomically replace the DB,
then restart and check health:

```bash
docker compose stop finhealth
python -m app.cli.sqlite_ops restore \
  backups/finhealth-YYYY-MM-DD.db \
  sqlite:///data/finhealth.db
docker compose up -d finhealth
curl http://localhost:8000/api/v1/health
```

For a stopped-container backup variant, stop the container and copy the
database directory snapshot with the same helper used by the tests:

```bash
python -c "from app.cli.sqlite_ops import copy_stopped_container_db; copy_stopped_container_db('data', 'backups/finhealth-stopped.db')"
```

The exact bind-mount lifecycle `docker compose down && docker compose up -d`
preserves `./data/finhealth.db` because `./data` is a host directory, not a
named Docker volume. Likewise, `docker compose down -v does **not** delete bind-mounted host directories`;
it removes named volumes only.

Restore removes stale `data/finhealth.db-wal` and
`data/finhealth.db-shm` before replacement. A corrupt backup or a
non-SQLite URL is rejected before the destination is mutated. After
restore, compare the generated `.manifest.json` counts with the
post-restore manifest and smoke-test `/dashboard` plus the JSON health
endpoint.

### File ownership (HOST_UID / HOST_GID)

The compose file maps the container's effective user to
`${HOST_UID:-0}:${HOST_GID:-0}`. The default `0:0` (root)
works for any host that does not care who owns the files in
`./data` and `./shared`; the SQLite database and any uploaded
PDFs will be owned by `root` on the host.

To keep those files owned by your own user (recommended for
self-hosted deployments), set `HOST_UID` and `HOST_GID` in
`.env` to match your host user:

```bash
echo "HOST_UID=$(id -u)" >> .env
echo "HOST_GID=$(id -g)" >> .env
docker compose up -d
```

The variables are read on every `docker compose` invocation, so
no rebuild is needed — `docker compose restart` is enough to
apply a new UID/GID.

### Updating

Pull the new code, rebuild, and restart:

```bash
git pull
docker compose build
docker compose up -d
```

Migrations are applied automatically on every container start,
so a schema change in a newer image does not need a manual
`alembic upgrade head`.

### Logs

```bash
# Stream the app's stdout/stderr.
docker compose logs -f finhealth

# Last 100 lines, no follow.
docker compose logs --tail=100 finhealth
```

### Health check

The container declares a Docker-level `HEALTHCHECK` against
`/api/v1/health`, which does a DB round-trip. Check the state
with:

```bash
docker compose ps
#   NAME        IMAGE           COMMAND                  SERVICE     STATUS
#   finhealth   finhealth:local "sh -c alembic upgrad…"  finhealth   Up 2 minutes (healthy)
```

The status column switches to `(unhealthy)` if the DB is
unreachable or the migrations fail.
