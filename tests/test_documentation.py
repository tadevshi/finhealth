"""Operator documentation contracts for PostgreSQL-only deployment."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SELF_HOSTED_COMPOSE = "docker compose -f docker-compose.yml -f docker-compose.self-hosted.yml"


def test_environment_template_uses_only_structured_postgres_settings() -> None:
    environment = (ROOT / ".env.example").read_text(encoding="utf-8")

    for field in (
        "POSTGRES_USER=",
        "POSTGRES_PASSWORD=",
        "POSTGRES_HOST=",
        "POSTGRES_PORT=",
        "POSTGRES_DB=",
    ):
        assert field in environment
    assert "DATABASE_URL" not in environment
    assert "sqlite" not in environment.lower()


def test_readme_has_a_postgresql_compose_lifecycle_runbook() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    for command in (
        "docker compose up -d",
        "docker compose run --rm migrate",
        "docker compose ps",
        "docker compose restart finhealth",
        "pg_dump",
        "pg_restore",
        "docker compose down -v",
    ):
        assert command in readme
    assert "PostgreSQL" in readme
    assert "sqlite" not in readme.lower()


def test_self_hosted_commands_and_database_tools_are_compose_native() -> None:
    environment = (ROOT / ".env.example").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    pull_script = (ROOT / "scripts/pull-ollama-model.sh").read_text(encoding="utf-8")

    for document in (environment, readme, pull_script):
        assert "docker compose -f docker-compose.self-hosted.yml" not in document

    assert f"{SELF_HOSTED_COMPOSE} up -d" in environment
    assert f"{SELF_HOSTED_COMPOSE} exec ollama ollama pull" in pull_script
    assert 'sh -c \'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc\'' in readme
    assert 'sh -c \'dropdb -U "$POSTGRES_USER" "$POSTGRES_DB"\'' in readme
    assert 'sh -c \'createdb -U "$POSTGRES_USER" "$POSTGRES_DB"\'' in readme
    assert (
        'sh -c \'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists\'' in readme
    )
    assert "docker start finhealth" in readme
    assert "docker volume rm finhealth_postgres_data" in readme


def test_readme_assigns_migrations_to_the_compose_owner() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "one-shot `migrate` service" in readme
    assert "Migrations are owned by the one-shot Compose service." in readme
    assert "docker compose run --rm migrate" in readme
