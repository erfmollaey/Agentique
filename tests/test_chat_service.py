"""Chat orchestration (Phase 2 § 9, § 15, § 16; FR-1 … FR-6, FR-21, FR-22).

Driven through the real :class:`ChatService` with the in-memory repositories, so
the orchestration, context assembly, persistence order, and idempotency logic are
all under test without a database. The schema and constraints are covered
separately in ``test_database_layer.py``; the end-to-end path with a real database
is in ``test_chat_integration.py``.
"""

from __future__ import annotations

import pytest

from app.domain.chat import ChatTurn, ConversationStatus, Role
from app.domain.errors import (
    LLMResponseError,
    LLMServiceError,
    LLMTimeoutError,
    MessageTooLongError,
    PersistenceError,
)
from app.infrastructure.asyncio_runtime import run_coroutine
from app.services.chat import ChatService, derive_turn_id
from app.services.context import ContextBuilder
from tests.conftest import FakeLLMClient
from tests.fakes import in_memory_uow_factory, user_turn


def _run(coro):
    return run_coroutine(coro)


def _service(settings, llm, store):
    factory, _ = in_memory_uow_factory(store)
    return ChatService(
        settings=settings, llm=llm, uow_factory=factory, context=ContextBuilder(settings)
    )


def _sent(llm) -> list[str]:
    """The user-visible text of each provider call, oldest first."""
    out = []
    for call in llm.chat_calls:
        out.extend(m.content for m in call if m.role == Role.USER)
    return out


# --- New and existing conversations (FR-4, FR-5) --------------------------

def test_a_first_message_creates_the_user_and_the_conversation(
    chat_service, chat_store
):
    outcome = _run(chat_service.handle_message(user_turn(7, 70, "hello there", 1)))

    assert outcome.conversation_id > 0
    assert outcome.user_message_id > 0
    assert outcome.assistant_message_id is not None
    assert list(chat_store.users_by_telegram) == [7]
    assert len(chat_store.conversations) == 1
    assert next(iter(chat_store.conversations.values())).title == "hello there"


def test_the_same_user_keeps_one_conversation_across_messages(
    chat_service, chat_store
):
    """FR-4, and the § 2 objective: the conversation continues."""
    first = _run(chat_service.handle_message(user_turn(7, 70, "first", 1)))
    second = _run(chat_service.handle_message(user_turn(7, 70, "second", 2)))
    third = _run(chat_service.handle_message(user_turn(7, 70, "third", 3)))

    assert first.conversation_id == second.conversation_id == third.conversation_id
    assert len(chat_store.conversations) == 1
    assert list(chat_store.users_by_telegram) == [7]


def test_different_users_get_different_conversations(chat_service, chat_store):
    a = _run(chat_service.handle_message(user_turn(1, 10, "a", 1)))
    b = _run(chat_service.handle_message(user_turn(2, 20, "b", 1)))
    assert a.conversation_id != b.conversation_id
    assert len(chat_store.conversations) == 2


def test_the_same_user_in_two_chats_shares_one_conversation(chat_service):
    """Identity is the user, not the chat: a group and a DM are one history."""
    a = _run(chat_service.handle_message(user_turn(5, 100, "in group", 1)))
    b = _run(chat_service.handle_message(user_turn(5, 200, "in dm", 2)))
    assert a.conversation_id == b.conversation_id


# --- Context construction (FR-7, FR-8) ------------------------------------

def test_the_model_receives_prior_turns(chat_service, fake_llm):
    _run(chat_service.handle_message(user_turn(1, 1, "what is entropy?", 1)))
    _run(chat_service.handle_message(user_turn(1, 1, "why does it matter?", 2)))

    assert _sent(fake_llm)[-1] == "why does it matter?"
    second_call = fake_llm.chat_calls[1]
    contents = [m.content for m in second_call]
    assert "what is entropy?" in contents
    assert contents.index("what is entropy?") < contents.index("why does it matter?")


def test_the_interleaved_order_is_preserved(chat_service, fake_llm):
    for i in range(3):
        _run(chat_service.handle_message(user_turn(1, 1, f"q{i}", i + 1)))

    second_call_roles = [m.role for m in fake_llm.chat_calls[2]]
    assert second_call_roles == [
        Role.SYSTEM,
        Role.USER, Role.ASSISTANT,
        Role.USER, Role.ASSISTANT,
        Role.USER,
    ]


def test_the_system_prompt_leads_and_the_current_message_ends(chat_service, fake_llm):
    _run(chat_service.handle_message(user_turn(1, 1, "q", 1)))
    call = fake_llm.chat_calls[0]
    assert call[0].role == Role.SYSTEM
    assert call[-1].role == Role.USER
    assert call[-1].content == "q"


def test_history_is_capped_so_context_cannot_grow_without_bound(
    settings, chat_store
):
    llm = FakeLLMClient()
    service = _service(settings, llm, chat_store)
    for i in range(settings.LLM_CONTEXT_MAX_MESSAGES + 5):
        _run(service.handle_message(user_turn(1, 1, f"q{i}", i + 1)))

    final_call = llm.chat_calls[-1]
    non_system = [m for m in final_call if m.role != Role.SYSTEM]
    assert len(non_system) <= settings.LLM_CONTEXT_MAX_MESSAGES + 1


# --- Persistence (FR-2, FR-3) ---------------------------------------------

def test_both_messages_of_a_turn_are_persisted(chat_service, chat_store):
    _run(chat_service.handle_message(user_turn(1, 1, "q", 1)))
    roles = [m.role for m in chat_store.messages]
    assert roles == [Role.USER, Role.ASSISTANT]
    assert chat_store.messages[0].content == "q"
    assert chat_store.messages[1].content == "fake reply"


def test_the_assistant_message_carries_usage_metadata(chat_service, chat_store):
    """FR-30, T-20."""
    _run(chat_service.handle_message(user_turn(1, 1, "q", 1)))
    assistant = chat_store.messages[-1]
    assert assistant.model == "test-model"
    assert (assistant.prompt_tokens, assistant.completion_tokens) == (11, 7)


def test_both_messages_of_a_turn_share_one_turn_id(chat_service, chat_store):
    _run(chat_service.handle_message(user_turn(1, 1, "q", 1)))
    assert len({m.turn_id for m in chat_store.messages}) == 1


def test_the_conversation_is_touched_so_it_stays_current(chat_service, chat_store):
    _run(chat_service.handle_message(user_turn(1, 1, "q", 1)))
    conversation = next(iter(chat_store.conversations.values()))
    assert conversation.updated_at is not None


def test_the_reply_text_is_what_gets_delivered(chat_service):
    """The service returns text; it does not send anything itself."""
    outcome = _run(chat_service.handle_message(user_turn(1, 1, "q", 1)))
    assert outcome.text == "fake reply"


# --- Input validation ------------------------------------------------------

@pytest.mark.parametrize("text", ["", "   ", "\n\t "])
def test_an_empty_message_is_rejected(chat_service, chat_store, text):
    with pytest.raises(MessageTooLongError):
        _run(chat_service.handle_message(ChatTurn(
            telegram_user_id=1, chat_id=1, text=text
        )))
    assert chat_store.messages == [], "an empty message was persisted"


def test_an_oversized_message_is_rejected_before_persistence(settings, chat_store):
    llm = FakeLLMClient()
    service = _service(settings, llm, chat_store)
    with pytest.raises(MessageTooLongError):
        _run(service.handle_message(user_turn(1, 1, "x" * 5000, 1)))
    assert chat_store.messages == [], "an oversized message reached the database"
    assert llm.chat_calls == [], "an oversized message reached the provider"


def test_the_message_limit_is_configuration(settings, chat_store):
    tight = settings.model_copy(update={"CHAT_MAX_MESSAGE_CHARS": 10})
    service = _service(tight, FakeLLMClient(), chat_store)
    with pytest.raises(MessageTooLongError):
        _run(service.handle_message(user_turn(1, 1, "x" * 11, 1)))
    _run(service.handle_message(user_turn(1, 1, "x" * 10, 2)))


# --- Failure paths (FR-22, § 15) ------------------------------------------

@pytest.mark.parametrize(
    "error", [LLMServiceError("down"), LLMTimeoutError("slow"), LLMResponseError("bad")]
)
def test_a_provider_failure_is_typed_and_surfaces_to_the_caller(
    settings, chat_store, error
):
    service = _service(settings, FakeLLMClient(error=error), chat_store)
    with pytest.raises(type(error)):
        _run(service.handle_message(user_turn(1, 1, "q", 1)))


def test_no_assistant_message_is_fabricated_when_the_provider_fails(
    settings, chat_store
):
    """Phase 2 § 15: never invent a reply."""
    service = _service(settings, FakeLLMClient(error=LLMServiceError("down")), chat_store)
    with pytest.raises(LLMServiceError):
        _run(service.handle_message(user_turn(1, 1, "q", 1)))
    assert Role.ASSISTANT not in [m.role for m in chat_store.messages]


def test_a_provider_failure_does_not_lose_the_users_message(settings, chat_store):
    """Phase 2 § 15: "a user message should not disappear silently".

    The user message is committed before the provider is called, in its own
    transaction, so an outage cannot discard what the user typed.
    """
    service = _service(settings, FakeLLMClient(error=LLMServiceError("down")), chat_store)
    with pytest.raises(LLMServiceError):
        _run(service.handle_message(user_turn(1, 1, "do not lose this", 1)))

    assert [m.content for m in chat_store.messages] == ["do not lose this"]
    assert [m.role for m in chat_store.messages] == [Role.USER], (
        "a half-turn is user-message-only; no reply may be invented"
    )


def test_an_unexpected_error_becomes_a_persistence_error(settings, chat_store):
    class Exploding:
        def complete(self, *_a, **_k):
            raise ZeroDivisionError("boom")

    service = _service(settings, Exploding(), chat_store)
    with pytest.raises(PersistenceError):
        _run(service.handle_message(user_turn(1, 1, "q", 1)))


# --- Idempotency (Phase 2 § 16) -------------------------------------------

def test_a_redelivered_update_does_not_reach_the_provider(chat_service, fake_llm):
    """Telegram may deliver the same update more than once."""
    turn = user_turn(1, 1, "only once please", 1)
    first = _run(chat_service.handle_message(turn))
    second = _run(chat_service.handle_message(turn))

    assert len(fake_llm.chat_calls) == 1, "the provider was called twice for one message"
    assert second.duplicate is True
    assert second.text == first.text
    assert second.assistant_message_id == first.assistant_message_id


def test_a_redelivered_update_creates_no_second_assistant_message(
    chat_service, chat_store
):
    turn = user_turn(1, 1, "once", 1)
    _run(chat_service.handle_message(turn))
    _run(chat_service.handle_message(turn))
    assert [m.role for m in chat_store.messages] == [Role.USER, Role.ASSISTANT]


def test_turn_ids_are_stable_for_the_same_platform_message():
    """The idempotency key is derived, not random, so a retry recomputes it."""
    assert derive_turn_id(42, 7) == derive_turn_id(42, 7)
    assert derive_turn_id(42, 7) != derive_turn_id(42, 8)
    assert derive_turn_id(42, 7) != derive_turn_id(43, 7)


def test_a_turn_id_falls_back_when_platform_ids_are_absent():
    assert derive_turn_id(None, None) != derive_turn_id(None, None)


def test_a_retry_after_a_provider_failure_completes_the_half_turn(
    settings, chat_store
):
    """The first attempt left a user message with no reply; the retry finishes it.

    This is the idempotency boundary doing real work: the retry recomputes the
    same turn id, the user-message insert collides, and the service recognises
    the half-turn and answers it rather than storing a second user message.
    """
    failing = FakeLLMClient(error=LLMTimeoutError("slow"))
    with pytest.raises(LLMTimeoutError):
        _run(_service(settings, failing, chat_store).handle_message(user_turn(1, 1, "q", 1)))
    assert [m.role for m in chat_store.messages] == [Role.USER]

    working = FakeLLMClient(reply="second attempt")
    outcome = _run(
        _service(settings, working, chat_store).handle_message(user_turn(1, 1, "q", 1))
    )

    assert outcome.text == "second attempt"
    assert [m.role for m in chat_store.messages] == [Role.USER, Role.ASSISTANT]
    assert len(working.chat_calls) == 1, "the retry must not have stored a second question"


def test_different_messages_from_one_user_are_not_treated_as_duplicates(
    chat_service, fake_llm
):
    _run(chat_service.handle_message(user_turn(1, 1, "one", 1)))
    _run(chat_service.handle_message(user_turn(1, 1, "two", 2)))
    assert len(fake_llm.chat_calls) == 2


# --- Conversation lifecycle (FR-5, FR-6) ----------------------------------

def test_new_conversation_archives_the_previous_one(chat_service, chat_store):
    _run(chat_service.handle_message(user_turn(1, 1, "first", 1)))
    first_id = next(iter(chat_store.conversations))

    text = _run(chat_service.start_new_conversation(user_turn(1, 1, "", 0)))

    assert "new conversation" in text.lower()
    assert chat_store.conversations[first_id].status == ConversationStatus.ARCHIVED
    assert len(chat_store.conversations) == 2


def test_messages_after_new_go_to_the_new_conversation(chat_service, chat_store):
    _run(chat_service.handle_message(user_turn(1, 1, "old", 1)))
    old_id = next(iter(chat_store.conversations))
    _run(chat_service.start_new_conversation(user_turn(1, 1, "", 0)))

    outcome = _run(chat_service.handle_message(user_turn(1, 1, "new", 2)))
    assert outcome.conversation_id != old_id
    assert all(m.conversation_id == outcome.conversation_id for m in chat_store.messages[-2:])


def test_listing_conversations_reports_the_current_one(chat_service):
    _run(chat_service.handle_message(user_turn(1, 1, "a title", 1)))
    _run(chat_service.start_new_conversation(user_turn(1, 1, "", 0)))

    text = _run(chat_service.list_conversations(user_turn(1, 1, "", 0)))
    assert "a title" in text
    assert "current conversation" in text
    assert "*" in text


def test_listing_conversations_for_a_new_user_is_not_an_error(chat_service):
    text = _run(chat_service.list_conversations(user_turn(99, 99, "", 0)))
    assert "no conversations" in text.lower()


def test_listing_never_shows_another_users_conversation(chat_service):
    """SR-4, T-16 through the command path."""
    _run(chat_service.handle_message(user_turn(1, 1, "private matters", 1)))
    text = _run(chat_service.list_conversations(user_turn(2, 2, "", 0)))
    assert "private matters" not in text


def test_reset_clears_messages_but_keeps_the_conversation(chat_service, chat_store):
    outcome = _run(chat_service.handle_message(user_turn(1, 1, "q", 1)))
    assert len(chat_store.messages) == 2

    text = _run(chat_service.reset_conversation(user_turn(1, 1, "", 0)))

    assert "Cleared 2" in text
    assert chat_store.messages == []
    assert outcome.conversation_id in chat_store.conversations
    assert 1 in chat_store.users_by_telegram, "FR-6: the user record must survive"


def test_reset_lets_the_conversation_continue_afterwards(chat_service, chat_store):
    _run(chat_service.handle_message(user_turn(1, 1, "before", 1)))
    _run(chat_service.reset_conversation(user_turn(1, 1, "", 0)))
    outcome = _run(chat_service.handle_message(user_turn(1, 1, "after", 2)))
    assert outcome.text == "fake reply"
    assert [m.content for m in chat_store.messages] == ["after", "fake reply"]


def test_reset_without_a_conversation_is_reported_not_crashed(chat_service):
    from app.domain.errors import ConversationNotFoundError

    with pytest.raises(ConversationNotFoundError):
        _run(chat_service.reset_conversation(user_turn(1, 1, "", 0)))


# --- Observability (SR-5) --------------------------------------------------

def test_no_message_content_is_written_to_the_log(chat_service, caplog):
    """SR-5: identifiers and metadata, not content."""
    secret_ish = "my bank password is hunter2 and my name is John Smith"
    with caplog.at_level("INFO"):
        _run(chat_service.handle_message(user_turn(1, 1, secret_ish, 1)))

    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "hunter2" not in logged
    assert "John Smith" not in logged
    assert "user resolved" in logged or "conversation created" in logged


def test_the_provider_key_never_appears_in_the_log(chat_service, settings, caplog):
    with caplog.at_level("DEBUG"):
        _run(chat_service.handle_message(user_turn(1, 1, "q", 1)))
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert settings.GROQ_API_KEY.get_secret_value() not in logged
