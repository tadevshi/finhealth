#!/usr/bin/env bash
set -euo pipefail

pytest tests/test_dashboard.py tests/test_dashboard_api.py tests/test_web_phase3.py tests/test_seed_demo.py tests/test_sqlite_ops.py tests/test_dashboard_selection.py tests/test_config.py tests/test_documentation.py tests/test_docker_lifecycle.py --no-cov
ruff check app tests
python -m compileall -q app tests
pytest tests/test_documentation.py tests/test_docker_lifecycle.py --no-cov
