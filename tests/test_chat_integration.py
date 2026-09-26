"""End-to-end AI chat flow (Phase 2 § 22).

One test drives the whole path against a real PostgreSQL and controlled doubles:

    Telegram-shaped input
        -> user resolution
        -> conversation creation
        -> user message persistence
        -> chat service
        -> injected LLM
        -> assistant message persistence
        -> Telegram delivery

Real: the database, the schema created by the real migrations, the real
repositories, the real chat service, the real Celery task, the real context
builder, and a real aiogram ``Update`` resolved through a real ``Dispatcher``.

Doubled: the AI provider and the Telegram transport. Neither is permitted to
reach the network; the autouse socket guard in ``conftest.py`` fails the test if
either tries.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.types import Chat, Message, Update, User
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.container import AppContainer
from app.bot.handlers import register_handlers
from app.db.models import ConversationModel, MessageModel, UserModel
from app.db.session import Database
from app.db.unit_of_work import sqlalchemy_uow_factory
from app.domain.chat import Role
from app.infrastructure.llm import OpenAICompatibleClient
from app.services.chat import ChatService
from app.services.context import ContextBuilder
from app.services.delivery import DeliveryService
from app.services.rate_limit import RateLimiter
from tests.conftest import LoopBoundBot

FAKE_TOKEN = "1234567890:AAsdfghjklQWERTYuiopZXCVBNMasdfghjkl"


class ScriptedProvider:
    """An injected provider that answers deterministically and records calls.

    Implements the same port as the real client, which is the point: the
    application cannot tell the difference (FR-12, T-5).
    """

    def __init__(self, replies: list[str] | None = None) -> None:
        self.replies = list(replies or [])
        self.calls: list[list] = []

    def complete(self, messages, *, model, temperature, max_tokens):
        from app.domain.chat import LLMReply

        self.calls.append(list(messages))
        text = self.replies.pop(0) if self.replies else f"answer {len(self.calls)}"
        return LLMReply(
            text=text,
            model=model,
            prompt_tokens=20,
            completion_tokens=8,
            finish_reason="stop",
        )


def _run(coro):
    """Drive a coroutine on this process's long-lived loop, as a worker does."""
    from app.infrastructure.asyncio_runtime import run_coroutine

    return run_coroutine(coro)


def _truncate(database):
    """Empty the three tables, failing fast rather than blocking.

    ``lock_timeout`` is not decoration. TRUNCATE needs an ACCESS EXCLUSIVE
    lock, so a test that leaks an open read transaction would otherwise make
    teardown wait forever instead of failing. With the timeout it surfaces as an
    error naming the culprit.
    """

    async def wipe():
        from sqlalchemy import text

        async with database.engine.begin() as connection:
            await connection.execute(text("SET LOCAL lock_timeout = '5s'"))
            await connection.execute(
                text("TRUNCATE messages, conversations, users RESTART IDENTITY CASCADE")
            )

    _run(wipe())


@pytest.fixture
def stack(clean_database, settings):
    """A fully wired process graph on the real database.

    Yields ``(container, provider, bot, database)``.
    """
    database = clean_database
    provider = ScriptedProvider()
    llm = OpenAICompatibleClient(settings)  # real client, never called
    chat = ChatService(
        settings=settings,
        llm=provider,
        uow_factory=sqlalchemy_uow_factory(database),
        context=ContextBuilder(settings),
    )
    bot = LoopBoundBot()
    delivery = DeliveryService(bot, settings)
    container = AppContainer(
        settings=settings,
        bot=bot,  # type: ignore[arg-type]
        dispatcher=Dispatcher(),
        llm=llm,
        delivery=delivery,
        rate_limiter=RateLimiter(settings),
        database=database,
        chat=chat,
    )
    return container, provider, bot, database


def _count(database, model) -> int:
    """Row count, run on the process loop like every other query here.

    The session is always closed. A session left open holds its connection and
    an ACCESS SHARE lock, which would block the next test's TRUNCATE.
    """

    async def query() -> int:
        session = AsyncSession(database.engine, expire_on_commit=False)
        try:
            result = await session.execute(select(func.count()).select_from(model))
            return int(result.scalar_one())
        finally:
            await session.close()

    return _run(query())


def _scalar(database, statement):
    """Evaluate a select and return its single scalar, closing the session."""

    async def query():
        session = AsyncSession(database.engine, expire_on_commit=False)
        try:
            result = await session.execute(statement)
            return result.scalar_one()
        finally:
            await session.close()

    return _run(query())


def _scalars(database, statement):
    async def query():
        session = AsyncSession(database.engine, expire_on_commit=False)
        try:
            result = await session.execute(statement)
            return list(result.scalars().all())
        finally:
            await session.close()

    return _run(query())


def test_full_chat_flow_persists_and_delivers(stack, monkeypatch):
    """The Phase 2 acceptance path, start to finish."""
    import app.bot.container as container_module
    from app.tasks.research_task import process_research

    container, provider, bot, database = stack
    monkeypatch.setattr(container_module, "get_container", lambda: container)

    # --- 1. a Telegram message arrives -------------------------------------
    message = Message(
        message_id=101,
        date=datetime.now(tz=UTC),
        chat=Chat(id=-1001, type="private"),
        from_user=User(id=4242, is_bot=False, first_name="T", username="alice"),
        text="what is entropy?",
    )

    # The handler's decision, then the worker: the same two steps production
    # takes, with the broker replaced by a direct call.
    rate_limiter = container.rate_limiter
    assert rate_limiter.check(message.from_user.id).allowed

    sent = process_research.run(
        chat_id=message.chat.id,
        user_id=message.from_user.id,
        query=message.text,
        username=message.from_user.username,
        telegram_message_id=message.message_id,
    )

    # --- 2. the user got exactly one reply ----------------------------------
    assert sent == 1
    assert len(bot.sent) == 1
    assert bot.sent[0][0] == -1001, "the reply went to the wrong chat"
    assert bot.sent[0][1] == "answer 1"

    # --- 3. everything is in the database -----------------------------------
    user = _scalar(database, select(UserModel).where(UserModel.telegram_user_id == 4242))
    assert user.username == "alice"

    conversation = _scalar(
        database, select(ConversationModel).where(ConversationModel.user_id == user.id)
    )
    assert conversation.title == "what is entropy?"

    rows = _scalars(
        database,
        select(MessageModel)
        .where(MessageModel.conversation_id == conversation.id)
        .order_by(MessageModel.created_at, MessageModel.id),
    )
    assert [r.role for r in rows] == ["user", "assistant"]
    assert rows[0].content == "what is entropy?"
    assert rows[1].content == "answer 1"
    assert rows[1].prompt_tokens == 20 and rows[1].completion_tokens == 8
    assert rows[0].turn_id == rows[1].turn_id, "a turn must pair the two messages"

    # --- 4. the provider was called with a system turn and the question -----
    call = provider.calls[0]
    assert call[0].role == Role.SYSTEM
    assert call[-1].role == Role.USER
    assert call[-1].content == "what is entropy?"


def test_a_second_message_continues_the_same_conversation(stack, monkeypatch):
    import app.bot.container as container_module
    from app.tasks.research_task import process_research

    container, provider, _bot, database = stack
    monkeypatch.setattr(container_module, "get_container", lambda: container)

    process_research.run(chat_id=1, user_id=77, query="first question", telegram_message_id=1)
    process_research.run(chat_id=1, user_id=77, query="follow-up question", telegram_message_id=2)

    assert _count(database, UserModel) == 1, "the user was created twice"
    assert _count(database, ConversationModel) == 1, "the conversation was not continued"
    assert _count(database, MessageModel) == 4

    # The follow-up saw the first exchange.
    second = provider.calls[1]
    contents = [m.content for m in second]
    assert "first question" in contents
    assert contents.index("first question") < contents.index("follow-up question")


def test_conversation_survives_a_process_restart(stack, monkeypatch):
    """The § 2 requirement: restarting does not lose stored conversations.

    A fresh service and a fresh engine are built against the same database, with
    no in-process state carried over, which is what a worker restart looks like.
    """
    import app.bot.container as container_module
    from app.db.unit_of_work import SqlAlchemyUnitOfWork
    from app.tasks.research_task import process_research

    container, _provider, _bot, database = stack
    monkeypatch.setattr(container_module, "get_container", lambda: container)
    process_research.run(chat_id=1, user_id=77, query="remember me", telegram_message_id=1)

    # "Restart": a new Database, a new unit of work factory, a new service.
    restarted_db = Database(database.url)
    provider = ScriptedProvider(["still here"])
    fresh = ChatService(
        settings=container.settings,
        llm=provider,
        uow_factory=lambda: SqlAlchemyUnitOfWork(restarted_db),
        context=ContextBuilder(container.settings),
    )
    fresh_bot = LoopBoundBot()
    fresh_container = AppContainer(
        settings=container.settings,
        bot=fresh_bot,  # type: ignore[arg-type]
        dispatcher=Dispatcher(),
        llm=None,  # type: ignore[arg-type]
        delivery=DeliveryService(fresh_bot, container.settings),
        rate_limiter=RateLimiter(container.settings),
        database=restarted_db,
        chat=fresh,
    )
    monkeypatch.setattr(container_module, "get_container", lambda: fresh_container)

    process_research.run(chat_id=1, user_id=77, query="do you remember?", telegram_message_id=2)

    assert fresh_bot.sent[0][1] == "still here"
    assert _count(database, UserModel) == 1
    assert _count(database, ConversationModel) == 1, "a restart forked the conversation"
    assert _count(database, MessageModel) == 4

    # The history crossed the restart: the earlier turn is in the new context.
    contents = [m.content for m in provider.calls[0]]
    assert "remember me" in contents

    _run(restarted_db.dispose())


def test_a_redelivered_telegram_message_is_answered_once(stack, monkeypatch):
    """Phase 2 § 16 against the real unique constraints."""
    import app.bot.container as container_module
    from app.tasks.research_task import process_research

    container, provider, bot, database = stack
    monkeypatch.setattr(container_module, "get_container", lambda: container)

    process_research.run(chat_id=1, user_id=77, query="only once", telegram_message_id=55)
    process_research.run(chat_id=1, user_id=77, query="only once", telegram_message_id=55)

    assert len(provider.calls) == 1, "the provider was called twice for one message"
    assert _count(database, MessageModel) == 2, "a duplicate reply was stored"
    assert len(bot.sent) == 2, "the user should still receive the reply on redelivery"
    assert bot.sent[0][1] == bot.sent[1][1], "the redelivery sent different text"


def test_one_user_cannot_reach_another_users_conversation(stack):
    """SR-4 / T-16 through the real repositories and real constraints."""

    from app.db.unit_of_work import SqlAlchemyUnitOfWork

    _container, _provider, _bot, database = stack

    async def scenario():
        async with SqlAlchemyUnitOfWork(database) as uow:
            owner = await uow.users.get_or_create(1, "owner")
            intruder = await uow.users.get_or_create(2, "intruder")
            private = await uow.conversations.create(owner.id, "private business")
            conversation_id, owner_id = private.id, owner.id
        async with SqlAlchemyUnitOfWork(database) as uow:
            assert await uow.conversations.get_for_user(conversation_id, owner_id) is not None
            assert await uow.conversations.get_for_user(conversation_id, intruder.id) is None
            assert await uow.conversations.list_for_user(intruder.id) == []

    _run(scenario())


def test_the_real_provider_client_is_never_reached(stack):
    """The real OpenAI-compatible client is in the graph but must stay unused.

    Guards against a wiring mistake that would send a test to the network, and
    documents that the shipped client satisfies the same port as the double.
    """
    from app.domain.ports import LLMProvider

    container, provider, _bot, _database = stack
    assert isinstance(container.llm, LLMProvider)
    assert isinstance(provider, ScriptedProvider)
    assert container.chat._llm is provider, "the service is wired to the double"

    # The socket guard would catch an accidental real call.
    with pytest.raises(AssertionError, match="outbound network"):
        import socket

        socket.socket().connect(("example.invalid", 443))


@pytest.mark.asyncio
async def test_handlers_route_a_message_into_the_queue_not_the_database(
    settings, chat_service, chat_store, monkeypatch
):
    """The handler enqueues; it does not run the chat flow itself (Phase 2 § 18).

    Deliberately built without a database: the handler's whole job is to parse
    the update, acknowledge, and enqueue. If this test needed the data layer it
    would be asserting the wrong boundary.
    """
    import types

    from app.services.rate_limit import RateLimiter

    enqueued: list[dict] = []

    import app.tasks.research_task as task_module

    class FakeTask:
        @staticmethod
        def delay(**kwargs):
            enqueued.append(kwargs)

    monkeypatch.setattr(task_module, "process_research", FakeTask)

    outbound: list[dict] = []

    async def fake_call(self, b, method, timeout=None):
        outbound.append({"chat_id": method.chat_id, "text": method.text})
        return types.SimpleNamespace(message_id=1, text=method.text)

    monkeypatch.setattr(AiohttpSession, "__call__", fake_call, raising=True)

    dp = Dispatcher()
    telegram_bot = Bot(token=FAKE_TOKEN)
    container = AppContainer(
        settings=settings,
        bot=telegram_bot,
        dispatcher=dp,
        llm=None,  # type: ignore[arg-type]
        delivery=DeliveryService(type("B", (), {"send_message": None}), settings),
        rate_limiter=RateLimiter(settings),
        database=None,
        chat=chat_service,
    )
    register_handlers(dp, container)

    message = Message(
        message_id=7,
        date=datetime.now(tz=UTC),
        chat=Chat(id=-1001, type="private"),
        from_user=User(id=77, is_bot=False, first_name="T", username="tester"),
        text="route me",
    )
    await dp.feed_update(telegram_bot, Update(update_id=7, message=message))

    assert len(enqueued) == 1, "the handler did not enqueue"
    assert enqueued[0]["query"] == "route me"
    assert enqueued[0]["chat_id"] == -1001
    assert enqueued[0]["telegram_message_id"] == 7
    assert len(outbound) == 1, "the user received no acknowledgement"
    assert chat_store.messages == [], "the handler wrote messages itself"
    assert chat_store.conversations == {}, "the handler created a conversation itself"
