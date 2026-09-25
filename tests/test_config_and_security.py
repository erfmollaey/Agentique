"""T-10 — Configuration robustness; plus security and dependency invariants.

Audit references: M-6 (CWD-dependent config, import-time crash), C-1 (hardcoded
secret), C-2 (undeclared dependency), FR-1.6, FR-1.7, SR-1, SR-2, SR-3, T-13.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from app.core.config import ENV_FILE, PROJECT_ROOT, ConfigurationError, Settings

ROOT = Path(__file__).resolve().parents[1]

# Matches a Telegram bot token or a provider key shape.
SECRET_PATTERN = re.compile(
    r"sk-[A-Za-z0-9_-]{16,}"          # provider keys (sk-..., sk-or-v1-...)
    r"|gsk-[A-Za-z0-9_-]{16,}"        # Groq provider keys
    r"|\b\d{8,10}:[A-Za-z0-9_-]{30,}\b"  # Telegram bot tokens
)


def _python_files() -> list[Path]:
    return sorted(p for p in (ROOT / "app").rglob("*.py"))


# --- SR-1: no hardcoded secrets in source ----------------------------------

def test_sr1_no_secret_literal_in_application_source():
    offenders: list[str] = []
    for path in _python_files():
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            if SECRET_PATTERN.search(line):
                offenders.append(f"{path.relative_to(ROOT)}:{lineno}")
    assert not offenders, f"secret-shaped literal(s) in source: {offenders}"


def test_sr1_settings_reads_credentials_from_the_environment(settings):
    assert settings.BOT_TOKEN.get_secret_value()
    assert settings.GROQ_API_KEY.get_secret_value()
    # SecretStr must not leak through repr/str (defence in depth, SR-5).
    assert "TEST_TOKEN" not in repr(settings.BOT_TOKEN)
    assert "test-key" not in repr(settings.GROQ_API_KEY)


# --- SR-2 / SR-3: gitignore and env example --------------------------------

def test_sr2_gitignore_exists_and_excludes_env_and_venv():
    gitignore = ROOT / ".gitignore"
    assert gitignore.exists(), ".gitignore is missing; .env is unprotected"
    rules = gitignore.read_text()
    for required in (".env", "venv/", "__pycache__/"):
        assert required in rules, f".gitignore does not exclude {required}"


def test_sr2_gitignore_keeps_env_example():
    rules = (ROOT / ".gitignore").read_text()
    assert "!.env.example" in rules, ".env.example would be ignored and could not be committed"


def test_sr3_env_example_exists_and_contains_no_real_values():
    example = ROOT / ".env.example"
    assert example.exists(), ".env.example is missing"
    content = example.read_text()

    assert not SECRET_PATTERN.search(content), ".env.example contains a secret-shaped value"

    for var in ("BOT_TOKEN", "GROQ_API_KEY", "REDIS_URL", "DATABASE_URL"):
        assert var in content, f".env.example does not document {var}"

    # Every assignment must be empty, an obvious placeholder, or a known-safe
    # public value that carries no credential (local service URLs, the public
    # provider endpoint, a public model name).
    safe_prefixes = (
        "redis://localhost",
        "postgresql+asyncpg://USER:PASSWORD@",
        "https://api.groq.com/openai/v1",
        "llama-3.3-70b-versatile",
    )
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        value = line.split("=", 1)[1].strip()
        assert (
            value == "" or value.startswith(safe_prefixes)
        ), f".env.example line looks like it carries a real value: {line!r}"


def test_env_is_not_committed_and_env_example_is_not_gitignored():
    """Sanity check that the two files are distinguished correctly."""
    rules = (ROOT / ".gitignore").read_text().splitlines()
    assert ".env" in rules
    assert "!.env.example" in rules


# --- T-10 / FR-1.7: CWD independence --------------------------------------

def test_t10_settings_resolve_regardless_of_working_directory(tmp_path):
    """FR-1.7: an absolute env_file makes configuration CWD-independent."""
    assert ENV_FILE.is_absolute()
    assert ENV_FILE == PROJECT_ROOT / ".env"

    env = {
        "BOT_TOKEN": "999:CWDINDEPENDENCETESTTOKEN",
        "GROQ_API_KEY": "gsk-cwd-independent-test",
        "REDIS_URL": "redis://localhost:6379/15",
    }
    code = (
        "import app.core.config as c;"
        "s = c.get_settings();"
        "print(s.BOT_TOKEN.get_secret_value())"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(ROOT), **env},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "CWDINDEPENDENCETESTTOKEN" in result.stdout


# --- T-10 / FR-1.6: readable failure on missing configuration --------------

def test_t10_missing_required_variable_is_a_validation_error():
    """FR-1.6: with no env file and no kwargs, the required field is enforced.

    ``_env_file=None`` bypasses the real ``.env`` so the missing-value path is
    what actually gets exercised.
    """
    from pydantic import ValidationError

    with pytest.raises(ValidationError) as info:
        Settings(_env_file=None)
    assert "BOT_TOKEN" in str(info.value)


def test_t10_configuration_error_names_the_offending_field(monkeypatch):
    """``get_settings`` must surface a readable, actionable error (FR-1.6)."""
    from app.core.config import get_settings as cached_get

    cached_get.cache_clear()

    def boom(*_args, **_kwargs):
        raise ValueError("BOT_TOKEN field required")

    monkeypatch.setattr("app.core.config.Settings", boom)

    with pytest.raises(ConfigurationError) as info:
        cached_get()

    message = str(info.value)
    assert "BOT_TOKEN" in message, "the failing field is not named"
    assert ".env.example" in message, "error does not point the operator at a fix"
    assert "Traceback" not in message
    cached_get.cache_clear()


def test_t10_settings_expose_llm_and_reliability_configuration(settings):
    assert settings.LLM_TIMEOUT_SECONDS > 0
    assert settings.LLM_MAX_TOKENS > 0
    assert settings.CELERY_TASK_TIME_LIMIT > settings.CELERY_TASK_SOFT_TIME_LIMIT
    assert settings.TELEGRAM_MAX_MESSAGE_LENGTH <= 4096


def test_t10_database_url_is_optional():
    """DATABASE_URL has no consumer in Phase 1, so it cannot block startup."""
    s = Settings(
        _env_file=None,
        BOT_TOKEN="1:TEST",
        GROQ_API_KEY="gsk-test",
        REDIS_URL="redis://localhost:6379/15",
    )
    assert s.DATABASE_URL is None, "an unused variable is still mandatory"
