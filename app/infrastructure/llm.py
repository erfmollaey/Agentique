"""AI provider client.

Wraps the OpenAI-compatible SDK behind an injectable interface so the
application never imports a provider SDK directly (P1-6, FR-5.4).

Audit references: C-1 (hardcoded key/endpoint/model), H-3 (no timeout, no
token limit, no retry), H-4 (unguarded json.loads), M-10 (untyped contract).

Phase 2 additions: :meth:`OpenAICompatibleClient.complete` returns a validated
:class:`~app.domain.chat.LLMReply` for the chat path, and
:class:`FallbackProvider` implements the bounded model fallback chain
(FR-31 … FR-34). :func:`create_llm_client` is the documented extension point
that makes the provider choice reversible (FR-16, T-6): adding a provider means
adding a branch there, never editing the service layer.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from typing import Any, Protocol

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    OpenAI,
    RateLimitError,
)
from pydantic import ValidationError

from app.core.config import Settings
from app.domain.chat import LLMReply
from app.domain.chat import Message as ChatMessage
from app.domain.errors import (
    LLMAuthenticationError,
    LLMResponseError,
    LLMServiceError,
    LLMTimeoutError,
    ResearchError,
)
from app.domain.schemas import QueryAnalysis

log = logging.getLogger(__name__)

# Statuses worth retrying: rate limiting, request timeout, and conflict.
_TRANSIENT_STATUS = frozenset({408, 409, 429})

_ANALYSIS_PROMPT = """You are a Supervisor that breaks one complex question into simpler sub-questions.

User question: {user_query}

Do exactly two things:
1. Write a one-line summary of the question.
2. Split it into 2 or 3 key sub-questions, each independently answerable.

Return JSON with this shape:
{{
    "summary": "one-line summary",
    "sub_questions": ["sub-question 1", "sub-question 2", "sub-question 3"]
}}
"""


class LLMClient(Protocol):
    """What the application needs from an AI provider."""

    def analyze_query(self, user_query: str) -> QueryAnalysis: ...

    def generate_final_answer(self, user_query: str, sub_answers: list[str]) -> str: ...


class OpenAICompatibleClient:
    """OpenAI-compatible chat client with bounded, explicit failure modes."""

    def __init__(self, settings: Settings, client: OpenAI | None = None) -> None:
        self._settings = settings
        self._client = client or OpenAI(
            # Read from configuration, never a literal (P0-1, P0-2).
            api_key=settings.GROQ_API_KEY.get_secret_value(),
            base_url=settings.LLM_BASE_URL,
            # Bounded, non-blocking transport-level retry (P0-12).
            timeout=settings.LLM_TIMEOUT_SECONDS,
            max_retries=settings.LLM_MAX_RETRIES,
        )

    def analyze_query(self, user_query: str) -> QueryAnalysis:
        """Decompose a question into a summary and sub-questions.

        Raises a typed :mod:`app.domain.errors` failure rather than letting a
        provider or JSON error escape untyped (FR-6.4).
        """
        response = self._create_completion(
            prompt=_ANALYSIS_PROMPT.format(user_query=user_query),
            temperature=self._settings.LLM_TEMPERATURE,
            json_mode=True,
        )
        return self._parse_analysis(response)

    def generate_final_answer(self, user_query: str, sub_answers: list[str]) -> str:
        """Combine sub-answers into one response.

        Retained from the original ``Supervisor`` so no existing capability is
        removed. **Unwired: not called by the Phase 2 request path.** It belongs
        to the Phase 3 research synthesis stage, which consumes the
        sub-questions ``analyze_query`` produces. Kept, rather than deleted,
        because removing a capability this phase is not entitled to replace
        would be a scope violation (AD-026).
        """
        joined = "\n".join(f"- {answer}" for answer in sub_answers)
        prompt = (
            f"Original user question: {user_query}\n\n"
            f"Collected answers to the sub-questions:\n{joined}\n\n"
            "Write one comprehensive, accurate, readable final answer."
        )
        return self._create_completion(prompt=prompt, temperature=0.5, json_mode=False)

    # --- internals ---------------------------------------------------------

    def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: str,
        temperature: float,
        max_tokens: int,
    ) -> LLMReply:
        """One chat completion, returned as a validated domain object (FR-15).

        The provider's payload is parsed here and nowhere else, so no raw
        provider structure reaches persistence or Telegram delivery.
        """
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": [{"role": m.role.value, "content": m.content} for m in messages],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        response = self._call(**kwargs)
        return self._to_reply(response, model)

    def _to_reply(self, response: Any, model: str) -> LLMReply:
        try:
            choice = response.choices[0]
        except (AttributeError, IndexError, TypeError) as exc:
            log.warning("AI provider returned a response with no choices")
            raise LLMResponseError("AI provider returned no choices") from exc

        content = getattr(getattr(choice, "message", None), "content", None)
        if not content or not str(content).strip():
            log.warning("AI provider returned an empty response")
            raise LLMResponseError("AI provider returned an empty response")

        usage = getattr(response, "usage", None)
        return LLMReply(
            text=str(content),
            model=model,
            prompt_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
            completion_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
            finish_reason=getattr(choice, "finish_reason", None),
        )

    def _call(self, **kwargs: Any) -> Any:
        """Invoke the provider, mapping every failure to a typed domain error."""
        try:
            return self._client.chat.completions.create(**kwargs)
        except AuthenticationError:
            # The cause is deliberately dropped: the provider's message can
            # echo request headers, which would leak a credential into logs
            # (SR-5). The exception is logged without the provider text.
            log.error("AI provider rejected credentials")
            raise LLMAuthenticationError("AI provider rejected credentials") from None
        except (APITimeoutError, TimeoutError) as exc:
            log.warning("AI provider timed out after %ss", self._settings.LLM_TIMEOUT_SECONDS)
            raise LLMTimeoutError("AI provider timed out") from exc
        except RateLimitError as exc:
            log.warning("AI provider rate limit reached")
            raise LLMServiceError("AI provider rate limit reached") from exc
        except APIStatusError as exc:
            # 4xx statuses other than the explicitly transient ones will fail
            # identically on every attempt, so they must not be retried
            # (FR-4.4). 402 in particular means the account has no credit.
            status = exc.status_code
            if status in _TRANSIENT_STATUS:
                log.warning("AI provider transient error: %s", status)
                raise LLMServiceError(f"AI provider transient error {status}") from exc
            log.error("AI provider rejected the request: %s", status)
            error = LLMServiceError(f"AI provider rejected the request ({status})")
            error.retryable = False
            raise error from exc
        except APIConnectionError as exc:
            log.warning("AI provider unreachable")
            raise LLMServiceError("AI provider unreachable") from exc
        except Exception as exc:
            log.exception("unexpected AI provider error")
            raise LLMServiceError("AI provider error") from exc

    def _create_completion(self, prompt: str, temperature: float, json_mode: bool) -> str:
        kwargs: dict[str, Any] = {
            "model": self._settings.LLM_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
            # Explicit output bound; previously unbounded (H-3, P0-11).
            "max_tokens": self._settings.LLM_MAX_TOKENS,
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        try:
            response = self._client.chat.completions.create(**kwargs)
        except AuthenticationError:
            # The cause is deliberately dropped: the provider's message can
            # echo request headers, which would leak a credential into logs
            # (SR-5). The exception is logged without the provider text.
            log.error("AI provider rejected credentials")
            raise LLMAuthenticationError("AI provider rejected credentials") from None
        except (APITimeoutError, TimeoutError) as exc:
            log.warning("AI provider timed out after %ss", self._settings.LLM_TIMEOUT_SECONDS)
            raise LLMTimeoutError("AI provider timed out") from exc
        except RateLimitError as exc:
            log.warning("AI provider rate limit reached")
            raise LLMServiceError("AI provider rate limit reached") from exc
        except APIStatusError as exc:
            # 4xx statuses other than the explicitly transient ones will fail
            # identically on every attempt, so they must not be retried
            # (FR-4.4). 402 in particular means the account has no credit.
            status = exc.status_code
            if status in _TRANSIENT_STATUS:
                log.warning("AI provider transient error: %s", status)
                raise LLMServiceError(f"AI provider transient error {status}") from exc
            log.error("AI provider rejected the request: %s", status)
            error = LLMServiceError(f"AI provider rejected the request ({status})")
            error.retryable = False
            raise error from exc
        except APIConnectionError as exc:
            log.warning("AI provider unreachable")
            raise LLMServiceError("AI provider unreachable") from exc
        except Exception as exc:
            log.exception("unexpected AI provider error")
            raise LLMServiceError("AI provider error") from exc

        content = response.choices[0].message.content
        if not content or not content.strip():
            log.warning("AI provider returned an empty response")
            raise LLMResponseError("AI provider returned an empty response")
        return content

    def _parse_analysis(self, content: str) -> QueryAnalysis:
        try:
            payload = json.loads(content)
        except json.JSONDecodeError as exc:
            log.warning("AI provider returned non-JSON content")
            raise LLMResponseError("AI provider returned malformed JSON") from exc

        if not isinstance(payload, dict):
            log.warning("AI provider returned JSON of unexpected type: %s", type(payload).__name__)
            raise LLMResponseError("AI provider returned an unexpected payload shape")

        try:
            return QueryAnalysis.model_validate(payload)
        except ValidationError as exc:
            log.warning("AI provider response failed validation: %s", exc.error_count())
            raise LLMResponseError("AI provider response failed validation") from exc


def parse_fallback_models(raw: str) -> tuple[str, ...]:
    """Parse the configured fallback chain.

    Duplicates and blanks are dropped, and the primary model is removed if it
    appears again, so the chain cannot retry a model that already failed.
    """
    seen: list[str] = []
    for part in (raw or "").split(","):
        name = part.strip()
        if name and name not in seen:
            seen.append(name)
    return tuple(seen)


class FallbackProvider:
    """Tries a bounded chain of models on retryable failures (FR-32, FR-33).

    Contract:

    * Only a **retryable** failure advances the chain. Authentication failure,
      invalid request, and a malformed response are terminal (FR-34) — trying
      the same broken input against another model cannot help, and it would
      multiply cost.
    * The chain is bounded twice: by the number of configured models and by
      ``max_attempts``, so a persistent failure cannot fan out without limit.
    * The last failure is re-raised, so the caller still sees the real error and
      still knows whether it is retryable at the task level.
    """

    def __init__(
        self,
        provider: Any,
        models: Sequence[str],
        *,
        max_attempts: int = 1,
    ) -> None:
        self._provider = provider
        self._models = tuple(models)
        # max_attempts counts total tries, including the primary. 0 disables
        # fallback entirely.
        self._max_attempts = max(0, max_attempts)
        self.attempts: list[str] = []
        "Records the models tried, for assertions and for log correlation."

    @property
    def models(self) -> tuple[str, ...]:
        return self._models

    def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: str,
        temperature: float,
        max_tokens: int,
    ) -> LLMReply:
        chain = self._chain(model)
        last: BaseException | None = None

        for index, candidate in enumerate(chain):
            try:
                reply = self._provider.complete(
                    messages,
                    model=candidate,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
            except ResearchError as exc:
                last = exc
                self.attempts.append(candidate)
                if not exc.retryable or index == len(chain) - 1:
                    log.warning(
                        "model %s failed (%s); not falling back", candidate, type(exc).__name__
                    )
                    raise
                log.warning(
                    "model %s failed with a retryable error (%s); trying %s",
                    candidate, type(exc).__name__, chain[index + 1],
                )
                continue
            self.attempts.append(candidate)
            return reply

        # Unreachable while the chain is non-empty, which it always is because
        # the primary model is prepended. Kept so a future edit cannot silently
        # return None.
        raise last or LLMServiceError("no model available")  # pragma: no cover

    def _chain(self, primary: str) -> tuple[str, ...]:
        ordered = [primary, *(m for m in self._models if m != primary)]
        if self._max_attempts <= 0:
            return (primary,)
        return tuple(ordered[: self._max_attempts + 1])


def create_llm_client(settings: Settings, provider: str | None = None) -> Any:
    """Build the configured provider, wrapped in the fallback chain (FR-16).

    This is the extension point. ``LLM_PROVIDER`` selects the implementation;
    the service layer depends only on :class:`app.domain.ports.LLMProvider`, so
    adding a provider is a change here and nowhere else (T-6).
    """
    name = (provider or settings.LLM_PROVIDER or "openai_compatible").strip().lower()

    if name in {"openai_compatible", "openai-compatible", "openai", "groq", "deepseek"}:
        # One implementation covers every OpenAI-compatible endpoint. The
        # endpoint and key are configuration, so the same class serves Groq,
        # DeepSeek, OpenRouter, or a local server.
        base: Any = OpenAICompatibleClient(settings)
    else:
        raise ValueError(
            f"Unknown LLM_PROVIDER {name!r}. Supported: openai_compatible. "
            "Add a branch here to register another provider (FR-16)."
        )

    models = parse_fallback_models(settings.LLM_FALLBACK_MODELS)
    if not models or settings.LLM_FALLBACK_MAX_ATTEMPTS <= 0:
        return base
    return FallbackProvider(
        base, models, max_attempts=settings.LLM_FALLBACK_MAX_ATTEMPTS
    )
