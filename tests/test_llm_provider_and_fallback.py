"""LLM service boundary and the model fallback chain.

Phase 2 FR-12 … FR-16, FR-30 … FR-34; tests T-5, T-6, T-14, T-15, T-20.

No network. The provider SDK is replaced at the ``chat.completions.create``
boundary, which is the same seam the Phase 1 tests use, so a change in the SDK
cannot silently change what these verify.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from openai import APIStatusError, AuthenticationError

from app.domain.chat import LLMReply, Role
from app.domain.chat import Message as ChatMessage
from app.domain.errors import (
    LLMAuthenticationError,
    LLMResponseError,
    LLMServiceError,
    LLMTimeoutError,
)
from app.infrastructure.llm import (
    FallbackProvider,
    OpenAICompatibleClient,
    create_llm_client,
    parse_fallback_models,
)


def _reply(text: str = "an answer", prompt: int = 30, completion: int = 12):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=text), finish_reason="stop"
            )
        ],
        usage=SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion),
    )


def _client(settings, result=None, raise_exc: Exception | None = None, calls: list | None = None):
    payload = result if result is not None else _reply()

    def create(**kwargs):
        if calls is not None:
            calls.append(kwargs)
        if raise_exc is not None:
            raise raise_exc
        return payload

    sdk = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    return OpenAICompatibleClient(settings, client=sdk)


def _messages():
    return [
        ChatMessage(role=Role.SYSTEM, content="system"),
        ChatMessage(role=Role.USER, content="hello"),
    ]


def _status_error(status: int) -> APIStatusError:
    return APIStatusError(
        message=f"status {status}",
        response=SimpleNamespace(status_code=status, headers={}, request=None),
        body=None,
    )


# --- FR-15: validated domain type ------------------------------------------

def test_complete_returns_a_validated_domain_object(settings):
    reply = _client(settings).complete(
        _messages(), model="m", temperature=0.3, max_tokens=100
    )
    assert isinstance(reply, LLMReply)
    assert reply.text == "an answer"
    assert reply.model == "m"


def test_token_usage_is_returned_for_cost_tracking(settings):
    """FR-30, T-20: usage comes from the provider, never from a client claim."""
    reply = _client(settings).complete(
        _messages(), model="m", temperature=0.3, max_tokens=100
    )
    assert (reply.prompt_tokens, reply.completion_tokens) == (30, 12)
    assert reply.total_tokens == 42


def test_usage_is_optional_and_defaults_to_zero(settings):
    payload = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="x"), finish_reason=None)],
        usage=None,
    )
    reply = _client(settings, payload).complete(
        _messages(), model="m", temperature=0.3, max_tokens=100
    )
    assert reply.total_tokens == 0


def test_the_request_carries_the_configured_model_and_limits(settings):
    calls: list = []
    _client(settings, calls=calls).complete(
        _messages(), model="the-model", temperature=0.7, max_tokens=123
    )
    assert calls[0]["model"] == "the-model"
    assert calls[0]["max_tokens"] == 123
    assert calls[0]["temperature"] == 0.7
    assert calls[0]["messages"] == [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "hello"},
    ]


# --- Malformed and empty provider output -----------------------------------

@pytest.mark.parametrize("empty", [None, "", "   "])
def test_an_empty_response_is_rejected(settings, empty):
    payload = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=empty), finish_reason="stop")],
        usage=None,
    )
    with pytest.raises(LLMResponseError):
        _client(settings, payload).complete(
            _messages(), model="m", temperature=0.3, max_tokens=10
        )


@pytest.mark.parametrize("payload", [SimpleNamespace(choices=[]), SimpleNamespace()])
def test_a_response_with_no_choices_is_rejected(settings, payload):
    with pytest.raises(LLMResponseError):
        _client(settings, payload).complete(
            _messages(), model="m", temperature=0.3, max_tokens=10
        )


# --- Error classification is preserved from Phase 1 ------------------------

def test_a_timeout_is_classified_as_retryable(settings):
    with pytest.raises(LLMTimeoutError):
        _client(settings, raise_exc=TimeoutError("slow")).complete(
            _messages(), model="m", temperature=0.3, max_tokens=10
        )


def test_an_authentication_failure_is_not_retryable_and_leaks_nothing(settings):
    exc = AuthenticationError(
        message="Incorrect API key provided: sk-live-SHOULD-NOT-APPEAR",
        response=SimpleNamespace(status_code=401, headers={}, request=None),
        body=None,
    )
    with pytest.raises(LLMAuthenticationError) as info:
        _client(settings, raise_exc=exc).complete(
            _messages(), model="m", temperature=0.3, max_tokens=10
        )
    assert "sk-live" not in str(info.value)
    assert info.value.retryable is False


@pytest.mark.parametrize("status", [400, 401, 402, 403, 404, 422])
def test_a_non_transient_status_is_not_retryable(settings, status):
    """FR-34: a request the provider will reject again must not be retried."""
    with pytest.raises(LLMServiceError) as info:
        _client(settings, raise_exc=_status_error(status)).complete(
            _messages(), model="m", temperature=0.3, max_tokens=10
        )
    assert info.value.retryable is False


@pytest.mark.parametrize("status", [408, 409, 429])
def test_a_transient_status_is_retryable(settings, status):
    with pytest.raises(LLMServiceError) as info:
        _client(settings, raise_exc=_status_error(status)).complete(
            _messages(), model="m", temperature=0.3, max_tokens=10
        )
    assert info.value.retryable is True


# --- T-6 / FR-16: the provider is swappable --------------------------------

def test_the_service_layer_accepts_any_object_with_complete(settings, chat_service):
    """T-6: nothing in the service layer knows which provider it is talking to.

    A structurally identical double is enough. If the service ever reached for a
    provider-SDK type, this would fail.
    """
    from tests.fakes import user_turn

    class AlienProvider:
        def complete(self, messages, *, model, temperature, max_tokens):
            return LLMReply(text="from an alien provider", model="alien-1")

    chat_service._llm = AlienProvider()
    outcome = _run(chat_service.handle_message(user_turn(1, 1, "hi", 1)))
    assert outcome.text == "from an alien provider"
    assert outcome.model == "alien-1"


def test_the_shipped_provider_satisfies_the_port_protocol(settings):
    from app.domain.ports import LLMProvider

    assert isinstance(_client(settings), LLMProvider)


def test_the_factory_builds_the_configured_provider(settings):
    client = create_llm_client(settings, provider="openai_compatible")
    assert isinstance(client, OpenAICompatibleClient)


def test_every_openai_compatible_vendor_maps_to_the_one_implementation(settings):
    """Groq, DeepSeek, and a local server all speak the same protocol."""
    for name in ("openai_compatible", "groq", "deepseek", "openai"):
        assert isinstance(create_llm_client(settings, provider=name), OpenAICompatibleClient)


def test_an_unknown_provider_is_rejected_with_a_readable_error(settings):
    with pytest.raises(ValueError, match="Unknown LLM_PROVIDER"):
        create_llm_client(settings, provider="telepathy")


def test_the_default_provider_needs_no_fallback_configuration(settings):
    assert isinstance(create_llm_client(settings), OpenAICompatibleClient)


# --- T-14 / FR-32: fallback on a retryable failure -------------------------

class _ScriptedProvider:
    """Fails the first N models with a chosen error, then succeeds."""

    def __init__(self, errors: dict[str, Exception], text: str = "recovered"):
        self.errors = errors
        self.text = text
        self.tried: list[str] = []

    def complete(self, messages, *, model, temperature, max_tokens):
        self.tried.append(model)
        if model in self.errors:
            raise self.errors[model]
        return LLMReply(text=self.text, model=model)


def test_a_retryable_failure_falls_back_to_the_next_model():
    """T-14, FR-32."""
    provider = _ScriptedProvider({"primary": LLMServiceError("overloaded")})
    fallback = FallbackProvider(provider, ["backup", "tertiary"], max_attempts=2)

    reply = fallback.complete(
        _messages(), model="primary", temperature=0.3, max_tokens=10
    )

    assert reply.model == "backup"
    assert provider.tried == ["primary", "backup"]


def test_a_non_retryable_failure_does_not_fall_back():
    """T-15, FR-34: trying again would only multiply cost."""
    provider = _ScriptedProvider({"primary": LLMAuthenticationError("bad key")})
    fallback = FallbackProvider(provider, ["backup"], max_attempts=3)

    with pytest.raises(LLMAuthenticationError):
        fallback.complete(_messages(), model="primary", temperature=0.3, max_tokens=10)

    assert provider.tried == ["primary"], "a terminal failure triggered a retry"


def test_a_malformed_response_does_not_fall_back():
    """A different model will not fix a response this application cannot parse."""
    provider = _ScriptedProvider({"primary": LLMResponseError("malformed")})
    fallback = FallbackProvider(provider, ["backup"], max_attempts=3)

    with pytest.raises(LLMResponseError):
        fallback.complete(_messages(), model="primary", temperature=0.3, max_tokens=10)
    assert provider.tried == ["primary"]


def test_the_last_failure_is_re_raised_with_its_retryability():
    provider = _ScriptedProvider(
        {"primary": LLMServiceError("a"), "backup": LLMServiceError("b")}
    )
    fallback = FallbackProvider(provider, ["backup"], max_attempts=2)

    with pytest.raises(LLMServiceError) as info:
        fallback.complete(_messages(), model="primary", temperature=0.3, max_tokens=10)

    assert info.value.retryable is True, "the task must still be able to retry the turn"


# --- FR-33: the chain is bounded -------------------------------------------

def test_the_chain_never_exceeds_the_configured_attempt_bound():
    """FR-33: a persistent failure must not fan out without limit."""
    provider = _ScriptedProvider({m: LLMServiceError("down") for m in ("a", "b", "c", "d")})
    fallback = FallbackProvider(provider, ["b", "c", "d"], max_attempts=1)

    with pytest.raises(LLMServiceError):
        fallback.complete(_messages(), model="a", temperature=0.3, max_tokens=10)

    assert provider.tried == ["a", "b"], "the chain ran past its bound"


def test_fallback_can_be_disabled_entirely():
    provider = _ScriptedProvider({"primary": LLMServiceError("down")})
    fallback = FallbackProvider(provider, ["backup"], max_attempts=0)

    with pytest.raises(LLMServiceError):
        fallback.complete(_messages(), model="primary", temperature=0.3, max_tokens=10)
    assert provider.tried == ["primary"]


def test_the_primary_model_is_not_repeated_in_the_chain():
    """Retrying the model that just failed cannot help and doubles the cost."""
    provider = _ScriptedProvider({"primary": LLMServiceError("down")})
    fallback = FallbackProvider(provider, ["primary", "other"], max_attempts=5)
    fallback.complete(_messages(), model="primary", temperature=0.3, max_tokens=10)
    assert provider.tried == ["primary", "other"]


def test_a_successful_first_model_never_touches_the_chain():
    provider = _ScriptedProvider({})
    fallback = FallbackProvider(provider, ["backup"], max_attempts=3)
    fallback.complete(_messages(), model="primary", temperature=0.3, max_tokens=10)
    assert provider.tried == ["primary"]


# --- Chain parsing ---------------------------------------------------------

@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", ()),
        ("   ", ()),
        ("a", ("a",)),
        ("a,b", ("a", "b")),
        (" a , b ", ("a", "b")),
        ("a,,b", ("a", "b")),
        ("a,a,b", ("a", "b")),
    ],
)
def test_the_fallback_chain_is_parsed_defensively(raw, expected):
    assert parse_fallback_models(raw) == expected


def test_the_factory_wraps_the_provider_when_a_chain_is_configured(settings):
    configured = settings.model_copy(
        update={"LLM_FALLBACK_MODELS": "backup-a,backup-b", "LLM_FALLBACK_MAX_ATTEMPTS": 2}
    )
    client = create_llm_client(configured)
    assert isinstance(client, FallbackProvider)
    assert client.models == ("backup-a", "backup-b")


def _run(coro):
    from app.infrastructure.asyncio_runtime import run_coroutine

    return run_coroutine(coro)
