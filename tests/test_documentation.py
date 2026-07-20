"""Documentation and Compose assertions for SQLite operations."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_readme_docker_runbook_claims() -> None:
    """README documents verified SQLite backup/restore and bind-mount semantics."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    required = {
        "backup command": "python -m app.cli.sqlite_ops backup",
        "restore command": "python -m app.cli.sqlite_ops restore",
        "down -v clarification": "does **not** delete bind-mounted host directories",
        "shared bind mount": "| `./shared`  | `/app/shared`",
        "data bind mount": "| `./data`    | `/app/data`",
        "health smoke": "curl http://localhost:8000/api/v1/health",
    }

    missing = [name for name, needle in required.items() if needle not in readme]
    assert missing == [], f"README missing required runbook lines: {', '.join(missing)}"


def test_compose_bind_mount_uses_data_directory() -> None:
    """Compose uses host bind mounts for data/shared and the canonical container DB URL."""
    for filename in ("docker-compose.yml", "docker-compose.self-hosted.yml"):
        compose = yaml.safe_load((ROOT / filename).read_text(encoding="utf-8"))
        service = compose["services"]["finhealth"]
        assert "./data:/app/data" in service["volumes"]
        assert "./shared:/app/shared" in service["volumes"]
        assert "DATABASE_URL=sqlite+aiosqlite:////app/data/finhealth.db" in service[
            "environment"
        ]


def test_readme_documents_stopped_container_backup() -> None:
    """README documents the stopped-container copy and exact Compose lifecycle."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    required = {
        "stopped-container copy": "copy_stopped_container_db('data', 'backups/finhealth-stopped.db')",
        "down/up lifecycle": "docker compose down && docker compose up -d",
        "down -v host data": "docker compose down -v does **not** delete bind-mounted host directories",
    }
    missing = [name for name, needle in required.items() if needle not in readme]
    assert missing == [], f"README missing stopped-container runbook lines: {', '.join(missing)}"


def test_canonical_phase3_spec_uses_calendar_day_denominator() -> None:
    """The live canonical Phase 3 spec is synced from the MODIFIED delta."""
    spec = (ROOT / "openspec/specs/phase3-dashboard/spec.md").read_text(encoding="utf-8")
    assert "distinct days" not in spec
    assert "calendar days of the period month" in spec
