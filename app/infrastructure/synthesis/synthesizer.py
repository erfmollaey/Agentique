"""The concrete cross-source synthesizer (Phase 3, P3.8).

One adapter satisfies the provider port this stage needs —
:class:`~app.domain.research.ports.SynthesisProvider`. It is the *only* place that
knows how to turn a run's verified evidence into model output, and it holds no
capability beyond that: no search client, no HTTP client, no fetcher, no browser, no
filesystem, no database session, no Telegram client, and no tool-calling surface. That
is the whole security argument for the stage (AD-032) — the model is asked to produce
structured data, and there is nothing here for that data to *do*.

It reuses Phase 2's :class:`~app.domain.ports.LLMProvider` rather than introducing a
second provider abstraction. The Phase 2 port is
``complete(messages, model=..., temperature=..., max_tokens=...) -> LLMReply``, which
already takes provider-neutral ``Message`` objects, returns a validated domain object,
and maps every provider failure to a typed ``LLMError`` carrying the ``retryable``
flag the task-level policy reads. Nothing needed extending, so nothing was extended.

**The output contract is closed and validated here, and it carries identifiers only.**
The reply is parsed as JSON, prose around it is refused, unknown keys are refused, and
the result is handed to the domain as a proposal of *references*. A model cannot
construct a verified citation: :meth:`synthesize` returns
:class:`~app.domain.research.synthesis.SynthesisProposal`, whose citations are bare
ints, and producing a ``VerifiedCitation`` requires ``verify_synthesis``, which this
layer does not call and could not make meaningful even if it did — the registry lives
in the application layer, which owns the unit of work.

**No retry here**, for P3.6/P3.7's reason: the provider's own bounded retry
(``LLM_MAX_RETRIES``) and model fallback chain (AD-024) already sit below this class.
A malformed response is a property of that one answer rather than of the provider
being unavailable, so re-sending the same request would be a second retry system for
a different failure.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from app.core.config import Settings
from app.domain.chat import LLMReply
from app.domain.errors import SynthesisSchemaError
from app.domain.research.synthesis import (
    SynthesisInput,
    SynthesisProposal,
)
from app.infrastructure.synthesis.prompts import (
    SYNTHESIS_PROMPT_VERSION,
    build_synthesis_messages,
    prompt_fingerprint,
)

log = logging.getLogger(__name__)

#: A model asked for bare JSON will sometimes wrap it in a code fence despite the
#: instruction not to. Stripping exactly one leading fence and one trailing fence is
#: the narrowest accommodation that works; anything more generous (searching for a
#: brace-balanced substring anywhere in the text) would accept a response that also
#: contains prose, which is the case worth rejecting — especially here, where prose
#: around the JSON is a plausible place for an injected instruction to ride.
_FENCE_RE = re.compile(r"\A\s*```(?:json)?\s*\n(?P<body>.*?)\n?```\s*\Z", re.DOTALL)


class LLMSynthesizer:
    """``SynthesisProvider`` backed by the existing OpenAI-compatible provider."""

    def __init__(self, llm: Any, settings: Settings) -> None:
        # Injected, never constructed here: a test supplies a fake and no synthesis
        # test needs a model.
        self._llm = llm
        self._settings = settings

    async def synthesize(self, synthesis_input: SynthesisInput) -> SynthesisProposal:
        """Ask the provider to write an answer citing the evidence it was shown.

        Returns a **proposal**: answer prose plus citation *identifiers*. Nothing here
        verifies anything, because verification is
        :func:`app.domain.research.synthesis.verify_synthesis`, called by the service
        against the persisted registry — the ability to accept a reference must not
        live behind a port an implementation can replace (AD-033).
        """
        messages = build_synthesis_messages(
            synthesis_input, max_sections=self._settings.SYNTHESIS_MAX_SECTIONS
        )

        # Provider failures (timeout, authentication, rate limit, empty response)
        # propagate untouched: they are already typed and already carry the
        # retryable flag the task policy reads.
        reply: LLMReply = self._llm.complete(
            messages,
            model=self._settings.LLM_MODEL,
            temperature=self._settings.LLM_TEMPERATURE,
            max_tokens=self._settings.SYNTHESIS_MAX_TOKENS,
        )

        payload = _parse_object(reply.text)
        proposal = SynthesisProposal.from_payload(payload)
        log.info(
            "synthesis proposal produced: sections=%d contradictions=%d "
            "evidence=%d prompt version %s fingerprint %s",
            len(proposal.sections),
            len(proposal.contradictions),
            len(synthesis_input.context.entries),
            SYNTHESIS_PROMPT_VERSION,
            prompt_fingerprint(messages[0].content),
        )
        return proposal


def _parse_object(text: str) -> dict[str, Any]:
    """Parse a provider reply as a JSON object, or raise ``SynthesisSchemaError``.

    One narrow parser, the same three failure shapes P3.7's evidence parser handled:
    empty text, non-JSON, and JSON that is not an object.

    No provider response text is logged, quoted, or included in an error: it is model
    output derived from untrusted fetched content, and a model will occasionally echo
    an injected instruction back (SR-7).
    """
    if not isinstance(text, str) or not text.strip():
        raise SynthesisSchemaError("synthesis provider returned an empty response")

    candidate = text.strip()
    fenced = _FENCE_RE.match(candidate)
    if fenced:
        candidate = fenced.group("body").strip()

    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError as exc:
        log.warning("synthesis provider returned unparseable output (%s)", exc.msg)
        raise SynthesisSchemaError(
            "synthesis provider did not return valid JSON"
        ) from None

    if not isinstance(payload, dict):
        raise SynthesisSchemaError("synthesis provider must return a JSON object")
    return payload


def create_synthesizer(settings: Settings, llm: Any | None = None) -> LLMSynthesizer:
    """Build the configured synthesizer (the single selection point for P3.8)."""
    return LLMSynthesizer(_provider(settings, llm), settings)


def _provider(settings: Settings, llm: Any | None) -> Any:
    """The Phase 2 provider, injected or created once by the composition root.

    ``llm`` is injected rather than constructed here for the same reason P3.6's
    planner factory takes no client: the composition root decides which provider
    exists (:func:`app.infrastructure.llm.create_llm_client` builds it, once), and
    this stage must not create a second one. ``DEVELOPMENT_RULES.md`` § 4 is explicit
    that a module must not build globals at import time.

    One implementation, no chain, matching the single-adapter decisions P3.2
    (AD-036), P3.6, and P3.7 made. A second synthesis backend would be a branch here,
    not a change to the domain.
    """
    if llm is not None:
        return llm
    from app.infrastructure.llm import create_llm_client

    return create_llm_client(settings)


__all__ = ["SYNTHESIS_PROMPT_VERSION", "LLMSynthesizer", "create_synthesizer", "prompt_fingerprint"]