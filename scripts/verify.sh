#!/usr/bin/env bash
set -euo pipefail

# Keep collection of PostgreSQL-backed settings tests deterministic when the
# verifier is invoked from a clean shell. Integration tests still require an
# explicitly configured POSTGRES_TEST_HOST and skip when it is unavailable.
: "${POSTGRES_USER:=finhealth}"
: "${POSTGRES_PASSWORD:=secret}"
: "${POSTGRES_DB:=finhealth}"
export POSTGRES_USER POSTGRES_PASSWORD POSTGRES_DB

pytest tests/test_dashboard.py tests/test_dashboard_api.py tests/test_web_phase3.py tests/test_seed_demo.py tests/test_dashboard_selection.py tests/test_config.py tests/test_documentation.py tests/test_docker_lifecycle.py --no-cov
ruff check app tests
python -m compileall -q app tests
pytest tests/test_documentation.py tests/test_docker_lifecycle.py --no-cov
