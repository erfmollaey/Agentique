"""Context assembly.

Owns retrieval order, budgeting, and truncation — and nothing else. It has no
knowledge of Telegram and no knowledge of the provider SDK (Phase 2 § 6), so it
is testable on its own.

Policy (Phase 2 § 11, FR-7 … FR-10)
-----------------------------------
Deterministic recent-window truncation:

1. History is read oldest-first from the repository, already ordered by
   ``(created_at, id)`` so reconstruction is stable.
2. At most ``LLM_CONTEXT_MAX_MESSAGES`` messages are considered.
3. The oldest are dropped until the estimated total — system prompt plus history
   plus the new message — fits ``LLM_CONTEXT_MAX_TOKENS``.
4. If a single message still does not fit, it is kept alone and the shortfall is
   reported. Dropping the current message would mean answering a question the
   model never saw, which is worse than a long context.

Summarization and semantic memory are deliberately absent. Phase 2 § 24 and the
phase's own § 4 exclude them, and both would need a second model call per turn.

Token estimation is behind :class:`TokenEstimator`. The shipped implementation
is a character-ratio estimate: deterministic, dependency-free, and slightly
conservative. A real tokenizer replaces it without touching any caller.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass

from app.core.config import Settings
from app.domain.chat import Message as ChatMessage
from app.domain.chat import MessageRecord, Role
from app.services.prompts import HISTORY_TRUNCATED_NOTICE, build_system_prompt

log = logging.getLogger(__name__)

# Per-message framing the provider adds on top of the text. Small, fixed, and
# only used to keep the estimate from being absurdly low for many short turns.
_MESSAGE_OVERHEAD_TOKENS = 4


class CharRatioTokenEstimator:
    """Deterministic token estimate: ``ceil(len(text) / chars_per_token)``.

    Not a tokenizer. It is a conservative, reproducible bound that needs no model
    file and no network. Isolated behind the interface so a real tokenizer can
    replace it (Phase 2 § 6).
    """

    def __init__(self, chars_per_token: float = 4.0) -> None:
        if chars_per_token <= 0:
            raise ValueError("chars_per_token must be positive")
        self._chars_per_token = chars_per_token

    def count(self, text: str) -> int:
        if not text:
            return 0
        return max(1, -(-len(text) // int(self._chars_per_token)))

    def count_messages(self, messages: Sequence[ChatMessage]) -> int:
        total = 0
        for message in messages:
            total += self.count(message.content) + _MESSAGE_OVERHEAD_TOKENS
        return total


@dataclass(frozen=True)
class AssembledContext:
    """The messages to send, plus what had to be given up to fit."""

    messages: tuple[ChatMessage, ...]
    included: int
    dropped: int
    estimated_tokens: int
    truncated: bool
    over_budget: bool
    """True when even the current message alone exceeds the budget."""


class ContextBuilder:
    """Builds the provider-facing message list for one turn."""

    def __init__(self, settings: Settings, estimator: CharRatioTokenEstimator | None = None) -> None:
        self._max_messages = settings.LLM_CONTEXT_MAX_MESSAGES
        self._max_tokens = settings.LLM_CONTEXT_MAX_TOKENS
        self._estimator = estimator or CharRatioTokenEstimator(settings.LLM_CHARS_PER_TOKEN)

    @property
    def estimator(self) -> CharRatioTokenEstimator:
        return self._estimator

    def assemble(
        self,
        history: Sequence[MessageRecord],
        user_text: str,
        *,
        max_messages: int | None = None,
        max_tokens: int | None = None,
    ) -> AssembledContext:
        """Assemble system instructions + history + the current message.

        ``history`` must already be in ascending chronological order. The current
        message is appended last and is never dropped for being old.
        """
        budget_messages = self._max_messages if max_messages is None else max_messages
        budget_tokens = self._max_tokens if max_tokens is None else max_tokens

        system = ChatMessage(role=Role.SYSTEM, content=build_system_prompt())
        window = [m for m in history if m.role in (Role.USER, Role.ASSISTANT)]

        # Step 1: the message cap. Anything dropped here is dropped history, and
        # the model is told, exactly as for a token-budget trim.
        dropped = 0
        truncated = False
        if budget_messages >= 0 and len(window) > budget_messages:
            dropped = len(window) - budget_messages
            window = window[-budget_messages:] if budget_messages else []
            truncated = True

        # Step 2: the token budget, trimming from the oldest end.
        while window:
            candidate = self._as_messages(window, user_text, truncated=truncated)
            if self._estimator.count_messages(candidate) <= budget_tokens:
                break
            window.pop(0)
            dropped += 1
            truncated = True

        messages = self._as_messages(window, user_text, truncated=truncated)
        estimated = self._estimator.count_messages(messages)
        over_budget = estimated > budget_tokens

        if truncated:
            log.info(
                "context truncated: kept %s of %s messages (%s tokens of %s budget)",
                len(window), len(window) + dropped, estimated, budget_tokens,
            )
        return AssembledContext(
            messages=(system, *messages),
            included=len(window),
            dropped=dropped,
            estimated_tokens=estimated,
            truncated=truncated,
            over_budget=over_budget,
        )

    # --- internals ---------------------------------------------------------

    def _as_messages(
        self,
        window: Sequence[MessageRecord],
        user_text: str,
        *,
        truncated: bool,
    ) -> list[ChatMessage]:
        out: list[ChatMessage] = [
            ChatMessage(role=record.role, content=record.content) for record in window
        ]
        if truncated:
            # A static instruction so the model is told history is incomplete,
            # rather than left to infer it. Contains no user content (FR-11).
            out.insert(0, ChatMessage(role=Role.SYSTEM, content=HISTORY_TRUNCATED_NOTICE))
        out.append(ChatMessage(role=Role.USER, content=user_text))
        return out
