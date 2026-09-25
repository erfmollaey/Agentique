"""Application configuration.

Single source of truth for every environment-dependent value. Business logic
must read configuration from here and must not call ``os.getenv`` directly.

Audit references: M-6 (CWD-dependent config, import-time crash), C-1 (hardcoded
secret), H-3 (no timeouts/limits), P1-13 (robust configuration).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# app/core/config.py -> app/core -> app -> <project root>
PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = PROJECT_ROOT / ".env"


class ConfigurationError(RuntimeError):
    """Raised when required configuration is missing or unusable.

    Exists so a misconfigured deployment fails with a readable message rather
    than a raw pydantic traceback at import time (FR-1.6).
    """


class Settings(BaseSettings):
    """Every environment-dependent value the application uses.

    Secrets use ``SecretStr`` so they cannot be emitted by an accidental
    ``str()``, ``repr()``, or f-string in a log record (SR-5).
    """

    model_config = SettingsConfigDict(
        # Absolute path: settings must resolve identically regardless of the
        # directory the process was launched from (FR-1.7, M-6).
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=True,
    )

    # --- Telegram (C-1, P0-1, P0-2) -----------------------------------------
    BOT_TOKEN: SecretStr

    # --- AI provider: OpenAI-compatible endpoint ---------------------------
    GROQ_API_KEY: SecretStr
    LLM_BASE_URL: str = "https://api.deepseek.com/v1"
    LLM_MODEL: str = "deepseek-chat"

    # --- AI provider reliability (H-3, P0-11, P0-12) -----------------------
    LLM_TIMEOUT_SECONDS: float = Field(default=60.0, gt=0)
    LLM_MAX_TOKENS: int = Field(default=1024, gt=0)
    LLM_TEMPERATURE: float = Field(default=0.3, ge=0.0, le=2.0)
    LLM_MAX_RETRIES: int = Field(default=2, ge=0, le=10)

    # --- Celery (H-3, M-9, P0-13, P1-9) ------------------------------------
    REDIS_URL: str
    CELERY_TASK_TIME_LIMIT: int = Field(default=120, gt=0)
    CELERY_TASK_SOFT_TIME_LIMIT: int = Field(default=100, gt=0)
    CELERY_TASK_MAX_RETRIES: int = Field(default=2, ge=0, le=10)
    CELERY_TASK_RETRY_BACKOFF: int = Field(default=5, ge=0)
    CELERY_WORKER_PREFETCH_MULTIPLIER: int = Field(default=1, ge=1)

    # --- Cost control (M-8, P1-8) -----------------------------------------
    RATE_LIMIT_MAX_REQUESTS: int = Field(default=5, gt=0)
    RATE_LIMIT_WINDOW_SECONDS: int = Field(default=60, gt=0)
    MAX_IN_FLIGHT_REQUESTS: int = Field(default=8, gt=0)

    # --- Telegram output (C-4, P0-8, P0-9) --------------------------------
    TELEGRAM_PARSE_MODE: str = "HTML"
    TELEGRAM_MAX_MESSAGE_LENGTH: int = Field(default=4096, gt=0)

    # --- Observability (H-5, P1-3) ----------------------------------------
    HEALTH_PROBE_TIMEOUT_SECONDS: float = Field(default=2.0, gt=0)
    LOG_LEVEL: str = "INFO"

    # --- Declared, not yet used -------------------------------------------
    # PostgreSQL is provisioned in docker-compose but no database code exists.
    # Optional so its absence cannot crash the process (M-6). Wired in Phase 2.
    DATABASE_URL: str | None = None

    # --- Deployment mode ----------------------------------------------------
    ENVIRONMENT: str = "development"

    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT.lower() in {"production", "prod"}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Build settings once per process.

    A missing or malformed variable raises :class:`ConfigurationError` naming
    the offending field, instead of an import-time traceback (FR-1.6).
    """
    try:
        # Required values are supplied by the environment or the env file,
        # not by keyword arguments, so the constructor call takes none.
        return Settings()  # type: ignore[call-arg]
    except Exception as exc:  # pydantic ValidationError and friends
        fields = sorted(getattr(exc, "fields", None) or _field_names_from(exc))
        detail = ", ".join(fields) if fields else str(exc)
        raise ConfigurationError(
            f"Invalid configuration. Checked {ENV_FILE} and the environment. "
            f"Problem field(s): {detail}. "
            "See .env.example for the required variables."
        ) from None


def _field_names_from(exc: Exception) -> list[str]:
    """Best-effort extraction of offending field names from a pydantic error."""
    text = str(exc)
    names: list[str] = []
    for chunk in text.split("; "):
        head = chunk.strip()
        if head and head[0].isalpha() and head.replace("_", "").isalnum():
            names.append(head.split(" ")[0])
    return names
