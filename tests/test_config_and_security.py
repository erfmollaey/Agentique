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
#
# The Groq alternative is ``gsk_`` with an UNDERSCORE, which is what Groq issues.
# It was previously written ``gsk-`` with a hyphen, which matched nothing: a real
# Groq key slipped through this scanner and the CI secret scan unnoticed. A
# scanner that cannot fail is not a control, so
# ``test_the_secret_scanner_matches_realistic_key_shapes`` now pins the shapes
# this pattern must catch.
SECRET_PATTERN = re.compile(
    r"sk-[A-Za-z0-9_-]{16,}"              # OpenAI-style (sk-..., sk-or-v1-...)
    r"|gsk_[A-Za-z0-9_-]{16,}"            # Groq (gsk_...)
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
        "qwen/qwen3.8-27b",
        # A documented enum value, not a credential.
        "openai_compatible",
        # An explicit placeholder, not a chosen password; and a database name.
        "USER",
        "PASSWORD",
        "research_db",
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

def test_t10_missing_required_variable_is_a_validation_error(monkeypatch):
    """FR-1.6: with no env file and no kwargs, the required field is enforced.

    ``_env_file=None`` bypasses the real ``.env`` so the missing-value path is
    what actually gets exercised. The process environment must also be cleared,
    otherwise a globally exported ``DATABASE_URL`` (as CI sets for the
    integration steps) silently satisfies the required fields and no
    ``ValidationError`` is raised.
    """
    from pydantic import ValidationError

    for name in ("BOT_TOKEN", "GROQ_API_KEY", "REDIS_URL", "DATABASE_URL", "TEST_DATABASE_URL"):
        monkeypatch.delenv(name, raising=False)

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


def test_t10_database_url_is_required_and_optional_settings_stay_optional(monkeypatch):
    """Phase 2 inverted the Phase 1 rule for DATABASE_URL.

    It was optional in Phase 1 precisely because nothing consumed it. Phase 2
    wires it into a real engine, so a missing value must stop the process at
    startup rather than surface as a failure on the first user message. The
    neighbouring assertions guard the opposite direction: genuinely optional
    settings must stay optional, or the process cannot start on a minimal
    environment.
    """
    from pydantic import ValidationError

    # A globally exported DATABASE_URL (CI sets one for the integration steps)
    # would otherwise satisfy the field and mask the required-field check.
    monkeypatch.delenv("DATABASE_URL", raising=False)
    # CI exports TEST_DATABASE_URL globally; the "stays optional" assertion
    # below requires it to be unset in this test.
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)

    with pytest.raises(ValidationError) as info:
        Settings(
            _env_file=None,
            BOT_TOKEN="1:TEST",
            GROQ_API_KEY="gsk-test",
            REDIS_URL="redis://localhost:6379/15",
        )
    assert "DATABASE_URL" in str(info.value)

    s = Settings(
        _env_file=None,
        BOT_TOKEN="1:TEST",
        GROQ_API_KEY="gsk-test",
        REDIS_URL="redis://localhost:6379/15",
        DATABASE_URL="postgresql+asyncpg://u:p@localhost:5432/chat_test",
    )
    assert s.DATABASE_URL.endswith("chat_test")
    assert s.TEST_DATABASE_URL is None, "an unused variable is still mandatory"


# --- The scanner itself must be able to fail --------------------------------
#
# Found during the Phase 2 completion pass: every secret scanner in the
# repository used ``gsk-`` with a HYPHEN, while Groq issues ``gsk_`` with an
# UNDERSCORE. The pattern therefore matched no real Groq key, and this test file
# passed while a realistic key sat in `app/`. A control that cannot fail is not a
# control, so the shapes are pinned here.

# Realistic shapes, with the secret part obviously synthetic.
_GROQ_KEY = "gsk_" + "A1b2C3d4E5f6G7h8I9j0KlMnOpQrStUvWxYz0123456789Ab"
_OPENAI_KEY = "sk-" + "A1b2C3d4E5f6G7h8I9j0KlMnOpQrStUvWx"
_OPENROUTER_KEY = "sk-or-v1-" + "A1b2C3d4E5f6G7h8I9j0KlMnOpQrStUvWx"
_TELEGRAM_TOKEN = "1234567890:" + "A1b2C3d4E5f6G7h8I9j0KlMnOpQrStUvWxY"


@pytest.mark.parametrize(
    "secret",
    [_GROQ_KEY, _OPENAI_KEY, _OPENROUTER_KEY, _TELEGRAM_TOKEN],
    ids=["groq", "openai", "openrouter", "telegram"],
)
def test_the_secret_scanner_matches_realistic_key_shapes(secret):
    assert SECRET_PATTERN.search(secret), (
        "the scanner does not match this credential family; a scanner that "
        "cannot fail is not a control"
    )


@pytest.mark.parametrize(
    "benign",
    [
        "SKETCH=1",
        "task_id=1aa2660ba631cb43e9975a5361942b70",
        "postgres://user:password@localhost:5432/db",
        "https://api.groq.com/openai/v1",
        "gsk_",
        "sk-",
    ],
)
def test_the_secret_scanner_does_not_match_benign_text(benign):
    """A scanner that cries wolf gets switched off, which is worse."""
    assert not SECRET_PATTERN.search(benign), f"false positive on {benign!r}"


def test_the_repository_wide_scan_also_matches_a_groq_key():
    """The structural test's own pattern must have the same coverage."""
    import ast

    source = (ROOT / "tests" / "test_dependencies_and_structure.py").read_text()
    tree = ast.parse(source)
    patterns = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        and "gsk" in node.value
    ]
    assert patterns, "the gsk_ pattern is missing from the repository scan"
    for pattern in patterns:
        compiled = re.compile(pattern)
        assert compiled.search(_GROQ_KEY), (
            f"the repository scan cannot match a Groq key: {pattern!r}"
        )


# --- Logging configuration must not destroy other listeners (P2-9) ---------

def test_configure_logging_preserves_handlers_other_components_attached():
    """Found during Phase 2 completion.

    ``configure_logging`` used to call ``root.handlers.clear()``, which silently
    removed any handler something else had attached — including a test
    framework's capture handler. Records still reached the console, so nothing
    looked broken, but every other listener went deaf. A Phase 2 security
    assertion (SR-5) then passed or failed depending on module import order.
    """
    import logging

    from app.core.logging_config import _RedactingFilter  # noqa: F401  documents intent

    root = logging.getLogger()
    sentinel = logging.NullHandler()
    root.addHandler(sentinel)
    try:
        import app.core.logging_config as logging_config

        logging_config._CONFIGURED = False
        logging_config.configure_logging()
        assert sentinel in root.handlers, (
            "configure_logging removed a handler it did not own; other "
            "components' log listeners are silently destroyed"
        )
    finally:
        root.removeHandler(sentinel)
        import app.core.logging_config as logging_config

        # Leave the process in the configured state for later tests.
        logging_config._CONFIGURED = True


def test_configure_logging_is_still_idempotent():
    """FR-9.1 is provided by the guard, not by clearing handlers."""
    import logging

    import app.core.logging_config as logging_config

    before = list(logging.getLogger().handlers)
    logging_config.configure_logging()
    logging_config.configure_logging()
    assert logging.getLogger().handlers == before, (
        "repeated calls changed the handler set; the app must not duplicate output"
    )
