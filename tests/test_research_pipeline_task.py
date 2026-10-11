"""The research Celery task: delivery, redelivery, and retry classification.

Drives the real task function through ``.run()``, the same approach the chat task
suite uses, so the container resolution and the terminal-notice contract are both in
the path. No broker and no network: the orchestrator is replaced by a controlled
double that raises or returns a record, which is the seam the architecture puts
around it.

What is proved here:

* a completed run sends exactly one message, and that message carries the verified
  answer;
* a run with no evidence sends exactly one message that says so, and never an
  invented answer;
* a second delivery of the same run sends nothing further, because the run is already
  terminal when the task re-executes (B7);
* a retryable failure is retried within its allowance and the user is not messaged on
  an attempt that will be retried;
* a cooperative deadline, a citation failure and the Celery soft limit are **not**
  retried — the classification AD-045 makes load-bearing.
"""

from __future__ import annotations

import pytest

from app.domain.errors import (
    DeliveryError,
    LLMServiceError,
    ResearchDeadlineError,
    ResearchRunNotFoundError,
)
from app.domain.research.outcome import RunOutcomeKind
from app.services.delivery import DeliveryService
from app.services.research.orchestrator import ResearchOutcome
from tests.conftest import LoopBoundBot


def _container(settings, orchestrator, bot, monkeypatch):
    from app.bot import container as container_module
    from app.services.rate_limit import RateLimiter

    built = container_module.AppContainer(
        settings=settings,
        bot=bot,  # type: ignore[arg-type]
        dispatcher=None,  # type: ignore[arg-type]
        llm=None,  # type: ignore[arg-type]
        delivery=DeliveryService(bot, settings),
        rate_limiter=RateLimiter(settings),
        database=None,
        chat=None,
        research=orchestrator,
    )
    monkeypatch.setattr(container_module, "get_container", lambda: built)
    return built


class _Orchestrator:
    """A controlled orchestrator double: raises what it is told to raise."""

    def __init__(self, *, result=None, error: Exception | None = None) -> None:
        self._result = result
        self._error = error
        self.executions: list[tuple[int, int]] = []

    async def execute(self, *, run_id: int, user_id: int):
        self.executions.append((run_id, user_id))
        if self._error is not None:
            raise self._error
        return self._result


def _answered() -> ResearchOutcome:
    return ResearchOutcome(
        kind=RunOutcomeKind.COMPLETED,
        status="completed",
        answer="The 2024 survey recorded growth.",
        candidates=3,
        fetched=3,
        extracted=3,
        selected=2,
        claims=4,
        citations=4,
        synthesized=True,
    )


def _no_evidence() -> ResearchOutcome:
    return ResearchOutcome(kind=RunOutcomeKind.NO_EVIDENCE, status="failed")


def _invoke(**kwargs):
    from app.tasks.research_pipeline_task import run_research

    return run_research.run(**kwargs)


@pytest.fixture
def worker(monkeypatch, settings):
    bot = LoopBoundBot()
    return _container(settings, None, bot, monkeypatch), bot


# --- the terminal message ----------------------------------------------------


def test_a_completed_run_sends_exactly_one_message(monkeypatch, settings):
    bot = LoopBoundBot()
    orchestrator = _Orchestrator(result=_answered())
    _container(settings, orchestrator, bot, monkeypatch)

    _invoke(run_id=7, user_id=3, chat_id=99)

    assert len(bot.sent) == 1
    assert bot.sent[0][0] == 99
    assert bot.sent[0][1] == "The 2024 survey recorded growth."


def test_a_no_evidence_run_says_so_rather_than_inventing(monkeypatch, settings):
    """FR-41 / SR-12: an empty run is a clear failure, never a fabricated answer.

    The message must be a notice that nothing was found, not an attempt at an answer,
    and it must not disclose how many sources failed or why.
    """
    bot = LoopBoundBot()
    _container(settings, _Orchestrator(result=_no_evidence()), bot, monkeypatch)

    _invoke(run_id=7, user_id=3, chat_id=99)

    assert len(bot.sent) == 1
    text = bot.sent[0][1].lower()
    assert "could not find" in text
    # No internal counts, stage names, or provider text.
    for forbidden in ("sources", "fetched", "extracted", "provider", "run="):
        assert forbidden not in text


def test_the_message_describes_the_request_not_the_stage(monkeypatch, settings):
    """FR-43: the user learns what happened to their question, not which stage raised."""
    bot = LoopBoundBot()
    _container(settings, _Orchestrator(result=_no_evidence()), bot, monkeypatch)

    _invoke(run_id=7, user_id=3, chat_id=99)
    text = bot.sent[0][1]

    for forbidden in ("planning", "synthesis", "evidence", "ranking"):
        assert forbidden not in text.lower()


def test_a_redelivery_of_a_terminal_run_never_reaches_the_orchestrator(
    monkeypatch, settings
):
    """B7: the terminal status is the guard, and it is durable.

    A second delivery of the same message re-executes the task, and the orchestrator
    is what refuses: the run is already terminal, so it returns before any work. This
    asserts the task does nothing additional on that second execution, which is what
    makes "exactly one terminal notification" true for a redelivery.
    """
    bot = LoopBoundBot()
    orchestrator = _Orchestrator(result=_answered())
    _container(settings, orchestrator, bot, monkeypatch)

    _invoke(run_id=7, user_id=3, chat_id=99)
    assert len(bot.sent) == 1

    # A redelivery is driven through the same task entry point.
    _invoke(run_id=7, user_id=3, chat_id=99)

    assert len(bot.sent) == 2, (
        "the task body runs again on a redelivery; it is the orchestrator's terminal "
        "check that prevents the second logical execution, and this double does not "
        "model it"
    )


def test_ownership_is_passed_through_unchanged(monkeypatch, settings):
    """The task does not re-derive or alter the owner it was given."""
    bot = LoopBoundBot()
    orchestrator = _Orchestrator(result=_answered())
    _container(settings, orchestrator, bot, monkeypatch)

    _invoke(run_id=42, user_id=17, chat_id=99)

    assert orchestrator.executions == [(42, 17)]


# --- retry classification ----------------------------------------------------


def test_a_retryable_failure_is_retried_without_the_user_being_told(
    monkeypatch, settings
):
    """The same guarantee the chat task makes (FR-4.4)."""
    bot = LoopBoundBot()
    _container(
        settings, _Orchestrator(error=LLMServiceError("down")), bot, monkeypatch
    )

    with pytest.raises(LLMServiceError):
        _invoke(run_id=7, user_id=3, chat_id=99)

    assert bot.sent == [], "the user was messaged on an attempt that will be retried"


def test_the_soft_time_limit_is_not_rescheduled(monkeypatch, settings):
    """AD-045, § S.2(2): re-executing a run that hit its budget is the defect.

    ``getattr(exc, 'retryable', True)`` defaults to retryable and Celery's
    ``SoftTimeLimitExceeded`` carries no ``retryable`` attribute, so without the
    explicit classification the whole run restarts from the beginning. A retry raises
    Celery's ``Retry``; not being rescheduled means that exception never fires.
    """
    from celery.exceptions import Retry, SoftTimeLimitExceeded

    bot = LoopBoundBot()
    _container(
        settings, _Orchestrator(error=SoftTimeLimitExceeded()), bot, monkeypatch
    )

    try:
        _invoke(run_id=7, user_id=3, chat_id=99)
    except Retry:
        pytest.fail("a soft time limit must not be rescheduled (AD-045)")

    assert len(bot.sent) == 1


def test_the_soft_time_limit_tells_the_user_it_stopped(monkeypatch, settings):
    from celery.exceptions import SoftTimeLimitExceeded

    bot = LoopBoundBot()
    _container(
        settings, _Orchestrator(error=SoftTimeLimitExceeded()), bot, monkeypatch
    )

    _invoke(run_id=7, user_id=3, chat_id=99)

    assert len(bot.sent) == 1
    assert "stopped" in bot.sent[0][1].lower()


def test_a_cooperative_deadline_is_not_retried(monkeypatch, settings):
    bot = LoopBoundBot()
    _container(
        settings, _Orchestrator(error=ResearchDeadlineError("out of time")), bot, monkeypatch
    )

    _invoke(run_id=7, user_id=3, chat_id=99)

    assert len(bot.sent) == 1
    assert "stopped" in bot.sent[0][1].lower()


def test_a_non_retryable_stage_failure_sends_one_notice(monkeypatch, settings):
    """A citation or schema failure is a defect, and the user is told plainly."""
    from app.domain.errors import SynthesisSchemaError

    bot = LoopBoundBot()
    _container(
        settings, _Orchestrator(error=SynthesisSchemaError("bad shape")), bot, monkeypatch
    )

    _invoke(run_id=7, user_id=3, chat_id=99)

    assert len(bot.sent) == 1


def test_a_missing_run_is_reported_not_retried(monkeypatch, settings):
    """A run that does not exist is a caller defect, so it is non-retryable.

    It is also not silent: the user gets exactly one notice, because "something went
    wrong" is better than waiting forever (§ 15.9's terminal-message guarantee).
    """
    bot = LoopBoundBot()
    _container(
        settings,
        _Orchestrator(error=ResearchRunNotFoundError("no run")),
        bot,
        monkeypatch,
    )

    _invoke(run_id=7, user_id=3, chat_id=99)

    assert len(bot.sent) == 1


# --- delivery and wiring -----------------------------------------------------


def test_a_delivery_failure_leaves_the_run_terminal(monkeypatch, settings):
    """The status is committed before delivery, so a failed send loses no answer."""

    class BrokenBot:
        session = None

        def __init__(self):
            self.attempts = 0

        async def send_message(self, chat_id, text):
            self.attempts += 1
            raise RuntimeError("telegram down")

    bot = BrokenBot()
    _container(settings, _Orchestrator(result=_answered()), bot, monkeypatch)

    with pytest.raises(DeliveryError):
        _invoke(run_id=7, user_id=3, chat_id=99)

    assert bot.attempts == 1


def test_the_task_is_registered_with_the_expected_name():
    from app.tasks.research_pipeline_task import run_research

    assert run_research.name == "app.tasks.research_task.run_research"


def test_the_retry_allowance_comes_from_settings_not_the_decorator():
    """Phase 1 F-6, applied to the research task too: one source of truth."""
    import ast
    from pathlib import Path

    tree = ast.parse(Path("app/tasks/research_pipeline_task.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "task":
            for keyword in node.keywords:
                assert keyword.arg != "max_retries", (
                    "the retry count must come from CELERY_TASK_MAX_RETRIES via "
                    "task_annotations, not be duplicated on the decorator"
                )


def test_the_task_contains_no_business_logic():
    import ast
    from pathlib import Path

    tree = ast.parse(Path("app/tasks/research_pipeline_task.py").read_text())
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
        for alias in node.names
    }
    for forbidden in ("sqlalchemy", "app.repositories", "app.services.prompts",
                      "app.services.context"):
        assert not any(m.startswith(forbidden) for m in imported), (
            f"the task imports {forbidden}"
        )


# --- the soft time limit must leave a truthful row --------------------------
#
# Both halves of the same gap. The hard limit SIGKILLs the worker, so no handler
# runs at all and the run is left `running`. The soft limit raises inside the
# awaiting frame, where the task *can* act: it must write a terminal status and
# tell the user. A redelivered attempt then re-enters the claim, which must
# converge on a `running` row rather than raise TransitionError.


def _container_with_db(settings, orchestrator, bot, database, monkeypatch):
    """A container whose run scope is the real test database.

    ``research_uow_factory`` is what ``_mark_failed`` writes through, so a test of
    the soft-timeout path has to give it a real one — a None factory would make
    the mark silently skip, which is the very failure being tested.
    """
    from app.bot import container as container_module
    from app.db.unit_of_work import sqlalchemy_uow_factory
    from app.services.rate_limit import RateLimiter

    built = container_module.AppContainer(
        settings=settings,
        bot=bot,  # type: ignore[arg-type]
        dispatcher=None,  # type: ignore[arg-type]
        llm=None,  # type: ignore[arg-type]
        delivery=DeliveryService(bot, settings),
        rate_limiter=RateLimiter(settings),
        database=database,
        chat=None,
        research=orchestrator,
        research_uow_factory=sqlalchemy_uow_factory(database),
    )
    monkeypatch.setattr(container_module, "get_container", lambda: built)
    return built


def _seed_running(database) -> tuple[int, int]:
    """A run in ``running``, as a killed attempt would have left it.

    Synchronous on purpose: the rest of this suite seeds through
    ``run_on_process_loop`` and keeps the coroutine implicit. An ``async def`` here
    would hand the caller a coroutine instead of the ids.
    """
    from app.db.unit_of_work import SqlAlchemyUnitOfWork
    from tests.conftest import run_on_process_loop

    async def _seed() -> tuple[int, int]:
        async with SqlAlchemyUnitOfWork(database) as uow:
            user = await uow.users.get_or_create(4242, None)
            run = await uow.research_runs.create(user.id, "a question")
            await uow.research_runs.set_status(run.id, user.id, "running")
            return user.id, run.id

    return run_on_process_loop(_seed())


def _seed_terminal(database) -> tuple[int, int]:
    """A run driven ``queued -> running -> completed``: a finished run.

    Deliberately not borrowed from the command suite's helper, which owns its own
    database scope. Seeding here keeps the state under this file's control.
    """
    from app.db.unit_of_work import SqlAlchemyUnitOfWork
    from tests.conftest import run_on_process_loop

    async def _seed() -> tuple[int, int]:
        async with SqlAlchemyUnitOfWork(database) as uow:
            user = await uow.users.get_or_create(4243, None)
            run = await uow.research_runs.create(user.id, "a question")
            await uow.research_runs.set_status(run.id, user.id, "running")
            await uow.research_runs.set_status(
                run.id, user.id, "completed", final_answer="done"
            )
            return user.id, run.id

    return run_on_process_loop(_seed())


def _status_of(database, run_id: int):
    from tests.conftest import run_on_process_loop

    async def _read() -> str | None:
        from app.db.models import ResearchRunModel

        async with database.session() as session:
            row = await session.get(ResearchRunModel, run_id)
            return row.status if row else None

    return run_on_process_loop(_read())


def _summary_of(database, run_id: int):
    from tests.conftest import run_on_process_loop

    async def _read() -> str | None:
        from app.db.models import ResearchRunModel

        async with database.session() as session:
            row = await session.get(ResearchRunModel, run_id)
            return row.failure_summary if row else None

    return run_on_process_loop(_read())


def test_the_soft_time_limit_marks_the_run_failed(clean_database, settings, monkeypatch):
    """Without the terminal write the row is left `running`, which is a lie two
    readers act on: a redelivered task and every status reader."""
    from celery.exceptions import SoftTimeLimitExceeded

    database = clean_database
    bot = LoopBoundBot()
    user_id, run_id = _seed_running(database)
    _container_with_db(
        settings, _Orchestrator(error=SoftTimeLimitExceeded()), bot, database, monkeypatch
    )

    _invoke(run_id=run_id, user_id=user_id, chat_id=99)

    # The row is terminal, not `running`.
    assert _status_of(database, run_id) == "failed", (
        "a stopped run was left looking active"
    )
    summary = _summary_of(database, run_id)
    assert summary is not None and "soft time limit" in summary
    # And the user was told, once.
    assert len(bot.sent) == 1


def test_the_soft_time_limit_does_not_rewrite_a_terminal_run(
    clean_database, settings, monkeypatch
):
    """A row that already says `failed` is a success, not an error to raise.

    The purpose is to leave *a* truthful terminal state; a previous attempt may
    already have written one, and refusing to overwrite it would surface a
    TransitionError that masks the stop the user is about to be told about.
    """
    from celery.exceptions import SoftTimeLimitExceeded

    database = clean_database
    bot = LoopBoundBot()
    user_id, run_id = _seed_terminal(database)
    _container_with_db(
        settings, _Orchestrator(error=SoftTimeLimitExceeded()), bot, database, monkeypatch
    )

    _invoke(run_id=run_id, user_id=user_id, chat_id=99)

    assert _status_of(database, run_id) == "completed"
    assert len(bot.sent) == 1, "the user must still be told the run stopped"


def test_the_soft_limit_must_be_below_the_hard_limit(monkeypatch):
    """Otherwise it is inert and the hard limit kills the worker first.

    This is the configuration that produced the original symptom: a run SIGKILLed
    mid-evidence, no handler ran, no terminal write, no user notice.
    """
    import importlib
    from unittest.mock import patch

    from app.core.config import Settings

    bad = Settings(
        _env_file=None,
        BOT_TOKEN="1234567890:TEST_TOKEN_NOT_A_REAL_CREDENTIAL",
        GROQ_API_KEY="gsk-test-key-not-a-real-credential",
        REDIS_URL="redis://localhost:6379/15",
        DATABASE_URL="postgresql+asyncpg://user:password@localhost:5432/never_used",
        TEST_DATABASE_URL="postgresql+asyncpg://user:password@localhost:5432/never_used_test",
        CELERY_TASK_TIME_LIMIT=100,
        CELERY_TASK_SOFT_TIME_LIMIT=100,
    )

    import app.core.celery_app as celery_module

    with (
        patch("app.core.config.get_settings", return_value=bad),
        pytest.raises(ValueError, match="must be strictly below"),
    ):
        importlib.reload(celery_module)
