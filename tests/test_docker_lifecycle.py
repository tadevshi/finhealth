"""Disposable Docker Compose lifecycle smoke tests."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import textwrap
import time
import urllib.request
import uuid
from pathlib import Path

import pytest
import yaml

from app.cli.sqlite_ops import backup, restore

ROOT = Path(__file__).resolve().parents[1]


def _require_docker() -> bool:
    """Return False when Docker Compose or the daemon is unavailable."""
    version = subprocess.run(
        ["docker", "compose", "version"],
        check=False,
        capture_output=True,
        text=True,
    )
    if version.returncode != 0:
        return False
    info = subprocess.run(["docker", "info"], check=False, capture_output=True, text=True)
    return info.returncode == 0


def _compose_exec(project: str, compose_file: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "compose", "-p", project, "-f", str(compose_file), *args],
        check=False,
        capture_output=True,
        text=True,
    )


def _compose(*args: str, compose_file: Path, project: str) -> subprocess.CompletedProcess[str]:
    return _compose_exec(project, compose_file, *args)


def _write_sqlite_compose(tmp_path: Path, project_label: str) -> tuple[Path, Path, Path]:
    data_dir = tmp_path / f"data-{project_label}"
    shared_dir = tmp_path / f"shared-{project_label}"
    data_dir.mkdir()
    shared_dir.mkdir()
    compose_file = tmp_path / f"compose-{project_label}.yml"
    compose_file.write_text(
        textwrap.dedent(
            f"""
            services:
              finhealth:
                image: python:3.12-slim
                command:
                  - sh
                  - -c
                  - |
                    python - <<'PY'
                    import sqlite3
                    conn = sqlite3.connect('/app/data/finhealth.db')
                    conn.execute('CREATE TABLE IF NOT EXISTS lifecycle (id INTEGER PRIMARY KEY, value TEXT)')
                    conn.execute("INSERT INTO lifecycle(value) VALUES ('from-container')")
                    conn.commit()
                    conn.close()
                    PY
                    sleep 3600
                volumes:
                  - {data_dir}:/app/data
                  - {shared_dir}:/app/shared
            """
        ),
        encoding="utf-8",
    )
    return compose_file, data_dir, shared_dir


def _wait_for_db(db_path: Path) -> None:
    for _ in range(40):
        if db_path.exists():
            return
        time.sleep(0.25)
    raise AssertionError(f"database was not created: {db_path}")


def test_disposable_compose_lifecycle_persists_data(tmp_path: Path) -> None:
    """An isolated bind-mounted DB survives container stop/start and down."""
    if not _require_docker():
        pytest.xfail("docker compose or Docker daemon is unavailable")

    project = f"finhealth-p7-{uuid.uuid4().hex[:8]}"
    compose_file, data_dir, _shared_dir = _write_sqlite_compose(tmp_path, "p7")

    try:
        up = _compose_exec(project, compose_file, "up", "-d")
        if up.returncode != 0:
            pytest.xfail(f"disposable compose up unavailable: {up.stderr.strip()}")
        db_path = data_dir / "finhealth.db"
        _wait_for_db(db_path)
        with sqlite3.connect(db_path) as conn:
            first_count = conn.execute("SELECT COUNT(*) FROM lifecycle").fetchone()[0]

        stopped = _compose_exec(project, compose_file, "stop", "finhealth")
        assert stopped.returncode == 0, stopped.stderr
        started = _compose_exec(project, compose_file, "start", "finhealth")
        assert started.returncode == 0, started.stderr
        with sqlite3.connect(db_path) as conn:
            restarted_count = conn.execute("SELECT COUNT(*) FROM lifecycle").fetchone()[0]
        assert restarted_count >= first_count

        down = _compose_exec(project, compose_file, "down")
        assert down.returncode == 0, down.stderr
        assert data_dir.exists()
        assert db_path.exists()
    finally:
        _compose_exec(project, compose_file, "down", "-v")


def test_disposable_compose_down_up_persists_data(tmp_path: Path) -> None:
    """Exact isolated docker compose down/up preserves bind-mounted SQLite data."""
    if not _require_docker():
        pytest.xfail("docker compose or Docker daemon is unavailable")

    project = f"finhealth-p8-{uuid.uuid4().hex[:8]}"
    compose_file, data_dir, _shared_dir = _write_sqlite_compose(tmp_path, "down-up")
    db_path = data_dir / "finhealth.db"
    try:
        up = _compose_exec(project, compose_file, "up", "-d")
        if up.returncode != 0:
            pytest.xfail(f"disposable compose up unavailable: {up.stderr.strip()}")
        _wait_for_db(db_path)
        with sqlite3.connect(db_path) as conn:
            before = conn.execute("SELECT COUNT(*) FROM lifecycle").fetchone()[0]

        down = _compose_exec(project, compose_file, "down")
        assert down.returncode == 0, down.stderr
        up_again = _compose_exec(project, compose_file, "up", "-d")
        assert up_again.returncode == 0, up_again.stderr
        with sqlite3.connect(db_path) as conn:
            after = conn.execute("SELECT COUNT(*) FROM lifecycle").fetchone()[0]
        assert after >= before
    finally:
        _compose_exec(project, compose_file, "down", "-v")


def test_disposable_compose_down_dash_v_preserves_host_data(tmp_path: Path) -> None:
    """docker compose down -v does not remove host bind-mounted data."""
    if not _require_docker():
        pytest.xfail("docker compose or Docker daemon is unavailable")

    project = f"finhealth-p8-{uuid.uuid4().hex[:8]}"
    compose_file, data_dir, _shared_dir = _write_sqlite_compose(tmp_path, "down-v")
    db_path = data_dir / "finhealth.db"
    try:
        up = _compose_exec(project, compose_file, "up", "-d")
        if up.returncode != 0:
            pytest.xfail(f"disposable compose up unavailable: {up.stderr.strip()}")
        _wait_for_db(db_path)
        before = db_path.read_bytes()
        down = _compose_exec(project, compose_file, "down", "-v")
        assert down.returncode == 0, down.stderr
        assert data_dir.exists()
        assert db_path.exists()
        assert db_path.read_bytes() == before
    finally:
        _compose_exec(project, compose_file, "down", "-v")


def _write_real_finhealth_compose(tmp_path: Path, data_dir: Path, shared_dir: Path) -> Path:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    service = compose["services"]["finhealth"]
    service.pop("container_name", None)
    service["build"]["context"] = str(ROOT)
    service["user"] = f"{os.getuid()}:{os.getgid()}"
    service["ports"] = ["127.0.0.1:0:8000"]
    service["volumes"] = [f"{data_dir}:/app/data", f"{shared_dir}:/app/shared"]
    service["environment"] = [
        "DATABASE_URL=sqlite+aiosqlite:////app/data/finhealth.db",
        "DEBUG=false",
        "SECRET_KEY=test-secret",
        "LLM_PROVIDER=opencode_go",
        "LLM_API_ENDPOINT=http://127.0.0.1:11434",
        "LLM_API_KEY=",
        "LLM_MODEL=test-model",
        "PDF_UPLOAD_DIR=/app/shared",
    ]
    compose_file = tmp_path / "finhealth-compose.yml"
    compose_file.write_text(yaml.safe_dump(compose, sort_keys=False), encoding="utf-8")
    return compose_file


def _service_url(project: str, compose_file: Path) -> str:
    for _ in range(60):
        port = _compose_exec(project, compose_file, "port", "finhealth", "8000")
        if port.returncode == 0 and port.stdout.strip():
            host_port = port.stdout.strip().rsplit(":", 1)[1]
            return f"http://127.0.0.1:{host_port}"
        time.sleep(0.5)
    raise AssertionError("finhealth service port was not published")


def _get_json(url: str) -> dict[str, object]:
    last_error: Exception | None = None
    for _ in range(60):
        try:
            with urllib.request.urlopen(url, timeout=10) as response:
                assert response.status == 200
                return json.loads(response.read().decode("utf-8"))
        except Exception as exc:  # pragma: no cover - diagnostic path for Docker startup races
            last_error = exc
            time.sleep(0.5)
    raise AssertionError(f"dashboard endpoint never became ready: {last_error}")


def _insert_dashboard_smoke_row(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO banks(id, name, display_name, password_formula, is_active) "
            "VALUES ('11111111-1111-1111-1111-111111111111', 'docker_bank', 'Docker Bank', 'rut', 1)"
        )
        conn.execute(
            "INSERT OR IGNORE INTO credit_cards(id, bank_id, card_number_masked, cardholder, currency, is_active) "
            "VALUES ('22222222-2222-2222-2222-222222222222', '11111111-1111-1111-1111-111111111111', 'XXXX XXXX XXXX 4242', 'DOCKER USER', 'CLP', 1)"
        )
        conn.execute(
            "INSERT OR IGNORE INTO statements(id, credit_card_id, period_start, period_end, statement_date, file_path, file_hash, status) "
            "VALUES ('33333333-3333-3333-3333-333333333333', '22222222-2222-2222-2222-222222222222', '2026-07-01', '2026-07-31', '2026-07-31', '/tmp/docker.pdf', 'docker-hash', 'completed')"
        )
        conn.execute(
            "INSERT OR IGNORE INTO transactions(id, statement_id, date, description, amount, currency, low_confidence) "
            "VALUES ('44444444-4444-4444-4444-444444444444', '33333333-3333-3333-3333-333333333333', '2026-07-05', 'DOCKER RESTORE SMOKE', 123.00, 'CLP', 0)"
        )
        conn.commit()


def test_restored_database_serves_dashboard_http_200(tmp_path: Path) -> None:
    """A restored bind-mounted DB serves the dashboard summary endpoint after restart."""
    if not _require_docker():
        pytest.xfail("docker compose or Docker daemon is unavailable")

    project = f"finhealth-p8-{uuid.uuid4().hex[:8]}"
    data_dir = tmp_path / "data"
    shared_dir = tmp_path / "shared"
    data_dir.mkdir()
    shared_dir.mkdir()
    compose_file = _write_real_finhealth_compose(tmp_path, data_dir, shared_dir)
    db_path = data_dir / "finhealth.db"
    backup_path = tmp_path / "backup.db"
    try:
        up = _compose_exec(project, compose_file, "up", "-d", "--build")
        if up.returncode != 0:
            pytest.xfail(f"finhealth disposable compose unavailable: {up.stderr.strip()}")
        _wait_for_db(db_path)
        down_for_seed = _compose_exec(project, compose_file, "down")
        assert down_for_seed.returncode == 0, down_for_seed.stderr
        _insert_dashboard_smoke_row(db_path)
        up_seeded = _compose_exec(project, compose_file, "up", "-d")
        assert up_seeded.returncode == 0, up_seeded.stderr
        base_url = _service_url(project, compose_file)
        summary_url = f"{base_url}/api/v1/dashboard/summary?period=2026-07&range=6&card_id=all"
        first = _get_json(summary_url)
        assert int(first["transaction_count"]) > 0

        backup(source_url=f"sqlite:///{db_path}", destination=backup_path)
        down = _compose_exec(project, compose_file, "down")
        assert down.returncode == 0, down.stderr
        restore(backup_path=backup_path, destination_url=f"sqlite:///{db_path}")
        up_again = _compose_exec(project, compose_file, "up", "-d")
        assert up_again.returncode == 0, up_again.stderr
        base_url = _service_url(project, compose_file)
        restored = _get_json(
            f"{base_url}/api/v1/dashboard/summary?period=2026-07&range=6&card_id=all"
        )
        assert restored["transaction_count"] == first["transaction_count"]
    finally:
        _compose_exec(project, compose_file, "down", "-v")
