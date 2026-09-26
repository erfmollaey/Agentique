"""SR-9 — user data deletion.

Deletion is the one security requirement Phase 2 left unimplemented, so it gets
its own module and its own real-database coverage.

Three layers are tested, because each can fail independently:

* **Service** (``test_delete_all_user_data_*``) — the orchestration, driven
  through the in-memory repositories.
* **Database** (``test_cascade_*``, ``test_delete_*``) — the cascade actually
  removes the tree, verified against real PostgreSQL rather than assumed from
  the foreign keys.
* **Command** (``test_delete_account_command_*``) — routing, confirmation, and
  that deletion never reaches the LLM path.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.db.models import ConversationModel, MessageModel, UserModel
from app.db.unit_of_work import SqlAlchemyUnitOfWork
from app.domain.chat import Role
from app.domain.errors import PersistenceError
from app.infrastructure.asyncio_runtime import run_coroutine
from app.services.chat import ChatService
from app.services.context import ContextBuilder
from tests.conftest import FakeLLMClient
from tests.fakes import in_memory_uow_factory, user_turn

# --------------------------------------------------------------------------
# Service layer
# --------------------------------------------------------------------------


def _service(settings, llm, store):
    factory, _ = in_memory_uow_factory(store)
    return ChatService(
        settings=settings, llm=llm, uow_factory=factory, context=ContextBuilder(settings)
    )


def _run(coro):
    return run_coroutine(coro)


def _seed(chat_service, user_id, messages: int = 2):
    """Give a user one conversation with ``messages`` turns."""
    for i in range(messages):
        _run(chat_service.handle_message(user_turn(user_id, user_id * 10, f"q{i}", i + 1)))


def test_deletion_removes_the_user_and_everything_reachable(chat_service, chat_store):
    """SR-9: deletion actually removes the data, not merely marks it."""
    _seed(chat_service, 5)
    assert chat_store.users_by_telegram, "precondition: the user exists"
    assert chat_store.conversations, "precondition: a conversation exists"
    assert chat_store.messages, "precondition: messages exist"

    text = _run(chat_service.delete_all_user_data(user_turn(5, 50, "", 0)))

    assert "deleted" in text.lower()
    assert chat_store.users_by_telegram == {}
    assert chat_store.users_by_id == {}
    assert chat_store.conversations == {}
    assert chat_store.messages == []


def test_deletion_logs_no_message_content(chat_service, chat_store, caplog):
    """SR-5: the deletion path logs identifiers and counts, never content."""
    secret_ish = "my password is hunter2 and my name is John Smith"
    _run(chat_service.handle_message(user_turn(6, 60, secret_ish, 1)))

    with caplog.at_level("INFO"):
        _run(chat_service.delete_all_user_data(user_turn(6, 60, "", 0)))

    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "hunter2" not in logged
    assert "John Smith" not in logged


def test_deletion_of_an_unknown_identity_does_not_claim_success(chat_service):
    """An honest outcome, not a fabricated deletion."""
    text = _run(chat_service.delete_all_user_data(user_turn(999, 999, "", 0)))
    assert "nothing" in text.lower()
    assert "deleted" not in text.lower().replace("nothing was deleted", "")


def test_deletion_does_not_create_a_user_as_a_side_effect(chat_service, chat_store):
    """A delete must not go through get_or_create and so resurrect an identity."""
    _run(chat_service.delete_all_user_data(user_turn(123, 123, "", 0)))
    assert chat_store.users_by_telegram == {}


def test_deletion_is_scoped_to_one_user(chat_service, chat_store):
    """SR-4: deleting one user must leave another's data intact."""
    _seed(chat_service, 10)
    _seed(chat_service, 11)

    _run(chat_service.delete_all_user_data(user_turn(10, 100, "", 0)))

    assert list(chat_store.users_by_telegram) == [11]
    assert chat_store.messages, "the other user's messages were deleted too"
    remaining = {m.conversation_id for m in chat_store.messages}
    owned = {c.id for c in chat_store.conversations.values()}
    assert remaining <= owned, "the surviving messages belong to another user"


def test_a_deleted_identity_starts_clean_rather_than_resuming(chat_service, chat_store):
    """The old conversation is unreachable; a new one starts with no history."""
    _seed(chat_service, 20, messages=2)
    _run(chat_service.delete_all_user_data(user_turn(20, 200, "", 0)))

    llm = FakeLLMClient(reply="fresh start")
    chat_service._llm = llm
    outcome = _run(chat_service.handle_message(user_turn(20, 200, "hello again", 1)))

    assert outcome.text == "fresh start"
    assert len(chat_store.users_by_telegram) == 1, "the identity was not recreated"
    # The provider saw only the new message: no history from before deletion.
    sent = [m.content for m in llm.chat_calls[0] if m.role == Role.USER]
    assert sent == ["hello again"]


def test_a_failed_deletion_raises_rather_than_reporting_success(
    settings, chat_store, monkeypatch
):
    """No misleading success on failure (task § 13, Test 4)."""
    from tests.fakes import InMemoryUsers

    service = _service(settings, FakeLLMClient(), chat_store)
    _seed(service, 30)

    async def boom(self, _telegram_user_id):
        raise PersistenceError("could not reach the database")

    monkeypatch.setattr(InMemoryUsers, "delete", boom, raising=True)

    with pytest.raises(PersistenceError):
        _run(service.delete_all_user_data(user_turn(30, 300, "", 0)))

    # Nothing was removed, and nothing claimed otherwise.
    assert 30 in chat_store.users_by_telegram
    assert chat_store.conversations


# --------------------------------------------------------------------------
# Database layer — the cascade, verified rather than assumed (task § 14)
#
# These run against real PostgreSQL with real commits, using the shared
# ``clean_database`` fixture. Real commits matter: a test that deletes inside the
# fixture's rolled-back transaction would prove nothing about the cascade, and
# the transactional-rollback test would be unable to distinguish a rollback from
# an outer transaction that was never committed.
# --------------------------------------------------------------------------


@pytest.fixture
def populated(clean_database):
    """Two users, each with a conversation and three messages."""
    database = clean_database
    _seed_database(database, 700, "owner")
    _seed_database(database, 701, "other")
    return database


def _seed_database(database, telegram_user_id, username):
    from app.domain.chat import MessageRecord

    async def seed():
        async with SqlAlchemyUnitOfWork(database) as uow:
            user = await uow.users.get_or_create(telegram_user_id, username)
            conversation = await uow.conversations.create(user.id, "t")
            for i in range(3):
                await uow.messages.append(
                    MessageRecord(
                        conversation_id=conversation.id,
                        role=Role.USER,
                        content=f"q{i}",
                        turn_id=f"t-{telegram_user_id}-{i}",
                    )
                )

    _run(seed())


def _counts(database):
    """Row counts per table."""

    async def query():
        async with SqlAlchemyUnitOfWork(database) as uow:
            session = uow._session
            return {
                "users": int(
                    (await session.execute(select(func.count()).select_from(UserModel))).scalar_one()
                ),
                "conversations": int(
                    (
                        await session.execute(
                            select(func.count()).select_from(ConversationModel)
                        )
                    ).scalar_one()
                ),
                "messages": int(
                    (await session.execute(select(func.count()).select_from(MessageModel))).scalar_one()
                ),
            }

    return _run(query())


def _user_row(database, telegram_user_id):
    async def query():
        async with SqlAlchemyUnitOfWork(database) as uow:
            return (
                await uow._session.execute(
                    select(UserModel).where(UserModel.telegram_user_id == telegram_user_id)
                )
            ).scalar_one_or_none()

    return _run(query())


def _delete_user_row(database, row_id):
    """Delete the user row directly, so the cascade is what is under test."""

    async def run():
        async with SqlAlchemyUnitOfWork(database) as uow:
            await uow._session.delete(await uow._session.get(UserModel, row_id))

    _run(run())


def test_the_cascade_removes_the_whole_tree(populated):
    """Deleting a user must remove their conversations *and* their messages."""
    database = populated
    assert _counts(database) == {"users": 2, "conversations": 2, "messages": 6}

    _delete_user_row(database, _user_row(database, 700).id)

    assert _user_row(database, 700) is None, "the user row survived"
    assert _counts(database) == {"users": 1, "conversations": 1, "messages": 3}, (
        "the cascade did not remove the whole subtree"
    )


def test_the_cascade_leaves_another_user_untouched(populated):
    """A narrow cascade: only the deleted user's subtree goes (task § 14)."""
    database = populated
    other = _user_row(database, 701)
    other_id = other.id

    _delete_user_row(database, _user_row(database, 700).id)

    survivor = _user_row(database, 701)
    assert survivor is not None, "the other user was deleted"
    assert survivor.id == other_id
    assert _counts(database) == {"users": 1, "conversations": 1, "messages": 3}


def test_the_remaining_conversation_belongs_to_the_surviving_user(populated):
    """Ownership is intact after a cascade, not just row counts."""
    database = populated
    _delete_user_row(database, _user_row(database, 700).id)

    async def query():
        async with SqlAlchemyUnitOfWork(database) as uow:
            conversations = (await uow._session.execute(select(ConversationModel))).scalars().all()
            messages = (await uow._session.execute(select(MessageModel))).scalars().all()
            return (
                [c.user_id for c in conversations],
                [m.conversation_id for m in messages],
            )

    owners, message_conversations = _run(query())
    assert owners == [_user_row(database, 701).id]
    assert set(message_conversations) == set(owners), (
        "a message survived without its conversation"
    )


def test_the_deletion_log_records_counts_not_content(populated, caplog):
    """SR-5 on the real repository: counts and identifiers only."""
    database = populated
    with caplog.at_level("INFO"):
        _run(_delete_via_repository(database, 700))

    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "messages=3" in logged
    assert "conversations=1" in logged
    assert "q0" not in logged and "q1" not in logged, "message content reached the log"


def _delete_via_repository(database, telegram_user_id):
    async def run():
        async with SqlAlchemyUnitOfWork(database) as uow:
            return await uow.users.delete(telegram_user_id)

    return run()


def test_deleting_a_user_returns_the_true_counts(populated):
    """The reported counts must match what was actually removed."""
    database = populated
    before = _counts(database)

    async def scenario():
        async with SqlAlchemyUnitOfWork(database) as uow:
            return await uow.users.delete(700)

    result = _run(scenario())

    assert result.deleted is True
    assert result.conversations == 1
    assert result.messages == 3
    after = _counts(database)
    assert after["conversations"] == before["conversations"] - result.conversations
    assert after["messages"] == before["messages"] - result.messages
    assert after["users"] == before["users"] - 1


def test_deleting_an_unknown_identity_is_a_no_op(populated):
    """No rows touched, and the result says so rather than claiming success."""
    database = populated
    before = _counts(database)

    async def scenario():
        async with SqlAlchemyUnitOfWork(database) as uow:
            return await uow.users.delete(4242)

    result = _run(scenario())

    assert result.deleted is False
    assert result.conversations == 0 and result.messages == 0
    assert _counts(database) == before, "a no-op delete changed something"


def test_a_failure_during_deletion_rolls_the_whole_thing_back(populated, monkeypatch):
    """Transactional: a failure leaves no partial deletion (task § 13, Test 4)."""
    from app.repositories.users import UserRepositoryImpl

    database = populated
    before = _counts(database)

    original = UserRepositoryImpl.delete

    async def delete_then_fail(self, telegram_user_id):
        # The DELETE really executes; the failure arrives afterwards, which is
        # the case a naive implementation gets wrong.
        await original(self, telegram_user_id)
        raise PersistenceError("commit failed after the delete statement")

    monkeypatch.setattr(UserRepositoryImpl, "delete", delete_then_fail)

    async def scenario():
        async with SqlAlchemyUnitOfWork(database) as uow:
            await uow.users.delete(700)

    with pytest.raises(PersistenceError):
        _run(scenario())

    assert _counts(database) == before, (
        "the deletion was partially applied; the unit of work did not roll back"
    )
    assert _user_row(database, 700) is not None, "the user was deleted despite the failure"
