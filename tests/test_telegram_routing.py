"""Handler routing through a real aiogram Dispatcher.

The other handler tests invoke callbacks directly. This module builds real
``Update``/``Message`` objects and resolves them through aiogram's actual filter
chain, so it proves the *routing* is correct: ``/start`` wins over the
unknown-command catch-all, non-text never reaches the text handler, and a
photo is never enqueued.

Only the network boundary is stubbed — ``Bot.send_message`` — so no request
reaches Telegram.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.types import Chat, Message, PhotoSize, Update, User

from app.bot.container import AppContainer
from app.bot.handlers import register_handlers

FAKE_TOKEN = "1234567890:AAsdfghjklQWERTYuiopZXCVBNMasdfghjkl"


@pytest.fixture
def sent(monkeypatch):
    """Record outbound Telegram calls without touching the network.

    ``Message.answer`` reaches Telegram through ``AiohttpSession.__call__``,
    not ``Bot.send_message``, so the session is the correct boundary to stub.
    """
    calls: list[dict] = []

    async def fake_call(self, bot, method, timeout=None):
        calls.append(
            {"chat_id": method.chat_id, "text": method.text, "method": type(method).__name__}
        )
        return SimpleNamespace(message_id=1, text=method.text)

    monkeypatch.setattr(AiohttpSession, "__call__", fake_call, raising=True)
    return calls


@pytest.fixture
def enqueued(monkeypatch):
    """Record enqueued tasks without needing a broker."""
    calls: list[dict] = []

    class FakeTask:
        @staticmethod
        def delay(**kwargs):
            calls.append(kwargs)

    import app.tasks.research_task as task_module

    monkeypatch.setattr(task_module, "process_research", FakeTask)
    return calls


@pytest.fixture
def dispatcher(settings, fake_llm, chat_service):
    """Return ``(dispatcher, bot)``; the Bot is not reachable from Dispatcher."""
    from app.bot.container import AppContainer
    from app.services.delivery import DeliveryService
    from app.services.rate_limit import RateLimiter

    dp = Dispatcher()
    bot = Bot(token=FAKE_TOKEN)
    delivery = DeliveryService(type("B", (), {"send_message": None}), settings)
    container = AppContainer(
        settings=settings,
        bot=bot,
        dispatcher=dp,
        llm=fake_llm,
        delivery=delivery,
        rate_limiter=RateLimiter(settings),
        database=None,
        chat=chat_service,
    )
    register_handlers(dp, container)
    return dp, bot


def _update(text=None, chat_id=-1001, user_id=77, content_type="text") -> Update:
    message = Message(
        message_id=1,
        date=datetime.now(tz=UTC),
        chat=Chat(id=chat_id, type="private"),
        from_user=User(id=user_id, is_bot=False, first_name="T") if user_id else None,
        text=text,
    )
    return Update(update_id=1, message=message)


async def _feed(dp, bot, update) -> bool:
    return await dp.feed_update(bot, update)


@pytest.mark.asyncio
async def test_start_command_is_answered(dispatcher, enqueued, sent):
    dp, bot = dispatcher
    await _feed(dp, bot, _update(text="/start"))

    # Assert on observable behaviour, not on Dispatcher.feed_update's return
    # value, which is not part of the contract we depend on.
    assert enqueued == [], "/start must not enqueue a research task"
    assert len(sent) == 1
    assert "Welcome" in sent[0]["text"]


@pytest.mark.asyncio
async def test_unknown_command_is_answered_not_enqueued(dispatcher, enqueued, sent):
    dp, bot = dispatcher
    await _feed(dp, bot, _update(text="/research something"))

    assert enqueued == [], "an unknown command was treated as free text"
    assert len(sent) == 1
    # Phase 2 FR-18: a helpful response listing a way forward. The wording is
    # user-facing copy, so the assertion is on the contract (it names /help and
    # does not become free text), not on an exact string.
    assert "/help" in sent[0]["text"]


@pytest.mark.asyncio
async def test_photo_is_rejected_and_not_enqueued(dispatcher, enqueued, sent):
    dp, bot = dispatcher
    # A real photo message: Message.content_type is a derived, frozen field,
    # so the update must be constructed rather than mutated.
    photo_message = Message(
        message_id=2,
        date=datetime.now(tz=UTC),
        chat=Chat(id=-1001, type="private"),
        from_user=User(id=77, is_bot=False, first_name="T"),
        photo=[PhotoSize(file_id="f", file_unique_id="u", width=1, height=1)],
    )

    await _feed(dp, bot, Update(update_id=2, message=photo_message))

    assert enqueued == [], "a photo enqueued a research task"
    assert len(sent) == 1
    assert "text messages" in sent[0]["text"]


@pytest.mark.asyncio
async def test_text_is_enqueued_with_distinct_chat_and_user_ids(dispatcher, enqueued, sent):
    dp, bot = dispatcher
    await _feed(dp, bot, _update(text="what is entropy?", chat_id=-100999, user_id=555))

    assert len(enqueued) == 1
    # Phase 2 adds username and telegram_message_id to the payload: the first
    # for the user record, the second for turn-id derivation, which is what
    # makes update redelivery idempotent.
    assert enqueued[0] == {
        "chat_id": -100999,
        "user_id": 555,
        "query": "what is entropy?",
        "username": None,
        "telegram_message_id": 1,
    }
    assert enqueued[0]["chat_id"] != enqueued[0]["user_id"]
    assert len(sent) == 1, "user did not receive the acknowledgement"


@pytest.mark.asyncio
async def test_text_is_stripped_before_enqueue(dispatcher, enqueued, sent):
    dp, bot = dispatcher
    await _feed(dp, bot, _update(text="   spaced question   "))
    assert enqueued[0]["query"] == "spaced question"


@pytest.mark.asyncio
async def test_whitespace_only_text_is_not_enqueued(dispatcher, enqueued, sent):
    dp, bot = dispatcher
    await _feed(dp, bot, _update(text="     "))
    assert enqueued == []
    assert len(sent) == 1


# --- T-9: broker failure is surfaced to the user ---------------------------

@pytest.mark.asyncio
async def test_broker_failure_produces_a_user_visible_message(dispatcher, monkeypatch):
    """The acknowledgement was already sent, so the user is left waiting.

    A queue outage must be reported, not swallowed (FR-6.3).
    """
    import app.tasks.research_task as task_module

    class FailingTask:
        @staticmethod
        def delay(**_kwargs):
            raise ConnectionError("broker unreachable")

    monkeypatch.setattr(task_module, "process_research", FailingTask)
    dp, bot = dispatcher
    sent: list[dict] = []

    async def fake_call(self, bot_, method, timeout=None):
        sent.append({"text": method.text})
        return SimpleNamespace(message_id=1)

    monkeypatch.setattr(AiohttpSession, "__call__", fake_call)
    await _feed(dp, bot, _update(text="a question"))

    assert len(sent) == 2, "broker failure was silent"
    assert "queue is unavailable" in sent[1]["text"]
    assert "broker unreachable" not in sent[1]["text"], "internal detail leaked to the user"


# --- Throttling (T-12) -----------------------------------------------------
#
# These drive the real RateLimiter rather than stubbing its decision, so they
# verify the limiter's actual behaviour. The settings fixture allows 3 requests
# per user per window and 2 concurrent in-flight requests.

@pytest.mark.asyncio
async def test_global_capacity_is_enforced_and_explained(dispatcher, enqueued, sent):
    """With MAX_IN_FLIGHT_REQUESTS=2 and no task run, the 3rd is refused."""
    dp, bot = dispatcher

    await _feed(dp, bot, _update(text="q1"))   # in_flight 1
    await _feed(dp, bot, _update(text="q2"))   # in_flight 2
    assert len(sent) == 2

    await _feed(dp, bot, _update(text="q3"))   # capacity exhausted

    assert len(sent) == 3
    assert "capacity is full" in sent[2]["text"], "capacity refusal was not explained"


@pytest.mark.asyncio
async def test_user_rate_limit_is_enforced_after_allowance(dispatcher, enqueued, sent):
    """With capacity released each turn, the per-user limit trips on the 4th."""
    dp, bot = dispatcher
    container = _container_of(dp)

    for i in range(3):  # allowance is 3
        await _feed(dp, bot, _update(text=f"q{i}"))
        container.rate_limiter.release()

    assert len(sent) == 3

    await _feed(dp, bot, _update(text="q4"))  # 4th exceeds the per-user limit
    container.rate_limiter.release()

    assert len(sent) == 4
    assert "Too many requests" in sent[3]["text"]


def _container_of(dp):
    """Recover the container the handlers closed over."""
    router = dp.sub_routers[0]
    for observer in router.message.handlers:
        cell = observer.callback.__closure__
        if cell:
            for var in cell:
                if isinstance(var.cell_contents, AppContainer):
                    return var.cell_contents
    raise AssertionError("container not found in handler closure")
