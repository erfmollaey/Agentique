"""SR-9 command surface: routing, confirmation, and isolation from the LLM path.

Deletion is reachable only through an explicit confirmation argument, exactly as
``/reset`` is. These tests drive a real aiogram ``Dispatcher`` with real
``Update`` objects so filter resolution and handler ordering are exercised, and
they assert the thing that matters most about a destructive command: that it
never reaches the model.
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
from app.domain.errors import PersistenceError
from app.services.delivery import DeliveryService
from app.services.rate_limit import RateLimiter
from tests.fakes import user_turn

FAKE_TOKEN = "1234567890:AAsdfghjklQWERTYuiopZXCVBNMasdfghjkl"


@pytest.fixture
def sent(monkeypatch):
    """Record outbound Telegram calls without touching the network."""
    calls: list[dict] = []

    async def fake_call(self, bot, method, timeout=None):
        calls.append({"chat_id": method.chat_id, "text": method.text})
        return SimpleNamespace(message_id=1, text=method.text)

    monkeypatch.setattr(AiohttpSession, "__call__", fake_call, raising=True)
    return calls


@pytest.fixture
def enqueued(monkeypatch):
    """Record anything that reaches the Celery queue."""
    calls: list[dict] = []

    class FakeTask:
        @staticmethod
        def delay(**kwargs):
            calls.append(kwargs)

    import app.tasks.research_task as task_module

    monkeypatch.setattr(task_module, "process_research", FakeTask)
    return calls


@pytest.fixture
def dispatcher(settings, chat_service, fake_llm):
    dp = Dispatcher()
    bot = Bot(token=FAKE_TOKEN)
    container = AppContainer(
        settings=settings,
        bot=bot,
        dispatcher=dp,
        llm=fake_llm,
        delivery=DeliveryService(type("B", (), {"send_message": None}), settings),
        rate_limiter=RateLimiter(settings),
        database=None,
        chat=chat_service,
    )
    register_handlers(dp, container)
    return dp, bot, container


def _update(text, user_id=77, chat_id=-1001, message_id=1) -> Update:
    return Update(
        update_id=message_id,
        message=Message(
            message_id=message_id,
            date=datetime.now(tz=UTC),
            chat=Chat(id=chat_id, type="private"),
            from_user=(
                User(id=user_id, is_bot=False, first_name="T", username="tester")
                if user_id
                else None
            ),
            text=text,
        ),
    )


async def _feed(dp, bot, update) -> None:
    await dp.feed_update(bot, update)


async def _seed(chat_service, user_id, turns: int = 2) -> None:
    """Give a user a conversation with messages, through the real service.

    Awaited rather than driven through the process loop: these tests already
    run inside a loop, which is exactly the re-entrancy ``run_coroutine``
    refuses.
    """
    for i in range(turns):
        await chat_service.handle_message(user_turn(user_id, user_id * 10, f"q{i}", i + 1))


# --- Test 1: confirmation is required -------------------------------------

@pytest.mark.asyncio
async def test_delete_account_without_confirmation_deletes_nothing(
    dispatcher, enqueued, sent, chat_store
):
    dp, bot, _container = dispatcher
    await _seed(_container.chat, 77)
    before = (
        dict(chat_store.users_by_telegram),
        dict(chat_store.conversations),
        list(chat_store.messages),
    )

    await _feed(dp, bot, _update("/delete_account", message_id=100))

    assert "delete_account confirm" in sent[0]["text"], "no confirmation was requested"
    assert "cannot be undone" in sent[0]["text"]
    assert len(sent) == 1, "more than one reply was sent"
    assert (
        dict(chat_store.users_by_telegram),
        dict(chat_store.conversations),
        list(chat_store.messages),
    ) == before, "an unconfirmed delete removed data"


@pytest.mark.asyncio
async def test_an_arbitrary_argument_is_not_a_confirmation(dispatcher, enqueued, sent, chat_store):
    """Only the literal word confirms. 'yes', 'delete', '' must not."""
    dp, bot, _container = dispatcher
    await _seed(_container.chat, 77)

    for index, argument in enumerate(("yes", "delete", "please", "confirm now")):
        await _feed(dp, bot, _update(f"/delete_account {argument}", message_id=200 + index))

    assert 77 in chat_store.users_by_telegram
    assert chat_store.messages, "data was deleted on a non-confirmation"


@pytest.mark.asyncio
async def test_delete_account_can_be_cancelled(dispatcher, enqueued, sent, chat_store):
    """An explicit non-confirmation path, distinct from silence."""
    dp, bot, _container = dispatcher
    await _seed(_container.chat, 77)

    await _feed(dp, bot, _update("/delete_account cancel", message_id=300))

    assert "nothing was deleted" in sent[0]["text"].lower()
    assert 77 in chat_store.users_by_telegram
    assert chat_store.messages


@pytest.mark.asyncio
async def test_reset_does_not_delete_the_account(dispatcher, enqueued, sent, chat_store):
    """The two destructive commands must stay distinct (task § 6)."""
    dp, bot, _container = dispatcher
    await _seed(_container.chat, 77)

    await _feed(dp, bot, _update("/reset confirm", message_id=400))

    assert 77 in chat_store.users_by_telegram, "/reset deleted the user"
    assert chat_store.conversations, "/reset deleted the conversation"
    assert not chat_store.messages, "/reset should have cleared the messages"


# --- Test 2: confirmed deletion -------------------------------------------

@pytest.mark.asyncio
async def test_delete_account_confirm_removes_everything(dispatcher, enqueued, sent, chat_store):
    dp, bot, _container = dispatcher
    await _seed(_container.chat, 77)
    assert chat_store.users_by_telegram and chat_store.messages

    await _feed(dp, bot, _update("/delete_account confirm", message_id=500))

    assert chat_store.users_by_telegram == {}
    assert chat_store.conversations == {}
    assert chat_store.messages == []
    assert "deleted" in sent[0]["text"].lower()
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_delete_account_confirm_message_is_user_facing_not_technical(
    dispatcher, enqueued, sent
):
    dp, bot, _container = dispatcher
    await _feed(dp, bot, _update("/delete_account confirm", message_id=501))
    text = sent[0]["text"]
    for jargon in ("cascade", "foreign key", "transaction", "commit", "rows", "table"):
        assert jargon not in text.lower(), f"user-facing text leaked {jargon!r}"


# --- Test 3: isolation between users --------------------------------------

@pytest.mark.asyncio
async def test_deleting_one_user_leaves_another_untouched(
    dispatcher, enqueued, sent, chat_store
):
    dp, bot, container = dispatcher
    await _seed(container.chat, 77)
    await _seed(container.chat, 78)
    assert len(chat_store.conversations) == 2

    await _feed(dp, bot, _update("/delete_account confirm", user_id=77, message_id=600))

    assert list(chat_store.users_by_telegram) == [78], "the wrong user's data was removed"
    assert chat_store.messages, "the other user's messages were deleted"
    survivors = {m.conversation_id for m in chat_store.messages}
    owned = {c.id for c in chat_store.conversations.values()}
    assert survivors == owned, "surviving messages belong to a deleted conversation"


# --- Test 4: failure must not report success ------------------------------

@pytest.mark.asyncio
async def test_a_failed_deletion_reports_a_failure_not_a_success(
    dispatcher, enqueued, sent, chat_store, monkeypatch
):
    dp, bot, container = dispatcher
    await _seed(container.chat, 77)
    from tests.fakes import InMemoryUsers

    async def boom(self, _telegram_user_id):
        raise PersistenceError("relation \"users\" does not exist")

    monkeypatch.setattr(InMemoryUsers, "delete", boom, raising=True)

    await _feed(dp, bot, _update("/delete_account confirm", message_id=700))

    text = sent[0]["text"].lower()
    assert "deleted" not in text, "a failure was reported as a successful deletion"
    assert "could not save" in text or "went wrong" in text
    assert "relation" not in text and "traceback" not in text, "internal detail leaked"
    assert 77 in chat_store.users_by_telegram, "data was removed despite the failure"


# --- Test 5: routing — never the LLM path ---------------------------------

@pytest.mark.asyncio
async def test_the_deletion_command_never_enqueues_a_chat_turn(
    dispatcher, enqueued, sent
):
    """A destructive command must not consume a provider call (task § 13)."""
    dp, bot, _container = dispatcher

    await _feed(dp, bot, _update("/delete_account", message_id=800))
    await _feed(dp, bot, _update("/delete_account confirm", message_id=801))
    await _feed(dp, bot, _update("/delete_account cancel", message_id=802))

    assert enqueued == [], "a command was enqueued to the chat pipeline"
    assert len(sent) == 3, "each form should produce exactly one reply"


@pytest.mark.asyncio
async def test_the_deletion_command_is_documented_in_help(dispatcher, enqueued, sent):
    dp, bot, _container = dispatcher
    await _feed(dp, bot, _update("/help", message_id=900))
    assert "/delete_account" in sent[0]["text"]


@pytest.mark.asyncio
async def test_a_message_without_a_sender_cannot_delete_anything(
    dispatcher, enqueued, sent, chat_store
):
    """A missing from_user must not become a deletion request."""
    dp, bot, _container = dispatcher
    await _feed(dp, bot, _update("/delete_account confirm", user_id=None, message_id=901))
    assert chat_store.users_by_telegram == {}
    assert len(sent) == 1
