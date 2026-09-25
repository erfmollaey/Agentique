"""Research orchestration service.

The application layer: calls the AI service, renders the result, and hands it
to delivery. Contains no Telegram client construction, no provider SDK, and no
Celery machinery (P1-5, TR-2).

Audit references: the original task body did composition, formatting,
event-loop management and transport inline in 18 lines (M-4).
"""

from __future__ import annotations

import logging

from app.core.config import Settings
from app.domain.errors import ResearchError
from app.domain.schemas import QueryAnalysis
from app.infrastructure.llm import LLMClient
from app.services.delivery import DeliveryService
from app.services.formatting import render_analysis

log = logging.getLogger(__name__)

USER_ERRORS: dict[type[ResearchError], str] = {
    ResearchError: "Something went wrong. Please try again.",
}


def _user_message(exc: ResearchError) -> str:
    """Map a failure to a non-revealing, user-facing message (FR-6.4, SR-9).

    Never includes provider text, credentials, or a traceback.
    """
    from app.domain.errors import (  # local import keeps the module import graph flat
        BrokerUnavailableError,
        DeliveryError,
        LLMAuthenticationError,
        LLMResponseError,
        LLMServiceError,
        LLMTimeoutError,
        RateLimitedError,
    )

    return {
        RateLimitedError: "You have sent too many requests. Please try again shortly.",
        LLMAuthenticationError: "The AI service is unavailable. Please try again later.",
        LLMTimeoutError: "No response in time. Please try again.",
        LLMServiceError: "The AI service is unavailable. Please try again later.",
        LLMResponseError: "The response was invalid. Please rephrase your question.",
        BrokerUnavailableError: "The processing queue is unavailable. Please try again later.",
        DeliveryError: "Could not send the response.",
    }.get(type(exc), USER_ERRORS[ResearchError])


class ResearchService:
    """Analyses a question and delivers the result to a chat."""

    def __init__(
        self,
        llm: LLMClient,
        delivery: DeliveryService,
        settings: Settings,
    ) -> None:
        self._llm = llm
        self._delivery = delivery
        self._settings = settings

    def analyze(self, query: str) -> QueryAnalysis:
        """Run one analysis. Raises a typed ResearchError on failure."""
        return self._llm.analyze_query(query)

    def render(self, analysis: QueryAnalysis) -> str:
        """Render an analysis into safe, sendable Telegram text."""
        return render_analysis(analysis.summary, analysis.sub_questions)

    def execute(self, chat_id: int | str, query: str) -> int:
        """Analyse and deliver, raising on failure.

        Deliberately does not swallow the error. The task needs to know whether
        the failure is retryable so it does not message the user on an attempt
        that will simply be retried (FR-4.4).
        """
        analysis = self.analyze(query)
        return self._delivery.send(chat_id, self.render(analysis))

    def notify_failure(self, chat_id: int | str, exc: BaseException) -> int:
        """Send a terminal, non-revealing failure notice (FR-3.4, FR-6.2)."""
        message = (
            _user_message(exc)
            if isinstance(exc, ResearchError)
            else _user_message(ResearchError("unexpected"))
        )
        try:
            return self._delivery.send(chat_id, message)
        except Exception:  # never mask the original failure
            log.error("could not deliver failure notice to chat %s", chat_id)
            return 0

    def run(self, chat_id: int | str, query: str) -> int:
        """Analyse and deliver, guaranteeing a terminal user-facing outcome.

        Used where no retry policy applies. The Celery task uses
        :meth:`execute` and :meth:`notify_failure` instead so it can honour the
        retry policy.
        """
        try:
            return self.execute(chat_id, query)
        except Exception as exc:  # never let a task die silently
            if not isinstance(exc, ResearchError):
                log.exception("unexpected analysis failure for chat %s", chat_id)
            else:
                log.warning("analysis failed for chat %s: %s", chat_id, type(exc).__name__)
            return self.notify_failure(chat_id, exc)
