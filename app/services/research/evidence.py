"""Per-source summarization + evidence-grounded claim extraction service (P3.7).

The application-layer step between a selected source's persisted passages and the
persisted summary and claims. It owns the ordering of the operations and nothing
else: load this source's passages, summarize, propose candidates, verify them, and
write the accepted result atomically.

```text
selected Source + its SourceChunk rows
    → SourceSummarizer.summarize()      → SourceSummary        (about this source)
    → for each window:
        ClaimExtractor.extract()       → ClaimExtraction      (candidates, unverified)
        verify_claims()                → accepted + rejected  (programmatic)
    → one transaction: summary + claim rows + citation rows
```

**Verification happens here, not behind the ports.** ``ClaimExtractor`` returns
candidates because :func:`app.domain.research.evidence.verify_claims` is the only
way to produce a ``VerifiedClaim``. Putting verification inside an adapter would
mean the ability to accept a claim lived in a replaceable component — and the
adapter is the part of this stage closest to untrusted text. The service owns the
decision, the domain owns the rule, and no model is ever asked to check another
model (AD-033, § 15.7).

**Persistence is one transaction, after verification.** The summary and every
accepted claim and its citation are written in a single unit of work, so a failure
midway leaves nothing behind: no claims without evidence, no evidence without a
claim, no half-populated source. Nothing is written before every claim has been
decided, because a claim row with no citation row is precisely the state § 14
forbids.

**The service holds no session across a model call.** The read transaction closes
before the first provider call, so a slow or failing provider cannot hold database
connections or an open read snapshot (§ 6 performance: no unbounded waits on the
pool).

**What this service does not do.** It does not search, fetch, extract, rank,
compare sources, synthesize, or render. Cross-source analysis (FR-33, FR-34) is
P3.8; orchestration is P3.9. One source in, one source's summary and claims out.

**Re-execution is refused, not merged** (P3.9-D, B7). ``task_acks_late`` with
``task_reject_on_worker_lost`` means a hard-killed research task is redelivered and
runs from the start, so analysing the same source twice is an expected condition.
The persistence transaction therefore *claims* the source first — atomically and
under a row lock — and a source whose evidence already exists raises
:class:`~app.domain.errors.SourceAlreadyAnalyzedError`. Nothing is appended,
overwritten, or partially replaced; the caller records the source as already
evidenced and continues.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from app.core.config import Settings
from app.domain.errors import (
    ClaimEvidenceError,
    ResearchRunNotFoundError,
    ResearchSourceNotFoundError,
    SourceAlreadyAnalyzedError,
)
from app.domain.research.evidence import (
    ClaimVerification,
    EvidenceChunk,
    RejectedClaim,
    SourceContent,
    SourceEvidenceResult,
    SourceSummary,
    TokenCounter,
    VerifiedClaim,
    plan_chunk_windows,
    verify_claims,
)
from app.domain.research.ports import ClaimExtractor, SourceSummarizer
from app.domain.research.types import Source
from app.services.context import CharRatioTokenEstimator

log = logging.getLogger(__name__)


class SourceEvidenceService:
    """Summarize one source and persist its evidence-grounded claims."""

    def __init__(
        self,
        *,
        settings: Settings,
        summarizer: SourceSummarizer,
        extractor: ClaimExtractor,
        uow_factory: Callable[[], Any],
        counter: TokenCounter | None = None,
    ) -> None:
        self._settings = settings
        self._summarizer = summarizer
        self._extractor = extractor
        self._uow_factory = uow_factory
        # Phase 2's estimator, reused rather than reimplemented: the window planner
        # needs an estimate and this project already has one, with the same
        # `LLM_CHARS_PER_TOKEN` configuration behind it.
        self._counter = counter or CharRatioTokenEstimator(settings.LLM_CHARS_PER_TOKEN)

    async def analyze_source(
        self, *, run_id: int, user_id: int, source_id: int
    ) -> SourceEvidenceResult:
        """Summarize ``source_id`` and persist the claims its own passages support.

        Ownership is settled before any model call: the run is read through
        ``get_for_user`` and the source must belong to *that* run. A caller that
        named a source from another user's run is refused before anything is spent
        or written (SR-10, T-23).

        Raises, rather than returning an empty result:

        * :class:`ResearchRunNotFoundError` — the run is not this user's;
        * :class:`ResearchSourceNotFoundError` — the source is not in this run;
        * :class:`SourceAlreadyAnalyzedError` — this source's evidence is already
          persisted. Raised **before any model is called**, so a redelivered task
          does not buy a second summary and a second set of claims it cannot write
          (B7);
        * :class:`SourceContentEmptyError` — the source has no usable passage, so
          **no model is called** and nothing is written (FR-23);
        * :class:`~app.domain.errors.ChunkReplaceBlockedError` — a verified
          citation already points at one of this source's passages, so replacing
          them would destroy it;
        * the ``EvidenceError`` family — the model answered with something that is
          not the requested structure;
        * the existing ``LLM*Error`` family — provider failure, un-retried here and
          re-typed by nothing, so the task policy keeps reading ``retryable``.

        Partial success is **not** an error and not a silent thinning either: some
        candidates rejected by verification is a normal outcome, and the rejections
        are returned in the result so a caller can report them (FR-42).
        """
        content = await self._load(run_id, user_id, source_id)

        summary_window = plan_chunk_windows(
            content.chunks,
            max_tokens=self._settings.SUMMARY_MAX_INPUT_TOKENS,
            counter=self._counter,
            max_windows=1,
        )[0]
        summary_content = content.window(summary_window.chunks)
        summary: SourceSummary = await self._summarizer.summarize(summary_content)

        claim_windows = plan_chunk_windows(
            content.chunks,
            max_tokens=self._settings.CLAIM_MAX_INPUT_TOKENS,
            counter=self._counter,
            max_windows=self._settings.CLAIM_MAX_WINDOWS,
        )

        claims: list[VerifiedClaim] = []
        rejected: list[RejectedClaim] = []
        for window in claim_windows:
            window_content = content.window(window.chunks)
            extraction = await self._extractor.extract(window_content)
            verification: ClaimVerification = verify_claims(extraction, window_content)
            claims.extend(verification.accepted)
            rejected.extend(verification.rejected)

        self._assert_evidence_is_real(claims, content)
        await self._persist(run_id, source_id, summary, claims)

        result = SourceEvidenceResult(
            source_id=source_id,
            summary=summary,
            claims=tuple(claims),
            rejected=tuple(rejected),
            windows=len(claim_windows),
            summary_passages=len(summary_window.chunks),
            summary_truncated=summary_window.truncated,
            claim_passages=sum(len(window.chunks) for window in claim_windows),
            claim_truncated=any(window.truncated for window in claim_windows),
        )
        log.info(
            "source analyzed: source=%d passages=%d windows=%d claims=%d rejected=%d "
            "summary_truncated=%s claim_truncated=%s",
            source_id,
            content.passage_count,
            result.windows,
            result.claim_count,
            result.rejected_count,
            result.summary_truncated,
            result.claim_truncated,
        )
        return result

    # --- internals -----------------------------------------------------------

    async def _load(self, run_id: int, user_id: int, source_id: int) -> SourceContent:
        """Read the run, the source, and that source's passages.

        The whole read happens in one short transaction, which is closed before any
        provider call. Ordering is ``(ordinal, id)`` from the repository, and
        :class:`SourceContent` re-establishes it canonically, so the prompt, the
        window planner, and verification all see the same order.

        **A source that already has evidence is refused here, before anything is
        spent** (B7). ``summary`` is written in the same transaction as the claims
        and the citations, so it is the marker for "the evidence stage already ran",
        and reading it costs nothing this transaction was not already doing. The
        cheap read and the atomic row-locked claim in :meth:`_persist` are
        deliberately *both* present: the read keeps a redelivery from rebuying a
        summary and a set of claims it cannot write, and the claim is what makes the
        decision correct when two workers genuinely race — a read alone cannot.
        """
        async with self._uow_factory() as uow:
            run = await uow.research_runs.get_for_user(run_id, user_id)
            if run is None:
                raise ResearchRunNotFoundError(
                    f"research run {run_id} does not exist for this user"
                )
            source = await uow.sources.get(source_id)
            if source is None or source.run_id != run_id:
                # Not "found, then filtered": the source must belong to *this*
                # run, so naming a source id from any other run is indistinguishable
                # from naming one that does not exist.
                raise ResearchSourceNotFoundError(
                    f"source {source_id} does not exist for research run {run_id}"
                )
            if source.summary is not None:
                raise SourceAlreadyAnalyzedError(
                    f"source {source_id} of run {run_id} already has persisted "
                    f"evidence; re-analysing it would duplicate it"
                )
            chunks = await uow.source_chunks.list_for_source(source_id)

        # The passage rows are already this source's `source_chunks`, so there is no
        # second source's text here to leak into the prompt (§ 9).
        return SourceContent(
            source_id=source_id,
            domain=source.domain,
            title=source.title,
            chunks=tuple(
                _as_evidence_chunk(chunk, source_id=source_id) for chunk in chunks
            ),
        )

    def _assert_evidence_is_real(
        self, claims: list[VerifiedClaim], content: SourceContent
    ) -> None:
        """Re-check every accepted claim against the passages, before persistence.

        :func:`verify_claims` already did this, and doing it twice is deliberate:
        ``VerifiedClaim`` is a frozen value type, so a future caller could hand this
        service one it built by hand. The database cannot check that one row's text
        occurs inside another row's, so this is the last application-level gate, and
        a violation is a defect (``ClaimEvidenceError``) rather than a tolerance.
        """
        by_id = {chunk.chunk_id: chunk for chunk in content.chunks}
        for claim in claims:
            evidence = claim.evidence
            chunk = by_id.get(evidence.chunk_id)
            if evidence.source_id != content.source_id or chunk is None:
                raise ClaimEvidenceError(
                    f"accepted claim cites passage {evidence.chunk_id}, which is not "
                    f"part of source {content.source_id}"
                )
            if not evidence.verify_against(chunk):
                raise ClaimEvidenceError(
                    f"accepted claim's quote is not present in passage "
                    f"{evidence.chunk_id}"
                )

    async def _persist(
        self,
        run_id: int,
        source_id: int,
        summary: SourceSummary,
        claims: list[VerifiedClaim],
    ) -> None:
        """Write the summary and every accepted claim with its citation, atomically.

        One unit of work, so the whole result commits or none of it does. Each
        claim row is written with its citation row in the same transaction: a claim
        with no citation is a claim with no evidence, which § 14 forbids and which
        no constraint in this schema can express.

        **The transaction begins by claiming the source** (B7, P3.9-D).
        ``claim_unanalysed`` locks the source row and reports whether its evidence
        has already been written; because the summary is written *here*, in this
        same transaction, that flag and the data cannot disagree. Re-execution is an
        expected condition rather than a hypothetical — a hard-killed task is
        redelivered and re-runs from the start (audit § R.11) — and without this
        claim a redelivery would append a second set of claims for the same source
        and a second copy of every citation. ``uq_citations_claim_source_chunk``
        would not catch it: each duplicate claim row has its own id, so each
        citation is unique for its own claim.

        The claim is taken *after* the provider calls and *before* the first write,
        not at the start of :meth:`analyze_source`. An earlier check would still be a
        check-then-write, and this one is inside the transaction that writes. A
        refusal raises :class:`SourceAlreadyAnalyzedError` and the transaction rolls
        back, leaving the existing evidence untouched.

        Accepted claims are persisted as ``supported`` — the only status that means
        something here, and it is earned by verification rather than asserted by a
        model. ``confidence`` is left ``NULL`` on purpose: nothing in P3.7 measures
        it, and a model-supplied number stored as a confidence would be exactly the
        self-certification this stage forbids. A later stage that measures agreement
        across sources may set it.
        """
        async with self._uow_factory() as uow:
            if not await uow.sources.claim_unanalysed(run_id, source_id):
                raise SourceAlreadyAnalyzedError(
                    f"source {source_id} of run {run_id} already has persisted evidence"
                )
            await uow.sources.set_summary(source_id, run_id, summary.text)
            for claim in claims:
                persisted = await uow.claims.add(run_id, claim.statement, status="supported")
                if persisted.id is None:  # pragma: no cover - add() flushes
                    raise ClaimEvidenceError("persisted claim has no id")
                await uow.citations.add(
                    persisted.id,
                    claim.evidence.source_id,
                    claim.evidence.chunk_id,
                    claim.evidence.quote,
                    location=claim.evidence.location,
                )


def _as_evidence_chunk(chunk: Any, *, source_id: int) -> EvidenceChunk:
    """Convert a persisted ``SourceChunk`` into an :class:`EvidenceChunk`.

    Deliberately explicit rather than a pydantic ``from_attributes`` coercion: the
    chunk id is taken from the persisted row, never from anything in the passage
    text, and ``source_id`` is threaded through so a caller cannot accidentally
    build evidence content for the wrong source.
    """
    if chunk.id is None:  # pragma: no cover - add() flushes and assigns an id
        raise ClaimEvidenceError("a persisted passage has no id")
    if chunk.source_id != source_id:
        # The repository scopes the query by source, so this cannot happen through
        # the normal path. It is checked rather than assumed because this is the one
        # place where a mistake would put another source's text in this source's
        # prompt.
        raise ClaimEvidenceError(
            f"passage {chunk.id} belongs to source {chunk.source_id}, not {source_id}"
        )
    return EvidenceChunk(chunk_id=chunk.id, ordinal=chunk.ordinal, text=chunk.text)


async def load_source_evidence(
    run_id: int, user_id: int, source_id: int, uow_factory: Callable[[], Any]
) -> tuple[Source, str | None]:
    """Read a source's persisted summary back, ownership-scoped.

    Returns ``(source, summary_text_or_None)``. ``None`` is the normal state of a
    source P3.2–P3.6 created and P3.7 has not analyzed yet — not an error.
    """
    async with uow_factory() as uow:
        run = await uow.research_runs.get_for_user(run_id, user_id)
        if run is None:
            raise ResearchRunNotFoundError(
                f"research run {run_id} does not exist for this user"
            )
        source = await uow.sources.get(source_id)
        if source is None or source.run_id != run_id:
            raise ResearchSourceNotFoundError(
                f"source {source_id} does not exist for research run {run_id}"
            )
        return source, source.summary


__all__ = ["SourceEvidenceService", "load_source_evidence"]