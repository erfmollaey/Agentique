"""Database layer against a real PostgreSQL (Phase 2 § 21, T-18, T-19).

These run against the dedicated test database created by the real migrations, in
a transaction that is rolled back afterwards. A real database rather than SQLite
because the requirements are specifically about PostgreSQL behaviour: unique
constraint violation handling, foreign key cascades, and the ordering guarantee
that comes from a ``(created_at, id)`` index.

Isolation is enforced three ways: the database name must end in ``_test``
(``app.db.session.resolve_database_url``), the schema comes from the migrations
rather than from ``create_all``, and each test is rolled back.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.db.models import ConversationModel, MessageModel, UserModel
from app.domain.chat import ConversationStatus, MessageRecord, Role
from app.domain.errors import DuplicateMessageError
from app.repositories.conversations import ConversationRepositoryImpl
from app.repositories.messages import MessageRepositoryImpl
from app.repositories.users import UserRepositoryImpl


@pytest.fixture
def users(db):
    return UserRepositoryImpl(db)


@pytest.fixture
def conversations(db):
    return ConversationRepositoryImpl(db)


@pytest.fixture
def messages(db):
    return MessageRepositoryImpl(db)


# --- FR-1 / T-1: user records ---------------------------------------------

async def test_a_user_record_is_created_on_first_interaction(users):
    user = await users.get_or_create(4242, "alice")
    assert user.telegram_user_id == 4242
    assert user.username == "alice"
    assert user.id is not None


async def test_a_second_interaction_reuses_the_same_user(users):
    """T-1: the user is created once and found thereafter (FR-1)."""
    first = await users.get_or_create(4242, "alice")
    second = await users.get_or_create(4242, "alice-renamed")
    assert first.id == second.id


async def test_lookup_by_telegram_id_does_not_create(users):
    assert await users.get_by_telegram_id(999) is None
    created = await users.get_or_create(999, "bob")
    found = await users.get_by_telegram_id(999)
    assert found is not None and found.id == created.id


async def test_duplicate_telegram_identity_is_impossible(users, db):
    """The database, not the application, prevents a second row (Phase 2 § 8).

    Bypasses the repository on purpose: an application-level check cannot be
    relied on under concurrency, so the constraint is what is under test.
    """
    await users.get_or_create(7, "dup")
    await db.commit()
    db.add(UserModel(telegram_user_id=7, username="impostor"))
    with pytest.raises(IntegrityError):
        async with db.begin_nested():
            await db.flush()


async def test_only_the_platform_identity_is_stored(users, db):
    """Phase 2 § 5: no unnecessary Telegram data is retained."""
    user = await users.get_or_create(11, "carol")
    await db.commit()
    row = await db.get(UserModel, user.id)
    assert set(row.__table__.columns.keys()) == {
        "id", "telegram_user_id", "username", "created_at", "updated_at",
    }


# --- FR-4: conversations ---------------------------------------------------

async def test_a_conversation_is_created_for_a_user(users, conversations):
    user = await users.get_or_create(1, "a")
    conversation = await conversations.create(user.id, "first question")
    assert conversation.user_id == user.id
    assert conversation.status == ConversationStatus.ACTIVE
    assert conversation.title == "first question"


async def test_a_user_may_have_many_conversations(users, conversations):
    user = await users.get_or_create(2, "b")
    for i in range(3):
        await conversations.create(user.id, f"c{i}")
    assert len(await conversations.list_for_user(user.id)) == 3


async def test_get_active_returns_none_before_any_conversation(users, conversations):
    user = await users.get_or_create(3, "c")
    assert await conversations.get_active(user.id) is None


async def test_archiving_moves_the_active_conversation_forward(users, conversations):
    user = await users.get_or_create(4, "d")
    first = await conversations.create(user.id, "one")
    second = await conversations.create(user.id, "two")

    assert (await conversations.get_active(user.id)).id == second.id
    await conversations.set_status(second.id, ConversationStatus.ARCHIVED.value)
    assert (await conversations.get_active(user.id)).id == first.id
    assert len(await conversations.list_for_user(user.id)) == 2, "archiving must not delete"


async def test_conversations_are_listed_newest_first(users, conversations):
    user = await users.get_or_create(5, "e")
    for i in range(3):
        conversation = await conversations.create(user.id, f"c{i}")
        await conversations.touch(conversation.id)
    listed = await conversations.list_for_user(user.id)
    assert [c.title for c in listed] == ["c2", "c1", "c0"]


# --- SR-4 / T-16: ownership ------------------------------------------------

async def test_a_conversation_is_not_returned_to_another_user(
    users, conversations
):
    """SR-4, T-16: ownership is a query predicate, not a caller check."""
    owner = await users.get_or_create(10, "owner")
    intruder = await users.get_or_create(11, "intruder")
    conversation = await conversations.create(owner.id, "private")

    assert await conversations.get_for_user(conversation.id, owner.id) is not None
    assert await conversations.get_for_user(conversation.id, intruder.id) is None


async def test_one_user_cannot_see_another_users_conversations(
    users, conversations
):
    owner = await users.get_or_create(12, "owner")
    intruder = await users.get_or_create(13, "intruder")
    await conversations.create(owner.id, "mine")
    assert await conversations.list_for_user(intruder.id) == []


# --- FR-2 / T-3: messages and ordering -------------------------------------

async def _seed(users, conversations, messages, turns: int = 4):
    user = await users.get_or_create(100, "chatter")
    conversation = await conversations.create(user.id, "history")
    for i in range(turns):
        await messages.append(
            MessageRecord(
                conversation_id=conversation.id,
                role=Role.USER if i % 2 == 0 else Role.ASSISTANT,
                content=f"message {i}",
                turn_id=f"turn-{i}",
            )
        )
    await messages.append(
        MessageRecord(
            conversation_id=conversation.id,
            role=Role.USER,
            content="current",
            turn_id="turn-current",
        )
    )
    return user, conversation


async def test_messages_are_returned_in_chronological_order(
    users, conversations, messages, db
):
    """T-3: history order is explicit, not insertion-order dependent."""
    _user, conversation = await _seed(users, conversations, messages)
    await db.commit()

    history = await messages.history(conversation.id)
    assert [m.content for m in history] == [
        "message 0", "message 1", "message 2", "message 3", "current",
    ]


async def test_history_is_trimmed_from_the_oldest_end(
    users, conversations, messages, db
):
    """A long conversation loses its oldest turns, which the budget drops anyway."""
    _user, conversation = await _seed(users, conversations, messages, turns=6)
    await db.commit()

    history = await messages.history(conversation.id, limit=3)
    assert [m.content for m in history] == ["message 4", "message 5", "current"]


async def test_history_is_scoped_to_one_conversation(
    users, conversations, messages, db
):
    user = await users.get_or_create(101, "multi")
    first = await conversations.create(user.id, "one")
    second = await conversations.create(user.id, "two")
    await messages.append(
        MessageRecord(conversation_id=first.id, role=Role.USER, content="a", turn_id="t1")
    )
    await messages.append(
        MessageRecord(conversation_id=second.id, role=Role.USER, content="b", turn_id="t2")
    )
    await db.commit()

    assert [m.content for m in await messages.history(first.id)] == ["a"]
    assert [m.content for m in await messages.history(second.id)] == ["b"]


async def test_token_usage_is_recorded_per_message(users, conversations, messages, db):
    """FR-30, T-20: usage is stored, derived server-side from the provider."""
    user = await users.get_or_create(102, "metered")
    conversation = await conversations.create(user.id, "usage")
    await messages.append(
        MessageRecord(
            conversation_id=conversation.id,
            role=Role.ASSISTANT,
            content="answer",
            turn_id="t-usage",
            model="test-model",
            prompt_tokens=120,
            completion_tokens=45,
        )
    )
    await db.commit()

    stored = await messages.find_by_turn(conversation.id, "t-usage", "assistant")
    assert stored is not None
    assert (stored.model, stored.prompt_tokens, stored.completion_tokens) == (
        "test-model", 120, 45,
    )
    assert await messages.total_tokens_for_user(user.id) == 165


async def test_the_message_table_holds_no_provider_payload_or_secret(
    users, conversations, messages, db
):
    """Phase 2 § 5: content plus usage metadata, nothing else from the provider."""
    columns = set(MessageModel.__table__.columns.keys())
    assert columns == {
        "id", "conversation_id", "role", "content", "turn_id", "created_at",
        "model", "prompt_tokens", "completion_tokens",
        "telegram_chat_id", "telegram_message_id",
    }
    for forbidden in ("choices", "raw_response", "payload", "api_key", "usage_json"):
        assert forbidden not in columns


# --- T-19: constraints enforced by the database ---------------------------

async def test_foreign_key_cascade_deletes_messages_with_their_conversation(
    users, conversations, messages, db
):
    user = await users.get_or_create(104, "cascade")
    conversation = await conversations.create(user.id, "doomed")
    await messages.append(
        MessageRecord(conversation_id=conversation.id, role=Role.USER, content="x", turn_id="t")
    )
    await db.commit()

    await db.delete(await db.get(ConversationModel, conversation.id))
    await db.commit()

    assert await messages.history(conversation.id) == []


async def test_deleting_a_user_removes_their_conversations(users, conversations, db):
    """FR-6: the user record is the root; conversations do not outlive it."""
    user = await users.get_or_create(105, "root")
    conversation = await conversations.create(user.id, "child")
    await db.commit()

    await db.delete(await db.get(UserModel, user.id))
    await db.commit()

    assert await conversations.get_for_user(conversation.id, user.id) is None


async def test_a_message_cannot_reference_a_missing_conversation(db):
    db.add(
        MessageModel(
            conversation_id=999_999, role="user", content="orphan", turn_id="t-orphan"
        )
    )
    with pytest.raises(IntegrityError):
        async with db.begin_nested():
            await db.flush()


@pytest.mark.parametrize("role", ["wizard", "", "USER ", "tool"])
async def test_role_is_constrained_to_the_closed_set(db, role):
    """A hand-run INSERT cannot introduce a role the application would reject."""
    db.add(
        MessageModel(conversation_id=1, role=role, content="x", turn_id=f"t-{role or 'blank'}")
    )
    with pytest.raises(IntegrityError):
        async with db.begin_nested():
            await db.flush()


async def test_conversation_status_is_constrained(db):
    db.add(
        ConversationModel(user_id=1, status="deleted", title="x")
    )
    with pytest.raises(IntegrityError):
        async with db.begin_nested():
            await db.flush()


# --- Idempotency constraints ----------------------------------------------

async def test_one_assistant_reply_per_turn(users, conversations, messages, db):
    """uq_messages_turn_id_role is the boundary that stops a duplicate reply."""
    user = await users.get_or_create(106, "idem")
    conversation = await conversations.create(user.id, "turn")
    await messages.append(
        MessageRecord(
            conversation_id=conversation.id, role=Role.ASSISTANT, content="first", turn_id="t1"
        )
    )
    await db.commit()

    with pytest.raises(DuplicateMessageError):
        await messages.append(
            MessageRecord(
                conversation_id=conversation.id,
                role=Role.ASSISTANT, content="second", turn_id="t1",
            )
        )


async def test_a_user_and_an_assistant_message_may_share_a_turn(
    users, conversations, messages, db
):
    """The uniqueness is on (turn, role), not on turn alone."""
    user = await users.get_or_create(107, "pair")
    conversation = await conversations.create(user.id, "pair")
    await messages.append(
        MessageRecord(conversation_id=conversation.id, role=Role.USER, content="q", turn_id="t")
    )
    await messages.append(
        MessageRecord(conversation_id=conversation.id, role=Role.ASSISTANT, content="a", turn_id="t")
    )
    await db.commit()
    assert len(await messages.history(conversation.id)) == 2


async def test_the_same_telegram_message_cannot_be_stored_twice(
    users, conversations, messages, db
):
    user = await users.get_or_create(108, "tg")
    conversation = await conversations.create(user.id, "tg")
    record = MessageRecord(
        conversation_id=conversation.id, role=Role.USER, content="hi", turn_id="t1"
    )
    await messages.append(record, telegram_chat_id=99, telegram_message_id=5)
    await db.commit()

    with pytest.raises(DuplicateMessageError):
        await messages.append(
            MessageRecord(
                conversation_id=conversation.id, role=Role.USER, content="hi", turn_id="t2"
            ),
            telegram_chat_id=99,
            telegram_message_id=5,
        )


async def test_assistant_messages_may_all_have_null_telegram_ids(
    users, conversations, messages, db
):
    """PostgreSQL treats NULLs as distinct, so the constraint must not fire."""
    user = await users.get_or_create(109, "nulls")
    conversation = await conversations.create(user.id, "nulls")
    for i in range(3):
        await messages.append(
            MessageRecord(
                conversation_id=conversation.id,
                role=Role.ASSISTANT, content=f"a{i}", turn_id=f"t{i}",
            )
        )
    await db.commit()
    assert len(await messages.history(conversation.id)) == 3


# --- Reset semantics (FR-6) ------------------------------------------------

async def test_reset_deletes_messages_but_keeps_the_conversation_and_user(
    users, conversations, messages, db
):
    user = await users.get_or_create(110, "resetter")
    conversation = await conversations.create(user.id, "to reset")
    for i in range(3):
        await messages.append(
            MessageRecord(
                conversation_id=conversation.id, role=Role.USER, content=f"m{i}", turn_id=f"t{i}"
            )
        )
    await db.commit()

    removed = await messages.delete_for_conversation(conversation.id)
    await db.commit()

    assert removed == 3
    assert await messages.history(conversation.id) == []
    assert await users.get_by_telegram_id(110) is not None, "FR-6: the user must survive"
    assert await conversations.get_for_user(conversation.id, user.id) is not None


# --- Indexes and retrieval paths (Phase 2 § 25) ---------------------------

async def test_every_declared_index_exists_in_the_database(db):
    """A migration that silently skipped an index would leave a table scan."""
    rows = await db.execute(
        text(
            "SELECT indexname FROM pg_indexes "
            "WHERE schemaname = 'public' AND tablename IN ('users','conversations','messages')"
        )
    )
    present = {row[0] for row in rows}
    expected = {
        "ix_users_telegram_user_id",
        "ix_conversations_user_id",
        "ix_conversations_user_id_updated_at",
        "ix_conversations_project_id",
        "ix_messages_conversation_id_created_at",
    }
    assert expected <= present, f"missing indexes: {sorted(expected - present)}"


async def test_message_history_retrieval_uses_the_declared_index(db):
    """EXPLAIN must not fall back to a sequential scan for the hot path."""
    await db.execute(text("SET LOCAL enable_seqscan = off"))
    plan = await db.execute(
        text(
            "EXPLAIN SELECT * FROM messages "
            "WHERE conversation_id = 1 ORDER BY created_at, id"
        )
    )
    text_plan = " ".join(str(row[0]) for row in plan)
    assert "Index Scan" in text_plan or "Bitmap" in text_plan, text_plan
