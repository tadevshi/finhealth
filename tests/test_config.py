"""Smoke tests for :mod:`app.core.config`."""

from collections.abc import Iterator

import pytest
from pydantic import ValidationError

from app.core.config import Settings, get_settings


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> Iterator[None]:
    """Ensure each test sees a fresh ``get_settings()`` instance."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_settings_defaults() -> None:
    """Defaults match the documented values when no env is set."""
    # _env_file=None forces Settings to read only from the process env,
    # not from a local .env file (which may override defaults in dev).
    settings = Settings(_env_file=None)

    assert settings.APP_NAME == "finhealth"
    assert settings.DEBUG is False
    assert settings.SECRET_KEY == "change-me-in-production"
    assert settings.DATABASE_URL == "sqlite+aiosqlite:///./finhealth.db"
    assert settings.CORS_ORIGINS == ["http://localhost:8000"]
    # LLM defaults: OpenCode Zen with a free model — no API key required to start.
    assert settings.LLM_PROVIDER == "opencode_zen"
    assert settings.LLM_API_ENDPOINT == "https://opencode.ai/zen/v1"
    assert settings.LLM_MODEL == "deepseek-v4-flash-free"


def test_settings_env_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Environment variables override defaults."""
    monkeypatch.setenv("APP_NAME", "finhealth-test")
    monkeypatch.setenv("DEBUG", "true")
    monkeypatch.setenv("SECRET_KEY", "super-secret")
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///./custom-test.db")
    monkeypatch.setenv(
        "CORS_ORIGINS", '["https://example.com","https://api.example.com"]'
    )
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("LLM_API_ENDPOINT", "http://test-llm:11434")

    settings = Settings(_env_file=None)

    assert settings.APP_NAME == "finhealth-test"
    assert settings.DEBUG is True
    assert settings.SECRET_KEY == "super-secret"
    assert settings.DATABASE_URL == "sqlite+aiosqlite:///./custom-test.db"
    assert settings.CORS_ORIGINS == [
        "https://example.com",
        "https://api.example.com",
    ]
    assert settings.LLM_PROVIDER == "ollama"
    assert settings.LLM_API_ENDPOINT == "http://test-llm:11434"


def test_get_settings_is_cached() -> None:
    """``get_settings`` returns the same object across calls (lru_cache)."""
    first = get_settings()
    second = get_settings()

    assert first is second


def test_get_settings_cache_clear_returns_fresh_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Clearing the cache picks up new environment values."""
    first = get_settings()
    assert first.APP_NAME == "finhealth"

    monkeypatch.setenv("APP_NAME", "after-clear")
    get_settings.cache_clear()

    second = get_settings()
    assert second.APP_NAME == "after-clear"
    assert second is not first


def test_settings_extra_fields_are_ignored() -> None:
    """Unknown env vars do not raise (extra='ignore')."""
    import os

    os.environ["FINHEALTH_UNKNOWN_FIELD"] = "irrelevant"
    try:
        settings = Settings()  # must not raise
        assert settings.APP_NAME == "finhealth"
    finally:
        del os.environ["FINHEALTH_UNKNOWN_FIELD"]


def test_settings_rejects_invalid_debug_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-boolean ``DEBUG`` values fail validation with a clear error."""
    monkeypatch.setenv("DEBUG", "not-a-bool")

    with pytest.raises(ValidationError):
        Settings()
