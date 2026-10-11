"""Research pipeline orchestration (Phase 3, P3.9-D — AD-043, AD-045).

The application-layer sequence that turns one queued ``research_runs`` row into a
verified, cited, terminal outcome, and nothing else:

```text
run (queued, ownership checked)
  → running                    (one claim, transactionally)
  → plan                       PlanningService          (plan persisted on the run)
  → search                     SearchProvider           (bounded, deterministic)
  → persist sources            SourceRepository          (idempotent by URL)
  → fetch                      SafeFetcher               (the SSRF boundary)
  → extract                    ContentExtraction
  → chunk                      SourceChunkService        (replacing, guarded)
  → dedup + rank               SourceDeduplication + RankingPolicy  (pure)
  → persist ranking            SourceRepository.set_rankings
  → evidence                   SourceEvidenceService     (per selected source)
  → synthesis                  SynthesisService          (one call, verified)
  → terminal                   completed | partial | failed
```

**What this service owns and what it does not.** It owns *sequencing*, *work
bounds*, *outcome classification*, and the *deadline*. It owns no prompt, no SQL,
no HTTP client, no provider SDK, and no domain rule: every stage's operation and
validation belongs to the service or domain module that already defines it, and this
module calls them in order. That is the same rule the chat task already asserts
structurally (``test_celery_chat_task.py:292``), applied to a longer pipeline.

**Ownership is settled once and never re-derived.** The run is read through
``get_for_user`` and every ``user_id`` used downstream is the one from that read. A
source reached outside that scope is refused by the stage that reaches it, the way
P3.7 and P3.8 already do — because ``uow.sources.get`` is not user-scoped, the
caller has to close that gap, and this is where it is closed.

**No session is held across a provider call** (AD-035, § S.3.3). Every stage opens
its own unit of work, and the orchestrator holds none: the read that decides what
to fetch closes before the fetch, and the write that records it closes before the
next source. That is what makes a cooperative deadline land on a *committed*
boundary rather than in the middle of a transaction — the property AD-045 rests
on, and the reason the signal-based soft limit cannot be the mechanism.

**Idempotency is inherited, not invented here** (B7). Sources converge on
``get_for_url``; passages are *replaced* by ``SourceChunkService`` and refused when
already cited; evidence refuses a source that already has a summary; the claim on
the run is one atomic ``queued → running`` that a second delivery cannot repeat;
and the terminal write is guarded by the same lifecycle that refuses to reopen a
terminal run. A redelivered task re-executes and converges.

**A partial answer is read back from persisted evidence, never reconstructed.**
When the deadline expires, the orchestrator lets ``SynthesisService`` do exactly what
it does for any other terminal — read the citation registry from the database, verify
against it, and persist — and simply *labels* the outcome. Nothing is guessed from
transient state: if there is no persisted verified evidence, there is no partial
answer, and the run is ``failed`` saying so (AD-044, FR-41, § 15.9).

**Untrusted content stays untrusted.** No fetched string is fetched, no retrieved
text becomes an instruction or a URL, and no model output selects a source or a
stage: URLs come only from ``SearchResult`` through ``SafeFetcher``, source
identity is assigned by the database, and the type ever consulted from a page is
P3.4's ``ExtractedContent`` (AD-031, AD-032, § 15.6).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.core.config import Settings
from app.domain.errors import (
    FetchError,
    ResearchNoEvidenceError,
    ResearchPipelineError,
    ResearchRunNotFoundError,
    SearchNoResultsError,
    SynthesisInputError,
)
from app.domain.research.dedup import (
    DedupRankingResult,
    RankedResult,
    SourceCandidate,
)
from app.domain.research.evidence import SourceEvidenceResult
from app.domain.research.extraction import ExtractedContent
from app.domain.research.fetch import FetchedSource, FetchRequest
from app.domain.research.outcome import (
    CooperativeDeadline,
    RunOutcomeKind,
    summarize_run,
)
from app.domain.research.planning import ResearchPlan
from app.domain.research.ports import (
    ContentExtraction,
    RankingPolicy,
    SafeFetcher,
    SearchProvider,
    SourceDeduplication,
)
from app.domain.research.search import SafeSearchLevel, SearchRequest
from app.domain.research.synthesis import SynthesizedAnswer
from app.domain.research.types import ResearchRunStatus, Source, SourceRanking
from app.services.research.planning import PlanningService
from app.services.research.synthesis import SynthesisService

log = logging.getLogger(__name__)

#: Source statuses a candidate may be driven to by the orchestrator. Deliberately
#: the closed set the database CHECK already carries (``_SOURCE_STATUS_VALUES``):
#: the orchestrator selects a value, it never invents one.
_STATUS_PENDING = "pending"
_STATUS_FETCHED = "fetched"
_STATUS_FETCH_FAILED = "fetch_failed"
_STATUS_EXTRACTED = "extracted"
_STATUS_UNUSABLE = "unusable"
_STATUS_DUPLICATE = "duplicate"
_STATUS_EXCLUDED = "excluded"
_STATUS_SELECTED = "selected"

#: Statuses that may be selected for the evidence stage. ``excluded`` (below the
#: quality threshold) and ``duplicate`` (a non-representative copy) are both
#: *recorded* and both kept out of the model's prompt — the first is a quality
#: decision FR-30 requires be recorded, the second is a duplicate.
_SELECTABLE = frozenset({_STATUS_EXTRACTED})


class CandidateWork(BaseModel):
    """One candidate source's place in the pipeline before it becomes evidence.

    A bookkeeping value, not a domain type: it ties together the three identities a
    candidate acquires — the ``SearchResult`` the provider offered, the persisted
    ``Source`` row that result created, and the ``ExtractedContent`` P3.4 produced
    for it — because a candidate has to be identifiable in all three at once when
    it reaches the ranking stage, and ``SourceCandidate`` itself carries neither a
    source id nor a chunk set (P3.5's type is about identity, not persistence).

    ``fetched`` is the raw ``FetchedSource``, which is what ``ContentExtraction``
    takes and what ``SourceCandidate.from_search_result`` derives its content from.
    It is held in memory only for the lifetime of the pipeline and never persisted:
    a ``FetchedSource`` is transport provenance, not research content (§ 15.8).

    **Mutable on purpose.** The pipeline is a sequence of facts about one candidate
    accumulating — fetched, then extracted, then chunked, then ranked — and a frozen
    value would make every stage return a second copy with one more field filled in.
    The invariant worth protecting is not on this record: it is on the ``Source``
    row, which is durable, ownership-scoped, and only ever written through the
    repository. This working value is never persisted, never handed to a model, and
    never used to decide anything a caller could observe outside the run.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    search_order: tuple[int, int]
    """``(step index, provider rank)`` — the deterministic fetch order.

    Provider order within a step and step order across the plan. Used instead of
    arrival order because a bounded fetch has to pick the *same* candidates every
    time, including when a provider answers two plan steps out of order (§ 6).
    """

    source_id: int
    result: Any
    """The ``SearchResult`` this source was persisted from."""

    extracted: ExtractedContent | None = None
    fetched: FetchedSource | None = None
    failure_reason: str | None = None
    status: str = _STATUS_PENDING


class SourceChunkResult(BaseModel):
    """What one source's chunking produced, as the orchestrator needs to see it."""

    model_config = ConfigDict(frozen=True)

    source_id: int
    chunk_ids: tuple[int, ...] = ()
    was_replaced: bool = False

    @property
    def is_empty(self) -> bool:
        return not self.chunk_ids


class ResearchOutcome(BaseModel):
    """The terminal result of one research run, for the caller that owns it.

    **A report, not a verdict the caller re-derives.** It carries the outcome kind,
    the persisted status, the plain-text answer (when there is one), the run-level
    summary, and the counts a notification layer needs to describe coverage honestly.
    The caller sends exactly one message from it and never inspects the database to
    work out what happened.

    ``answer`` is ``None`` for every non-``COMPLETED``/``PARTIAL`` outcome, because
    a verified answer is the only thing that may be delivered (§ 17). There is
    deliberately no field for model output that failed verification: an unvalidated
    citation must not be reachable as research output by any route (§ 16).
    """

    model_config = ConfigDict(frozen=True)

    kind: RunOutcomeKind
    status: str
    answer: str | None = None
    summary: str = ""

    candidates: int = 0
    fetched: int = 0
    extracted: int = 0
    unusable: int = 0
    excluded: int = 0
    selected: int = 0
    claims: int = 0
    citations: int = 0
    synthesized: bool = False
    """Whether a verified answer was persisted, for the caller's terminal message."""

    @property
    def is_deliverable(self) -> bool:
        """Whether there is a verified answer a user may be shown."""
        return self.answer is not None


class _Tallies(BaseModel):
    """Mutable per-run counters, kept private and folded into the outcome once."""

    model_config = ConfigDict(frozen=False)

    candidates: int = 0
    fetched: int = 0
    extracted: int = 0
    unusable: int = 0
    excluded: int = 0
    selected: int = 0
    claims: int = 0
    citations: int = 0

    def degraded(self) -> bool:
        """Whether anything went wrong that a completed run would not have.

        The one test behind § R.17's ``partial`` decision: a fetch that failed, an
        extraction that produced nothing, a source excluded below the quality
        threshold, or a candidate the bound did not select. Any of them means the
        run answered with less than it set out to gather, which is exactly what
        ``partial`` is for.
        """
        return bool(self.unusable or self.excluded or self.selected < self.extracted)


class ResearchOrchestrator:
    """Sequence one research run from ``queued`` to a verified terminal outcome.

    Constructed by the composition root with every collaborator it already builds
    for the rest of the application: the shared ``LLMProvider`` reaches it only
    through the four provider ports, the persistence only through
    ``research_uow_factory``, and the search only through the provider the
    container already selects. No stage here constructs a client, a session, or a
    second copy of any collaborator.

    The deadline is passed in rather than read from the clock, for the reason every
    injected dependency in this project is: the orchestrator asks it at each
    boundary and the test controls the answer.
    """

    def __init__(
        self,
        *,
        settings: Settings,
        uow_factory: Callable[[], Any],
        planning: PlanningService,
        search_provider: SearchProvider,
        fetcher: SafeFetcher,
        extractor: ContentExtraction,
        deduplicator: SourceDeduplication,
        ranking: RankingPolicy,
        chunking: Any,
        evidence: Any,
        synthesis: SynthesisService,
        deadline: CooperativeDeadline | None = None,
    ) -> None:
        self._settings = settings
        self._uow = uow_factory
        self._planning = planning
        self._search = search_provider
        self._fetcher = fetcher
        self._extractor = extractor
        self._deduplicate = deduplicator
        self._ranking = ranking
        self._chunking = chunking
        self._evidence = evidence
        self._synthesis = synthesis
        self._deadline = deadline

    # --- the pipeline -------------------------------------------------------

    async def execute(self, *, run_id: int, user_id: int) -> ResearchOutcome:
        """Run the pipeline to a verified terminal outcome.

        The order is fixed and each step is a named stage, so a reader can locate
        where a behaviour lives without tracing the whole method:

        1. **claim** — the run is read through ``get_for_user`` and moved to
           ``running``. Both halves matter: ownership before anything is spent, and
           the ``queued → running`` transition as the single claim on the run.
        2. **plan** — one bounded model call, plan persisted on the run before any
           search happens.
        3. **search + persist sources** — every query in plan order; every result
           becomes a ``Source`` at ``pending``, converging on the existing row when
           the URL is already there (B7).
        4. **fetch + extract + chunk** — at most ``RESEARCH_MAX_SOURCES``
           candidates, chosen deterministically before any of them is fetched. Each
           source's own outcome is recorded per source (FR-19, FR-23), and one bad
           source never fails the run.
        5. **deduplicate + rank** — the pure P3.5 stage over every candidate that
           reached extraction, then one batched write of ``rank``, ``quality_score``,
           ``content_hash``, and ``duplicate_of_id`` (B2).
        6. **evidence** — ``SourceEvidenceService`` per *selected* source, in rank
           order, stopping at the deadline.
        7. **synthesis** — exactly one call, over the persisted, verified registry.
        8. **terminal** — the outcome § R.17 defines.

        Failures are classified, not swallowed: the error families are re-typed
        nowhere (provider failures keep the ``retryable`` flag the task reads), and
        the outcome a caller receives is always one of the five in
        :class:`RunOutcomeKind`.

        Raises :class:`ResearchRunNotFoundError` when the run is not this user's,
        and propagates the provider, evidence, synthesis, and citation families
        unchanged so the task layer can apply its retry policy.
        """
        run = await self._claim(run_id, user_id)
        question = run.question

        tallies = _Tallies()
        plan = await self._planning.plan_for_run(run_id, user_id)
        candidates = await self._gather(plan, run_id, user_id, tallies)
        fetched = await self._retrieve(candidates, run_id, user_id, tallies)
        selected_ids = await self._rank_and_persist(
            fetched, run_id, user_id, tallies, question
        )
        await self._collect_evidence(selected_ids, run_id, user_id, tallies)
        return await self._finish(run_id, user_id, tallies, question)

    # --- stage 1: claim -----------------------------------------------------

    async def _claim(self, run_id: int, user_id: int):
        """Own the run and move it to ``running`` (AD-043, B7).

        Two operations, deliberately in that order. ``get_for_user`` settles
        ownership before anything is spent (SR-10): a caller naming someone else's
        run is refused here rather than after a model call. ``set_status`` is the
        single durable claim on the run — it locks the row, re-reads the committed
        status under the lock, and refuses any transition the lifecycle does not
        allow.

        **The claim is idempotent, and that is the point.** A previous attempt
        that died mid-run — a worker SIGKILLed at the hard time limit, or an
        interrupted soft one — leaves the row at ``running``. With
        ``task_acks_late`` + ``task_reject_on_worker_lost`` the broker *will*
        redeliver, and the redelivered attempt re-enters here with a row that is
        already ``running``. Refusing that as ``SAME_STATE`` would make the
        redelivery fail before any work resumed, which is not idempotency: it is
        a redelivery that cannot converge. So a claim of an already-``running``
        run this user owns is accepted and logged as a no-op, and the pipeline
        resumes from persisted state.

        What is *not* idempotent, deliberately: a terminal run is still refused,
        because re-running a completed run would spend the budget again and
        overwrite a verified answer. The durable row, not a flag in this process,
        is the authority — so this survives a restart, a second worker, and a
        hard kill followed by redelivery (§ R.14).
        """
        async with self._uow() as uow:
            run = await uow.research_runs.get_for_user(run_id, user_id)
            if run is None:
                raise ResearchRunNotFoundError(
                    f"research run {run_id} does not exist for this user"
                )
            await self._claim_status(uow, run_id, user_id, run.status)
        log.info(
            "research run claimed: run=%d user=%d question_chars=%d status=%s",
            run_id,
            user_id,
            len(run.question),
            run.status,
        )
        return run

    async def _claim_status(
        self, uow: Any, run_id: int, user_id: int, current: ResearchRunStatus
    ) -> None:
        """Move ``queued -> running``, treating an already-running row as claimed.

        Everything else the lifecycle refuses is re-raised: a terminal run stays
        terminal, and a row owned by someone else is not this call's business.
        """
        from app.domain.research.lifecycle import TransitionError, TransitionErrorKind

        try:
            await uow.research_runs.set_status(run_id, user_id, "running")
        except TransitionError as exc:
            if (
                exc.kind is TransitionErrorKind.SAME_STATE
                and exc.current_status is ResearchRunStatus.RUNNING
                and current is ResearchRunStatus.RUNNING
            ):
                # Already claimed by an earlier attempt of the same delivery;
                # accepting it is what lets a redelivery resume.
                log.info(
                    "research run already running, resuming: run=%d user=%d",
                    run_id,
                    user_id,
                )
                return
            raise

    # --- stage 2 + 3: search and source persistence -------------------------

    async def _gather(
        self,
        plan: ResearchPlan,
        run_id: int,
        user_id: int,
        tallies: _Tallies,
    ) -> list[CandidateWork]:
        """Search every step in plan order and persist each result as a source.

        Every query the plan carries is executed, in the plan's own execution
        order, and the sources are created before anything is fetched: ``section R.7``
        records that ``uow.sources.add`` having no production caller is one of the
        three reasons the page-to-answer path could not execute at all.

        **Search failures are counted, not fatal** (§ 15.9: "R (bounded), P if
        partial query set succeeded, F if total"). A step whose provider call raises
        contributes nothing and the run continues with the steps that answered; a
        step that returns no results is a *typed* outcome rather than an error and is
        logged, because "found nothing" is distinguishable from "could not ask"
        (FR-41, FR-43). When nothing at all was produced, the caller classifies the
        run as ``NO_EVIDENCE``.

        **Order is deterministic.** Each result is tagged ``(step index, provider
        rank)`` so the fetch bound of :meth:`_retrieve` picks the same candidates
        every time regardless of how the provider ordered its answers or how
        ``asyncio`` interleaved the steps.

        Nothing is ranked here. Ranking happens after extraction, because content
        identity is only knowable then (§ R.9), and a source whose extraction failed
        is still ranked and recorded — never silently dropped (FR-30).
        """
        queries = plan.search_queries
        if not queries:
            # A plan with no search step is degenerate, which the domain already
            # refuses at construction (FR-3). Reaching here means the plan was
            # written by an older build or hand-edited, so it is reported as the
            # defect it is rather than turned into an empty successful run.
            raise ResearchNoEvidenceError(
                "the research plan contains no search step"
            )

        candidates: list[CandidateWork] = []
        for step_index, query in enumerate(queries):
            if self._deadline is not None and self._deadline.expired():
                log.info(
                    "search stopped at the deadline: run=%d steps_done=%d",
                    run_id,
                    step_index,
                )
                break
            for result in await self._search_step(query, plan):
                source = await self._persist_source(run_id, result)
                candidates.append(
                    CandidateWork(
                        search_order=(step_index, result.rank),
                        source_id=_source_id_of(source),
                        result=result,
                    )
                )

        tallies.candidates = len(candidates)
        log.info(
            "search complete: run=%d queries=%d candidates=%d provider=%s",
            run_id,
            len(queries),
            len(candidates),
            self._settings.SEARCH_PROVIDER,
        )
        return candidates

    async def _search_step(
        self, query: str, plan: ResearchPlan
    ) -> list[Any]:
        """Run one plan step's query and return its results.

        The request is built here rather than accepting the provider's own defaults,
        because three of the fields are security-relevant and none of them should be
        left to a vendor: ``max_results`` is bounded by our configuration
        (``SEARCH_MAX_RESULTS``, itself capped at 20), ``safe_search`` defaults to
        the configured level, and the language hint is the plan's own.

        ``SearchNoResultsError`` is caught and reported rather than propagated: a
        step that found nothing is a legitimate partial outcome (FR-41), and
        treating it as fatal would let one empty query kill a run whose other steps
        succeeded. Every other ``SearchError`` propagates, so the retry policy keeps
        reading ``retryable``.

        The step *index* is not a parameter: the ordering it would carry is
        ``plan.execution_order()``'s job, and the caller already holds the index when
        it builds the work item.
        """
        from app.domain.research.search import MAX_QUERY_CHARS

        request = SearchRequest(
            query=query[:MAX_QUERY_CHARS],
            max_results=self._settings.SEARCH_MAX_RESULTS,
            language=plan.language,
            country=self._settings.SEARCH_REGION or None,
            safe_search=self._safe_search(),
        )
        try:
            response = await self._search.search(request)
        except SearchNoResultsError:
            log.info(
                "search step found nothing: provider=%s",
                self._settings.SEARCH_PROVIDER,
            )
            return []

        return list(response.results)

    def _safe_search(self) -> SafeSearchLevel | None:
        """The configured safe-search level, or the strict default.

        Read from configuration rather than hardcoded because § 15.4 makes the
        filtering level a caller's decision; defaulting to ``STRICT`` rather than
        ``None`` because the queries come from arbitrary public-chat users (SR-2),
        so an unconfigured value must not silently become "off".
        """
        level = (self._settings.SEARCH_SAFE_SEARCH or "").strip().lower()
        try:
            return SafeSearchLevel(level) if level else SafeSearchLevel.STRICT
        except ValueError:
            return SafeSearchLevel.STRICT

    async def _persist_source(self, run_id: int, result: Any) -> Source:
        """Persist one search result as a ``Source``, converging on an existing row.

        The read-then-create is not an optimisation: ``uq_sources_run_id_url`` means
        a re-run's insert collides, and a collision aborts the whole run. Reading by
        the exact stored URL first is what makes re-execution converge (B7), and it
        is scoped by ``run_id`` so one run can never reach another's source
        (SR-10).

        The URL stored is the provider's, verbatim. ``canonical_url`` is a
        comparison key for P3.5 and is deliberately not a second identity (§ 16.1):
        the stored form is what was offered, and rewriting it would create a second
        URL policy outside the fetch boundary.
        """
        async with self._uow() as uow:
            existing = await uow.sources.get_for_url(run_id, result.url)
            if existing is not None:
                return existing
            return await uow.sources.add(
                run_id,
                result.url,
                result.domain,
                title=result.title,
                snippet=result.snippet,
            )

    # --- stage 4: fetch, extraction, chunking (the bounded work) ------------

    async def _retrieve(
        self,
        candidates: Sequence[CandidateWork],
        run_id: int,
        user_id: int,
        tallies: _Tallies,
    ) -> list[CandidateWork]:
        """Fetch, extract, and chunk at most ``RESEARCH_MAX_SOURCES** candidates.

        **The bound is applied before the first fetch, not after.** That is the
        whole point of Gate 3: a fetch costs up to ``FETCH_TOTAL_TIMEOUT_SECONDS``
        of wall clock, so the only place the bound can save real time is before it.
        Ordering is ``(step index, provider rank)`` — deterministic, and derived
        from the plan rather than from response arrival.

        **One source's failure never fails the run** (FR-19, FR-23). A fetch that
        raises ``FetchError`` is recorded as ``fetch_failed`` with a coarse reason
        and the run continues; an extraction that raises ``ExtractionError`` or
        produces nothing usable is recorded as ``unusable`` and the run continues.
        What is *not* done is recording the provider's own text: the typed families
        are written to be safe, but copying their message into a column would make a
        second place a future adapter change could leak through (SR-7, FR-42).

        **The deadline is checked before each source and before the chunking of
        each one.** Checking only between stages would let a single slow origin
        consume a whole budget: the check before the fetch is the one that matters,
        because a fetch is the only stage whose cost this process does not
        otherwise bound.

        Chunking is called *after* extraction, in the same stage, because the
        passages are derived from the extracted text and both belong to the source's
        own outcome. ``SourceChunkService`` owns the replacement and its guard; the
        orchestrator owns only the decision to run it.
        """
        limit = self._settings.RESEARCH_MAX_SOURCES
        ordered = sorted(candidates, key=lambda c: c.search_order)

        for candidate in ordered[:limit]:
            if self._deadline is not None and self._deadline.expired():
                log.info(
                    "retrieval stopped at the deadline: run=%d remaining=%d",
                    run_id,
                    len(ordered) - limit,
                )
                break
            await self._retrieve_one(candidate, run_id, user_id, tallies)

        # Sources the bound did not reach are recorded, never silently dropped
        # (FR-30, FR-42): a run that fetched five of nine candidates says so, and the
        # remaining four stay ``pending`` where a reader can see them.
        for candidate in ordered[limit:]:
            await self._set_source_status(
                candidate.source_id,
                _STATUS_PENDING,
                run_id=run_id,
                failure_reason="not selected: candidate bound",
            )
        return [
            candidate
            for candidate in ordered[:limit]
            if candidate.status == _STATUS_EXTRACTED
        ]

    async def _retrieve_one(
        self,
        candidate: CandidateWork,
        run_id: int,
        user_id: int,
        tallies: _Tallies,
    ) -> None:
        """Produce one candidate's passages, or record why it produced none."""
        if _source_is_http_url(candidate.result.url):
            fetched = await self._fetch_one(candidate, run_id, user_id, tallies)
            if fetched is None:
                return
            extracted = await self._extract_one(candidate, run_id, user_id, tallies)
            if extracted is None:
                return
            await self._chunk_one(
                candidate, run_id=run_id, user_id=user_id, tallies=tallies
            )
            return
        # A provider that returns a non-HTTP(S) URL is refused at the boundary, not
        # by a second scheme check here: ``FetchRequest``'s own validator rejects it,
        # and re-implementing the rule would create the second boundary AD-031
        # forbids. The candidate is recorded per source (FR-42) and the run continues.
        await self._set_source_status(
            candidate.source_id,
            _STATUS_FETCH_FAILED,
            run_id=run_id,
            failure_reason="the search provider returned a URL the fetch boundary refuses",
        )
        tallies.fetched += 0  # no fetch was attempted

    async def _fetch_one(
        self,
        candidate: CandidateWork,
        run_id: int,
        user_id: int,
        tallies: _Tallies,
    ) -> FetchedSource | None:
        """Fetch one candidate through the SSRF boundary, recording its outcome.

        The URL comes from a ``SearchResult`` and nowhere else (SR-2): a user
        supplies a question, never a URL to fetch, and the orchestrator never
        constructs an HTTP client of its own (AD-031, § 15.5).

        The whole ``FetchError`` family is caught rather than re-raised, which is
        safe because every branch of it is *already* non-revealing by construction
        and non-retryable except ``FetchConnectionError``. A connection failure is
        still recorded rather than retried here: retry policy belongs to the stage
        that owns the run budget (FR-44), and the task decides it once, not once per
        source.
        """

        try:
            fetched = await self._fetcher.fetch(
                FetchRequest(url=candidate.result.url)
            )
        except FetchError as exc:
            log.info(
                "fetch failed: run=%d url=%s error=%s",
                run_id,
                _domain_of(candidate.result.url),
                type(exc).__name__,
            )
            await self._set_source_status(
                candidate.source_id,
                _STATUS_FETCH_FAILED,
                run_id=run_id,
                failure_reason=type(exc).__name__,
            )
            return None

        tallies.fetched += 1
        await self._set_source_status(
            candidate.source_id, _STATUS_FETCHED, run_id=run_id
        )
        candidate.fetched = fetched
        return fetched

    async def _set_source_status(
        self,
        source_id: int,
        status: str,
        *,
        run_id: int,
        failure_reason: str | None = None,
    ) -> None:
        """Record one source's outcome, ownership-checked in the same unit of work.

        The write is preceded by a ``sources.get`` + ``run_id`` check inside the
        **same** transaction, which is the gap ``SourceRepository.set_status`` cannot
        close itself: it takes ``source_id`` alone, because the source statuses it
        writes are per-source facts with no user-scoped read path. The orchestrator
        is the caller that already settled ownership, so it is the caller that must
        recheck (SR-10, § R.15).

        ``failure_reason`` carries only the typed error's *class name* or a coarse
        phrase — never the exception's own text, which can contain a URL, a hostname,
        or a provider message (SR-7, FR-42). A reader can still tell a timeout from a
        blocked destination, because the typed families already separate those.
        """
        async with self._uow() as uow:
            source = await uow.sources.get(source_id)
            if source is None or source.run_id != run_id:
                raise ResearchPipelineError(
                    f"source {source_id} does not exist for research run {run_id}"
                )
            await uow.sources.set_status(
                source_id, status, failure_reason=failure_reason
            )

    async def _extract_one(
        self,
        candidate: CandidateWork,
        run_id: int,
        user_id: int,
        tallies: _Tallies,
    ) -> ExtractedContent | None:
        """Extract text from one fetched page, recording unusable outcomes.

        ``ExtractedContent.is_empty`` is the P3.4 contract for "reached but
        unusable" and is a **typed outcome, not an error** (FR-43): a page that
        answered with no readable text is distinguished from a page that could not
        be fetched at all, because those are different facts about a source.

        ``ExtractionError`` is caught for the same reason as ``FetchError``: every
        branch is non-retryable, non-revealing, and per-source by contract, so
        recording and continuing is the behaviour § 15.9 already requires.

        The extracted content's own ``url`` is *not* compared against the source
        row's: it is the redirect destination that answered, and the row keeps the
        URL search returned. ``SourceChunkService`` documents the same choice.
        """
        from app.domain.errors import ExtractionError

        fetched = candidate.fetched
        assert fetched is not None  # the caller checked the fetch succeeded
        try:
            extracted = await self._extractor.extract(fetched)
        except ExtractionError as exc:
            log.info(
                "extraction failed: run=%d url=%s error=%s",
                run_id,
                _domain_of(candidate.result.url),
                type(exc).__name__,
            )
            await self._set_source_status(
                candidate.source_id,
                _STATUS_UNUSABLE,
                run_id=run_id,
                failure_reason=type(exc).__name__,
            )
            tallies.unusable += 1
            return None

        if extracted.is_empty or not extracted.text.strip():
            await self._set_source_status(
                candidate.source_id,
                _STATUS_UNUSABLE,
                run_id=run_id,
                failure_reason="extracted no readable text",
            )
            tallies.unusable += 1
            return None

        tallies.extracted += 1
        candidate.extracted = extracted
        candidate.status = _STATUS_EXTRACTED
        await self._set_source_status(
            candidate.source_id, _STATUS_EXTRACTED, run_id=run_id
        )
        return extracted

    async def _chunk_one(
        self,
        candidate: CandidateWork,
        *,
        run_id: int,
        user_id: int,
        tallies: _Tallies,
    ) -> SourceChunkResult:
        """Persist one source's passages (stage 14, § 15.2 / FR-22).

        Delegated whole to ``SourceChunkService`` (P3.9-A), which owns the span
        policy, the replacement, and the guard that refuses to delete passages a
        verified citation points at. The orchestrator's only decisions are *whether*
        to run it and what to record when it refuses.

        ``ChunkReplaceBlockedError`` is caught and recorded rather than propagated,
        because it is the normal end state of a redelivered run: the source's
        evidence already exists and is already cited, and re-chunking it would
        destroy verified citations (AD-033). The source stays as it is, contributes
        nothing new, and the run continues. ``ChunkLimitError`` is *not* caught —
        exceeding the structural passage ceiling is a defect or an attack, and it
        fails the run.
        """
        from app.domain.errors import ChunkReplaceBlockedError

        source_id = candidate.source_id
        try:
            produced = await self._chunking.chunk_source(
                run_id=run_id,
                user_id=user_id,
                source_id=source_id,
                content=candidate.extracted,
            )
        except ChunkReplaceBlockedError:
            log.info(
                "source already evidenced, not re-chunked: run=%d source=%d",
                run_id,
                source_id,
            )
            return SourceChunkResult(source_id=source_id)

        if produced.is_empty:
            # Nothing to read and nothing to write. The source stays ``extracted``,
            # which is the honest record of what P3.4 produced.
            return SourceChunkResult(source_id=source_id)

        return SourceChunkResult(
            source_id=source_id,
            chunk_ids=produced.chunk_ids,
            was_replaced=produced.replaced_count > 0,
        )

    # --- stage 5: deduplicate, rank, and persist the metadata ----------------

    async def _rank_and_persist(
        self,
        candidates: Sequence[CandidateWork],
        run_id: int,
        user_id: int,
        tallies: _Tallies,
        question: str,
    ) -> list[int]:
        """Collapse duplicates, rank, and write P3.5's output (B2, FR-26 … FR-30).

        The pure stage runs over every candidate that reached extraction — including
        the ones that failed, which stay in the output as recorded exclusions rather
        than disappearing (FR-30). The *order* is deterministic because the input
        order is: it is ``(step index, provider rank)``, which is what the fetch
        bound already used, so the same run produces the same ranking twice.

        The mapping back to persisted source ids is by the candidate's own ``url``,
        which is the identity this pipeline assigned the row (``_persist_source``)
        and the only key ``SourceCandidate`` carries. It is a lookup into a
        dictionary rather than a scan, so a source the ranking moved does not have to
        be re-identified by anything derived from page content.

        ``set_rankings`` is called **once** for the run, and it is the batch form
        because the port documents why: a half-applied ranking leaves some sources at
        the previous order and others at the new one, and both
        ``list_citable_for_run`` and ``plan_synthesis_context`` budget over that
        order — so a partial write silently changes which evidence the model is
        offered. It either writes the run's ranking or none of it.

        Returns the source ids eligible for the evidence stage, in rank order.
        """
        ranked = self._rank(candidates, question)

        by_url = {candidate.result.url: candidate for candidate in candidates}
        rankings: list[SourceRanking] = []
        statuses: list[tuple[int, str, str | None]] = []

        for position, result in enumerate(ranked.ranked_results, start=1):
            candidate = by_url.get(result.candidate.url)
            if candidate is None:
                # A candidate the mapping cannot place. Refused rather than skipped:
                # an unranked source would sort last and could be silently offered
                # ahead of a ranked one by ``sources.id`` order alone.
                raise ResearchPipelineError(
                    "a ranked candidate does not map to a persisted source"
                )
            duplicate_of = self._duplicate_target(ranked, result, by_url)

            rankings.append(
                SourceRanking(
                    source_id=candidate.source_id,
                    rank=position,
                    quality_score=result.score,
                    content_hash=result.candidate.content_hash
                    or None,
                    duplicate_of_id=duplicate_of,
                )
            )
            statuses.append(
                (
                    candidate.source_id,
                    _STATUS_EXCLUDED if result.is_excluded else _STATUS_DUPLICATE
                    if duplicate_of is not None
                    else _STATUS_SELECTED,
                    result.excluded_reason,
                )
            )

        async with self._uow() as uow:
            await uow.sources.set_rankings(run_id, rankings)
            for source_id, status, reason in statuses:
                current = await uow.sources.get(source_id)
                if current is None or current.run_id != run_id:
                    raise ResearchPipelineError(
                        "a ranked source does not belong to the run being ranked"
                    )
                await uow.sources.set_status(
                    source_id, status, failure_reason=reason
                )

        # The three counts only this stage can know. ``selected`` is the number of
        # candidates the evidence stage was offered, ``excluded`` the number the
        # quality threshold refused, and both are what ``_Tallies.degraded`` reads to
        # decide ``partial`` (§ R.17): a run that ranked five sources and offered one
        # of them is a run that answered with less than it gathered.
        tallies.excluded = sum(1 for _, status, _ in statuses if status == _STATUS_EXCLUDED)
        tallies.selected = sum(1 for _, status, _ in statuses if status == _STATUS_SELECTED)
        return [
            source_id
            for source_id, status, _ in statuses
            if status == _STATUS_SELECTED
        ]

    def _rank(
        self, candidates: Sequence[CandidateWork], question: str
    ) -> DedupRankingResult:
        """The pure P3.5 stage: deduplicate then rank, with no I/O.

        Delegated to the two ports the container already builds, with the run's own
        question as the ranking query — the same string the planner was given and the
        synthesis will be asked again, so the ranking signal is about what the user
        asked rather than a per-step query.
        """
        source_candidates = [
            SourceCandidate.from_search_result(candidate.result, candidate.extracted)
            for candidate in candidates
        ]
        deduplicated = self._deduplicate.deduplicate(source_candidates)
        return self._ranking.rank(deduplicated, question)

    def _duplicate_target(
        self,
        ranked: DedupRankingResult,
        result: RankedResult,
        by_url: dict[str, CandidateWork],
    ) -> int | None:
        """The persisted id of this candidate's duplicate representative, if any.

        ``None`` for the representative itself. The target is resolved through the
        same URL lookup as the rest of the mapping, and the group is only used to
        *find* which candidate is the representative — the id comes from the row, so
        a duplicate edge points at a persisted source of this run (FR-26).
        """
        if result.candidate.url not in {
            member.url for group in ranked.duplicate_groups for member in group.members
        }:
            return None
        for group in ranked.duplicate_groups:
            if result.candidate.url in {member.url for member in group.members}:
                if result.candidate.url == group.canonical.url:
                    return None
                target = by_url.get(group.canonical.url)
                return target.source_id if target is not None else None
        return None

    # --- stage 6: evidence ----------------------------------------------------

    async def _collect_evidence(
        self,
        selected_ids: Sequence[int],
        run_id: int,
        user_id: int,
        tallies: _Tallies,
    ) -> int:
        """Run the evidence stage for each selected source, in rank order.

        One source at a time, and the deadline is checked before each one, because
        evidence is the most expensive stage in the pipeline: it makes
        ``1 + CLAIM_MAX_WINDOWS`` model calls per source (§ R.12), so a run that is
        nearly out of budget must stop *before* buying the next source's summary
        rather than after it.

        ``SourceAlreadyAnalyzedError`` is caught rather than propagated, and that is
        the idempotency contract working: a redelivered task meets a source whose
        evidence is already stored, the stage refuses to duplicate it, and the run
        moves on (B7). Every other ``EvidenceError`` propagates — a schema violation
        is a defect, not a degradation.

        Returns the number of sources that produced claims.
        """
        claims = 0
        citations = 0
        for source_id in selected_ids:
            if self._deadline is not None and self._deadline.expired():
                log.info("evidence stopped at the deadline: run=%d", run_id)
                break
            result = await self._evidence_for_one(source_id, run_id, user_id)
            if result is not None:
                claims += result.claim_count
                # Every accepted claim is persisted with exactly one citation row,
                # so the two move together (P3.7 ``_persist`` writes them in one
                # transaction). Counting them separately here would require a second
                # database read for a number the stage already has.
                citations += result.claim_count

        tallies.claims = claims
        tallies.citations = citations
        return claims

    async def _evidence_for_one(
        self, source_id: int, run_id: int, user_id: int
    ) -> SourceEvidenceResult | None:
        """Summarize one source and persist its evidence-grounded claims.

        ``SourceContentEmptyError`` is caught and counts as *no evidence from this
        source*: a source with no passage is a source P3.4 called unusable, and its
        absence was already recorded at extraction. The run continues — one silent
        source must not fail a run whose other sources are readable.
        """
        from app.domain.errors import SourceContentEmptyError

        try:
            return await self._evidence.analyze_source(
                run_id=run_id, user_id=user_id, source_id=source_id
            )
        except SourceContentEmptyError:
            log.info(
                "source has no usable passage: run=%d source=%d", run_id, source_id
            )
            return None

    # --- stage 7 + 8: synthesis and the terminal outcome ---------------------

    async def _finish(
        self, run_id: int, user_id: int, tallies: _Tallies, question: str
    ) -> ResearchOutcome:
        """Synthesize the answer, or classify why there is none (AD-044, § R.17).

        Three outcomes, decided by what is actually persisted:

        * a verified answer and nothing degraded → ``completed``;
        * a verified answer and something degraded → ``partial``. Degradation is
          ``_Tallies.degraded()``: a fetch that failed, an extraction that produced
          nothing, a source excluded below quality, or a candidate the bound never
          reached. An answer synthesized under any of those is an honest *partial*
          answer, not a complete one (FR-42, SR-12);
        * no verified answer → ``failed`` with ``NO_EVIDENCE`` semantics. This runs
          *before* any model call, because ``SynthesisService._load`` raises
          ``SynthesisInputError`` when the registry is empty — which is exactly the
          "a run with no usable sources produces a clear failure, never an invented
          answer" rule of FR-41 and SR-12.

        The write is one transaction in the service (synthesis + ``final_answer`` +
        status together), so there is no state in which a run is terminal without a
        validated answer behind it (§ 17).

        ``SynthesisInputError`` is the signal that there is no verified evidence, and
        it is re-raised as ``ResearchNoEvidenceError`` so the caller and the
        notification layer see the *pipeline's* classification rather than a
        synthesis-stage detail. The message a user gets is the research one; the
        distinction that matters (nothing usable was found, versus something broke)
        is preserved in the typed error rather than in its text.
        """
        degraded = tallies.degraded()
        terminal = "partial" if degraded else "completed"

        if self._deadline is not None and self._deadline.expired():
            # AD-045: a deadline-terminated run is a *partial* run, and only a
            # partial one — it can never be labelled ``completed``, because the
            # run stopped before the stages that would have made it complete.
            terminal = "partial"

        try:
            outcome = await self._synthesis.synthesize_run(
                run_id=run_id, user_id=user_id, terminal_status=terminal
            )
        except SynthesisInputError as exc:
            # No verified evidence anywhere: nothing was retrieved, every source
            # failed, or every proposed claim was rejected. Distinguishable from a
            # pipeline failure (FR-41, FR-43) and never turned into an answer.
            log.info("research produced no verified evidence: run=%d", run_id)
            await self._fail(
                run_id,
                user_id,
                RunOutcomeKind.NO_EVIDENCE,
                tallies,
                reason="no verified evidence was produced",
                error=exc,
            )
            return self._outcome(RunOutcomeKind.NO_EVIDENCE, tallies)

        answer = outcome.answer
        await self._record_terminal(
            run_id,
            user_id,
            RunOutcomeKind.PARTIAL if degraded else RunOutcomeKind.COMPLETED,
            tallies,
        )
        return self._outcome(
            RunOutcomeKind.PARTIAL if degraded else RunOutcomeKind.COMPLETED,
            tallies,
            answer=answer,
        )

    def _outcome(
        self,
        kind: RunOutcomeKind,
        tallies: _Tallies,
        *,
        answer: SynthesizedAnswer | None = None,
        summary: str | None = None,
    ) -> ResearchOutcome:
        """Fold the run's counters into the value a caller delivers exactly once."""
        return ResearchOutcome(
            kind=kind,
            status=kind.run_status(),
            answer=answer.plain_text if answer is not None else None,
            summary=summary
            if summary is not None
            else summarize_run(
                outcome=kind,
                candidates=tallies.candidates,
                fetched=tallies.fetched,
                extracted=tallies.extracted,
                unusable=tallies.unusable,
                excluded=tallies.excluded,
                selected=tallies.selected,
                claims=tallies.claims,
                citations=tallies.citations,
            ),
            candidates=tallies.candidates,
            fetched=tallies.fetched,
            extracted=tallies.extracted,
            unusable=tallies.unusable,
            excluded=tallies.excluded,
            selected=tallies.selected,
            claims=tallies.claims,
            citations=tallies.citations,
            synthesized=answer is not None,
        )

    # --- terminal writes ------------------------------------------------------

    async def _record_terminal(
        self, run_id: int, user_id: int, kind: RunOutcomeKind, tallies: _Tallies
    ) -> None:
        """Write the outcome's summary to the run, alongside what already exists.

        The status was already written — by ``SynthesisService._persist`` in the same
        transaction as the answer — so this writes only ``failure_summary``, which is
        the one part of the record the stage that writes the status cannot know.

        It deliberately does **not** call ``set_status`` again. Doing so is refused as
        ``SAME_STATE``, and that refusal is the property protecting a terminal run
        from being reopened, so the correct answer is the status-free write — not
        relaxing the lifecycle to allow a no-op transition.
        """
        async with self._uow() as uow:
            await uow.research_runs.set_summary_note(
                run_id,
                user_id,
                summarize_run(
                    outcome=kind,
                    candidates=tallies.candidates,
                    fetched=tallies.fetched,
                    extracted=tallies.extracted,
                    unusable=tallies.unusable,
                    excluded=tallies.excluded,
                    selected=tallies.selected,
                    claims=tallies.claims,
                    citations=tallies.citations,
                ),
            )

    async def _fail(
        self,
        run_id: int,
        user_id: int,
        kind: RunOutcomeKind,
        tallies: _Tallies,
        *,
        reason: str,
        error: BaseException | None = None,
    ) -> None:
        """Record a terminal failure, with a summary that names the stage only.

        ``failure_summary`` carries the counts and the *stage* that ended the run —
        never the exception's own text, which can contain a provider message, a SQL
        fragment, or a URL (SR-7, FR-42). The exception's type is used for the log
        line and for the classification the task layer reads; neither reaches a user.
        """
        log.warning(
            "research failed: run=%d kind=%s reason=%s error=%s",
            run_id,
            kind.value,
            reason,
            type(error).__name__ if error is not None else "none",
        )
        async with self._uow() as uow:
            await uow.research_runs.set_status(
                run_id,
                user_id,
                "failed",
                failure_summary=self._failure_summary(kind, tallies, reason),
            )

    def _failure_summary(
        self, kind: RunOutcomeKind, tallies: _Tallies, reason: str
    ) -> str:
        """A non-revealing terminal summary: counts, the stage, and no content."""
        base = summarize_run(
            outcome=kind,
            candidates=tallies.candidates,
            fetched=tallies.fetched,
            extracted=tallies.extracted,
            unusable=tallies.unusable,
            excluded=tallies.excluded,
            selected=tallies.selected,
            claims=tallies.claims,
            citations=tallies.citations,
        )
        return f"{base}; ended: {reason}"


# --- module helpers -----------------------------------------------------------


def _source_id_of(source: Source) -> int:
    """The persisted id of a source, refusing a row that has none.

    Not defensive noise: ``Source.id`` is optional on the base type because the same
    type describes a row before its flush, and this pipeline only ever reaches
    sources through ``add`` or ``get_for_url``, both of which flush. A ``None`` here
    would mean a source was used before it existed, which must not be recorded as an
    outcome.
    """
    if source.id is None:
        raise ResearchPipelineError(
            "a source was used before its row was persisted"
        )
    return source.id


def _domain_of(url: str) -> str:
    """The host of a URL, for a log line only.

    Never the full URL: the query string is where signed tokens and session
    identifiers live (SR-7, § 15.5), and a log line is exactly where a credential
    must not end up. The path is dropped as well, so a log records *which host* was
    reached and nothing about what was asked for.
    """
    from urllib.parse import urlsplit

    host = (urlsplit(url).hostname or "").strip().lower()
    return host or "unparsed-host"


def _source_is_http_url(url: str) -> bool:
    """Whether the URL is one the fetch boundary would even attempt.

    Checked here only to avoid building a ``FetchRequest`` the boundary would
    immediately reject, so the candidate gets its own recorded reason instead of an
    exception. The *rule* lives entirely in the boundary — this is an early-out, not
    a second scheme policy (AD-031).
    """
    return url.strip().lower().startswith(("http://", "https://"))


__all__ = [
    "CandidateWork",
    "ResearchOrchestrator",
    "ResearchOutcome",
    "SourceChunkResult",
]
