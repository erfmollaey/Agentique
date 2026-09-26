"""Telegram surface for AI chat (Phase 2 § 17, FR-17 … FR-21, FR-24, T-8, T-16).

Routed through a real aiogram ``Dispatcher`` with real ``Update``/``Message``
objects, so filter resolution and handler ordering are exercised rather than
assumed. Only the network boundary is stubbed.
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
from app.domain.errors import LLMServiceError, PersistenceError
from app.services.delivery import DeliveryService
from app.services.failures import user_message_for
from app.services.rate_limit import RateLimiter

FAKE_TOKEN = "1234567890:AAsdfghjklQWERTYuiopZXCVBNMasdfghjkl"


@pytest.fixture
def sent(monkeypatch):
    """Record outbound Telegram calls without touching the network.

    ``Message.answer`` reaches Telegram through ``AiohttpSession.__call__``,
    not ``Bot.send_message``, so the session is the correct boundary to stub.
    """
    calls: list[dict] = []

    async def fake_call(self, bot, method, timeout=None):
        calls.append({"chat_id": method.chat_id, "text": method.text})
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
    return dp, bot


def _update(text=None, chat_id=-1001, user_id=77, content_type="text",
            message_id=1, update_id=None) -> Update:
    message = Message(
        message_id=message_id,
        date=datetime.now(tz=UTC),
        chat=Chat(id=chat_id, type="private"),
        from_user=User(id=user_id, is_bot=False, first_name="T", username="tester")
        if user_id else None,
        text=text,
    )
    return Update(update_id=update_id or message_id, message=message)


async def _feed(dp, bot, update):
    return await dp.feed_update(bot, update)


# --- Commands (FR-17, FR-18, FR-5, FR-20) ---------------------------------

@pytest.mark.asyncio
async def test_start_still_works_and_does_not_enqueue(dispatcher, enqueued, sent):
    dp, bot = dispatcher
    await _feed(dp, bot, _update(text="/start"))
    assert enqueued == []
    assert len(sent) == 1
    assert "Welcome" in sent[0]["text"]


@pytest.mark.asyncio
async def test_help_lists_the_available_commands(dispatcher, enqueued, sent):
    """FR-17."""
    dp, bot = dispatcher
    await _feed(dp, bot, _update(text="/help"))

    assert enqueued == [], "/help must not enqueue a chat turn"
    text = sent[0]["text"]
    for command in ("/start", "/help", "/new", "/conversations", "/reset"):
        assert command in text, f"{command} is not documented in /help"


@pytest.mark.asyncio
async def test_an_unknown_command_is_answered_not_treated_as_text(dispatcher, enqueued, sent):
    """FR-18."""
    dp, bot = dispatcher
    await _feed(dp, bot, _update(text="/research something"))
    assert enqueued == [], "an unknown command was treated as free text"
    assert len(sent) == 1
    assert "/help" in sent[0]["text"]


@pytest.mark.asyncio
async def test_new_starts_a_conversation_and_does_not_enqueue(dispatcher, enqueued, sent):
    dp, bot = dispatcher
    await _feed(dp, bot, _update(text="/new"))
    assert enqueued == []
    assert "new conversation" in sent[0]["text"].lower()


@pytest.mark.asyncio
async def test_conversations_lists_them(dispatcher, enqueued, sent):
    dp, bot = dispatcher
    await _feed(dp, bot, _update(text="/conversations"))
    assert enqueued == []
    assert "no conversations" in sent[0]["text"].lower()


@pytest.mark.asyncio
async def test_reset_requires_explicit_confirmation(dispatcher, enqueued, sent):
    """FR-20: a destructive command must not act on first use."""
    dp, bot = dispatcher
    await _feed(dp, bot, _update(text="/reset"))
    assert "/reset confirm" in sent[0]["text"]
    assert "Cleared" not in sent[0]["text"]


@pytest.mark.asyncio
async def test_reset_confirm_performs_the_reset(dispatcher, enqueued, sent):
    from tests.fakes import user_turn

    dp, bot = dispatcher
    container = _container_of(dp)
    # Awaited, not driven through the process loop: this test already has a
    # running loop, which is exactly the re-entrancy run_coroutine refuses.
    await container.chat.handle_message(user_turn(77, -1001, "a question", 1))

    await _feed(dp, bot, _update(text="/reset confirm", message_id=9))
    assert "Cleared 2" in sent[0]["text"]


@pytest.mark.asyncio
async def test_a_command_failure_produces_a_readable_message(
    dispatcher, sent, monkeypatch, chat_service
):
    """FR-23: a persistence failure must not surface as a traceback."""
    dp, bot = dispatcher

    async def boom(_turn):
        raise PersistenceError("relation \"users\" does not exist")

    monkeypatch.setattr(chat_service, "reset_conversation", boom)
    await _feed(dp, bot, _update(text="/reset confirm", message_id=9))

    assert len(sent) == 1
    text = sent[0]["text"]
    assert "could not save" in text
    assert "relation" not in text and "Traceback" not in text


@pytest.mark.asyncio
async def test_a_message_without_a_sender_is_answered_not_processed(
    dispatcher, enqueued, sent
):
    """A missing from_user must not become a user row (L-8, SR-1)."""
    dp, bot = dispatcher
    await _feed(dp, bot, _update(text="hello", user_id=None))
    assert enqueued == []
    assert len(sent) == 1


# --- Message path (FR-21, § 17) -------------------------------------------

@pytest.mark.asyncio
async def test_a_text_message_is_acknowledged_and_enqueued(dispatcher, enqueued, sent):
    dp, bot = dispatcher
    await _feed(dp, bot, _update(text="what is entropy?"))
    assert len(enqueued) == 1
    assert enqueued[0]["query"] == "what is entropy?"
    assert len(sent) == 1, "the user received no acknowledgement"


@pytest.mark.asyncio
async def test_the_username_is_passed_through_for_the_user_record(
    dispatcher, enqueued, sent
):
    dp, bot = dispatcher
    await _feed(dp, bot, _update(text="hi"))
    assert enqueued[0]["username"] == "tester"


@pytest.mark.asyncio
async def test_the_telegram_message_id_is_passed_for_idempotency(
    dispatcher, enqueued, sent
):
    """Without it the turn id cannot be derived, so redelivery is undetectable."""
    dp, bot = dispatcher
    await _feed(dp, bot, _update(text="hi", message_id=4242))
    assert enqueued[0]["telegram_message_id"] == 4242


@pytest.mark.asyncio
async def test_a_non_text_message_is_rejected_and_not_enqueued(dispatcher, enqueued, sent):
    dp, bot = dispatcher
    photo = Message(
        message_id=2,
        date=datetime.now(tz=UTC),
        chat=Chat(id=-1001, type="private"),
        from_user=User(id=77, is_bot=False, first_name="T"),
        photo=[PhotoSize(file_id="f", file_unique_id="u", width=1, height=1)],
    )
    await _feed(dp, bot, Update(update_id=2, message=photo))
    assert enqueued == []
    assert "text messages" in sent[0]["text"]


@pytest.mark.asyncio
async def test_whitespace_only_text_is_not_enqueued(dispatcher, enqueued, sent):
    dp, bot = dispatcher
    await _feed(dp, bot, _update(text="     "))
    assert enqueued == []
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_a_broker_failure_is_reported_to_the_user(dispatcher, monkeypatch, sent):
    """FR-6.3, T-9: the acknowledgement was sent, so the user must be told."""
    import app.tasks.research_task as task_module

    class FailingTask:
        @staticmethod
        def delay(**_kwargs):
            raise ConnectionError("broker unreachable at 10.0.0.1:6379")

    monkeypatch.setattr(task_module, "process_research", FailingTask)
    dp, bot = dispatcher
    await _feed(dp, bot, _update(text="a question"))

    assert len(sent) == 2, "the broker failure was silent"
    assert "queue is unavailable" in sent[1]["text"]
    assert "10.0.0.1" not in sent[1]["text"], "internal detail leaked to the user"


# --- Throttling (FR-26, FR-29) -------------------------------------------

@pytest.mark.asyncio
async def test_throttling_states_when_the_user_may_retry(dispatcher, enqueued, sent):
    """FR-29."""
    dp, bot = dispatcher
    container = _container_of(dp)
    for i in range(ALLOWANCE):
        await _feed(dp, bot, _update(text=f"q{i}", message_id=i + 1))
        container.rate_limiter.release()

    await _feed(dp, bot, _update(text="over the limit", message_id=99))
    text = sent[-1]["text"]
    assert "Too many requests" in text
    assert "s" in text, "the response does not say when to retry"


@pytest.mark.asyncio
async def test_a_throttled_request_is_not_enqueued(dispatcher, enqueued, sent):
    dp, bot = dispatcher
    container = _container_of(dp)
    for i in range(ALLOWANCE):
        await _feed(dp, bot, _update(text=f"q{i}", message_id=i + 1))
        container.rate_limiter.release()
    before = len(enqueued)

    await _feed(dp, bot, _update(text="over", message_id=99))
    assert len(enqueued) == before, "a throttled request still reached the queue"


@pytest.mark.asyncio
async def test_the_daily_allowance_is_enforced_and_explained(
    dispatcher, enqueued, sent, monkeypatch
):
    """FR-28."""
    dp, bot = dispatcher
    container = _container_of(dp)
    container.rate_limiter._daily_max = 2  # type: ignore[attr-defined]
    for i in range(2):
        await _feed(dp, bot, _update(text=f"q{i}", message_id=i + 1))
        container.rate_limiter.release()

    await _feed(dp, bot, _update(text="third", message_id=3))
    assert "Too many requests" in sent[-1]["text"]


@pytest.mark.asyncio
async def test_global_capacity_is_explained(dispatcher, enqueued, sent):
    """FR-27."""
    dp, bot = dispatcher
    await _feed(dp, bot, _update(text="q1", message_id=1))
    await _feed(dp, bot, _update(text="q2", message_id=2))
    await _feed(dp, bot, _update(text="q3", message_id=3))
    assert "capacity is full" in sent[2]["text"]


# --- User-facing error copy (FR-22, § 17) ---------------------------------

@pytest.mark.parametrize(
    "error",
    [
        LLMServiceError("502 from https://provider.internal/v1"),
        PersistenceError("could not connect to server at db.internal:5432"),
    ],
)
def test_user_messages_expose_no_infrastructure_detail(error):
    text = user_message_for(error)
    for leak in ("provider.internal", "db.internal", "502", "5432"):
        assert leak not in text


def test_every_domain_failure_has_its_own_message():
    """FR-22, FR-23: distinguishable failures, not one generic string."""
    from app.domain import errors as domain

    mapped = {
        name: user_message_for(getattr(domain, name)("x"))
        for name in dir(domain)
        if isinstance(getattr(domain, name), type)
        and issubclass(getattr(domain, name), domain.ResearchError)
        and getattr(domain, name) is not domain.ResearchError
    }
    # Distinct messages for the categories a user can act on or report.
    assert len(set(mapped.values())) >= 5, mapped


def test_an_unknown_exception_maps_to_a_generic_message():
    assert user_message_for(ZeroDivisionError("secret detail")) == (
        "Something went wrong. Please try again."
    )


def test_the_generic_message_leaks_nothing():
    text = user_message_for(RuntimeError("postgres://user:hunter2@host/db"))
    assert "hunter2" not in text


# --- Layering (Phase 2 § 7) -----------------------------------------------

def _imported_modules(path: str) -> set[str]:
    """Top-level module names imported anywhere in a file."""
    import ast
    from pathlib import Path

    found: set[str] = set()
    for node in ast.walk(ast.parse(Path(path).read_text())):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            found.add(node.module)
    return found


def test_handlers_never_import_the_database_layer():
    """Phase 2 § 7: a handler parses the update and delegates. It holds no SQL."""
    imported = _imported_modules("app/bot/handlers.py")
    for forbidden in ("sqlalchemy", "app.db", "app.repositories"):
        assert not any(m == forbidden or m.startswith(forbidden + ".") for m in imported), (
            f"app/bot/handlers.py imports {forbidden}"
        )


def test_the_service_layer_never_imports_the_transport():
    """TR-2: the dependency points inward, never toward Telegram."""
    for module in ("app/services/chat.py", "app/services/context.py",
                   "app/repositories/conversations.py"):
        imported = _imported_modules(module)
        assert not any(m.startswith("aiogram") for m in imported), (
            f"{module} imports the transport"
        )


def test_the_data_layer_is_the_only_place_sqlalchemy_is_imported():
    """Phase 2 § 7: one layer owns persistence.

    The allowlist is the *file's* layer, not the imported name: only
    ``app/db`` and ``app/repositories`` may reach for the ORM.
    """
    import pathlib

    owning_layers = {("db",), ("repositories",)}
    offenders = []
    for path in pathlib.Path("app").rglob("*.py"):
        if "migrations" in path.parts:
            continue
        owning = (path.parent.name,)
        if owning in owning_layers:
            continue
        for module in _imported_modules(str(path)):
            if module.startswith("sqlalchemy"):
                offenders.append(f"{path}: {module}")
    assert not offenders, f"SQLAlchemy imported outside the data layer: {offenders}"


def test_no_module_builds_sql_by_string_concatenation():
    """SR-10: no f-string or %-format reaching a database call.

    A coarse but effective guard: any literal SQL keyword appearing inside an
    f-string or a ``%``/``+`` expression in the data layer is flagged.
    """
    import ast
    import pathlib

    keywords = ("SELECT ", "INSERT ", "UPDATE ", "DELETE ", "DROP ", "CREATE TABLE")
    offenders = []
    for path in [*pathlib.Path("app/db").rglob("*.py"), *pathlib.Path("app/repositories").rglob("*.py")]:
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.JoinedStr):
                text = " ".join(
                    part.value for part in node.values
                    if isinstance(part, ast.Constant) and isinstance(part.value, str)
                ).upper()
            elif isinstance(node, ast.BinOp):
                text = ""
            else:
                continue
            if any(keyword in text for keyword in keywords):
                offenders.append(f"{path}:{node.lineno}")
    assert not offenders, f"SQL assembled as a string: {offenders}"


def test_services_do_not_import_sqlalchemy():
    for module in ("app/services/chat.py", "app/services/context.py",
                   "app/services/prompts.py", "app/services/failures.py"):
        imported = _imported_modules(module)
        assert not any(m.startswith("sqlalchemy") for m in imported), (
            f"{module} imports SQLAlchemy; it must talk to the repository port"
        )


def test_the_chat_service_does_not_import_telegram():
    from pathlib import Path

    source = Path("app/services/chat.py").read_text()
    assert "aiogram" not in source, "the service layer depends on the transport"


# --- helpers ---------------------------------------------------------------

def _container_of(dp) -> AppContainer:
    """Recover the container the handlers closed over."""
    router = dp.sub_routers[0]
    for observer in router.message.handlers:
        cell = observer.callback.__closure__
        if cell:
            for var in cell:
                if isinstance(var.cell_contents, AppContainer):
                    return var.cell_contents
    raise AssertionError("container not found in handler closure")


def _run(coro):
    from app.infrastructure.asyncio_runtime import run_coroutine

    return run_coroutine(coro)


#: The ``settings`` fixture allows 3 requests per user per window.
ALLOWANCE = 3
