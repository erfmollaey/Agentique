"""The concrete per-source analyzer (Phase 3, P3.7).

One adapter satisfies both provider ports this stage needs —
:class:`~app.domain.research.ports.SourceSummarizer` and
:class:`~app.domain.research.ports.ClaimExtractor`. It is the *only* place that
knows how to turn a source's passages into model output, and it holds no
capability beyond that: no search client, no HTTP client, no fetcher, no browser,
no filesystem, no database session, no Telegram client, and no tool-calling
surface. That is the whole security argument for the stage (AD-032) — the model is
asked to produce structured data and there is nothing here for that data to *do*.

It reuses Phase 2's :class:`~app.domain.ports.LLMProvider` rather than
introducing a second provider abstraction. The Phase 2 port is
``complete(messages, model=..., temperature=..., max_tokens=...) -> LLMReply``,
which already takes provider-neutral ``Message`` objects, returns a validated
domain object, and maps every provider failure to a typed ``LLMError`` carrying the
``retryable`` flag the task-level policy reads. Nothing needed extending, so
nothing was extended — and the chat path is untouched, which is the strongest
available evidence that the two are independent.

**The output contract is closed and validated here.** Both methods parse the reply
as JSON, refuse prose around it, refuse unknown keys, and hand the result to the
domain for validation. A model cannot construct a summary or an accepted claim: it
can only return text, and everything that is written is text this layer checked
first.

**No retry here.** The provider's own bounded retry (``LLM_MAX_RETRIES``) and model
fallback chain (AD-024) already sit below this class. A malformed response is a
property of that one answer rather than of the provider being unavailable, so
re-sending the same request would be a second retry system for a different failure
— the same reasoning P3.6 recorded for the planner.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from app.core.config import Settings
from app.domain.chat import LLMReply
from app.domain.errors import (
    ClaimExtractionSchemaError,
    SummarySchemaError,
)
from app.domain.research.evidence import ClaimExtraction, SourceContent, SourceSummary
from app.infrastructure.evidence.prompts import (
    CLAIM_EXTRACTION_PROMPT_VERSION,
    SUMMARIZATION_PROMPT_VERSION,
    build_claim_extraction_messages,
    build_summary_messages,
    prompt_fingerprint,
)

log = logging.getLogger(__name__)

#: A model asked for bare JSON will sometimes wrap it in a code fence despite the
#: instruction not to. Stripping exactly one leading fence and one trailing fence
#: is the narrowest accommodation that works; anything more generous (searching for
#: a brace-balanced substring anywhere in the text) would accept a response that
#: also contains prose, which is the case worth rejecting — especially here, where
#: prose around the JSON is a plausible place for an injected instruction to ride.
_FENCE_RE = re.compile(r"\A\s*```(?:json)?\s*\n(?P<body>.*?)\n?```\s*\Z", re.DOTALL)

#: Keys the summary response may carry. Closed, because an unrecognized key is
#: content this stage would otherwise store next to a real field.
_SUMMARY_KEYS = frozenset({"schema_version", "summary", "language"})

#: Keys the claim-extraction response may carry, at the envelope level. The keys of
#: each claim entry are checked by the domain, which owns that contract.
_CLAIM_KEYS = frozenset({"schema_version", "claims"})


class LLMClaimExtractor:
    """``ClaimExtractor`` backed by the existing OpenAI-compatible provider."""

    def __init__(self, llm: Any, settings: Settings) -> None:
        # Injected, never constructed here: a test supplies a fake and no claim
        # extraction test needs a model.
        self._llm = llm
        self._settings = settings

    async def extract(self, source: SourceContent) -> ClaimExtraction:
        """Ask the provider for candidate claims over this source's passages.

        Returns **candidates**. Nothing here verifies anything: verification is
        :func:`app.domain.research.evidence.verify_claims`, called by the service,
        because the ability to accept a claim must not live behind a port an
        implementation can replace (AD-033).
        """
        messages = build_claim_extraction_messages(
            source, max_claims=self._settings.CLAIM_MAX_CLAIMS
        )

        # Provider failures (timeout, authentication, rate limit, empty response)
        # propagate untouched: they are already typed and already carry the
        # retryable flag the task policy reads.
        reply: LLMReply = self._llm.complete(
            messages,
            model=self._settings.LLM_MODEL,
            temperature=self._settings.LLM_TEMPERATURE,
            max_tokens=self._settings.CLAIM_MAX_TOKENS,
        )

        payload = _parse_object(reply.text, ClaimExtractionSchemaError)
        unknown = sorted(set(payload) - _CLAIM_KEYS)
        if unknown:
            raise ClaimExtractionSchemaError(
                "claim extraction response carries unsupported field(s): "
                f"{', '.join(unknown)}"
            )
        extraction = ClaimExtraction.from_payload(payload, source_id=source.source_id)
        log.info(
            "claim candidates produced: source=%d candidates=%d passages=%d "
            "prompt version %s",
            source.source_id,
            len(extraction.candidates),
            source.passage_count,
            CLAIM_EXTRACTION_PROMPT_VERSION,
        )
        return extraction


class LLMSourceSummarizer:
    """``SourceSummarizer`` backed by the existing OpenAI-compatible provider."""

    def __init__(self, llm: Any, settings: Settings) -> None:
        self._llm = llm
        self._settings = settings

    async def summarize(self, source: SourceContent) -> SourceSummary:
        """Ask the provider to describe this one source.

        One source, one call, no comparison. The port takes a single
        ``SourceContent``, so there is no second source in reach to synthesize
        with (FR-31 is a per-source requirement; FR-33 is P3.8's).
        """
        messages = build_summary_messages(source)

        reply: LLMReply = self._llm.complete(
            messages,
            model=self._settings.LLM_MODEL,
            temperature=self._settings.LLM_TEMPERATURE,
            max_tokens=self._settings.SUMMARY_MAX_TOKENS,
        )

        payload = _parse_object(reply.text, SummarySchemaError)
        unknown = sorted(set(payload) - _SUMMARY_KEYS)
        if unknown:
            raise SummarySchemaError(
                f"summary response carries unsupported field(s): {', '.join(unknown)}"
            )
        summary = SourceSummary.from_payload(payload, source_id=source.source_id)
        log.info(
            "source summary produced: source=%d chars=%d passages=%d prompt version %s",
            summary.source_id,
            len(summary.text),
            source.passage_count,
            SUMMARIZATION_PROMPT_VERSION,
        )
        return summary


def _parse_object(text: str, error: type[Exception]) -> dict[str, Any]:
    """Parse a provider reply as a JSON object, or raise ``error``.

    One narrow parser shared by both responses, because both have the same three
    failure shapes: empty text, non-JSON, and JSON that is not an object.

    No provider response text is logged, quoted, or included in an error: it is
    model output derived from untrusted fetched content, and a model will
    occasionally echo an injected instruction back (SR-7).
    """
    if not isinstance(text, str) or not text.strip():
        raise error("evidence provider returned an empty response")

    candidate = text.strip()
    fenced = _FENCE_RE.match(candidate)
    if fenced:
        candidate = fenced.group("body").strip()

    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError as exc:
        log.warning("evidence provider returned unparseable output (%s)", exc.msg)
        raise error("evidence provider did not return valid JSON") from None

    if not isinstance(payload, dict):
        raise error("evidence provider must return a JSON object")
    return payload


def create_source_summarizer(settings: Settings, llm: Any | None = None) -> LLMSourceSummarizer:
    """Build the configured summarizer (the single selection point for P3.7)."""
    return LLMSourceSummarizer(_provider(settings, llm), settings)


def create_claim_extractor(settings: Settings, llm: Any | None = None) -> LLMClaimExtractor:
    """Build the configured claim extractor (the single selection point for P3.7)."""
    return LLMClaimExtractor(_provider(settings, llm), settings)


def _provider(settings: Settings, llm: Any | None) -> Any:
    """The Phase 2 provider, injected or created once by the composition root.

    ``llm`` is injected rather than constructed here for the same reason the
    planner's factory takes no client: the composition root decides which provider
    exists (:func:`app.infrastructure.llm.create_llm_client` builds it, once), and
    this stage must not create a second one. ``DEVELOPMENT_RULES.md`` § 4 is
    explicit that a module must not build globals at import time.

    One implementation, no chain, matching the single-adapter decisions P3.2
    (AD-036) and P3.6 made. A second evidence backend would be a branch here, not a
    change to the domain.
    """
    if llm is not None:
        return llm
    from app.infrastructure.llm import create_llm_client

    return create_llm_client(settings)


__all__ = [
    "CLAIM_EXTRACTION_PROMPT_VERSION",
    "SUMMARIZATION_PROMPT_VERSION",
    "LLMClaimExtractor",
    "LLMSourceSummarizer",
    "create_claim_extractor",
    "create_source_summarizer",
    "prompt_fingerprint",
]