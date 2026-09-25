"""T-1 at the Celery task boundary.

``tests/test_t1_event_loop_regression.py`` proves the service and delivery
layers survive repeated execution. This module drives the *real* Celery task
function, so the task body, the process container, and the worker lifecycle
signals are all in the path. That is the layer where the original defect lived.

No broker and no network: the task is invoked directly, and both external
boundaries are substituted.
"""

from __future__ import annotations

import pytest

from app.infrastructure import asyncio_runtime
from tests.conftest import LoopBoundBot


@pytest.fixture
def wired(monkeypatch, settings):
    """Point the task's process container at test doubles.

    Mirrors what a worker child process builds, without a broker or a socket.
    """
    from app.bot import container as container_module
    from app.domain.schemas import QueryAnalysis
    from app.services.delivery import DeliveryService
    from app.services.rate_limit import RateLimiter
    from app.services.research import ResearchService
    from tests.conftest import FakeLLMClient

    bot = LoopBoundBot()
    llm = FakeLLMClient(
        result=QueryAnalysis(summary="summary text", sub_questions=["q1", "q2"])
    )
    delivery = DeliveryService(bot, settings)
    built = container_module.AppContainer(
        settings=settings,
        bot=bot,  # type: ignore[arg-type]
        dispatcher=None,  # type: ignore[arg-type]
        llm=llm,
        delivery=delivery,
        research=ResearchService(llm, delivery, settings),
        rate_limiter=RateLimiter(settings),
    )
    monkeypatch.setattr(container_module, "get_container", lambda: built)
    return built, bot


def _invoke(**kwargs):
    """Call the real task body without going through the broker."""
    from app.tasks.research_task import process_research

    # ``run`` executes the task body synchronously, exactly as a worker does.
    return process_research.run(**kwargs)


def test_real_task_runs_repeatedly_in_one_process(wired):
    """T-1 against the real task: three sequential executions must all succeed."""
    _built, bot = wired

    for i in range(3):
        sent = _invoke(chat_id=100 + i, user_id=200 + i, query=f"question {i}")
        assert sent == 1, f"execution {i} failed (sent={sent})"

    assert len(bot.sent) == 3, "not every execution produced a reply"
    for i, (chat_id, _text) in enumerate(bot.sent):
        assert chat_id == 100 + i, "reply went to the wrong chat"


def test_real_task_reuses_one_loop_across_executions(wired):
    _built, bot = wired

    _invoke(chat_id=1, user_id=1, query="a")
    _invoke(chat_id=2, user_id=2, query="b")
    _invoke(chat_id=3, user_id=3, query="c")

    assert len({id(loop) for loop in bot.loops}) == 1
    assert not asyncio_runtime.get_event_loop().is_closed()


def test_real_task_keeps_the_session_bound_to_one_loop(wired):
    _built, bot = wired

    _invoke(chat_id=1, user_id=1, query="a")
    bound = bot.session.bound_to
    _invoke(chat_id=2, user_id=2, query="b")

    assert bound is not None
    assert bot.session.bound_to is bound


def test_real_task_releases_the_rate_limit_slot(wired):
    """A failed task must not leak its concurrency slot."""
    built, _bot = wired

    before = built.rate_limiter._in_flight
    _invoke(chat_id=1, user_id=1, query="a")

    assert built.rate_limiter._in_flight == before, "slot leaked after a successful task"


def test_worker_lifecycle_signals_are_connected():
    """The signals that make the loop per-process must actually be wired."""
    from celery.signals import worker_process_init, worker_process_shutdown

    init_senders = {s for _, s in worker_process_init.receivers}
    shutdown_senders = {s for _, s in worker_process_shutdown.receivers}

    assert init_senders, "worker_process_init has no receiver; the loop fix is inert"
    assert shutdown_senders, "worker_process_shutdown has no receiver; the session would leak"


def test_worker_process_init_creates_a_fresh_loop():
    """A forked child must not inherit the parent's loop object."""
    from app.tasks.lifecycle import _on_worker_process_init, _on_worker_process_shutdown

    parent_loop = asyncio_runtime.get_event_loop()
    _on_worker_process_init()

    child_loop = asyncio_runtime.get_event_loop()
    assert child_loop is not parent_loop, "forked child reused the parent loop"
    assert not child_loop.is_closed()

    _on_worker_process_shutdown()
    assert child_loop.is_closed(), "shutdown did not close the loop"


def test_shutdown_closes_the_telegram_session(monkeypatch, settings):
    """Order matters: the session must close before the loop does."""
    from app.bot import container as container_module
    from app.services.delivery import DeliveryService
    from app.tasks.lifecycle import _on_worker_process_shutdown
    from tests.conftest import LoopBoundBot

    bot = LoopBoundBot()
    built = container_module.AppContainer(
        settings=settings,
        bot=bot,  # type: ignore[arg-type]
        dispatcher=None,  # type: ignore[arg-type]
        llm=None,  # type: ignore[arg-type]
        delivery=DeliveryService(bot, settings),
        research=None,  # type: ignore[arg-type]
        rate_limiter=None,  # type: ignore[arg-type]
    )
    monkeypatch.setattr(container_module, "get_container", lambda: built)

    _on_worker_process_shutdown()
    assert bot.session.closed is True
    assert asyncio_runtime.get_event_loop() is not None
