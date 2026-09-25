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
