# finhealth

A personal financial analyzer and spend control. Self-hosted, single-user,
opinionated: ingest bank statements, parse them with LLMs, and surface where the
money actually goes.

> **Status:** Phase 0 — foundation. The skeleton, database, app, migrations, and
> web shell are in place. PDF ingestion, LLM parsing, and the spend dashboard
> land in Phase 1+.

---

## Features

### Available now (Phase 0)

- **Async FastAPI** application with a clean factory pattern (`create_app`)
- **SQLite + SQLAlchemy 2.x (async)** with session-scoped dependency injection
- **Alembic** async migrations with up/down round-trip tests
- **Server-rendered web shell** (Jinja2 + HTMX + Alpine.js + Tailwind CSS)
  with a dark-mode toggle
- **Health endpoint** at `GET /api/v1/health` for liveness probes
- **Strict typing** (`mypy --strict`) and **linting** (`ruff check` +
  `ruff format`) wired in `pyproject.toml`
- **Test suite** with `pytest`, `pytest-asyncio`, and coverage

### Coming next

- **Phase 1 — PDF ingestion:** upload bank statement PDFs, extract text,
  parse transactions with an LLM
- **Phase 2 — Categorization:** automatic spend categories, merchants,
  recurring detection
- **Phase 3 — Dashboard:** charts, monthly trends, anomaly flags
- **Phase 4 — Insights:** budgeting rules, alerts, exports

---

## Prerequisites

- **Python 3.12+** (`python --version` should report 3.12 or newer)
- **pip** (bundled with Python 3.12)
- **git**
- A POSIX shell (bash, zsh). Windows works under WSL.

> No database server is required: Phase 0 uses a local SQLite file
> (`./finhealth.db`) created on first run.

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

Open `.env` in your editor. At minimum, replace `SECRET_KEY` with a real
random value for any non-development use:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

### 5. Run database migrations

```bash
alembic upgrade head
```

The SQLite database (`./finhealth.db`) is created on first run.

---

## Usage

### Start the development server

```bash
uvicorn app.main:app --reload
```

The server listens on `http://127.0.0.1:8000` by default.

### Available endpoints

| URL                              | Purpose                                     |
| -------------------------------- | ------------------------------------------- |
| `http://127.0.0.1:8000/`         | Web landing page (HTMX/Alpine/Tailwind UI)  |
| `http://127.0.0.1:8000/docs`     | Interactive OpenAPI / Swagger UI            |
| `http://127.0.0.1:8000/redoc`    | ReDoc API reference                         |
| `http://127.0.0.1:8000/api/v1/health` | JSON health check (liveness probe)     |
| `http://127.0.0.1:8000/static/`  | Static assets (CSS/JS/images)               |

### Quick health check

```bash
curl http://127.0.0.1:8000/api/v1/health
```

Expected response:

```json
{"status": "ok", "app": "finhealth", "version": "0.1.0"}
```

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
│   │   └── v1/              #     v1 router + health endpoint
│   ├── core/                #   Cross-cutting (settings, lifespan)
│   ├── db/                  #   Async engine + session factory
│   ├── models/              #   SQLAlchemy ORM models
│   ├── schemas/             #   Pydantic request/response models
│   ├── static/              #   Static assets served at /static
│   ├── web/                 #   Server-rendered HTML routes
│   │   └── templates/       #     Jinja2 templates (base, index)
│   └── main.py              #   FastAPI factory + module-level `app`
├── alembic/                 #   Database migrations
│   ├── env.py               #   Async-aware env config
│   └── versions/            #   Auto-generated migration scripts
├── tests/                   #   pytest test suite (async)
├── shared/                  #   Local-only data samples (gitignored)
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

| Variable        | Default                                | Purpose                          |
| --------------- | -------------------------------------- | -------------------------------- |
| `APP_NAME`      | `finhealth`                            | Display name + OpenAPI title     |
| `DEBUG`         | `false`                                | Verbose errors / autoreload hint |
| `SECRET_KEY`    | `change-me-in-production`              | Secret used for signing tokens   |
| `DATABASE_URL`  | `sqlite+aiosqlite:///./finhealth.db`   | Async SQLAlchemy URL             |
| `CORS_ORIGINS`  | `["http://localhost:8000", ...]`       | Allowed CORS origins             |

---

## License

[MIT](./LICENSE)
