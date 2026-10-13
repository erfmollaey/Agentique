"""The ``/research`` command: intake, acknowledgement, and the one-reply contract.

Driven through a real aiogram ``Dispatcher`` with real ``Update``/``Message``
objects, exactly as the Phase 2 chat tests are, so filter resolution and handler
ordering are exercised rather than assumed. The network boundary is the session, and
the research collaborators are substituted at the container seam — which is
substituting everything the handler depends on and nothing it owns.

Covered here:

* a missing or overlength question is rejected with **no run created and nothing
  enqueued** (§ 15.9, AD-046);
* a valid request produces exactly one acknowledgement and exactly one enqueue, and
  the enqueued message carries the stable run identifier rather than the question;
* a redelivered command converges on the existing run instead of a second one (B7);
* a queue failure leaves no apparently-active run, through the compensating
  transition;
* one user's request is scoped to them, and never to another user;
* the existing command surface is unchanged.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.types import Chat, Message, Update, User

from app.bot.container import AppContainer
from app.bot.handlers import register_handlers
from app.services.rate_limit import RateLimiter

FAKE_TOKEN = "1234567890:AAsdfghjklQWERTYuiopZXCVBNMasdfghjkl"


@pytest.fixture
def sent(monkeypatch):
    """Record outbound Telegram calls without touching the network.

    ``Message.answer`` reaches Telegram through ``AiohttpSession.__call__``, not
    ``Bot.send_message``, so the session is the correct boundary to stub.
    """
    calls: list[dict] = []

    async def fake_call(self, bot, method, timeout=None):
        calls.append({"chat_id": method.chat_id, "text": method.text})
        return SimpleNamespace(message_id=1, text=method.text)

    monkeypatch.setattr(AiohttpSession, "__call__", fake_call, raising=True)
    return calls


@pytest.fixture
def enqueued(monkeypatch):
    """Record enqueued research tasks without needing a broker."""
    calls: list[dict] = []

    class FakeTask:
        @staticmethod
        def delay(**kwargs):
            calls.append(kwargs)

        @staticmethod
        def apply_async(*a, **kw):  # pragma: no cover - not used here
            raise AssertionError("apply_async must not be used")

    import app.tasks.research_pipeline_task as task_module

    monkeypatch.setattr(task_module, "run_research", FakeTask)
    return calls


@pytest.fixture
def intake():
    """A research intake double that records what it was asked to accept."""
    from app.domain.research.types import ResearchRun
    from app.services.research.intake import AcceptedRequest

    class _Intake:
        def __init__(self, *, result=None, error: Exception | None = None) -> None:
            self._result = result or AcceptedRequest(
                run=ResearchRun(id=5, user_id=9, question="q"), created=True
            )
            self._error = error
            self.accepted: list[dict] = []

        async def accept(self, **kwargs):
            self.accepted.append(kwargs)
            if self._error is not None:
                raise self._error
            return self._result

    stub = _Intake()
    stub.error = None  # type: ignore[attr-defined]

    def _configure(*, result=None, error: Exception | None = None) -> _Intake:
        if result is not None:
            stub._result = result  # type: ignore[attr-defined]
        stub._error = error  # type: ignore[attr-defined]
        return stub

    stub.configure = _configure  # type: ignore[attr-defined]
    return stub


@pytest.fixture
def research(settings, intake, sent):
    """A dispatcher wired with the research collaborators and nothing else.

    ``research`` is an object satisfying the orchestrator seam, so the handler's
    resolution of ``container.research`` behaves as it does in production without any
    stage of the pipeline being exercised.
    """

    class _Orchestrator:
        def __init__(self) -> None:
            self.executions: list[tuple[int, int]] = []

        async def execute(self, *, run_id: int, user_id: int):
            self.executions.append((run_id, user_id))

    return _Orchestrator()


@pytest.fixture
def dispatcher(settings, intake, research, monkeypatch):
    dp = Dispatcher()
    bot = Bot(token=FAKE_TOKEN)
    container = AppContainer(
        settings=settings,
        bot=bot,
        dispatcher=dp,
        llm=None,  # type: ignore[arg-type]
        delivery=None,  # type: ignore[arg-type]
        rate_limiter=RateLimiter(settings),
        database=None,
        chat=None,
        research=research,
        intake=intake,
        research_rate_limiter=RateLimiter(settings),
    )
    register_handlers(dp, container)
    return dp, bot, container


def _update(text=None, chat_id=55, user_id=77, message_id=4321) -> Update:
    message = Message(
        message_id=message_id,
        date=datetime.now(tz=UTC),
        chat=Chat(id=chat_id, type="private"),
        from_user=User(id=user_id, is_bot=False, first_name="T", username="tester")
        if user_id
        else None,
        text=text,
    )
    return Update(update_id=message_id, message=message)


async def _feed(dp, bot, update):
    return await dp.feed_update(bot, update)


# --- intake validation (§ 15.9, AD-046) -------------------------------------


@pytest.mark.asyncio
async def test_a_missing_question_is_rejected_without_a_run(dispatcher, sent, enqueued, intake):
    dp, bot, _ = dispatcher
    await _feed(dp, bot, _update(text="/research"))

    assert enqueued == []
    assert intake.accepted == []
    assert len(sent) == 1
    assert "/research" in sent[0]["text"].lower()


@pytest.mark.asyncio
async def test_a_whitespace_only_question_is_rejected(dispatcher, sent, enqueued, intake):
    dp, bot, _ = dispatcher
    await _feed(dp, bot, _update(text="/research    "))

    assert enqueued == []
    assert intake.accepted == []
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_an_overlength_question_creates_no_state_at_all(
    dispatcher, sent, enqueued, intake, settings
):
    """AD-046: the cap is enforced at intake, before any row exists.

    A question the planner would refuse must never become a run, a queue message, and
    a worker execution — that sequence is the behaviour § 15.9 names and rejects.
    """
    dp, bot, _ = dispatcher
    await _feed(dp, bot, _update(text="/research " + "x" * (settings.PLAN_MAX_QUESTION_CHARS + 1)))

    assert enqueued == []
    assert intake.accepted == []
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_the_length_cap_is_the_planner_cap(dispatcher, sent, enqueued, intake, settings):
    dp, bot, _ = dispatcher
    at_cap = "y" * settings.PLAN_MAX_QUESTION_CHARS
    await _feed(dp, bot, _update(text="/research " + at_cap))

    assert len(intake.accepted) == 1
    assert intake.accepted[0]["question"] == at_cap


@pytest.mark.asyncio
async def test_a_service_rejection_is_mapped_not_leaked(dispatcher, sent, enqueued, intake):
    """The typed error's own text never reaches a user (§ 15.9, SR-7)."""
    from app.domain.errors import PlanningInputError

    dp, bot, _ = dispatcher
    intake.configure(error=PlanningInputError("exceeds PLAN_MAX_QUESTION_CHARS=2000"))

    await _feed(dp, bot, _update(text="/research " + "z" * 500))

    assert enqueued == []
    text = sent[0]["text"]
    for forbidden in ("PLAN_MAX_QUESTION_CHARS", "2000", "PlanningInput"):
        assert forbidden not in text


# --- the acknowledgement contract (§ 15.1) ----------------------------------


@pytest.mark.asyncio
async def test_a_valid_request_is_acknowledged_once_and_enqueued_once(
    dispatcher, sent, enqueued, intake
):
    dp, bot, _ = dispatcher
    await _feed(dp, bot, _update(text="/research what happened in 1403?"))

    assert len(sent) == 1, "exactly one acknowledgement per accepted request"
    assert len(enqueued) == 1


@pytest.mark.asyncio
async def test_the_enqueued_message_carries_the_stable_identifier(
    dispatcher, sent, enqueued, intake
):
    """AD-043: the message carries the run id, not the question."""
    dp, bot, _ = dispatcher
    await _feed(dp, bot, _update(text="/research what happened in 1403?"))

    assert enqueued[0]["run_id"] == 5
    assert "question" not in enqueued[0]


@pytest.mark.asyncio
async def test_destination_and_owner_are_distinct_values(dispatcher, sent, enqueued, intake):
    """H-1, kept distinct: the chat receives the reply, the user owns the run.

    The ``user_id`` in the queued message is the *run's owner* — the internal user
    id stored on ``research_runs.user_id`` — not the Telegram sender id. The
    orchestrator resolves ownership with
    ``research_runs.get_for_user(run_id, user_id)``, which filters on that internal
    column, so a Telegram id here could never match and every ``/research`` request
    would fail the ownership check before any work began (SR-10).
    """
    dp, bot, _ = dispatcher
    await _feed(dp, bot, _update(text="/research q", chat_id=55, user_id=4242))

    # The destination is the Telegram chat the reply goes to...
    assert enqueued[0]["chat_id"] == 55
    # ...while the owner is the run's user row, not the Telegram sender id.
    assert enqueued[0]["user_id"] == intake._result.run.user_id == 9
    assert enqueued[0]["user_id"] != 4242, "the Telegram id was passed as the run owner"
    # H-1: the two are still kept apart.
    assert enqueued[0]["chat_id"] != enqueued[0]["user_id"]


@pytest.mark.asyncio
async def test_the_run_is_created_with_the_commands_identity(dispatcher, sent, intake):
    """B7: the idempotency key is derived from the command's own platform ids."""
    dp, bot, _ = dispatcher
    await _feed(dp, bot, _update(text="/research q", message_id=9876))

    assert intake.accepted[0]["telegram_user_id"] == 77
    assert intake.accepted[0]["telegram_message_id"] == 9876


@pytest.mark.asyncio
async def test_a_redelivery_converges_and_is_not_ack_as_new(dispatcher, sent, enqueued, intake):
    """B7: a duplicate delivery neither duplicates the run nor re-acks it."""
    from app.domain.research.types import ResearchRun
    from app.services.research.intake import AcceptedRequest

    dp, bot, _ = dispatcher
    intake.configure(
        result=AcceptedRequest(
            run=ResearchRun(id=5, user_id=9, question="q"), created=False
        )
    )

    await _feed(dp, bot, _update(text="/research q"))

    assert enqueued == [], "a redelivered command must not enqueue a second execution"
    assert len(sent) == 1


# --- the queue failure path (AD-043) ----------------------------------------


@pytest.mark.asyncio
async def test_a_broken_enqueue_sends_one_notice_and_enqueues_nothing(
    dispatcher, sent, enqueued, intake, monkeypatch
):
    import app.tasks.research_pipeline_task as task_module

    async def _broken(**kwargs):
        raise RuntimeError("broker unreachable: redis://:6379/0")

    class _Failing:
        @staticmethod
        def delay(**kwargs):
            raise RuntimeError("broker unreachable: redis://:6379/0")

    monkeypatch.setattr(task_module, "run_research", _Failing)
    enqueued.clear()

    dp, bot, _ = dispatcher
    await _feed(dp, bot, _update(text="/research q"))

    assert enqueued == []
    assert len(sent) == 1
    text = sent[0]["text"]
    for forbidden in ("redis", "Traceback", "6379"):
        assert forbidden not in text


# --- ownership --------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_run_is_owned_by_the_requesting_user(dispatcher, sent, intake):
    dp, bot, _ = dispatcher
    await _feed(dp, bot, _update(text="/research q", user_id=4242))

    assert intake.accepted[0]["telegram_user_id"] == 4242


@pytest.mark.asyncio
async def test_two_users_requests_are_scoped_to_themselves(dispatcher, sent, enqueued, intake):
    dp, bot, _ = dispatcher
    await _feed(dp, bot, _update(text="/research q", user_id=1))
    await _feed(dp, bot, _update(text="/research q", user_id=2))

    assert sorted(a["telegram_user_id"] for a in intake.accepted) == [1, 2]
    assert len(enqueued) == 2


# --- the derived key (AD-017's pattern) -------------------------------------


def test_the_request_key_is_derived_and_stable():
    from app.services.research.intake import derive_request_key

    assert derive_request_key(1, 2) == derive_request_key(1, 2)
    assert derive_request_key(1, 2) != derive_request_key(1, 3)
    assert derive_request_key(1, 2) != derive_request_key(2, 2)
    assert derive_request_key(1, None) is None


# --- the existing surface is unchanged (§ 15.12) ---------------------------


@pytest.mark.asyncio
async def test_a_help_command_is_unchanged(dispatcher, sent, enqueued, intake):
    dp, bot, _ = dispatcher
    await _feed(dp, bot, _update(text="/help"))

    assert enqueued == []
    assert intake.accepted == []
    assert "/help" in sent[0]["text"]


@pytest.mark.asyncio
async def test_an_unknown_command_is_still_answered_not_treated_as_text(
    dispatcher, sent, enqueued, intake
):
    dp, bot, _ = dispatcher
    await _feed(dp, bot, _update(text="/doesnotexist x"))

    assert enqueued == []
    assert "/help" in sent[0]["text"]


@pytest.mark.asyncio
async def test_help_lists_the_research_command(dispatcher, sent, enqueued, intake):
    """The new command must appear in the list, or users cannot discover it."""
    dp, bot, _ = dispatcher
    await _feed(dp, bot, _update(text="/help"))

    assert "/research" in sent[0]["text"]


# --- the compensating transition, against a real PostgreSQL -----------------


class TestQueueFailureCompensation:
    def test_no_run_is_left_apparently_active(self, clean_database, settings, intake, monkeypatch):
        from tests.conftest import run_on_process_loop

        database = clean_database

        class _Failing:
            @staticmethod
            def delay(**kwargs):
                raise RuntimeError("broker unreachable")

        import app.tasks.research_pipeline_task as task_module

        monkeypatch.setattr(task_module, "run_research", _Failing)

        user_id, run_id = run_on_process_loop(_seed_queued(database))

        async def _compensate() -> None:
            from app.bot.handlers import _fail_research

            await _fail_research(_container(database, settings), run_id, user_id)

        run_on_process_loop(_compensate())

        assert run_on_process_loop(_run_status(database, run_id)) == "failed"
        assert run_on_process_loop(_run_summary(database, run_id)) is not None

    def test_the_compensation_never_overwrites_a_terminal_run(
        self, clean_database, settings
    ):
        """A worker that finished must not be reset by a later handler failure.

        The lifecycle refuses ``completed -> failed`` (``TERMINAL_STATE``), so a
        compensation that raced a completion is stopped by the same rule that stops a
        stale worker — which is the property this asserts.
        """
        from tests.conftest import run_on_process_loop

        database = clean_database
        user_id, run_id = run_on_process_loop(_seed_completed(database))

        async def _attempt() -> None:
            from app.bot.handlers import _fail_research

            await _fail_research(_container(database, settings), run_id, user_id)

        run_on_process_loop(_attempt())

        assert run_on_process_loop(_run_status(database, run_id)) == "completed"


class TestQueueFailureCompensationEndToEnd:
    """The whole ``/research`` path against a real database and a broken broker.

    ``TestQueueFailureCompensation`` above calls ``_fail_research`` directly and
    proves the repository honours the transition. It cannot prove the handler
    passes the *right* user id, because the caller picks both ids: it seeds the
    user row itself and passes that same value in, so the internal id and the
    Telegram id are one number by construction and the argument is never tested.

    This test closes that gap. The real ``ResearchIntakeService`` creates the
    row, the real unit of work writes to it, and the real handler decides which
    id to hand to ``set_status``. That decision is the AD-043 contract: a run
    whose enqueue failed must not be left ``queued`` looking active forever.
    """

    def test_a_broken_enqueue_marks_the_real_run_failed(
        self, clean_database, settings, monkeypatch
    ):
        """A broker failure leaves no run at ``queued`` (AD-043).

        The Telegram sender id and the run owner are *different numbers* here by
        construction: ``clean_database`` truncates with ``RESTART IDENTITY``, so
        the first user row takes internal id 1, and the sender id below is 4242.
        Keeping them apart is the only way this test can tell which id the
        handler passed. With the old Telegram-id argument the compensating
        ``set_status`` finds no owning row, ``_fail_research`` logs and swallows
        the error, and the run is stuck at ``queued`` — the exact state this test
        refuses.
        """
        from tests.conftest import run_on_process_loop

        database = clean_database
        telegram_user_id = 4242

        class _Broken:
            @staticmethod
            def delay(**kwargs):
                raise RuntimeError("broker unreachable")

        import app.tasks.research_pipeline_task as task_module

        monkeypatch.setattr(task_module, "run_research", _Broken)

        sent: list[dict] = self._record_outbound(monkeypatch)
        run = run_on_process_loop(
            self._drive_research_command(database, settings, telegram_user_id)
        )

        # The user is told the queue is down, without the broker's own detail.
        assert len(sent) == 1, "the user was not told the enqueue failed"
        text = sent[0]["text"].lower()
        assert "queue is unavailable" in text
        for forbidden in ("broker", "redis", "traceback"):
            assert forbidden not in text, "internal detail reached the user"

        # The run exists and its owner is not the sender — this test's premise.
        assert run is not None, "the accepted request left no run behind"
        assert run.user_id != telegram_user_id, (
            "the run owner and the Telegram sender must differ for this test to "
            f"detect which id the handler uses (owner={run.user_id}, "
            f"sender={telegram_user_id})"
        )

        # The assertion the original bug failed: a run no worker will ever see
        # is failed, not left at ``queued`` where every reader counts it active.
        assert run.status == "failed", (
            "a run whose enqueue failed was left at "
            f"{run.status!r} (AD-043); the handler passed an id that the run's "
            "own ownership check refuses, so the compensation never landed"
        )
        assert run.failure_summary is not None

    # --- helpers --------------------------------------------------------------

    @staticmethod
    def _record_outbound(monkeypatch) -> list[dict]:
        """Capture what the user was told, without a Telegram session."""
        calls: list[dict] = []

        async def fake_call(self, bot, method, timeout=None):
            calls.append({"chat_id": method.chat_id, "text": method.text})
            return SimpleNamespace(message_id=1, text=method.text)

        monkeypatch.setattr(AiohttpSession, "__call__", fake_call, raising=True)
        return calls

    @staticmethod
    async def _drive_research_command(database, settings, telegram_user_id: int):
        """Feed one ``/research`` update through the real handler.

        Returns the ``research_runs`` row the *handler* created, read back from
        the database. Every collaborator is real except the Celery enqueue, so
        the id the handler passes to ``set_status`` is the one under test.
        """
        from aiogram import Bot, Dispatcher

        from app.db.unit_of_work import sqlalchemy_uow_factory
        from app.services.research.intake import ResearchIntakeService

        bot = Bot(token=FAKE_TOKEN)
        uow_factory = sqlalchemy_uow_factory(database)
        container = AppContainer(
            settings=settings,
            bot=bot,
            dispatcher=Dispatcher(),
            llm=None,  # type: ignore[arg-type]
            delivery=None,  # type: ignore[arg-type]
            rate_limiter=RateLimiter(settings),
            database=database,
            chat=None,
            # A real unit of work: ``_fail_research`` reads ``research._uow``.
            research=_RealOrchestrator(database, settings),
            intake=ResearchIntakeService(settings=settings, uow_factory=uow_factory),
            research_rate_limiter=RateLimiter(settings),
        )
        dispatcher = Dispatcher()
        register_handlers(dispatcher, container)

        await dispatcher.feed_update(
            bot,
            Update(
                update_id=1,
                message=Message(
                    message_id=9876,
                    date=datetime.now(tz=UTC),
                    chat=Chat(id=55, type="private"),
                    from_user=User(
                        id=telegram_user_id,
                        is_bot=False,
                        first_name="T",
                        username="tester",
                    ),
                    text="/research what happened in 1403?",
                ),
            ),
        )

        # Read back through the database, not through the handler's return value.
        from sqlalchemy import select

        from app.db.models import ResearchRunModel

        async with database.session() as session:
            rows = (await session.execute(select(ResearchRunModel))).scalars().all()
        return rows[0] if len(rows) == 1 else None


class _RealOrchestrator:
    """The orchestrator seam with a real unit of work, for the handler path."""

    def __init__(self, database, settings) -> None:
        from app.db.unit_of_work import SqlAlchemyUnitOfWork

        self._database = database
        self._uow = lambda: SqlAlchemyUnitOfWork(database)


async def _seed_queued(database) -> tuple[int, int]:
    from app.db.unit_of_work import SqlAlchemyUnitOfWork

    async with SqlAlchemyUnitOfWork(database) as uow:
        user = await uow.users.get_or_create(9451, None)
        run = await uow.research_runs.create(user.id, "q")
        return user.id, run.id


async def _seed_completed(database) -> tuple[int, int]:
    from app.db.unit_of_work import SqlAlchemyUnitOfWork

    async with SqlAlchemyUnitOfWork(database) as uow:
        user = await uow.users.get_or_create(9452, None)
        run = await uow.research_runs.create(user.id, "q")
        await uow.research_runs.set_status(run.id, user.id, "running")
        await uow.research_runs.set_status(run.id, user.id, "completed", final_answer="done")
        return user.id, run.id


async def _run_status(database, run_id: int) -> str | None:
    from app.db.models import ResearchRunModel

    async with database.session() as session:
        row = await session.get(ResearchRunModel, run_id)
        return row.status if row else None


async def _run_summary(database, run_id: int) -> str | None:
    from app.db.models import ResearchRunModel

    async with database.session() as session:
        row = await session.get(ResearchRunModel, run_id)
        return row.failure_summary if row else None


def _container(database, settings):
    from aiogram import Bot, Dispatcher

    from app.services.rate_limit import RateLimiter

    return AppContainer(
        settings=settings,
        bot=Bot(token=FAKE_TOKEN),
        dispatcher=Dispatcher(),
        llm=None,  # type: ignore[arg-type]
        delivery=None,  # type: ignore[arg-type]
        rate_limiter=RateLimiter(settings),
        database=database,
        chat=None,
        research=_RealOrchestrator(database, settings),
        intake=None,
        research_rate_limiter=RateLimiter(settings),
    )
