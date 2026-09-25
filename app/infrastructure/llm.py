"""AI provider client.

Wraps the OpenAI-compatible SDK behind an injectable interface so the
application never imports a provider SDK directly (P1-6, FR-5.4).

Audit references: C-1 (hardcoded key/endpoint/model), H-3 (no timeout, no
token limit, no retry), H-4 (unguarded json.loads), M-10 (untyped contract).
"""

from __future__ import annotations

import json
import logging
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
from app.domain.errors import (
    LLMAuthenticationError,
    LLMResponseError,
    LLMServiceError,
    LLMTimeoutError,
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
        removed. Not called by any Phase 1 code path.
        """
        joined = "\n".join(f"- {answer}" for answer in sub_answers)
        prompt = (
            f"Original user question: {user_query}\n\n"
            f"Collected answers to the sub-questions:\n{joined}\n\n"
            "Write one comprehensive, accurate, readable final answer."
        )
        return self._create_completion(prompt=prompt, temperature=0.5, json_mode=False)

    # --- internals ---------------------------------------------------------

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
