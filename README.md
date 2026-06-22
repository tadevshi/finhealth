# finhealth

A personal financial analyzer and spend control. Self-hosted, single-user,
opinionated: ingest bank statement PDFs, parse them with LLMs, and surface where
the money actually goes.

> **Status:** Phase 1 — PDF ingestion with LLM extraction is live. Upload a
> Chilean bank statement (Santander, Itaú, Banco de Chile), the LLM extracts
> every line item, and the transactions list page lets you filter and tag.
> Categorization rules and the spend dashboard land in Phase 2+.

---

## Features

### Available now (Phase 1)

- **Async FastAPI** application with a clean factory pattern (`create_app`)
- **SQLite + SQLAlchemy 2.x (async)** with session-scoped dependency injection
- **Alembic** async migrations with up/down round-trip tests
- **Server-rendered web shell** (Jinja2 + HTMX + Alpine.js + Tailwind CSS)
  with a dark-mode toggle
- **PDF statement ingestion** — drop a Chilean bank statement PDF, the
  pipeline decrypts (pikepdf), extracts text (pdfplumber), detects the
  CMF NACIONAL / INTERNACIONAL variant, and parses transactions with the
  configured LLM
- **Provider-agnostic LLM** — `opencode_go` (default, OpenAI-compatible),
  `ollama`, or any `openai_compat` endpoint. Selected by `LLM_PROVIDER`
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

### Coming next

- **Phase 2 — Classification:** automatic spend categories, merchant alias
  map, recurring-transaction detection
- **Phase 3 — Dashboard:** charts, monthly trends, anomaly flags
- **Phase 4 — Insights:** budgeting rules, alerts, exports

---

## Prerequisites

- **Python 3.12+** (`python --version` should report 3.12 or newer)
- **pip** (bundled with Python 3.12)
- **git**
- A POSIX shell (bash, zsh). Windows works under WSL.

> No database server is required: finhealth uses a local SQLite file
> (`./finhealth.db`) created on first migration.

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

The SQLite database (`./finhealth.db`) is created on first run. The
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
| `http://127.0.0.1:8000/docs`     | Interactive OpenAPI / Swagger UI            |
| `http://127.0.0.1:8000/redoc`    | ReDoc API reference                         |

### API endpoints (v1)

| Method | Path                                  | Purpose                                       |
| ------ | ------------------------------------- | --------------------------------------------- |
| GET    | `/api/v1/health`                      | Liveness probe (DB round-trip)                |
| GET    | `/api/v1/banks`                       | List active banks (for the upload dropdown)   |
| POST   | `/api/v1/statements/upload`           | Upload a PDF + run the ingestion pipeline     |
| GET    | `/api/v1/statements/{statement_id}`   | Read a single statement (with transactions)   |
| GET    | `/api/v1/transactions`                | List transactions with filters                |
| PATCH  | `/api/v1/transactions/{transaction_id}` | Update a single transaction's category      |

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
   - Extracts text with `pdfplumber`
   - Detects the CMF NACIONAL / INTERNACIONAL variant
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

The Phase 1 E2E test (`tests/test_e2e_phase1.py`) requires the sample
PDFs in `shared/account-state-examples/`. The repo does not commit
those (they contain real card numbers); drop the bank's own statement
PDFs into that directory to exercise the pipeline locally.

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
│   │   ├── pdf/             #     PDF pipeline (decrypt, extract, variant, amount)
│   │   ├── llm/             #     LLM provider abstraction (opencode_go, ollama)
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
| `DATABASE_URL`      | `sqlite+aiosqlite:///./finhealth.db`   | Async SQLAlchemy URL                          |
| `CORS_ORIGINS`      | `["http://localhost:8000", ...]`       | Allowed CORS origins                          |
| `LLM_PROVIDER`      | `opencode_go`                          | LLM provider identifier                       |
| `LLM_API_ENDPOINT`  | `http://localhost:11434`               | Base URL for the LLM provider's HTTP API      |
| `LLM_MODEL`         | `qwen3.7-max`                          | Model name sent to the LLM provider           |
| `LLM_TIMEOUT`       | `60`                                   | Timeout (seconds) for one LLM call            |
| `LLM_MAX_RETRIES`   | `3`                                    | Automatic retries on transient LLM errors     |
| `PDF_UPLOAD_DIR`    | `shared`                               | Where uploaded PDFs are stored                |
| `MAX_FILE_SIZE_MB`  | `10`                                   | Upload size cap (413 over the cap)            |

---

## License

[MIT](./LICENSE)
