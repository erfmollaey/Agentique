"""Alembic environment.

The database URL is resolved from application settings rather than from
``alembic.ini``, so there is exactly one place a connection string is configured
and no place one can be committed.

Runs asynchronously, because the application owns an async engine and the two
must not disagree about the driver.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.core.config import get_settings
from app.db import models  # noqa: F401  imported for its side effect: table registration
from app.db.base import Base

config = context.config

if config.config_file_name is not None:
    # ``disable_existing_loggers=False`` is required, not cosmetic. Alembic's
    # logging config names only root/sqlalchemy/alembic, and ``fileConfig``
    # defaults to disabling every logger it did not name — which silently
    # switches off the whole ``app.*`` logger tree. Running a migration in the
    # same process as the application (as the test suite does) therefore muted
    # application logging. Found during Phase 2 completion, when a Phase 2
    # security assertion (SR-5) depended on a log record that had been silenced
    # by an unrelated import order.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _database_url() -> str:
    """The URL to migrate, honouring an explicit override.

    ``ALEMBIC_DATABASE_URL`` lets an operator or a CI job target a different
    database without editing a tracked file, and it is resolved through the same
    validator the application uses, so a connection string is never committed
    here or in ``alembic.ini``.

    ``ALEMBIC_REQUIRE_TEST_DB=1`` turns on the test-database name check. It is an
    explicit switch rather than an inference from the URL, because inferring it
    from "does the name look like a test database" would mean a *misspelled* test
    database silently migrates a real one. The test suite sets it; an operator
    migrating production deliberately does not.
    """
    import os

    from app.db.session import resolve_database_url

    override = os.environ.get("ALEMBIC_DATABASE_URL")
    require_test = os.environ.get("ALEMBIC_REQUIRE_TEST_DB", "").strip() in {"1", "true", "yes"}
    url = override or get_settings().DATABASE_URL
    return resolve_database_url(url, require_test=require_test)


def run_migrations_offline() -> None:
    """Emit SQL without a live connection."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection, target_metadata=target_metadata, compare_type=True
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Run migrations against a live database."""
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _database_url()
    engine = async_engine_from_config(section, prefix="sqlalchemy.", poolclass=None)
    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
