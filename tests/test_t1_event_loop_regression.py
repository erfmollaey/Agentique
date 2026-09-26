"""T-1 (MANDATORY) — Celery / asyncio event-loop regression.

Audit C-3: the original task created and closed a new event loop per
invocation while reusing a module-level ``aiogram.Bot``. aiogram caches its
``aiohttp.ClientSession`` on the ``AiohttpSession`` object, bound to whichever
loop was running when it was first created. Closing the loop per task left the
cached session bound to a dead loop, so the second task in a worker process
failed and every task leaked an unclosed session.

These tests assert the invariant the fix establishes: one long-lived loop per
process, never closed between tasks, with the loop-affine session created once
and reused. ``test_reproduces_original_defect`` demonstrates that the original
pattern violates the same invariant, so the suite would have caught C-3.

Phase 2 change: the request path is now the AI chat flow rather than query
decomposition, so ``_turn`` runs the chat service on the process loop and then
delivers — the same two steps the Celery task performs. The assertions are
unchanged: they are about loop identity, not about which service runs.
"""

from __future__ import annotations

import asyncio

import pytest

from app.domain.chat import ChatTurn
from app.infrastructure import asyncio_runtime
from app.infrastructure.asyncio_runtime import run_coroutine
from app.services.delivery import DeliveryService


def _turn(chat_id: int, text: str) -> ChatTurn:
    return ChatTurn(
        telegram_user_id=chat_id,
        chat_id=chat_id,
        text=text,
        telegram_message_id=chat_id,
    )


def _service(chat_service, delivery):
    """Run one turn the way the task does: async work on the loop, then send."""

    def run(chat_id: int, query: str) -> int:
        outcome = run_coroutine(chat_service.handle_message(_turn(chat_id, query)))
        return delivery.send(chat_id, outcome.text)

    return run


def test_t1_task_runs_repeatedly_in_one_process(fake_bot, chat_service, settings):
    """T-1: the request path executes successfully more than once.

    Three sequential invocations, as a prefork worker performs them, must all
    succeed against the same loop-affine session.
    """
    delivery = DeliveryService(fake_bot, settings)
    run = _service(chat_service, delivery)

    for i in range(1, 4):
        sent = run(chat_id=100 + i, query=f"question {i}")
        assert sent == 1, f"invocation {i} sent {sent} messages"

    assert len(fake_bot.sent) == 3


def test_t1_same_loop_is_reused_across_invocations(fake_bot, chat_service, settings):
    """The loop must be identical for every invocation, not recreated."""
    delivery = DeliveryService(fake_bot, settings)
    run = _service(chat_service, delivery)

    run(chat_id=1, query="first")
    run(chat_id=2, query="second")
    run(chat_id=3, query="third")

    assert len({id(loop) for loop in fake_bot.loops}) == 1, (
        "each invocation used a different event loop; the session would be "
        "reused across closed loops"
    )


def test_t1_loop_is_not_closed_between_invocations(fake_bot, chat_service, settings):
    """The process loop must stay open for the process lifetime."""
    delivery = DeliveryService(fake_bot, settings)
    run = _service(chat_service, delivery)

    run(chat_id=1, query="first")
    loop = asyncio_runtime.get_event_loop()
    assert not loop.is_closed()

    run(chat_id=2, query="second")
    assert not loop.is_closed(), "loop was closed while the process was still running"


def test_t1_session_is_created_once_and_reused(fake_bot, chat_service, settings):
    """The loop-affine session is bound on first use and never re-bound."""
    delivery = DeliveryService(fake_bot, settings)
    run = _service(chat_service, delivery)

    run(chat_id=1, query="first")
    bound = fake_bot.session.bound_to
    assert bound is not None

    run(chat_id=2, query="second")
    run(chat_id=3, query="third")
    assert fake_bot.session.bound_to is bound


def test_t1_reproduces_original_defect(fake_bot):
    """Demonstrates the original per-task-loop pattern is genuinely broken.

    If this test ever passes, the regression suite has lost its teeth. It
    encodes the old behaviour — create a loop, run, close — and asserts the
    second invocation fails, which is precisely the reported symptom.
    """
    text = "x"

    def old_pattern(chat_id: int) -> None:
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(fake_bot.send_message(chat_id=chat_id, text=text))
        finally:
            loop.close()

    old_pattern(chat_id=1)  # first task: works, binds the session
    with pytest.raises(RuntimeError, match="bound to a different event loop"):
        old_pattern(chat_id=2)  # second task: fails, exactly as reported


def test_t1_shutdown_closes_session_then_loop(fake_bot, settings):
    """Shutdown releases the session before the loop is closed (FR-4.2)."""
    delivery = DeliveryService(fake_bot, settings)
    delivery.send(chat_id=1, text="hello")

    delivery.close()
    assert fake_bot.session.closed is True

    asyncio_runtime.close_event_loop()
    assert asyncio_runtime.get_event_loop() is not None  # a fresh loop on next use


def test_t1_get_event_loop_is_idempotent():
    first = asyncio_runtime.get_event_loop()
    second = asyncio_runtime.get_event_loop()
    assert first is second
    assert not first.is_closed()


def test_t1_run_coroutine_reuses_the_process_loop():
    async def work() -> int:
        return id(asyncio.get_running_loop())

    first = asyncio_runtime.run_coroutine(work())
    second = asyncio_runtime.run_coroutine(work())
    assert first == second
