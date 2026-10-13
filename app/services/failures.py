"""Failure to user-facing message.

One mapping from a typed domain error to something a user can read. Two rules,
both from the Phase 1 findings this preserves:

* **Never disclose internal detail (FR-6.4, FR-22, SR-9).** No provider text, no
  status codes, no SQL, no traceback, no identifiers.
* **Every failure is distinguishable (FR-22, FR-23).** A provider outage, a
  timeout, a persistence fault, and a throttle produce different messages, so a
  user is not told "something went wrong" for four unrelated problems.

Moved out of the now-removed ``app/services/research.py`` in Phase 2 so the chat
service and the Telegram handler can both use it without importing each other.
"""

from __future__ import annotations

from app.domain.errors import (
    ConversationNotFoundError,
    DailyQuotaExceededError,
    DeliveryError,
    DuplicateMessageError,
    LLMAuthenticationError,
    LLMResponseError,
    LLMServiceError,
    LLMTimeoutError,
    MessageTooLongError,
    PersistenceError,
    PlanningInputError,
    RateLimitedError,
    ResearchDeadlineError,
    ResearchError,
    ResearchNoEvidenceError,
    ResearchPipelineError,
    SearchNoResultsError,
    SynthesisInputError,
)

GENERIC = "Something went wrong. Please try again."

#: Keyed by exact type. A subclass that is not listed deliberately falls through
#: to :data:`GENERIC` rather than inheriting a message meant for its parent.
MESSAGES: dict[type[ResearchError], str] = {
    RateLimitedError: "You have sent too many requests. Please try again shortly.",
    DailyQuotaExceededError: (
        "You have reached your daily request allowance. It resets shortly — "
        "please come back tomorrow."
    ),
    LLMAuthenticationError: "The AI service is unavailable. Please try again later.",
    LLMTimeoutError: "No response in time. Please try again.",
    LLMServiceError: "The AI service is unavailable. Please try again later.",
    LLMResponseError: "The response was invalid. Please rephrase your question.",
    PersistenceError: (
        "I could not save our conversation, so I did not answer. Please try again."
    ),
    DuplicateMessageError: "That message was already processed.",
    MessageTooLongError: "That message is too long for me to handle. Please shorten it.",
    ConversationNotFoundError: "There is no conversation to do that with.",
    DeliveryError: "Could not send the response.",
    # --- the research failure family (Phase 3, P3.9-D; FR-43, § 15.9) --------
    #
    # Four messages that are deliberately *different*: "I looked and found nothing
    # I could stand behind" and "I could not finish looking" are not the same
    # outcome, and neither is "your question was too long" or "I ran out of time".
    # The audit § R.10 records that all four used to converge on one message, and
    # § B5-P4 makes telling them apart a prerequisite of the deadline policy.
    #
    # Each is stated in the same shape the chat messages already use — plain,
    # actionable, and free of provider text, URLs, identifiers, and stage names.
    # No message discloses how many sources were tried, which provider failed, or
    # why a citation did not verify.
    ResearchNoEvidenceError: (
        "I could not find enough reliable information to answer that. "
        "Try rephrasing the question or asking about something else."
    ),
    SearchNoResultsError: (
        "I could not find anything about that. Try rephrasing the question."
    ),
    SynthesisInputError: (
        "I found sources but nothing in them supported an answer with citations. "
        "Try rephrasing the question."
    ),
    ResearchDeadlineError: (
        "The research took longer than I have, so I stopped early. "
        "Please try again with a more specific question."
    ),
    ResearchPipelineError: (
        "I could not complete that research. Please try again shortly."
    ),
    PlanningInputError: (
        "That research question is empty or too long for me to handle. "
        "Please keep it under the configured limit and try again."
    ),
}

#: Failures that are the user's own fault or the platform's, where retrying the
#: same message unchanged cannot help.
NOT_RETRYABLE = (
    LLMAuthenticationError,
    LLMResponseError,
    MessageTooLongError,
    ConversationNotFoundError,
    DailyQuotaExceededError,
    DuplicateMessageError,
    PersistenceError,
)


def user_message_for(exc: BaseException) -> str:
    """Return a non-revealing, user-readable message for ``exc``.

    Unknown exception types map to :data:`GENERIC`. The exception's own text is
    never used, because it can contain a provider message, a SQL fragment, or a
    credential echoed back by a service.
    """
    if isinstance(exc, ResearchError):
        return MESSAGES.get(type(exc), GENERIC)
    return GENERIC
