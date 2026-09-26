"""Celery task for the chat flow (Phase 2 § 18, FR-4.4).

Drives the real task function with ``.run()``, so the body, the container
resolution, and the retry policy are all in the path — the layer where a defect
would actually be observed. No broker and no network.
"""

from __future__ import annotations

import pytest

from app.domain.errors import (
    DeliveryError,
    LLMAuthenticationError,
    LLMResponseError,
    LLMServiceError,
    LLMTimeoutError,
)
from app.services.delivery import DeliveryService
from tests.conftest import FakeLLMClient, LoopBoundBot


def _build(settings, chat_service, bot, monkeypatch):
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
        chat=chat_service,
    )
    monkeypatch.setattr(container_module, "get_container", lambda: built)
    return built


def _invoke(**kwargs):
    from app.tasks.research_task import process_research

    return process_research.run(**kwargs)


@pytest.fixture
def worker(monkeypatch, settings, chat_service):
    bot = LoopBoundBot()
    built = _build(settings, chat_service, bot, monkeypatch)
    return built, bot


# --- Happy path ------------------------------------------------------------

def test_a_turn_produces_exactly_one_reply(worker):
    _built, bot = worker
    assert _invoke(chat_id=1, user_id=1, query="hello", telegram_message_id=1) == 1
    assert len(bot.sent) == 1
    assert bot.sent[0][1] == "fake reply"


def test_the_reply_goes_to_the_originating_chat(worker):
    """H-1: chat_id is the destination, user_id the sender."""
    _built, bot = worker
    _invoke(chat_id=-100999, user_id=555, query="hi", telegram_message_id=1)
    assert bot.sent[0][0] == -100999


def test_the_username_reaches_the_user_record(worker, chat_store):
    _built, _bot = worker
    _invoke(chat_id=1, user_id=42, query="hi", username="alice", telegram_message_id=1)
    assert chat_store.users_by_telegram[42].username == "alice"


def test_consecutive_turns_accumulate_in_one_conversation(worker, chat_store):
    _built, _bot = worker
    for i in range(3):
        _invoke(chat_id=1, user_id=1, query=f"q{i}", telegram_message_id=i + 1)
    assert len(chat_store.conversations) == 1
    assert len(chat_store.messages) == 6


# --- Retry policy (FR-4.4) ------------------------------------------------

def test_a_retryable_failure_is_retried_and_the_user_is_not_notified_yet(
    monkeypatch, settings, chat_store
):
    from app.services.chat import ChatService
    from app.services.context import ContextBuilder
    from tests.fakes import in_memory_uow_factory

    bot = LoopBoundBot()
    factory, _ = in_memory_uow_factory(chat_store)
    service = ChatService(
        settings=settings,
        llm=FakeLLMClient(error=LLMTimeoutError("slow")),
        uow_factory=factory,
        context=ContextBuilder(settings),
    )
    _build(settings, service, bot, monkeypatch)

    with pytest.raises(LLMTimeoutError):
        _invoke(chat_id=1, user_id=1, query="q", telegram_message_id=1)

    assert bot.sent == [], "the user was messaged on an attempt that will be retried"


def test_a_non_retryable_failure_sends_exactly_one_notice(
    monkeypatch, settings, chat_store
):
    from app.services.chat import ChatService
    from app.services.context import ContextBuilder
    from tests.fakes import in_memory_uow_factory

    bot = LoopBoundBot()
    factory, _ = in_memory_uow_factory(chat_store)
    service = ChatService(
        settings=settings,
        llm=FakeLLMClient(error=LLMAuthenticationError("bad key")),
        uow_factory=factory,
        context=ContextBuilder(settings),
    )
    _build(settings, service, bot, monkeypatch)

    with pytest.raises(LLMAuthenticationError):
        _invoke(chat_id=1, user_id=1, query="q", telegram_message_id=1)

    assert len(bot.sent) == 1
    assert "unavailable" in bot.sent[0][1]
    assert "bad key" not in bot.sent[0][1]


def test_a_malformed_response_is_terminal_and_explained(
    monkeypatch, settings, chat_store
):
    from app.services.chat import ChatService
    from app.services.context import ContextBuilder
    from tests.fakes import in_memory_uow_factory

    bot = LoopBoundBot()
    factory, _ = in_memory_uow_factory(chat_store)
    service = ChatService(
        settings=settings,
        llm=FakeLLMClient(error=LLMResponseError("malformed")),
        uow_factory=factory,
        context=ContextBuilder(settings),
    )
    _build(settings, service, bot, monkeypatch)

    with pytest.raises(LLMResponseError):
        _invoke(chat_id=1, user_id=1, query="q", telegram_message_id=1)
    assert len(bot.sent) == 1
    assert "invalid" in bot.sent[0][1]


def test_a_user_message_is_never_deleted_by_a_failure(
    monkeypatch, settings, chat_store
):
    """Phase 2 § 15: the user's text survives a provider failure."""
    from app.services.chat import ChatService
    from app.services.context import ContextBuilder
    from tests.fakes import in_memory_uow_factory

    bot = LoopBoundBot()
    factory, _ = in_memory_uow_factory(chat_store)
    service = ChatService(
        settings=settings,
        llm=FakeLLMClient(error=LLMServiceError("down")),
        uow_factory=factory,
        context=ContextBuilder(settings),
    )
    _build(settings, service, bot, monkeypatch)

    with pytest.raises(LLMServiceError):
        _invoke(chat_id=1, user_id=1, query="keep this", telegram_message_id=1)

    assert [m.content for m in chat_store.messages] == ["keep this"]


# --- Rate limiter slot (Phase 1 FR-8.2) -----------------------------------

def test_the_concurrency_slot_is_released_after_success(worker):
    built, _bot = worker
    before = built.rate_limiter._in_flight
    _invoke(chat_id=1, user_id=1, query="q", telegram_message_id=1)
    assert built.rate_limiter._in_flight == before


def test_the_concurrency_slot_is_released_after_a_failure(
    monkeypatch, settings, chat_store
):
    from app.services.chat import ChatService
    from app.services.context import ContextBuilder
    from tests.fakes import in_memory_uow_factory

    bot = LoopBoundBot()
    factory, _ = in_memory_uow_factory(chat_store)
    service = ChatService(
        settings=settings,
        llm=FakeLLMClient(error=LLMServiceError("down")),
        uow_factory=factory,
        context=ContextBuilder(settings),
    )
    built = _build(settings, service, bot, monkeypatch)
    # Take a slot so the release is observable.
    built.rate_limiter.check(1)
    held = built.rate_limiter._in_flight

    with pytest.raises(LLMServiceError):
        _invoke(chat_id=1, user_id=1, query="q", telegram_message_id=1)

    assert built.rate_limiter._in_flight == held - 1, "a failed task leaked its slot"


# --- Delivery failure ------------------------------------------------------

def test_a_delivery_failure_is_terminal_and_does_not_duplicate_the_reply(
    monkeypatch, settings, chat_service, chat_store
):
    """Phase 2 § 15: a retried delivery must not create a second reply."""
    from app.bot import container as container_module
    from app.services.rate_limit import RateLimiter

    class BrokenBot:
        session = None

        def __init__(self):
            self.attempts = 0

        async def send_message(self, chat_id, text):
            self.attempts += 1
            raise RuntimeError("telegram down")

    bot = BrokenBot()
    built = container_module.AppContainer(
        settings=settings,
        bot=bot,  # type: ignore[arg-type]
        dispatcher=None,  # type: ignore[arg-type]
        llm=None,  # type: ignore[arg-type]
        delivery=DeliveryService(bot, settings),
        rate_limiter=RateLimiter(settings),
        database=None,
        chat=chat_service,
    )
    monkeypatch.setattr(container_module, "get_container", lambda: built)

    with pytest.raises(DeliveryError):
        _invoke(chat_id=1, user_id=1, query="q", telegram_message_id=1)

    stored = [m for m in chat_store.messages if m.role.value == "assistant"]
    assert len(stored) == 1, "the reply must be stored even though delivery failed"

    # A redelivery of the same update re-sends the stored reply rather than
    # generating another one. Delivery still fails, because the transport is
    # still broken; what matters is that nothing new was written.
    with pytest.raises(DeliveryError):
        _invoke(chat_id=1, user_id=1, query="q", telegram_message_id=1)

    stored = [m for m in chat_store.messages if m.role.value == "assistant"]
    assert len(stored) == 1, "a redelivery created a second stored reply"
    assert len(chat_store.messages) == 2, "a redelivery created extra rows"
    # DeliveryError is retryable, so the task reschedules instead of sending a
    # failure notice on an attempt that will be retried (FR-4.4). Two attempts
    # total: one send per invocation, and no premature notice.
    assert bot.attempts == 2


# --- Task wiring -----------------------------------------------------------

def test_the_task_is_registered_with_the_expected_name():
    from app.tasks.research_task import process_research

    assert process_research.name == "app.tasks.research_task.process_research"


def test_the_retry_allowance_comes_from_settings_not_the_decorator():
    """Phase 1 F-6: one source of truth for the retry count."""
    import ast
    from pathlib import Path

    tree = ast.parse(Path("app/tasks/research_task.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "task":
            for keyword in node.keywords:
                assert keyword.arg != "max_retries", (
                    "the retry count must come from CELERY_TASK_MAX_RETRIES via "
                    "task_annotations, not be duplicated on the decorator"
                )


def test_the_task_contains_no_business_logic():
    """TR-2: the task orchestrates; it does not decide."""
    import ast
    from pathlib import Path

    tree = ast.parse(Path("app/tasks/research_task.py").read_text())
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
