"""Application configuration loaded from environment variables and `.env`."""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Strongly-typed application settings.

    Values are read first from environment variables, then from a local
    ``.env`` file if present. All fields are fully type-annotated so the
    configuration surface is introspectable and ``mypy --strict`` clean.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Application ----------------------------------------------------------------
    APP_NAME: str = Field(
        default="finhealth",
        description="Human-readable name of the application.",
    )
    DEBUG: bool = Field(
        default=False,
        description="Enable debug mode (verbose logs, autoreload, etc.).",
    )

    # Security -------------------------------------------------------------------
    SECRET_KEY: str = Field(
        default="change-me-in-production",
        description="Secret used for signing sessions, tokens, etc.",
    )

    # Database -------------------------------------------------------------------
    DATABASE_URL: str = Field(
        default="sqlite+aiosqlite:///./finhealth.db",
        description="Async SQLAlchemy database URL.",
    )

    # CORS -----------------------------------------------------------------------
    CORS_ORIGINS: list[str] = Field(
        default_factory=lambda: ["http://localhost:8000"],
        description="Allowed origins for CORS requests (JSON list in env).",
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached :class:`Settings` instance.

    The cache keeps a single parsed settings object per process so we don't
    re-read ``.env`` on every request. Tests can clear the cache via
    ``get_settings.cache_clear()`` to pick up new environment values.
    """
    return Settings()
