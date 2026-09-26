"""Shared fixtures.

Tests must never touch the network. Both external boundaries — the AI provider
and Telegram — are substitutable precisely because of the Phase 1 dependency
injection work (P1-6, FR-5.4). Phase 2 adds a third: the database.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from typing import TYPE_CHECKING

import pytest

from app.core.config import Settings
from app.db.session import Database
from app.domain.chat import LLMReply
from app.domain.schemas import QueryAnalysis

if TYPE_CHECKING:  # pragma: no cover
    from sqlalchemy.ext.asyncio import AsyncSession

# --- Test database isolation (Phase 2 § 23) ---------------------------------
#
# The suite talks to a real PostgreSQL, because the phase requires migrations and
# constraints to be verified against a real database rather than a fake. That
# makes isolation a correctness problem, not a convenience:
#
#   1. TEST_DATABASE_URL must be set, and its name must end in "_test".
#      app.db.session.resolve_database_url enforces the second half even if this
#      file is bypassed, so a misconfigured value fails loudly instead of
#      truncating real data.
#   2. The schema is created by running the real migrations, once per session.
#      The tests therefore exercise the same DDL a deployment would run.
#   3. Every test runs inside a transaction that is rolled back, so a test
#      cannot leak rows into the next one.

_TEST_DB_MARKER = "_test"


def _test_database_url() -> str:
    from app.db.session import DatabaseMisconfiguredError, resolve_database_url

    url = os_environ_test_url()
    try:
        return resolve_database_url(url, require_test=True)
    except DatabaseMisconfiguredError as exc:
        pytest.fail(
            f"{exc}\n\n"
            "The test suite needs a dedicated test database. Create one and point "
            "TEST_DATABASE_URL at it, for example:\n"
            "  createdb research_db_test\n"
            "  export TEST_DATABASE_URL="
            "postgresql+asyncpg://USER:PASSWORD@localhost:5432/research_db_test"
        )


def os_environ_test_url() -> str | None:
    """Read ``TEST_DATABASE_URL`` from the process environment, then ``.env``.

    The environment wins so CI can point the suite at its own service container
    without a ``.env`` file. Falling back to ``.env`` means a developer who has
    followed the README needs no extra setup. Either way the name check in
    :func:`_test_database_url` is what actually protects the data.
    """
    import os

    raw = os.environ.get("TEST_DATABASE_URL")
    if raw:
        return raw.strip() or None
    return _from_env_file()


def _from_env_file() -> str | None:

    from app.core.config import ENV_FILE

    if not ENV_FILE.exists():
        return None
    for line in ENV_FILE.read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("TEST_DATABASE_URL="):
            return stripped.split("=", 1)[1].strip() or None
    return None


@pytest.fixture(scope="session")
def database_url() -> str:
    """The validated test database URL, or a loud failure."""
    url = _test_database_url()
    assert url.rsplit("/", 1)[-1].endswith(_TEST_DB_MARKER), (
        "refusing to run database tests against a non-test database"
    )
    return url


@pytest.fixture(scope="session")
def migrated_database(database_url: str) -> Iterator[None]:
    """Bring the test database to head using the real migrations, once.

    Runs Alembic in-process rather than shelling out, so the suite does not
    depend on a ``alembic`` binary being on PATH.
    """
    from alembic import command
    from alembic.config import Config

    from app.core.config import PROJECT_ROOT

    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "app/db/migrations"))

    import os

    previous = os.environ.get("ALEMBIC_DATABASE_URL")
    os.environ["ALEMBIC_DATABASE_URL"] = database_url
    # Turn on the test-database name check for the duration of the suite, so a
    # migration can never be pointed at a real database from a test.
    os.environ["ALEMBIC_REQUIRE_TEST_DB"] = "1"
    try:
        command.upgrade(config, "head")
        yield
    finally:
        os.environ.pop("ALEMBIC_REQUIRE_TEST_DB", None)
        if previous is None:
            os.environ.pop("ALEMBIC_DATABASE_URL", None)
        else:
            os.environ["ALEMBIC_DATABASE_URL"] = previous


@pytest.fixture
async def db(migrated_database: None, database_url: str) -> AsyncIterator[AsyncSession]:
    """A session bound to the test database, with a per-test rollback.

    The test's work runs inside one transaction that is rolled back afterwards,
    so tests are isolated without truncating tables between them and without any
    test seeing another's rows.
    """
    from sqlalchemy.ext.asyncio import AsyncSession

    instance = Database(database_url)
    connection = await instance.engine.connect()
    transaction = await connection.begin()
    session = AsyncSession(bind=connection, expire_on_commit=False)
    try:
        yield session
    finally:
        # Order matters after a failed statement: the session must be closed
        # first, because closing it releases the transaction, and the rollback
        # is then guarded because it may already have happened.
        await session.close()
        if transaction.is_active:
            await transaction.rollback()
        await connection.close()
        await instance.dispose()


@pytest.fixture
def clean_database(database: Database) -> Iterator[Database]:
    """Empty the three tables around each test.

    For tests that must **commit for real** — a Celery task body, a unit of work
    whose rollback is the thing under test. The per-test-rollback ``db`` fixture
    cannot serve those: it holds an outer transaction that would hide whether a
    commit or a rollback actually happened.

    Truncation is confined to the test database, which the ``_test`` name guard
    in :func:`app.db.session.resolve_database_url` enforces.
    """
    _truncate(database)
    yield database
    _truncate(database)


def _truncate(database: Database) -> None:
    """Empty the tables, failing fast rather than blocking.

    ``lock_timeout`` is not decoration: TRUNCATE needs an ACCESS EXCLUSIVE lock,
    so a test that leaked an open read transaction would otherwise make teardown
    wait forever instead of failing.
    """

    async def wipe() -> None:
        from sqlalchemy import text

        async with database.engine.begin() as connection:
            await connection.execute(text("SET LOCAL lock_timeout = '5s'"))
            await connection.execute(
                text("TRUNCATE messages, conversations, users RESTART IDENTITY CASCADE")
            )

    run_on_process_loop(wipe())


def run_on_process_loop(coro):
    """Drive a coroutine on this process's long-lived event loop.

    The loop a Celery worker uses, so a synchronous test exercises the same
    machinery the product does.
    """
    from app.infrastructure.asyncio_runtime import run_coroutine

    return run_coroutine(coro)


@pytest.fixture
def database(database_url: str) -> Iterator[Database]:
    """A process-style ``Database``, for code under test that builds its own.

    Runs and disposes on the process's long-lived loop, the same loop the code
    under test uses. Disposing on a different loop would leave the pool's
    connections attached to a dead loop, which leaks them and eventually
    exhausts the server's connection limit mid-suite.

    Not rolled back per test; tests using this must clean up after themselves.
    Prefer the ``db`` session fixture where possible.
    """
    from app.db.session import Database
    from app.infrastructure.asyncio_runtime import run_coroutine

    instance = Database(database_url)
    try:
        yield instance
    finally:
        run_coroutine(instance.dispose())


@pytest.fixture
def settings() -> Settings:
    """Settings built from test values, never from the real ``.env``."""
    return Settings(
        _env_file=None,
        BOT_TOKEN="1234567890:TEST_TOKEN_NOT_A_REAL_CREDENTIAL",
        GROQ_API_KEY="gsk-test-key-not-a-real-credential",
        REDIS_URL="redis://localhost:6379/15",
        DATABASE_URL="postgresql+asyncpg://user:password@localhost:5432/never_used",
        TEST_DATABASE_URL="postgresql+asyncpg://user:password@localhost:5432/never_used_test",
        LLM_BASE_URL="https://provider.invalid/v1",
        LLM_MODEL="test-model",
        LLM_TIMEOUT_SECONDS=1.0,
        LLM_MAX_TOKENS=256,
        RATE_LIMIT_MAX_REQUESTS=3,
        RATE_LIMIT_WINDOW_SECONDS=60,
        RATE_LIMIT_DAILY_MAX_REQUESTS=100,
        MAX_IN_FLIGHT_REQUESTS=2,
        LOG_LEVEL="WARNING",
    )


class FakeLLMClient:
    """Stands in for the AI provider. Records calls; returns canned results.

    Implements both provider surfaces: the Phase 1 ``analyze_query`` used by the
    query-decomposition path, and the Phase 2 ``complete`` used by chat.
    """

    def __init__(
        self,
        result: QueryAnalysis | None = None,
        error: Exception | None = None,
        reply: str = "fake reply",
    ) -> None:
        self.result = result or QueryAnalysis(summary="s", sub_questions=["a", "b"])
        self.error = error
        self.reply_text = reply
        self.calls: list[str] = []
        self.chat_calls: list[list] = []
        self.models: list[str] = []

    def analyze_query(self, user_query: str) -> QueryAnalysis:
        self.calls.append(user_query)
        if self.error is not None:
            raise self.error
        return self.result

    def generate_final_answer(self, user_query: str, sub_answers: list[str]) -> str:
        return "final"

    def complete(self, messages, *, model, temperature, max_tokens) -> LLMReply:
        self.chat_calls.append(list(messages))
        self.models.append(model)
        if self.error is not None:
            raise self.error
        return LLMReply(
            text=self.reply_text,
            model=model,
            prompt_tokens=11,
            completion_tokens=7,
            finish_reason="stop",
        )


class LoopBoundSession:
    """Mimics ``aiogram``'s ``AiohttpSession`` caching an aiohttp session."""

    def __init__(self) -> None:
        self.bound_to: asyncio.AbstractEventLoop | None = None
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class LoopBoundBot:
    """A bot double that reproduces aiogram's loop-affine session caching.

    This is the shape of the real defect: the session is created on first use
    and bound to the loop that was running then. Reusing it from a different,
    closed loop is what failed (audit C-3). Modelling it faithfully is what
    makes the regression test meaningful rather than cosmetic.
    """

    def __init__(self) -> None:
        self.session = LoopBoundSession()
        self.sent: list[tuple[int, str]] = []
        self.loops: list[asyncio.AbstractEventLoop] = []

    async def send_message(self, chat_id: int, text: str, **_kwargs) -> str:
        running = asyncio.get_running_loop()
        if self.session.bound_to is None:
            # Mirrors AiohttpSession.create_session: bind once, on first use.
            self.session.bound_to = running
        elif self.session.bound_to is not running:
            raise RuntimeError(
                "session is bound to a different event loop than the current one"
            )
        await asyncio.sleep(0)  # yield, exercising real loop machinery
        self.loops.append(running)
        self.sent.append((chat_id, text))
        return "ok"


@pytest.fixture
def fake_bot() -> LoopBoundBot:
    return LoopBoundBot()


@pytest.fixture(autouse=True)
def block_outbound_network(monkeypatch):
    """Fail any attempt to open a non-loopback socket during a test.

    Phase 1 rule: no test may perform a real network call to an LLM or Telegram
    endpoint. ``Message.answer`` reaches the network through
    ``AiohttpSession.__call__`` rather than ``Bot.send_message``, so patching a
    client method is not sufficient. This guard makes an accidental outbound
    connection a loud test failure instead of a silent dependency on the
    internet.

    Loopback is still allowed, because Phase 2 database tests must reach a real
    PostgreSQL on 127.0.0.1. That is a deliberate, narrowed exception: the
    database is a declared local dependency, not an external service.
    """
    import socket

    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    allowed = {"127.0.0.1", "::1", "localhost"}

    def _guard(self, address, *args, **kwargs):
        host = address[0] if isinstance(address, tuple) else address
        if isinstance(host, str) and host not in allowed:
            raise AssertionError(
                f"test attempted an outbound network connection to {host!r}; "
                "Phase 1 tests must not touch the network"
            )
        return real_connect(self, address, *args, **kwargs)

    def _guard_ex(self, address, *args, **kwargs):
        host = address[0] if isinstance(address, tuple) else address
        if isinstance(host, str) and host not in allowed:
            raise AssertionError(
                f"test attempted an outbound network connection to {host!r}"
            )
        return real_connect_ex(self, address, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", _guard)
    monkeypatch.setattr(socket.socket, "connect_ex", _guard_ex)


@pytest.fixture
def fake_llm() -> FakeLLMClient:
    return FakeLLMClient()


@pytest.fixture
def chat_store():
    """The state behind the in-memory persistence double."""
    from tests.fakes import InMemoryStore

    return InMemoryStore()


@pytest.fixture
def chat_service(settings, fake_llm, chat_store):
    """A :class:`ChatService` wired to the in-memory repositories.

    Lets service-level tests exercise the real orchestration, context assembly,
    and idempotency logic without a database. Tests that need the real schema,
    constraints, or migrations use the ``db`` fixture instead.
    """
    from app.services.chat import ChatService
    from app.services.context import ContextBuilder
    from tests.fakes import in_memory_uow_factory

    factory, _store = in_memory_uow_factory(chat_store)
    return ChatService(
        settings=settings,
        llm=fake_llm,
        uow_factory=factory,
        context=ContextBuilder(settings),
    )


@pytest.fixture(autouse=True)
def forbid_real_container():
    """Fail loudly if a test ever builds the real production container.

    Guards against a specific failure mode observed during Phase 1
    implementation: a test that appears to inject doubles but actually reaches
    the real container, and therefore the real ``.env`` credentials, and passes
    for the wrong reason. Tests construct an ``AppContainer`` explicitly; any
    call to ``build_container`` is a bug in the test.
    """
    import app.bot.container as container_module

    def refuse(*_args, **_kwargs):
        raise AssertionError(
            "a test attempted to build the real application container; "
            "inject an AppContainer or patch get_container instead"
        )

    original = container_module.build_container
    container_module.build_container = refuse
    try:
        yield
    finally:
        container_module.build_container = original


@pytest.fixture(autouse=True)
def reset_loop_state():
    """Keep the process loop from leaking between tests."""
    from app.infrastructure import asyncio_runtime

    asyncio_runtime.reset_event_loop()
    yield
    asyncio_runtime.reset_event_loop()
