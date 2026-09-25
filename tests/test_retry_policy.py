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
    RateLimitedError,
    ResearchError,
)
from app.infrastructure.llm import OpenAICompatibleClient
from app.services.delivery import DeliveryService
from app.services.research import ResearchService
from tests.conftest import FakeLLMClient

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
    service = ResearchService(
        llm=FakeLLMClient(error=LLMResponseError("bad payload")),
        delivery=DeliveryService(fake_bot, settings),
        settings=settings,
    )
    service.run(chat_id=42, query="q")

    assert len(fake_bot.sent) == 1
    assert "invalid" in fake_bot.sent[0][1]


def test_retryable_failure_still_sends_one_notice_via_run(fake_bot, settings):
    """``run`` has no retry policy, so it always terminates the user wait."""
    service = ResearchService(
        llm=FakeLLMClient(error=LLMTimeoutError("slow")),
        delivery=DeliveryService(fake_bot, settings),
        settings=settings,
    )
    service.run(chat_id=42, query="q")

    assert len(fake_bot.sent) == 1
    assert "No response in time" in fake_bot.sent[0][1]


def test_execute_raises_so_the_task_can_apply_its_retry_policy(fake_bot, settings):
    """``execute`` must not swallow the error; the task decides."""
    service = ResearchService(
        llm=FakeLLMClient(error=LLMTimeoutError("slow")),
        delivery=DeliveryService(fake_bot, settings),
        settings=settings,
    )
    with pytest.raises(LLMTimeoutError):
        service.execute(chat_id=42, query="q")

    assert fake_bot.sent == [], "execute must not message the user on a retryable failure"


def test_notify_failure_uses_a_non_revealing_message(fake_bot, settings):
    service = ResearchService(
        llm=FakeLLMClient(), delivery=DeliveryService(fake_bot, settings), settings=settings
    )
    service.notify_failure(42, ResearchError("internal detail: db password hunter2"))

    text = fake_bot.sent[0][1]
    assert "hunter2" not in text
    assert "internal detail" not in text
    assert text


def test_notify_failure_survives_a_broken_transport(settings):
    """A failure while reporting a failure must not raise."""

    class BrokenBot:
        session = None

        async def send_message(self, chat_id, text):
            raise RuntimeError("telegram down")

    service = ResearchService(
        llm=FakeLLMClient(), delivery=DeliveryService(BrokenBot(), settings), settings=settings
    )
    assert service.notify_failure(42, ResearchError("x")) == 0
