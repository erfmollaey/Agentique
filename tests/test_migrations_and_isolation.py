"""Migrations and test-database isolation (Phase 2 § 6, § 23, § 30; T-18).

Two concerns, both of which are correctness rather than convenience:

* **Migrations are reproducible.** A fresh database must reach the schema by
  running the migrations, and a rollback must leave nothing behind. Nothing here
  calls ``create_all``, so the tests exercise the same DDL a deployment runs.
* **The suite cannot touch real data.** The guard is asserted directly, because a
  guard that is never tested is a guard that does not exist.
"""

from __future__ import annotations

import pytest
from sqlalchemy import inspect, text

from app.db.session import (
    TEST_DB_SUFFIX,
    DatabaseMisconfiguredError,
    resolve_database_url,
)

# --- Isolation guard (Phase 2 § 23) ---------------------------------------

def test_the_guard_accepts_a_database_named_for_testing():
    url = "postgresql+asyncpg://u:p@localhost:5432/research_db_test"
    assert resolve_database_url(url, require_test=True) == url


def test_the_guard_rejects_the_development_database():
    """The single most important assertion in this file."""
    with pytest.raises(DatabaseMisconfiguredError, match="_test"):
        resolve_database_url(
            "postgresql+asyncpg://u:p@localhost:5432/research_db", require_test=True
        )


def test_the_guard_rejects_a_production_style_name():
    with pytest.raises(DatabaseMisconfiguredError):
        resolve_database_url("postgresql+asyncpg://u:p@db/production", require_test=True)


@pytest.mark.parametrize("url", [None, "", "not-a-url", "research_db_test"])
def test_a_missing_or_malformed_url_is_refused(url):
    with pytest.raises(DatabaseMisconfiguredError):
        resolve_database_url(url, require_test=True)


def test_the_suffix_constant_is_what_the_guard_checks():
    assert TEST_DB_SUFFIX == "_test"


def test_the_suite_runs_against_the_test_database(database_url):
    assert database_url.rsplit("/", 1)[-1].endswith(TEST_DB_SUFFIX)
    assert "research_db_test" in database_url


def test_the_migration_environment_honours_a_test_override(monkeypatch):
    """CI and the suite target their own database through the override."""
    monkeypatch.setenv("ALEMBIC_REQUIRE_TEST_DB", "1")
    monkeypatch.setenv(
        "ALEMBIC_DATABASE_URL",
        "postgresql+asyncpg://u:p@localhost:5432/scratch_test",
    )
    assert _migration_url().endswith("scratch_test")


def test_the_migration_environment_refuses_a_real_database_in_test_mode(monkeypatch):
    """A misspelled test database must not migrate real data.

    The check is an explicit switch, not a guess from the URL: inferring it would
    mean a name that fails to look like a test database skips the check entirely.
    """
    monkeypatch.setenv("ALEMBIC_REQUIRE_TEST_DB", "1")
    monkeypatch.setenv(
        "ALEMBIC_DATABASE_URL", "postgresql+asyncpg://u:p@localhost:5432/research_db"
    )
    with pytest.raises(DatabaseMisconfiguredError, match="_test"):
        _migration_url()


def test_an_operator_may_still_migrate_a_real_database(monkeypatch):
    """Without the switch, an explicit override is honoured as written."""
    monkeypatch.delenv("ALEMBIC_REQUIRE_TEST_DB", raising=False)
    monkeypatch.setenv(
        "ALEMBIC_DATABASE_URL", "postgresql+asyncpg://u:p@localhost:5432/research_db"
    )
    assert _migration_url().endswith("research_db")


# --- T-18: migrations apply and roll back ---------------------------------

def test_the_test_database_is_at_the_migration_head(database):
    """The session-scoped migration fixture has already run."""

    async def current() -> str:
        async with database.engine.connect() as connection:
            result = await connection.execute(text("SELECT version_num FROM alembic_version"))
            return result.scalar_one()

    assert _run(current()) == "0001_phase2"


def test_the_migrated_schema_has_exactly_the_three_phase2_tables(database):
    async def names() -> set[str]:
        async with database.engine.connect() as connection:
            return await connection.run_sync(_inspect_tables)

    assert _run(names()) == {"users", "conversations", "messages"}


def test_migrations_round_trip_on_a_dedicated_scratch_database(database_url):
    """T-18, end to end: empty -> head -> base -> head.

    Uses its own throwaway database rather than the shared test one, so a
    rollback cannot disturb the schema the rest of the suite runs against. The
    database is created and dropped by this test, and its name ends in ``_test``
    so it is unmistakably not real data.
    """
    import os
    import uuid

    from alembic import command
    from alembic.config import Config
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.core.config import PROJECT_ROOT

    scratch_name = f"scratch_migration_{uuid.uuid4().hex[:8]}_test"
    maintenance_url = database_url.rsplit("/", 1)[0] + "/postgres"
    scratch_url = database_url.rsplit("/", 1)[0] + f"/{scratch_name}"

    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "app/db/migrations"))

    async def admin_command(statement: str) -> None:
        # AUTOCOMMIT: CREATE DATABASE and DROP DATABASE cannot run in a
        # transaction block.
        engine = create_async_engine(maintenance_url, isolation_level="AUTOCOMMIT")
        try:
            async with engine.connect() as connection:
                await connection.execute(text(statement))
        finally:
            await engine.dispose()

    async def tables() -> set[str]:
        engine = create_async_engine(scratch_url)
        try:
            async with engine.connect() as connection:
                return await connection.run_sync(_inspect_tables)
        finally:
            await engine.dispose()

    def migrate(action: str) -> None:
        previous = os.environ.get("ALEMBIC_DATABASE_URL")
        os.environ["ALEMBIC_DATABASE_URL"] = scratch_url
        os.environ["ALEMBIC_REQUIRE_TEST_DB"] = "1"
        try:
            getattr(command, action)(config, "head" if action == "upgrade" else "base")
        finally:
            os.environ.pop("ALEMBIC_REQUIRE_TEST_DB", None)
            if previous is None:
                os.environ.pop("ALEMBIC_DATABASE_URL", None)
            else:
                os.environ["ALEMBIC_DATABASE_URL"] = previous

    _run(admin_command(f'CREATE DATABASE "{scratch_name}"'))
    try:
        # 1. empty -> head
        migrate("upgrade")
        assert _run(tables()) == {"users", "conversations", "messages"}
        assert index_names(scratch_url) >= {
            "ix_users_telegram_user_id",
            "ix_conversations_user_id",
            "ix_conversations_user_id_updated_at",
            "ix_messages_conversation_id_created_at",
        }

        # 2. head -> base
        migrate("downgrade")
        assert _run(tables()) == set(), "rollback left tables behind"

        # 3. base -> head again: the migration is not one-way
        migrate("upgrade")
        assert _run(tables()) == {"users", "conversations", "messages"}
    finally:
        _run(admin_command(f'DROP DATABASE IF EXISTS "{scratch_name}"'))


def test_the_models_and_the_migrations_agree(database_url):
    """``alembic check`` must find no drift.

    Without this, a model edit that was never migrated would pass every test
    that builds its schema from the models, and only fail in production.
    """
    import os

    from alembic import command
    from alembic.config import Config

    from app.core.config import PROJECT_ROOT

    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "app/db/migrations"))
    previous = os.environ.get("ALEMBIC_DATABASE_URL")
    os.environ["ALEMBIC_DATABASE_URL"] = database_url
    os.environ["ALEMBIC_REQUIRE_TEST_DB"] = "1"
    try:
        command.check(config)
    finally:
        os.environ.pop("ALEMBIC_REQUIRE_TEST_DB", None)
        if previous is None:
            os.environ.pop("ALEMBIC_DATABASE_URL", None)
        else:
            os.environ["ALEMBIC_DATABASE_URL"] = previous


# --- helpers ---------------------------------------------------------------

def _inspect_tables(connection):
    """Synchronous helper for ``AsyncConnection.run_sync``."""
    return set(inspect(connection).get_table_names()) - {"alembic_version"}


def index_names(url: str) -> set[str]:
    """Every index in the public schema of ``url``."""

    async def query() -> set[str]:
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import create_async_engine

        engine = create_async_engine(url)
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text("SELECT indexname FROM pg_indexes WHERE schemaname='public'")
                )
                return {row[0] for row in result}
        finally:
            await engine.dispose()

    return _run(query())


def _migration_url() -> str:
    """The URL ``app/db/migrations/env.py`` would migrate.

    That module runs migrations at import time, so it cannot be imported to ask
    it a question. The selection is re-exercised here through the same resolver
    and the same two environment variables, which is precisely the logic the
    guard protects; ``env.py`` is a thin wrapper over both.
    """
    import os

    override = os.environ.get("ALEMBIC_DATABASE_URL")
    require_test = os.environ.get("ALEMBIC_REQUIRE_TEST_DB", "").strip() in {
        "1", "true", "yes",
    }
    return resolve_database_url(
        override or "postgresql+asyncpg://u:p@localhost/never_used",
        require_test=require_test,
    )


def _run(coro):
    from app.infrastructure.asyncio_runtime import run_coroutine

    return run_coroutine(coro)
