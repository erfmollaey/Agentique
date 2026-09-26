"""Retry classification and the terminal-notice contract.

Phase 1 FR-4.4: retryable failures are retried a bounded number of times with
backoff; non-retryable failures are not retried. The user must receive exactly
one terminal notice, and must not be messaged on an attempt that will be
retried.

Observed during Phase 1 validation: a provider 402 "Insufficient Balance" was
being retried three times, which cannot succeed and wastes provider calls.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from openai import APIStatusError

from app.domain.errors import (
    DeliveryError,
    LLMAuthenticationError,
    LLMResponseError,
    LLMServiceError,
    LLMTimeoutError,
    PersistenceError,
    RateLimitedError,
    ResearchError,
)
from app.infrastructure.llm import OpenAICompatibleClient
from app.services.delivery import DeliveryService
from app.services.failures import user_message_for

# --- Classification --------------------------------------------------------

@pytest.mark.parametrize(
    ("exc", "retryable"),
    [
        (LLMTimeoutError("t"), True),
        (DeliveryError("d"), True),
        (LLMAuthenticationError("a"), False),
        (LLMResponseError("r"), False),
        (RateLimitedError("rl"), False),
    ],
)
def test_domain_errors_declare_retryability(exc, retryable):
    assert exc.retryable is retryable


def _status_error(status: int) -> APIStatusError:
    return APIStatusError(
        message=f"status {status}",
        response=SimpleNamespace(status_code=status, headers={}, request=None),
        body=None,
    )


@pytest.mark.parametrize("status", [400, 401, 402, 403, 404, 422])
def test_non_transient_status_is_not_retryable(settings, status):
    """A 4xx that is not transient will fail identically on every attempt."""
    client = OpenAICompatibleClient(
        settings,
        client=SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(
                    create=lambda **_k: (_ for _ in ()).throw(_status_error(status))
                )
            )
        ),
    )
    with pytest.raises(LLMServiceError) as info:
        client.analyze_query("q")
    assert info.value.retryable is False, f"status {status} must not be retried"


@pytest.mark.parametrize("status", [408, 409, 429])
def test_transient_status_is_retryable(settings, status):
    client = OpenAICompatibleClient(
        settings,
        client=SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(
                    create=lambda **_k: (_ for _ in ()).throw(_status_error(status))
                )
            )
        ),
    )
    with pytest.raises(LLMServiceError) as info:
        client.analyze_query("q")
    assert info.value.retryable is True, f"status {status} should be retried"


def test_status_error_message_does_not_leak_the_response_body(settings):
    client = OpenAICompatibleClient(
        settings,
        client=SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(
                    create=lambda **_k: (_ for _ in ()).throw(_status_error(402))
                )
            )
        ),
    )
    with pytest.raises(LLMServiceError) as info:
        client.analyze_query("q")
    assert settings.GROQ_API_KEY.get_secret_value() not in str(info.value)


# --- Terminal notice -------------------------------------------------------

def test_non_retryable_failure_sends_exactly_one_notice(fake_bot, settings):
    """A non-retryable failure terminates the wait with one specific message."""
    delivery = DeliveryService(fake_bot, settings)
    delivery.send(42, user_message_for(LLMResponseError("bad payload")))

    assert len(fake_bot.sent) == 1
    assert "invalid" in fake_bot.sent[0][1]


def test_retryable_failure_still_sends_one_notice(fake_bot, settings):
    """A retryable failure also terminates the wait once retries are exhausted."""
    delivery = DeliveryService(fake_bot, settings)
    delivery.send(42, user_message_for(LLMTimeoutError("slow")))

    assert len(fake_bot.sent) == 1
    assert "No response in time" in fake_bot.sent[0][1]


def test_the_service_raises_so_the_task_can_apply_its_retry_policy(chat_service, settings):
    """The chat service must not swallow a provider failure; the task decides."""
    from app.domain.errors import LLMTimeoutError as Timeout
    from app.services.chat import ChatService
    from app.services.context import ContextBuilder
    from tests.conftest import FakeLLMClient as Fake
    from tests.fakes import in_memory_uow_factory, user_turn

    factory, _store = in_memory_uow_factory()
    service = ChatService(
        settings=settings,
        llm=Fake(error=Timeout("slow")),
        uow_factory=factory,
        context=ContextBuilder(settings),
    )

    with pytest.raises(Timeout):
        _run(service.handle_message(user_turn(1, 42, "q", 1)))


def test_user_message_for_is_non_revealing(settings):
    """FR-22, SR-9: the message must not carry internal detail."""
    text = user_message_for(ResearchError("internal detail: db password hunter2"))

    assert "hunter2" not in text
    assert "internal detail" not in text
    assert text


def test_user_message_for_distinguishes_failure_kinds():
    """FR-22, FR-23: different failures must not collapse into one message."""
    messages = {
        user_message_for(LLMTimeoutError("t")),
        user_message_for(LLMServiceError("s")),
        user_message_for(LLMAuthenticationError("a")),
        user_message_for(PersistenceError("p")),
    }
    assert len(messages) >= 3, "failures are indistinguishable to the user"


def test_user_message_for_never_leaks_the_exception_text():
    for exc in (
        LLMTimeoutError("timeout talking to https://internal-host/secret"),
        LLMServiceError("502 from db-password=hunter2"),
        ResearchError("Traceback (most recent call last): ..."),
    ):
        text = user_message_for(exc)
        assert "hunter2" not in text
        assert "Traceback" not in text
        assert "internal-host" not in text


def test_delivery_failure_is_translated_into_a_typed_error(settings):
    """A transport failure surfaces as ``DeliveryError``, not a raw SDK error."""
    from app.domain.errors import DeliveryError

    delivery = DeliveryService(_BrokenBot(), settings)
    with pytest.raises(DeliveryError):
        delivery.send(42, "anything")


class _BrokenBot:
    session = None

    async def send_message(self, chat_id, text):
        raise RuntimeError("telegram down")


def _run(coro):
    from app.infrastructure.asyncio_runtime import run_coroutine

    return run_coroutine(coro)
