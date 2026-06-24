"""Application configuration loaded from environment variables and `.env`."""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Strongly-typed application settings.

    Values are read first from environment variables, then from a local
    ``.env`` file if present. All fields are fully type-annotated so the
    configuration surface is introspectable and ``mypy --strict`` clean.

    New in Phase 1
    --------------
    The ``LLM_*`` block configures the LLM extraction provider used to
    turn decrypted PDF text into structured transactions. The
    ``PDF_*`` block governs the upload pipeline. Both blocks are
    documented inline; see ``.env.example`` for the full list of
    overridable keys.
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

    # LLM provider --------------------------------------------------------------
    # ``LLM_PROVIDER`` selects the concrete client implementation
    # (``opencode_go``, ``ollama``, ``opencode_zen``). The provider is
    # intentionally a string, not an enum, so adding a new one is a
    # matter of code — not a schema migration. ``LLM_API_ENDPOINT`` is
    # the base URL the provider's client talks to (an Ollama daemon
    # by default). ``LLM_MODEL`` is the model name passed to the
    # provider. ``LLM_TIMEOUT`` bounds a single extraction call;
    # ``LLM_MAX_RETRIES`` is the number of automatic retries on
    # transient errors (network blips, 5xx responses, timeouts).
    LLM_PROVIDER: str = Field(
        default="opencode_go",
        description=("LLM provider identifier (e.g. 'opencode_go', 'ollama', 'opencode_zen')."),
    )
    LLM_API_ENDPOINT: str = Field(
        ...,
        description="Base URL for the LLM provider's HTTP API. Required - no default.",
    )
    LLM_API_KEY: str = Field(
        default="",
        description="API key for LLM provider authentication (optional for local providers like Ollama).",
    )
    LLM_MODEL: str = Field(
        default="qwen2.5:1.5b",
        description="Model name sent to the LLM provider.",
    )
    LLM_TIMEOUT: int = Field(
        default=60,
        ge=1,
        description="Timeout in seconds for a single LLM extraction call.",
    )
    LLM_MAX_RETRIES: int = Field(
        default=3,
        ge=0,
        description="Number of automatic retries on transient LLM errors.",
    )

    # PDF ingestion --------------------------------------------------------------
    # ``PDF_UPLOAD_DIR`` is resolved relative to the project root when
    # it is not absolute. ``MAX_FILE_SIZE_MB`` caps the upload size —
    # exceeding it returns ``413 Payload Too Large`` from the route.
    PDF_UPLOAD_DIR: str = Field(
        default="shared",
        description="Directory where uploaded PDFs are stored (absolute or relative).",
    )
    MAX_FILE_SIZE_MB: int = Field(
        default=10,
        ge=1,
        description="Maximum upload size in megabytes.",
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached :class:`Settings` instance.

    The cache keeps a single parsed settings object per process so we don't
    re-read ``.env`` on every request. Tests can clear the cache via
    ``get_settings.cache_clear()`` to pick up new environment values.
    """
    return Settings()
