"""The concrete research planner (Phase 3, P3.6).

One adapter satisfies :class:`~app.domain.research.ports.PlanningProvider`. It is
the *only* place that knows how to turn a question into a plan, and it holds no
capability beyond that: no search client, no HTTP client, no fetcher, no
extractor, no database session, no Telegram client, and no filesystem. That is the
whole security argument for the stage (AD-032) — the model is asked to produce
structured data and there is nothing here for that data to *do*.

It reuses Phase 2's :class:`~app.domain.ports.LLMProvider` rather than
introducing a second provider abstraction. The Phase 2 port is
``complete(messages, model=..., temperature=..., max_tokens=...) -> LLMReply``,
which already: takes provider-neutral ``Message`` objects, returns a validated
domain object, and maps every provider failure to a typed ``LLMError`` carrying the
``retryable`` flag the task-level policy reads. Nothing needed extending for
planning, so nothing was extended — and the chat path is untouched, which is the
strongest available evidence that the two are independent.

**No retry here.** The provider's own bounded retry (``LLM_MAX_RETRIES``) and the
model fallback chain (Phase 2 AD-024) are already in place below this class. A
malformed *plan* is a property of that one response rather than of the provider
being unavailable, so re-sending the same request would be a second retry system
for a different failure. The planner reports it and lets the orchestrator decide.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from app.core.config import Settings
from app.domain.chat import LLMReply
from app.domain.errors import PlanningInputError, PlanSchemaError
from app.domain.research.planning import ResearchPlan
from app.domain.research.ports import PlanningProvider
from app.infrastructure.planning.prompts import (
    PLANNING_PROMPT_VERSION,
    build_planning_messages,
    prompt_fingerprint,
)

log = logging.getLogger(__name__)

#: A model asked for bare JSON will sometimes wrap it in a code fence despite the
#: instruction not to. Stripping exactly one leading fence and one trailing fence
#: is the narrowest accommodation that works; anything more generous (searching for
#: a brace-balanced substring anywhere in the text) would accept a response that
#: also contains prose, which is the case worth rejecting.
_FENCE_RE = re.compile(r"\A\s*```(?:json)?\s*\n(?P<body>.*?)\n?```\s*\Z", re.DOTALL)


class LLMPlanner:
    """``PlanningProvider`` backed by the existing OpenAI-compatible provider."""

    def __init__(
        self,
        llm: Any,
        settings: Settings,
    ) -> None:
        # Injected, never constructed here. The planner depends on the Phase 2 port,
        # so a test supplies a fake and no planner test needs a model.
        self._llm = llm
        self._settings = settings

    async def plan(self, question: str) -> ResearchPlan:
        """Produce a validated plan for ``question``.

        The boundary this method enforces is the important one: **whatever comes
        back is untrusted text until the domain has validated it.** The sequence is
        fixed — build the bounded prompt, ask the provider, parse, validate — and
        nothing downstream ever sees the raw response.
        """
        cleaned = self._validate_question(question)
        messages = build_planning_messages(cleaned, max_steps=self._settings.PLAN_MAX_STEPS)

        # Provider failures (timeout, authentication, rate limit, empty response)
        # propagate untouched: they are already typed and already carry the
        # retryable flag the task policy reads.
        reply: LLMReply = self._llm.complete(
            messages,
            model=self._settings.LLM_MODEL,
            temperature=self._settings.LLM_TEMPERATURE,
            max_tokens=self._settings.PLAN_MAX_TOKENS,
        )

        plan = self._to_plan(reply)
        log.info(
            "research plan produced: %d steps, depth %d, prompt version %s",
            len(plan.steps),
            plan.dependency_depth,
            PLANNING_PROMPT_VERSION,
        )
        return plan

    # --- internals -----------------------------------------------------------

    def _validate_question(self, question: str) -> str:
        """Reject an unusable question before any model call.

        Two reasons, both about bounding work rather than about content: an empty
        question cannot produce a plan, and an unbounded one would make the prompt
        unbounded (§ 29). The length check runs on the trimmed text so a question
        padded with whitespace is not rejected for being long.
        """
        if not isinstance(question, str):
            raise PlanningInputError("research question must be a string")
        cleaned = question.strip()
        if not cleaned:
            raise PlanningInputError("research question must not be empty")
        limit = self._settings.PLAN_MAX_QUESTION_CHARS
        if len(cleaned) > limit:
            # The question itself is attacker-controlled and deliberately absent
            # from the message: echoing it would put unbounded user content into a
            # log line (SR-7).
            raise PlanningInputError(
                f"research question must be at most {limit} characters"
            )
        return cleaned

    def _to_plan(self, reply: LLMReply) -> ResearchPlan:
        """Parse a provider reply into a validated plan.

        Three distinct failures, three types, so a caller can tell them apart:
        not JSON at all (``PlanSchemaError``), JSON of the wrong shape
        (``PlanSchemaError``), and a well-shaped but invalid plan
        (``PlanValidationError`` from the domain).
        """
        payload = self._parse_payload(reply.text)
        return ResearchPlan.from_payload(payload)

    def _parse_payload(self, text: str) -> dict[str, Any]:
        """Parse the model's text as the planning JSON object.

        No provider response text is logged, quoted, or included in an error: it is
        model output derived from an untrusted question, and a model will
        occasionally echo it back (SR-7).
        """
        if not isinstance(text, str) or not text.strip():
            raise PlanSchemaError("planning provider returned an empty response")

        candidate = text.strip()
        fenced = _FENCE_RE.match(candidate)
        if fenced:
            candidate = fenced.group("body").strip()

        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError as exc:
            log.warning("planning provider returned unparseable output (%s)", exc.msg)
            raise PlanSchemaError(
                "planning provider did not return valid JSON"
            ) from None

        if not isinstance(payload, dict):
            raise PlanSchemaError("planning provider must return a JSON object")

        # `steps` is the plan's substance. An object without it is a refusal, a
        # wrapper, or an answer to a different question — all cases where retrying
        # the parse would be pointless, so they are named rather than coerced into
        # an empty plan.
        if "steps" not in payload:
            raise PlanSchemaError(
                "planning response has no 'steps' field; it does not describe a plan"
            )
        return payload


def create_planner(settings: Settings, llm: Any | None = None) -> PlanningProvider:
    """Build the configured planner (the single selection point for P3.6).

    ``llm`` is injected rather than constructed here for the same reason the
    fetcher's factory takes no client: the composition root decides which provider
    exists (:func:`app.infrastructure.llm.create_llm_client` builds it, once), and
    this stage must not create a second one. ``DEVELOPMENT_RULES.md`` § 4 is
    explicit that a module must not build globals at import time.

    One planner, no chain: with a single implementation there is nothing to fall
    back to, matching the single-adapter decision P3.2 made for search (AD-036).
    A second planner backend would be a branch here, not a change to the domain.
    """
    if llm is None:
        from app.infrastructure.llm import create_llm_client

        llm = create_llm_client(settings)
    return LLMPlanner(llm, settings)


__all__ = [
    "PLANNING_PROMPT_VERSION",
    "LLMPlanner",
    "PlanningProvider",
    "create_planner",
    "prompt_fingerprint",
]