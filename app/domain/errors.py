"""Domain-level errors.

Typed so callers can distinguish failure modes without inspecting strings, and
so each maps to a distinct, non-revealing user-facing message (FR-6.4).

``retryable`` records whether retrying the operation could plausibly succeed.
Phase 1 FR-4.4 requires retryable failures to be retried a bounded number of
times and non-retryable ones not to be retried at all — a provider rejecting
the request for lack of credit, or a malformed response, will fail identically
on every attempt.
"""

from __future__ import annotations


class ResearchError(Exception):
    """Base class for every error the research flow can raise."""

    retryable: bool = True


class LLMAuthenticationError(ResearchError):
    """The provider rejected our credentials. Never retried."""

    retryable = False


class LLMTimeoutError(ResearchError):
    """The provider did not respond within the configured timeout. Retryable."""


class LLMServiceError(ResearchError):
    """The provider returned an error, or a response we cannot use.

    Retryable by default; set ``retryable = False`` for statuses that will not
    change on a second attempt.
    """


class LLMResponseError(ResearchError):
    """The provider responded, but the payload was unusable. Not retryable."""

    retryable = False


class RateLimitedError(ResearchError):
    """The caller exceeded the configured request allowance."""

    retryable = False


class BrokerUnavailableError(ResearchError):
    """The task queue could not be reached."""


class DeliveryError(ResearchError):
    """The reply could not be delivered to Telegram."""


class PersistenceError(ResearchError):
    """The database could not be read or written.

    Terminal by default: a schema or connectivity fault will not fix itself on a
    retry within the same task (FR-23).
    """

    retryable = False


class DuplicateMessageError(PersistenceError):
    """A message for this turn, or this Telegram message, already exists.

    Not a failure. It is how update redelivery and Celery redelivery are
    detected: the database constraint is the authority, and the caller decides
    whether to skip the turn (Phase 2 § 15, § 16).
    """

    retryable = False


class MessageTooLongError(ResearchError):
    """The inbound message exceeds the configured per-message limit."""

    retryable = False


class ConversationNotFoundError(ResearchError):
    """No conversation with that id belongs to that user (SR-4)."""

    retryable = False


class DailyQuotaExceededError(ResearchError):
    """The user exhausted their daily request allowance (FR-28)."""

    retryable = False

