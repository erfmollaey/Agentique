"""Ports for research persistence.

Every outward persistence dependency of the Research Engine is declared here
as a ``Protocol``, mirroring :mod:`app.domain.ports` for the chat flow. The
application layer depends on these; the concrete SQLAlchemy implementations
live in :mod:`app.repositories.research`, the only place that imports the
ORM.

Ownership is expressed structurally, the same way Phase 2 does it: runs are
read through a user (``get_for_user``), and the child artifacts
(sources, chunks, claims, citations) are reached through the run/source/claim
identifiers that situate them in the user's tree. A run id is the boundary of
one user's research; a user cannot name another user's run without first
naming that user's identity, which the run repository refuses to do
(SR-10, T-23).

Provider ports are introduced by the stage that first needs them, because a port
written before its caller exists is speculation (§ 15.3). :class:`SearchProvider`
arrives at P3.2, :class:`SafeFetcher` at P3.3, :class:`ContentExtraction` at
P3.4, :class:`SourceDeduplication` / :class:`RankingPolicy` at P3.5,
:class:`PlanningProvider` at P3.6, :class:`SourceSummarizer` /
:class:`ClaimExtractor` at P3.7, and :class:`SynthesisProvider` at P3.8. The
concrete search vendor is selected in
``app.infrastructure.search.create_search_provider`` and is configuration
(``SEARCH_PROVIDER``), never an import in this module (AD-036, T-3).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol, runtime_checkable

from app.domain.research.dedup import DedupRankingResult, SourceCandidate
from app.domain.research.evidence import ClaimExtraction, SourceContent, SourceSummary
from app.domain.research.extraction import ExtractedContent
from app.domain.research.fetch import FetchedSource, FetchRequest
from app.domain.research.planning import ResearchPlan
from app.domain.research.search import SearchRequest, SearchResponse
from app.domain.research.synthesis import (
    CitationEntry,
    SynthesisInput,
    SynthesisProposal,
)
from app.domain.research.types import (
    Citation,
    Claim,
    ResearchRun,
    Source,
    SourceChunk,
    SourceRanking,
)


@runtime_checkable
class ClaimExtractor(Protocol):
    """Candidate claim extraction for one source's passages (FR-32, § 15.3).

    One method, because proposing candidate claims is the whole capability this
    stage needs from a provider. It speaks only in
    :mod:`app.domain.research.evidence` types, so no provider SDK type, HTTP
    client, or message object can leak through it.

    What callers rely on, and why the port exists rather than a direct call into
    a module:

    * **the port returns candidates, never claims.** ``ClaimExtraction`` holds
      ``ClaimCandidate`` values; producing a ``VerifiedClaim`` requires
      :func:`app.domain.research.evidence.verify_claims`, which the application
      layer calls. An implementation therefore *cannot* return an accepted claim,
      whatever it is handed — the ability to mint evidence is not in its signature
      (AD-033, SR-9);
    * **the provider generates, it never verifies** — there is no method that
      confirms a claim, scores a quote, or asks a second model to adjudicate the
      first one's output. Evidence verification is programmatic here, and the
      port has no tool surface, so nothing a model returns can cause an action
      (§ 15.3, AD-032);
    * **the provider is replaceable and mockable** — no test of claim extraction
      needs a model, and a different backend changes no domain code.

    Implementations raise the ``EvidenceError`` family for a response that is not
    the requested structure. Provider-level failures (timeout, authentication,
    rate limit) are **not** re-typed: they propagate as the existing ``LLM*Error``
    types so the task-level retry policy keeps reading the ``retryable`` flag it
    already understands.
    """

    async def extract(self, source: SourceContent) -> ClaimExtraction: ...


@runtime_checkable
class ContentExtraction(Protocol):
    """Content extraction port (P3.4 — § 15.3, AD-034).

    One method: given the bytes from a fetched page, produce normalized
    content or a typed ``ExtractionError``. No parser type, no HTTP type,
    no provider SDK leaks through.
    """

    async def extract(self, source: FetchedSource) -> ExtractedContent: ...

    def extract_sync(self, source: FetchedSource) -> ExtractedContent: ...


@runtime_checkable
class PlanningProvider(Protocol):
    """Turn a research question into a validated plan (FR-1 … FR-3, § 15.3).

    One method, because producing a plan is the whole capability this stage needs.
    It speaks only in :mod:`app.domain.research.planning` types, so no provider SDK
    type, HTTP client, or message object can leak through it.

    What callers rely on, and why the port exists at all:

    * **the plan is validated before it is returned** — a malformed or degenerate
      model response raises a typed error from :mod:`app.domain.errors` rather
      than producing an empty plan (FR-3);
    * **the provider generates, it never executes** — the port has no method that
      searches, fetches, extracts, or writes, so nothing a model returns can cause
      an action (§ 15.3, AD-032). A planner with a tool surface is exactly the
      injection vector AD-032 removes by construction;
    * **the provider is replaceable and mockable** — no test of planning needs a
      model, and a different planning backend changes no domain code.

    Implementations raise the ``PlanningError`` family for a plan that could not be
    produced or validated. Provider-level failures (timeout, authentication,
    rate limit) are **not** re-typed: they propagate as the existing
    ``LLM*Error`` types so the task-level retry policy keeps reading the
    ``retryable`` flag it already understands.
    """

    async def plan(self, question: str) -> ResearchPlan: ...


@runtime_checkable
class SourceDeduplication(Protocol):
    """Candidate collapse into canonical representatives (FR-24, FR-25, FR-26).

    One method, because collapsing candidates is the whole capability this stage
    needs. It speaks only in :mod:`app.domain.research.dedup` types, so no URL
    library, hasher, or similarity implementation leaks through the port.

    Two properties callers rely on, and the reason the port exists rather than a
    direct call into a module:

    * the policy is replaceable (FR-29) — a caller that needs a different
      identity or similarity rule implements this port and changes nothing else;
    * an implementation performs no I/O — no socket, DNS lookup, database, or
      model call, so the whole strategy is testable without infrastructure.

    Implementations raise a typed error from :mod:`app.domain.errors` — the
    ``DeduplicationError`` family — for a candidate or identity they cannot
    express. An *unusable source* is not an error: it is returned with an
    exclusion reason (FR-30), because one bad source must not fail a run.
    """

    def deduplicate(
        self, candidates: Sequence[SourceCandidate]
    ) -> DedupRankingResult: ...


@runtime_checkable
class RankingPolicy(Protocol):
    """Deterministic ordering of deduplicated candidates (FR-27 … FR-30).

    One method. It is a port because FR-29 requires the ranking *strategy* to be
    replaceable, and because § 15.3 forbids domain and application logic from
    depending on a concrete ranking implementation. It speaks only in
    :mod:`app.domain.research.dedup` types.

    The contract every implementation owes:

    * deterministic — identical candidates in identical order produce identical
      scores and identical ordering, with no clock, no randomness, and no
      dictionary or set iteration order involved;
    * total — every deduplicated candidate appears exactly once, so a caller
      never has to guess whether a source was dropped or merely ranked lower;
    * explainable — the score is a documented weighted sum of normalized signals,
      not an opaque number;
    * I/O-free — no socket, database, model call, or network provider.
    """

    def rank(self, result: DedupRankingResult, query: str) -> DedupRankingResult: ...


@runtime_checkable
class SourceSummarizer(Protocol):
    """Summarize one source from its own passages (FR-31, § 15.3).

    One method, and one source per call. The single-argument shape is the
    containment: the summarizer is handed exactly one
    :class:`~app.domain.research.evidence.SourceContent`, so it has no second
    source to compare against, no second source's passages in the prompt, and no
    operation that could combine two sources. Cross-source synthesis is FR-33 and
    P3.8's business; the stage that could perform it does not exist in this
    signature.

    The implementation has no tools — no search, no fetch, no filesystem, no
    database, no browser — so retrieved text cannot cause a request to be made
    (AD-032).

    Implementations raise the ``EvidenceError`` family for a response that is not
    the requested structure, and do **not** re-type provider failures: those
    propagate as the existing ``LLM*Error`` types so the retryable flag survives.
    """

    async def summarize(self, source: SourceContent) -> SourceSummary: ...


@runtime_checkable
class SynthesisProvider(Protocol):
    """Cross-source synthesis and citation proposal (P3.8 — § 15.3, FR-33 … FR-39).

    One method, and one research run's evidence per call: that single-argument shape
    is what makes cross-source synthesis possible at all — ``SourceSummarizer`` at
    P3.7 was deliberately given one source so it *could not* compare two, and this
    port is given the run's whole bounded evidence set so it must.

    What callers rely on, and why the port exists:

    * **the provider proposes; it never establishes.** ``synthesize`` returns a
      :class:`~app.domain.research.synthesis.SynthesisProposal`, whose citations are
      ``CitationReference`` values — bare identifiers. Producing a
      :class:`~app.domain.research.synthesis.VerifiedCitation` requires
      :func:`~app.domain.research.synthesis.verify_synthesis`, which the *application
      layer* calls against the persisted citation registry. An implementation
      therefore cannot return a trusted citation, whatever it is handed: the ability
      to accept a reference is not in its signature (AD-033, SR-9, Rules 1–7);
    * **the provider generates, it never verifies and never fetches** — no method
      here searches, fetches, opens a URL, reads a database, or adjudicates, and the
      port has no tool surface, so nothing in a model response can cause an action
      (§ 15.3, AD-032);
    * **the provider is replaceable and mockable** — no test of synthesis needs a
      model, and a different backend changes no domain code.

    Implementations raise the ``SynthesisError`` family for a response that is not the
    requested structure. Provider-level failures (timeout, authentication, rate
    limit) are **not** re-typed: they propagate as the existing ``LLM*Error`` types so
    the task-level retry policy keeps reading the ``retryable`` flag it already
    understands.
    """

    async def synthesize(
        self, synthesis_input: SynthesisInput
    ) -> SynthesisProposal: ...


@runtime_checkable
class SafeFetcher(Protocol):
    """Retrieval of one untrusted URL (FR-12 … FR-19, § 15.5).

    The SSRF security boundary, expressed as a port. One method, because that is
    the whole capability this stage needs: given a URL that arrived from a search
    provider, produce bounded bytes plus the metadata needed to interpret them,
    or a typed :class:`app.domain.errors.FetchError` describing exactly why not.

    The name is deliberate. ``SafeFetcher`` is not a suggestion; it is the name
    of the § 15.3 boundary, and any future implementation that can reach the
    network on a search-derived URL must satisfy this port rather than be called
    directly. There is intentionally no second port for "fetch without checks".

    What it must never do — the properties callers rely on:

    * resolve the hostname more than once per hop, so the address validated is
      the address connected to (the DNS-rebinding requirement);
    * follow a redirect without re-running the whole validation on the new
      target;
    * buffer an unbounded body, or bound it on the wire rather than decoded;
    * carry a credential of any kind to the destination (SR-8).

    It speaks only in :mod:`app.domain.research.fetch` types, so no HTTP client
    type, DNS object, or address-class predicate can leak through it. The
    concrete implementation is in ``app.infrastructure.fetch`` and is
    configuration-free from the domain's point of view.
    """

    async def fetch(self, request: FetchRequest) -> FetchedSource: ...


@runtime_checkable
class SearchProvider(Protocol):
    """Web search, as the Research Engine needs it (FR-5, FR-6, § 15.4).

    One method, because that is the entire capability this stage needs. It
    speaks only in :mod:`app.domain.research.search` types, so no provider
    response shape, SDK object, or HTTP client type can leak through it.

    Implementations raise a typed error from :mod:`app.domain.errors` — the
    ``SearchError`` family — for every failure, including "no usable results".
    They do not retry across providers, rank, or fetch anything: a result is a
    URL and a snippet, not evidence.
    """

    async def search(self, request: SearchRequest) -> SearchResponse: ...


@runtime_checkable
class ResearchRunRepository(Protocol):
    """Persistence for research runs."""

    async def create(
        self,
        user_id: int,
        question: str,
        conversation_id: int | None = None,
        *,
        request_key: str | None = None,
    ) -> ResearchRun:
        """Create a queued run, optionally guarded by a request key (B7).

        ``request_key`` is the derived identity of the *request*, not of the row.
        When it is supplied, the database refuses a second run for the same user
        and the same key, so a redelivered ``/research`` update converges on the
        first run instead of producing a second logical run with its own enqueued
        task and its own terminal message (AD-017's pattern, § R.14).

        A duplicate raises a **retryable** ``DuplicateMessageError``: the
        constraint did its job, but the transaction is spent, so the caller
        retries and reads the winner's row with :meth:`get_for_request`.
        """

    async def get_for_user(self, run_id: int, user_id: int) -> ResearchRun | None:
        """Return the run only if ``user_id`` owns it (SR-10, T-23)."""
        ...

    async def get_for_request(self, user_id: int, request_key: str | None) -> ResearchRun | None:
        """The run a request key already produced for this user, if any (B7).

        The read half of the run-level idempotency contract: what a retry of a
        duplicated command reads after its insert collided. Ownership-scoped for
        the same reason :meth:`get_for_user` is, and returns ``None`` for a
        ``None`` key rather than matching some arbitrary run.
        """

    async def list_for_user(self, user_id: int, limit: int) -> Sequence[ResearchRun]: ...

    async def set_synthesis(
        self, run_id: int, user_id: int, synthesis: dict[str, Any]
    ) -> None:
        """Persist a **verified** synthesis on the run (FR-35 … FR-39, P3.8).

        Takes the already-validated payload, not a ``SynthesizedAnswer``: the
        repository is persistence and does not re-derive domain validity, and the
        answer was verified once at the boundary that produced it.

        Takes ``user_id`` for the same reason ``set_plan`` does. The synthesis is the
        run's authoritative research output — the prose a user will be shown and the
        citations they will be able to check — so the write is ownership-scoped by
        both columns: a caller naming another user's run affects zero rows rather
        than overwriting what that run concluded (SR-10).

        What may be written here is constrained by what can be constructed: only
        ``SynthesizedAnswer.to_payload()`` produces this shape, and that method is
        reachable only from an object verification built. There is deliberately no
        method that stores a raw model proposal.
        """

    async def set_plan(self, run_id: int, user_id: int, plan: dict[str, Any]) -> None:
        """Persist a validated plan on the run (FR-4).

        Takes ``user_id`` even though the run id alone identifies the row, and the
        difference from ``set_status`` is deliberate. A plan is the largest
        attacker-influenced value the research schema stores, so the write is
        ownership-scoped: a caller that could name a run belonging to someone else
        could overwrite what that run claims it will investigate. The write is a
        single ``UPDATE`` scoped by both columns.

        Takes the already-validated payload, not a ``ResearchPlan``: the repository
        is persistence and does not re-derive domain validity, and the plan was
        validated once at the boundary that produced it.
        """

    async def set_status(
        self,
        run_id: int,
        user_id: int,
        status: str,
        *,
        failure_summary: str | None = None,
        final_answer: str | None = None,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
    ) -> None:
        """Transition a run's status (P3.9-B, B4).

        Takes ``user_id`` so the write is ownership-scoped: a caller that names a run
        belonging to someone else affects zero rows rather than mutating another user's
        run (SR-10). Lifecycle validation is enforced by
        ``ResearchRunLifecycle`` before the write; terminal statuses cannot be reopened.

        The caller owns the commit boundary and the surrounding unit of work; this method
        does not commit.
        """


@runtime_checkable
class SourceRepository(Protocol):
    """Persistence for sources."""

    async def add(
        self,
        run_id: int,
        url: str,
        domain: str,
        *,
        title: str | None = None,
        snippet: str | None = None,
    ) -> Source: ...

    async def get(self, source_id: int) -> Source | None: ...

    async def get_for_url(self, run_id: int, url: str) -> Source | None:
        """The run's source for an exact ``url``, if it already has one (B7).

        Scoped by ``run_id`` as well as ``url``, because ``uq_sources_run_id_url``
        says a run cannot hold one URL twice but says nothing about *which* row a
        re-executed pipeline converges on. Matching is on the stored URL, verbatim:
        ``canonical_url`` is P3.5's comparison key and is deliberately not a second
        identity.
        """

    async def claim_unanalysed(self, run_id: int, source_id: int) -> bool:
        """Atomically claim the right to produce this source's evidence (B7).

        Returns ``True`` exactly once per source — to the caller that wrote its
        summary, claims, and citations — and ``False`` to every later one.

        It takes a row lock and reads the committed ``summary``, which P3.7 writes
        in the *same transaction* as the claims and citations, so the flag and the
        data can never disagree. The lock is what closes the race a plain
        check-then-write would leave open under READ COMMITTED, and this method is
        only meaningful when it is called inside the unit of work that does the
        writing.

        ``False`` also covers a source that belongs to another run, so a caller must
        settle ownership separately — exactly as it does for the other unscoped
        reads on this repository.
        """

    async def list_for_run(self, run_id: int) -> Sequence[Source]: ...

    async def set_status(
        self, source_id: int, status: str, *, failure_reason: str | None = None
    ) -> None: ...

    async def set_summary(self, source_id: int, run_id: int, summary: str) -> None:
        """Persist a source's summary (FR-31).

        Takes ``run_id`` as well as ``source_id``, which differs from
        ``set_status`` for the same reason ``set_plan`` does: the value is model
        output derived from fetched web content, so the write is scoped to the
        pair the caller has already established. A caller that could name a
        source belonging to another run would otherwise be able to overwrite what
        that run says its source says.

        Takes the already-validated text, not a ``SourceSummary``: the repository
        is persistence and does not re-derive domain validity, and the summary
        was validated once at the boundary that produced it.
        """

    async def set_ranking(
        self, source_id: int, run_id: int, ranking: SourceRanking
    ) -> int:
        """Persist one source's ranking and deduplication metadata (FR-26 … FR-30).

        The P3.5 writer the audit found missing (§ R.9). Returns the number of
        rows written — ``0`` when the source is not part of ``run_id``, which is
        the same zero-row outcome ``set_summary`` produces for a foreign source,
        and is why the caller owns the run: a caller that could name another
        run's source changes nothing rather than corrupting its ranking.

        Takes ``SourceRanking`` rather than four keyword arguments because the
        four values are not independent and a defaulted argument cannot express
        the difference between "this source has no duplicate target" and "do not
        touch this column". See :class:`~app.domain.research.types.SourceRanking`.
        """

    async def set_rankings(
        self, run_id: int, rankings: Sequence[SourceRanking]
    ) -> int:
        """Persist ranking metadata for several sources of **one** run, atomically.

        The batch form, and the one a caller ranking a whole candidate set wants:
        every source of a run is ranked in one stage, so either the run's ranking
        is written or none of it is. A partially applied ranking would leave some
        sources at the previous order and others at the new one, which is worse
        than no ranking at all — ``list_citable_for_run`` and
        ``plan_synthesis_context`` both budget over the order, so a half-applied
        result silently changes which evidence the model is offered.

        Ownership is validated **before** any row is written: every named
        ``source_id`` and every ``duplicate_of_id`` must be a source of
        ``run_id``. That is the part the schema cannot enforce — the
        self-referential foreign key has no ``run_id`` in it — so it is checked
        here, and a violation raises
        :class:`~app.domain.errors.DuplicateTargetError` rather than leaving a
        cross-run edge behind (FR-26).
        """


@runtime_checkable
class SourceChunkRepository(Protocol):
    """Persistence for source chunks."""

    async def add(
        self,
        source_id: int,
        ordinal: int,
        text: str,
        char_start: int,
        char_end: int,
    ) -> SourceChunk: ...

    async def list_for_source(self, source_id: int) -> Sequence[SourceChunk]: ...

    async def delete_for_source(self, source_id: int) -> int:
        """Delete every chunk of one source. Returns the row count.

        Scoped by ``source_id`` alone, which *is* the ownership boundary: a chunk
        belongs to exactly one source through ``source_chunks.source_id``, so this
        statement cannot reach another source's passages even if it is handed the
        wrong id. The run-scoping step — establishing that the source is the
        caller's — happens in the service before this is called, the same
        separation :meth:`SourceRepository.set_summary` makes.

        **The caller must first establish that nothing cites these chunks.**
        ``citations.chunk_id`` is ``ON DELETE CASCADE`` from ``source_chunks``,
        so the database removes every citation that cites a deleted passage. That
        is the destruction AD-033 exists to prevent, and no constraint here can
        refuse it, so the check is an application-level obligation and the
        service enforces it through
        :meth:`CitationRepository.count_for_source`.
        """


@runtime_checkable
class ClaimRepository(Protocol):
    """Persistence for claims."""

    async def add(
        self,
        run_id: int,
        text: str,
        *,
        status: str = "unsupported",
        confidence: float | None = None,
    ) -> Claim: ...

    async def list_for_run(self, run_id: int) -> Sequence[Claim]: ...

    async def set_status(self, claim_id: int, status: str) -> None: ...


@runtime_checkable
class CitationRepository(Protocol):
    """Persistence for citations.

    ``quote`` is a **required** argument, and that requirement is the contract:
    every citation row carries the passage that justifies it, so a writer cannot
    create an evidence link with nothing behind it (AD-033, SR-9). The text must
    already have been verified against the referenced chunk's stored content by
    :func:`app.domain.research.evidence.verify_claims`; the database cannot check
    containment between two rows, so this repository is the last application-level
    gate before the row exists.
    """

    async def add(
        self,
        claim_id: int,
        source_id: int,
        chunk_id: int,
        quote: str,
        *,
        location: str | None = None,
    ) -> Citation: ...

    async def list_for_claim(self, claim_id: int) -> Sequence[Citation]: ...

    async def count_for_source(self, source_id: int) -> int:
        """How many citations cite a passage of this source (P3.9-A).

        Exists so the chunk producer can answer one question before it replaces
        anything: **is any of this source's evidence already cited?**

        Counted by ``citations.source_id``, which is exact rather than an
        approximation: the composite foreign key P3.7 added
        (``fk_citations_chunk_id_source_id_source_chunks``) forces
        ``chunk.source_id == citation.source_id``, so a citation whose
        ``source_id`` is this source cites a passage that belongs to it. Counting
        by ``source_id`` and counting by "cites one of this source's chunks" are
        the same number, which is why one query answers the question.

        Returns ``0`` for a source nothing cites yet — the ordinary state, and
        the only state in which its passages may safely be replaced.
        """

    async def list_citable_for_run(self, run_id: int) -> Sequence[CitationEntry]:
        """Every citation of one run, resolved through its whole evidence chain.

        The **citation registry read path**, and the single place persisted evidence
        becomes something a model may be offered as citable. One statement over
        ``citations → claims → source_chunks → sources``, scoped by
        ``claims.run_id``, producing ``CitationEntry`` values that already carry the
        verified quote, the passage it came from, and the persisted source URL,
        domain, title, and rank.

        Two properties are structural and belong here rather than in the caller:

        * **the scope is ``claims.run_id``**, so a citation belonging to another run
          is simply not returned. Rule 2 of the trust chain is then satisfied by
          omission rather than by a later check that could be forgotten;
        * **the chain is joined, not reassembled.** ``chunk_id``, ``source_id``,
          ``ordinal``, ``quote``, and the source metadata all come from the same rows
          the composite foreign key of P3.7 already ties together, so an entry cannot
          describe a passage that belongs to a different source.

        A citation whose quote is not inside its passage text is **not** returned:
        P3.7 verified that on write, and a row edited afterwards is refused here
        rather than offered to a model. An empty sequence is a legitimate state — a
        run with no verified evidence — and the service turns it into a typed
        failure before spending anything (FR-41).
        """
