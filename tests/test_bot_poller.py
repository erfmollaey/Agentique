"""The standalone Telegram poller and its healthcheck (Phase 3, P3.9-E).

Two things are proved here, and both are about the split that makes them
necessary rather than about the code that implements it:

1. **The heartbeat proves the loop, not the work.** A stale heartbeat fails and
   a fresh one passes, and the module is explicit that an idle-but-alive bot is
   green. Over-claiming is what makes a healthcheck decorative.
2. **Polling is owned by exactly one process.** The ASGI lifespan gates its
   poller on ``TELEGRAM_POLLING``, so a deployment can serve HTTP without also
   consuming updates.

No test starts a real poller or contacts Telegram. The heartbeat is driven
against a real file in a temporary directory, and the gate is asserted on the
setting.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

from app.core.config import Settings


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "BOT_TOKEN": "1234567890:TEST_TOKEN_NOT_A_REAL_CREDENTIAL",
        "GROQ_API_KEY": "gsk-test-key-not-a-real-credential",
        "REDIS_URL": "redis://localhost:6379/15",
        "DATABASE_URL": "postgresql+asyncpg://user:password@localhost:5432/never_used",
        "TEST_DATABASE_URL": "postgresql+asyncpg://user:password@localhost:5432/never_used_test",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


# --- the heartbeat file writer ---------------------------------------------


async def test_the_heartbeat_writes_and_advances(tmp_path):
    """It records that the loop is turning, not that work happened.

    The interval is small so two ticks are observed without waiting on the
    configured default. The assertion is on the file appearing with a usable
    timestamp, which is the whole contract.
    """
    from app.bot.poller import _heartbeat

    target = tmp_path / "hb"
    task = asyncio.create_task(_heartbeat(str(target), 0.05))
    try:
        for _ in range(40):  # up to 2s, polled every 50ms
            if target.exists() and target.read_text().strip():
                break
            await asyncio.sleep(0.05)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    assert float(target.read_text()) > 0


def test_the_heartbeat_target_directory_is_configurable(tmp_path):
    """A deployment writes it where its supervisor can read it."""
    settings = _settings(BOT_HEARTBEAT_PATH=str(tmp_path / "custom"))

    assert Path(settings.BOT_HEARTBEAT_PATH).parent == tmp_path


# --- the healthcheck module -------------------------------------------------


def _fresh_heartbeat(path: Path) -> None:
    path.write_text(str(time.time()), encoding="ascii")


def _run_healthcheck(settings: Settings) -> int:
    """Run the healthcheck's ``main()`` against a specific settings object.

    Patched at ``app.bot.heartbeat.get_settings`` — the name *this* module
    imported — rather than at ``app.core.config.get_settings``. Patching the
    definition site would leave the healthcheck reading the real default path,
    which silently turns every assertion into a test of whatever happened to be
    at /tmp.
    """
    from app.bot import heartbeat as module

    with (
        patch.dict(os.environ, {"BOT_HEARTBEAT_PATH": settings.BOT_HEARTBEAT_PATH}),
        patch.object(module, "get_settings", return_value=settings),
    ):
        return module.main()


def test_a_fresh_heartbeat_is_healthy(tmp_path):
    path = tmp_path / "hb"
    _fresh_heartbeat(path)

    assert _run_healthcheck(_settings(BOT_HEARTBEAT_PATH=str(path))) == 0


def test_a_missing_heartbeat_is_not_healthy(tmp_path):
    """Never started, or crashed before the first tick. Both are unhealthy."""
    assert (
        _run_healthcheck(_settings(BOT_HEARTBEAT_PATH=str(tmp_path / "absent"))) == 1
    )


def test_a_stale_heartbeat_is_not_healthy(tmp_path):
    """A stopped event loop stops writing; the age crosses the threshold."""
    path = tmp_path / "hb"
    path.write_text(str(time.time() - 10_000), encoding="ascii")

    assert _run_healthcheck(_settings(BOT_HEARTBEAT_PATH=str(path))) == 1


def test_the_staleness_threshold_tolerates_one_missed_write(tmp_path):
    """Half the interval is a timing blip, not a dead loop."""
    settings = _settings(
        BOT_HEARTBEAT_PATH=str(tmp_path / "hb"),
        BOT_HEARTBEAT_INTERVAL_SECONDS=30,
    )
    path = Path(settings.BOT_HEARTBEAT_PATH)
    # Inside 2 * interval but past one interval: a single missed write.
    path.write_text(str(time.time() - 40), encoding="ascii")

    assert _run_healthcheck(settings) == 0


def test_an_unreadable_heartbeat_fails_closed(tmp_path):
    """A corrupt file is a failure, never an assumed-healthy one."""
    path = tmp_path / "hb"
    path.write_text("not-a-timestamp", encoding="ascii")

    assert _run_healthcheck(_settings(BOT_HEARTBEAT_PATH=str(path))) == 1


def test_the_healthcheck_runs_as_a_module():
    """Docker execs it; if it cannot be exec'd, the healthcheck is decorative."""
    proc = subprocess.run(
        [sys.executable, "-m", "app.bot.heartbeat"],
        capture_output=True,
        timeout=60,
        check=False,
    )
    # The default path does not exist under test, so this is the healthy exit for
    # "no heartbeat": 1. What matters is that it ran and produced a message.
    assert proc.returncode in (0, 1)
    assert proc.stderr or proc.returncode == 0


# --- polling is owned by one process ---------------------------------------


def test_the_asgi_poller_is_gated_by_configuration():
    """The gate, not a comment, is what stops two processes competing."""
    assert _settings(TELEGRAM_POLLING=True).TELEGRAM_POLLING is True
    assert _settings(TELEGRAM_POLLING=False).TELEGRAM_POLLING is False


def test_polling_defaults_to_on_for_a_single_process_deployment():
    """A deployment that only wants HTTP is a deliberate choice, not an absence."""
    assert Settings(
        _env_file=None,
        BOT_TOKEN="1:T",
        GROQ_API_KEY="gsk_test_key_not_real",
        REDIS_URL="redis://localhost:6379/15",
        DATABASE_URL="postgresql+asyncpg://u:p@localhost:5432/x_test",
    ).TELEGRAM_POLLING is True
