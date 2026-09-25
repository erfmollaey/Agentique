"""T-2 — LLM response validation, and T-8/T-9 failure paths.

Audit references: H-4 (unguarded ``json.loads`` and dict access), M-10 (untyped
contract), H-3 (no timeout, no token limit), FR-5.3, FR-6.4.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.domain.errors import (
    LLMAuthenticationError,
    LLMResponseError,
    LLMServiceError,
    LLMTimeoutError,
)
from app.domain.schemas import QueryAnalysis
from app.infrastructure.llm import OpenAICompatibleClient
from app.services.research import ResearchService


def _client(settings: Settings, content, raise_exc: Exception | None = None):
    """Build a client whose provider call returns ``content`` or raises."""
    captured: dict = {}

    def create(**kwargs):
        captured.update(kwargs)
        if raise_exc is not None:
            raise raise_exc
        message = SimpleNamespace(content=content)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    fake_sdk = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    return OpenAICompatibleClient(settings, client=fake_sdk), captured


# --- T-2: response parsing -------------------------------------------------

def test_t2_valid_json_is_parsed(settings):
    client, _ = _client(settings, json.dumps({"summary": "s", "sub_questions": ["a", "b"]}))
    result = client.analyze_query("q")
    assert result.summary == "s"
    assert result.sub_questions == ["a", "b"]


def test_t2_malformed_json_raises_typed_error(settings):
    client, _ = _client(settings, "this is not json")
    with pytest.raises(LLMResponseError):
        client.analyze_query("q")


def test_t2_json_scalar_payload_raises_typed_error(settings):
    client, _ = _client(settings, "[1, 2, 3]")
    with pytest.raises(LLMResponseError):
        client.analyze_query("q")


@pytest.mark.parametrize("empty", [None, "", "   "])
def test_t2_empty_or_null_content_raises_typed_error(settings, empty):
    client, _ = _client(settings, empty)
    with pytest.raises(LLMResponseError):
        client.analyze_query("q")


def test_t2_missing_fields_fall_back_to_defaults(settings):
    """A response missing keys must not crash; it degrades to defaults."""
    client, _ = _client(settings, json.dumps({"summary": "only summary"}))
    result = client.analyze_query("q")
    assert result.summary == "only summary"
    assert result.sub_questions == []


def test_t2_wrong_types_are_coerced_not_fatal(settings):
    payload = json.dumps({"summary": 123, "sub_questions": "single question"})
    client, _ = _client(settings, payload)
    result = client.analyze_query("q")
    assert result.summary == "123"
    assert result.sub_questions == ["single question"]


def test_t2_null_fields_become_empty(settings):
    client, _ = _client(settings, json.dumps({"summary": None, "sub_questions": None}))
    result = client.analyze_query("q")
    assert result.is_empty is True


# --- H-3: explicit timeout and token limit on every call --------------------

def test_llm_call_sets_timeout_and_token_limit(settings):
    client, captured = _client(settings, json.dumps({"summary": "s", "sub_questions": []}))
    client.analyze_query("q")
    assert captured["max_tokens"] == settings.LLM_MAX_TOKENS
    assert captured["model"] == settings.LLM_MODEL
    assert captured["response_format"] == {"type": "json_object"}


def test_llm_transport_is_configured_with_timeout_and_bounded_retries(settings):
    """The SDK client itself must carry a timeout and a bounded retry count."""
    client = OpenAICompatibleClient(settings)
    assert client._client.timeout == settings.LLM_TIMEOUT_SECONDS
    assert client._client.max_retries == settings.LLM_MAX_RETRIES


# --- T-8: provider failures map to typed errors and a user message ---------

@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (TimeoutError("slow"), LLMTimeoutError),
        (ConnectionError("down"), LLMServiceError),
    ],
)
def test_t8_provider_failures_map_to_typed_errors(settings, exc, expected):

    client, _ = _client(settings, None, raise_exc=exc)
    with pytest.raises(expected):
        client.analyze_query("q")


def test_t8_authentication_error_does_not_leak_provider_text(settings):
    from openai import AuthenticationError

    secret_echo = "Incorrect API key provided: sk-live-SHOULD-NOT-APPEAR"
    exc = AuthenticationError(
        message=secret_echo,
        response=SimpleNamespace(status_code=401, headers={}, request=None),
        body=None,
    )
    client, _ = _client(settings, None, raise_exc=exc)
    with pytest.raises(LLMAuthenticationError) as info:
        client.analyze_query("q")
    assert "sk-live" not in str(info.value)
    assert "SHOULD-NOT-APPEAR" not in str(info.value)


def test_t8_llm_failure_still_delivers_a_user_message(fake_bot, settings):
    from app.domain.errors import LLMServiceError
    from app.services.delivery import DeliveryService
    from tests.conftest import FakeLLMClient

    llm = FakeLLMClient(error=LLMServiceError("provider down"))
    delivery = DeliveryService(fake_bot, settings)
    service = ResearchService(llm=llm, delivery=delivery, settings=settings)

    service.run(chat_id=42, query="q")

    assert len(fake_bot.sent) == 1, "user received no terminal response"
    text = fake_bot.sent[0][1]
    assert "unavailable" in text
    assert "provider down" not in text, "internal error text leaked to the user"
    assert "Traceback" not in text


def test_t8_unexpected_exception_still_delivers_a_message(fake_bot, settings):
    from app.services.delivery import DeliveryService
    from tests.conftest import FakeLLMClient

    llm = FakeLLMClient(error=ZeroDivisionError("boom"))
    delivery = DeliveryService(fake_bot, settings)
    service = ResearchService(llm=llm, delivery=delivery, settings=settings)

    service.run(chat_id=42, query="q")
    assert len(fake_bot.sent) == 1
    assert "Traceback" not in fake_bot.sent[0][1]


def test_t8_every_message_gets_exactly_one_terminal_response(fake_bot, settings):
    """FR-3.4: a result or a failure notice, never silence, never two replies."""
    from app.services.delivery import DeliveryService

    delivery = DeliveryService(fake_bot, settings)

    service = ResearchService(llm=_OkLLM(), delivery=delivery, settings=settings)
    service.run(chat_id=1, query="q")
    assert len(fake_bot.sent) == 1

    from app.domain.errors import LLMTimeoutError
    from tests.conftest import FakeLLMClient

    failing = ResearchService(
        llm=FakeLLMClient(error=LLMTimeoutError("t")),
        delivery=DeliveryService(fake_bot, settings),
        settings=settings,
    )
    failing.run(chat_id=2, query="q")
    assert len(fake_bot.sent) == 2


def test_user_message_never_contains_credentials(fake_bot, settings):
    from app.services.delivery import DeliveryService
    from tests.conftest import FakeLLMClient

    llm = FakeLLMClient(error=LLMAuthenticationError("bad key"))
    service = ResearchService(
        llm=llm,
        delivery=DeliveryService(fake_bot, settings),
        settings=settings,
    )
    service.run(chat_id=1, query="q")
    text = fake_bot.sent[0][1]
    assert settings.BOT_TOKEN.get_secret_value() not in text
    assert settings.GROQ_API_KEY.get_secret_value() not in text


class _OkLLM:
    def analyze_query(self, user_query: str) -> QueryAnalysis:
        return QueryAnalysis(summary="s", sub_questions=["a"])
