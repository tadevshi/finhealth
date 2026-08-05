"""Compose topology contracts for PostgreSQL deployment."""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
BASE_COMPOSE = ROOT / "docker-compose.yml"
SELF_HOSTED_COMPOSE = ROOT / "docker-compose.self-hosted.yml"
POSTGRES_FIELDS = {
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "POSTGRES_HOST",
    "POSTGRES_PORT",
    "POSTGRES_DB",
}


def _compose(path: Path) -> dict[str, object]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _environment(service: dict[str, object]) -> dict[str, str]:
    environment = service["environment"]
    assert isinstance(environment, dict)
    return environment


def test_base_compose_has_the_single_ordered_migration_owner() -> None:
    compose = _compose(BASE_COMPOSE)
    services = compose["services"]
    assert isinstance(services, dict)

    postgres = services["postgres"]
    migrate = services["migrate"]
    finhealth = services["finhealth"]
    assert isinstance(postgres, dict)
    assert isinstance(migrate, dict)
    assert isinstance(finhealth, dict)

    assert str(postgres["image"]).startswith("postgres:16")
    assert postgres["healthcheck"]
    assert migrate["command"] == ["alembic", "upgrade", "head"]
    assert migrate["restart"] == "no"

    migrate_dependencies = migrate["depends_on"]
    finhealth_dependencies = finhealth["depends_on"]
    assert isinstance(migrate_dependencies, dict)
    assert isinstance(finhealth_dependencies, dict)
    assert migrate_dependencies["postgres"]["condition"] == "service_healthy"
    assert finhealth_dependencies["postgres"]["condition"] == "service_healthy"
    assert finhealth_dependencies["migrate"]["condition"] == "service_completed_successfully"


def test_compose_passes_structured_postgres_settings_without_database_url() -> None:
    compose = _compose(BASE_COMPOSE)
    services = compose["services"]
    assert isinstance(services, dict)

    for service_name in ("postgres", "migrate", "finhealth"):
        service = services[service_name]
        assert isinstance(service, dict)
        environment = _environment(service)
        assert environment.keys() >= POSTGRES_FIELDS
        assert "DATABASE_URL" not in environment

    finhealth_volumes = services["finhealth"]["volumes"]
    assert isinstance(finhealth_volumes, list)
    assert "./shared:/app/shared" in finhealth_volumes
    assert all("/app/data" not in volume for volume in finhealth_volumes)


def test_self_hosted_file_is_an_overlay_without_another_migration_owner() -> None:
    base = BASE_COMPOSE.read_text(encoding="utf-8")
    overlay = SELF_HOSTED_COMPOSE.read_text(encoding="utf-8")
    compose = _compose(SELF_HOSTED_COMPOSE)
    services = compose["services"]
    assert isinstance(services, dict)

    assert "migrate:" in base
    assert "migrate:" not in overlay
    assert "alembic upgrade" not in overlay
    assert "ollama" in services


def test_dockerfile_runtime_starts_uvicorn_without_running_alembic() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    command = dockerfile.rsplit("CMD ", maxsplit=1)[1]

    assert '"uvicorn", "app.main:app"' in command
    assert "alembic upgrade" not in command
