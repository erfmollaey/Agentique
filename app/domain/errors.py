"""Domain-level errors.

Typed so callers can distinguish failure modes without inspecting strings, and
so each maps to a distinct, non-revealing user-facing message (FR-6.4).

``retryable`` records whether retrying the operation could plausibly succeed.
Phase 1 FR-4.4 requires retryable failures to be retried a bounded number of
times and non-retryable ones not to be retried at all — a provider rejecting
the request for lack of credit, or a malformed response, will fail identically
on every attempt.

The structured failure record used by the synthesis stage lives here rather than
in :mod:`app.domain.research.synthesis` because this module imports nothing from
the domain package — every error family it defines is imported *by* the domain.
Putting the record beside the errors it describes keeps that direction one-way,
and it is a ``dataclass`` rather than a pydantic model for the same reason: a
pydantic model here would make the domain depend on this module's dependencies
instead of the reverse.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass


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


class ResearchRunNotFoundError(ResearchError):
    """No research run with that id belongs to that user (SR-10)."""

    retryable = False


# --- Search (Phase 3 P3.2) ---------------------------------------------------
#
# One family, one branch per outcome the frozen failure model (§ 15.9) lists
# for the search stage, so a caller can distinguish them without inspecting
# message text (FR-43). ``retryable`` follows § 15.9 exactly: provider failure,
# rate limit and timeout are retriable within a bounded budget; an invalid
# request, rejected credentials, a contract violation and an empty result set
# are not, because a second identical attempt fails identically.


class SearchError(ResearchError):
    """Base class for every search-stage failure."""


class SearchRequestInvalidError(SearchError):
    """The request was rejected here or by the provider (invalid search request).

    Covers an empty or over-long query, an out-of-range result count, and a
    provider 4xx that means "this request will never be accepted".
    """

    retryable = False


class SearchAuthenticationError(SearchError):
    """The provider rejected our credentials, or none are configured.

    Never retried, and never carried with the provider's own message: that text
    can echo the request headers, which is where the credential lives (SR-5).
    """

    retryable = False


class SearchRateLimitError(SearchError):
    """The provider's request allowance is exhausted.

    Retryable because the allowance refills, but only within a bounded budget
    (§ 15.9): an unbounded retry against a limit is how a run burns its whole
    time budget on failure.
    """

    retryable = True


class SearchTimeoutError(SearchError):
    """The provider did not answer within the configured timeout."""

    retryable = True


class SearchProviderError(SearchError):
    """The provider was unreachable or returned a server-side failure."""

    retryable = True


class SearchResponseInvalidError(SearchError):
    """The provider answered with a payload this adapter cannot normalize.

    Not retried: it is a contract violation, not a transient condition, and a
    second identical request would violate the same contract.
    """

    retryable = False


class SearchNoResultsError(SearchError):
    """The provider answered successfully and offered no usable result.

    Not retried (the frozen model marks "no results" non-retriable: an identical
    query yields an identical empty set) and terminal for the run unless another
    query in the plan succeeds — which is why it is distinguishable from a
    provider failure rather than folded into one (FR-41, FR-43).
    """

    retryable = False


# --- Fetch (Phase 3 P3.3) ---------------------------------------------------
#
# One branch per outcome the frozen failure model (§ 15.9) lists for the fetch
# stage, plus the transport outcomes it does not enumerate. Collapsing these into
# a single FetchError would destroy exactly the distinction FR-43 requires: "we
# refused to try" and "we tried and it was unreachable" are different facts for
# the run summary and different decisions for the orchestrator (P3.9).
#
# Every message here is written to be safe to surface. None of them contain the
# URL, the hostname, the resolved address, or the transport's own error text: the
# first two are attacker-supplied and the third is internal network topology,
# and FR-42 forbids exposing internal detail to a user. ``reason`` on
# FetchBlockedDestinationError carries a coarse address *category* for logs
# only, never for display.
#
# Retryability follows § 15.9 exactly, which is deliberately conservative for the
# fetch stage: timeout, DNS failure, redirect rejection, blocked destination,
# unsupported content type, and oversized response are all non-retryable,
# because every one of them is a property of the *destination*, and a second
# identical attempt reaches the same destination. Retrying them would multiply
# the wall-clock cost of a failing source against a finite run budget (FR-44).
# Connection failure is the one retryable branch: it is the only outcome here
# that is plausibly transient and not a property of the URL.


class FetchError(ResearchError):
    """Base class for every fetch-stage failure."""


class FetchInvalidURLError(FetchError):
    """The URL could not be parsed or violated a structural rule.

    Missing scheme, empty or malformed authority, embedded credentials,
    non-canonical numeric host, invalid port, whitespace or control characters.
    Not retryable: the same string fails identically forever.

    The rejected URL is never included in the message — it is attacker input,
    and echoing it back is how a control character or a credential ends up in a
    log line.
    """

    retryable = False


class FetchUnsupportedSchemeError(FetchError):
    """The scheme is not HTTP or HTTPS (FR-13).

    A distinct type from :class:`FetchInvalidURLError` because it is a policy
    decision rather than a parse failure, and because it is the signal that a
    caller tried ``file:``, ``gopher:``, or ``data:`` — worth its own log line.
    """

    retryable = False


class FetchBlockedDestinationError(FetchError):
    """The destination is not connectable: an SSRF violation (SR-1, SR-6).

    **Never retryable, under any circumstances.** A retry re-resolves and
    re-validates; on a rebinding name that is precisely the mechanism of the
    attack, and on a genuinely blocked address it wastes the run's time budget
    on a decision that cannot change (§ 15.9).

    ``reason`` is a coarse category (``ipv4-loopback``, ``ipv6-ULA``) intended
    for logs and metrics. It is deliberately not an address: an internal address
    is not the user's to be told about.
    """

    retryable = False

    def __init__(self, message: str, *, reason: str | None = None) -> None:
        super().__init__(message)
        self.reason = reason or "not-permitted"


class FetchDNSResolutionError(FetchError):
    """The hostname did not resolve, or resolved to nothing usable (SR-6).

    Non-retryable per § 15.9. A resolution failure is treated as *fail closed*:
    it is never interpreted as "no restriction found".
    """

    retryable = False


class FetchTimeoutError(FetchError):
    """Connect, read, or total deadline expired (FR-18).

    Non-retryable per § 15.9. A slow destination stays slow.
    """

    retryable = False


class FetchConnectionError(FetchError):
    """The TCP/TLS connection could not be established or was dropped.

    The one retryable fetch outcome: a refused connection or a reset is the only
    failure here that is plausibly transient rather than a property of the URL.
    It is still not retried by this stage — the fetcher performs exactly one
    attempt — because retry policy belongs to the stage that owns the run budget
    (P3.9, FR-44).
    """

    retryable = True


class FetchRedirectError(FetchError):
    """A redirect could not be followed safely (FR-15).

    No ``Location`` header, an unparseable ``Location``, a scheme the fetcher does
    not permit, a destination that fails validation, or a redirect loop.
    Non-retryable: the redirect chain is a property of the destination.

    Note that a redirect to a *blocked* destination raises
    :class:`FetchBlockedDestinationError` instead, not this, so an SSRF attempt
    via redirect is never reported as a generic redirect problem.
    """

    retryable = False


class FetchTooManyRedirectsError(FetchRedirectError):
    """The redirect limit was exceeded before a final response (FR-15).

    Separate from :class:`FetchRedirectError` because it is the outcome an
    operator needs to distinguish: this host is a redirect loop or a redirect
    chain, not a broken one.
    """

    retryable = False


class FetchResponseTooLargeError(FetchError):
    """The response exceeded the configured byte or decompression bound (FR-16).

    Measured against the *decoded* body while streaming, so a chunked response
    with no ``Content-Length`` and a compression bomb both hit the same wall.
    Non-retryable: the same response would be the same size.
    """

    retryable = False


class FetchUnsupportedContentTypeError(FetchError):
    """The response's content type is not one the pipeline processes (FR-17).

    The allowlist is HTTP text. A PDF, an image, an archive, or a JSON API
    response is recorded as a per-source failure rather than handed downstream
    as if it were readable text. Non-retryable.
    """

    retryable = False


class FetchUnsupportedEncodingError(FetchError):
    """The response used a content encoding the fetcher cannot decode.

    Distinguished from a size failure because the remedy differs: an undecodable
    encoding is a protocol mismatch, while an oversized body is a resource
    attack. Silently returning the still-compressed bytes as if they were text
    would be worse than refusing, so this stage refuses (SR-12).
    """

    retryable = False


class FetchHTTPStatusError(FetchError):
    """The origin returned a non-2xx, non-redirect status.

    A 404 or a 403 page is not source content, and presenting it as retrieved
    evidence would be the exact dishonesty SR-12 forbids. Whether it is worth
    retrying depends on the status, so ``status_code`` is retained and
    ``retryable`` is decided by :meth:`for_status` rather than fixed at the
    class level.
    """

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        # Set per instance rather than at class level because the answer depends
        # on the status: a 503 or a 429 may succeed shortly, a 404 never will.
        # Constructed from a bare message (the structural convention that every
        # ResearchError subclass accepts one positional message), no status is
        # known, so the failure is conservatively reported as non-retryable.
        self.retryable = status_code is not None and (
            status_code == 429 or 500 <= status_code <= 599
        )

    @classmethod
    def for_status(cls, status_code: int) -> FetchHTTPStatusError:
        """The typed error for a status the fetcher does not treat as content."""
        return cls(f"origin returned status {status_code}", status_code=status_code)


# --- Extraction (Phase 3 P3.4) ------------------------------------------------
#
# One family per outcome the frozen failure model (§ 15.9) lists for extraction.
# Collapsing into a single ExtractionError destroys the distinction FR-43 requires:
# "parse failure" (bad HTML) vs "empty result" (reached but useless) are different
# facts for the run summary and different orchestrator decisions (P3.9).
#
# Retryability: all extraction outcomes are non-retryable per § 15.9 — the bytes
# are already in memory, and a second pass over the same bytes produces the same
# result. The only exception is a transient parser failure that might succeed
# with a different parser strategy (not used here by design).


class ExtractionError(ResearchError):
    """Base class for every extraction-stage failure."""


class ExtractionParseError(ExtractionError):
    """The fetched content could not be parsed (malformed HTML / unreadable).

    Not retried: the same bytes fail identically. The caller records the
    source as unusable rather than retrying extraction.
    """

    retryable = False


class ExtractionEmptyResultError(ExtractionError):
    """Extraction completed but produced no usable text (FR-23, T-10).

    Distinguished from ``ExtractionParseError`` because a parse failure
    means "we could not read" while this means "we read and found nothing".
    Not retried: identical bytes yield identical emptiness.
    """

    retryable = False


class ExtractionUnsupportedContentError(ExtractionError):
    """The content type of the fetched source is not supported for extraction.

    Non-retryable: a PDF, image, archive, or JSON API response will never
    become readable text at this stage. The caller records per-source failure.
    """

    retryable = False


# ---------------------------------------------------------------------------
# Deduplication and ranking (Phase 3, P3.5)
# ---------------------------------------------------------------------------
#
# Every outcome here is deterministic: the same candidates in the same order
# always produce the same groups and the same ordering. A failure therefore
# always means the *input* is unusable, never that a retry could help, and every
# error in this family is non-retryable. The stage degrades instead of raising
# where it can: an unusable source is scored and marked with an exclusion reason
# (FR-30), not rejected. These errors exist for programming errors — a malformed
# candidate, an identity the canonicalizer cannot express — so that the defect is
# reported at the boundary instead of being silently absorbed into a ranking.


class DeduplicationError(ResearchError):
    """Base class for every deduplication-stage failure."""

    retryable = False


class InvalidSourceCandidateError(DeduplicationError, ValueError):
    """A source candidate cannot take part in deduplication or ranking.

    Raised when a candidate has no usable URL or no usable provider position —
    both of which the canonical identity and the provider signal require.

    Also a ``ValueError``, because this is an invalid argument rather than a
    failing subsystem: a caller already guarding the boundary with
    ``except ValueError`` keeps working, and a caller that wants the stage's
    specific contract can catch this type instead of a string.
    """

    retryable = False


class UnsupportedSourceIdentityError(DeduplicationError, ValueError):
    """A URL has no canonical form under the current identity policy.

    Raised when a value cannot be parsed into a scheme and authority, so it has
    no comparable form. Deliberately *not* a fetch validation: it says the value
    cannot be compared, not that it is safe to request. Also a ``ValueError``,
    for the reason given on :class:`InvalidSourceCandidateError`.
    """

    retryable = False


class RankingInputError(ResearchError, ValueError):
    """Ranking was called with an input its contract does not accept.

    Non-retryable, like the rest of this family: the same input fails the same
    way on every attempt. Also a ``ValueError``, since it is an invalid argument.
    """

    retryable = False


# ---------------------------------------------------------------------------
# Research planning (Phase 3, P3.6)
# ---------------------------------------------------------------------------
#
# Planning is the one stage whose output is *generated* rather than measured, so
# its failures split two ways and the types say which is which:
#
# * `PlanningInputError` — the research question itself was unusable (empty, too
#   long, not text). A caller defect, raised before any model is called.
# * `PlanSchemaError` — the model returned something that is not the planning
#   structure: not JSON, not an object, missing every required key. A provider
#   contract violation on *this* attempt.
# * `PlanValidationError` and its subclasses — the model returned a structure that
#   parsed but violates the plan contract: a degenerate plan (FR-3), an unknown
#   dependency, a cycle.
#
# All three are non-retryable *for the planner itself*. Retrying is the
# orchestrator's decision, not this layer's, and the planner holds no retry state
# — a bounded retry inside it would be a second retry system beside the provider's
# existing one (Phase 2 AD-024), which § 15 forbids. A structurally invalid plan
# is a property of the output, not of the attempt.
#
# Provider failures are NOT re-typed here. `LLMServiceError`, `LLMTimeoutError`,
# `LLMAuthenticationError`, and `LLMResponseError` already carry the `retryable`
# flag the task-level retry policy reads, and duplicating them into a planning
# family would lose that.
#
# **Unlike P3.5's ranking and dedup errors, none of these derives from
# `ValueError`**, and that is deliberate. Pydantic wraps a `ValueError` raised
# inside a validator into an opaque `ValidationError`, so a `ValueError` subclass
# raised from a field or model validator would be swallowed and the caller could
# not catch the specific defect. A non-`ValueError` propagates unchanged, which is
# what lets a caller distinguish "this plan has a cycle" from "this plan is
# degenerate" from "the model answered with the wrong shape" — the distinction
# FR-3 depends on. The ranking errors keep their `ValueError` base because they
# replaced code that raised `ValueError` and existing callers catch that type.


class PlanningError(ResearchError):
    """Base class for every planning-stage failure."""

    retryable = False


class PlanningInputError(PlanningError):
    """The research question cannot be planned.

    Raised before any model call: the question is empty, is not text, or exceeds
    the configured length bound. A bounded question is also what keeps the prompt
    bounded (§ 29 performance: no large unbounded prompts).
    """

    retryable = False


class PlanSchemaError(PlanningError):
    """The model's output is not the planning structure at all.

    Malformed JSON, a JSON value that is not an object, or an object missing every
    required key. Distinct from :class:`PlanValidationError` because "the model
    did not answer the question asked" and "the model answered with an invalid
    answer" are different defects with different fixes.
    """

    retryable = False


class PlanValidationError(PlanningError):
    """A parsed plan violates the plan contract.

    The general case: a degenerate plan (FR-3, T-2) — no objective, no steps, or
    no step that would actually retrieve anything.
    """

    retryable = False


class PlanDependencyError(PlanValidationError):
    """A step depends on a step that cannot be satisfied.

    A dependency naming a step that does not exist, or a step depending on
    itself. Kept separate from the cycle error because the fix differs: a missing
    or self-reference is a naming mistake, a cycle is an ordering mistake.
    """

    retryable = False


class PlanCycleError(PlanDependencyError):
    """The steps form a cycle, so they cannot be executed in any order.

    A dependency graph that is not acyclic has no valid execution order. This is
    detected rather than tolerated because a cycle silently means some steps are
    unreachable, which would present later as an under-researched answer rather
    than as an error.
    """

    retryable = False


# ---------------------------------------------------------------------------
# Per-source evidence (Phase 3, P3.7)
# ---------------------------------------------------------------------------
#
# One branch per outcome the frozen failure model (§ 15.9) lists for
# summarization and claim extraction, plus the two this stage distinguishes on
# its own. The split that matters is between three things a caller must never
# confuse:
#
# * `SourceContentEmptyError` / `SummaryInputError` — raised **before any model
#   is called**. The input is unusable, so nothing was spent and nothing may be
#   written.
# * `SummarySchemaError` / `ClaimExtractionSchemaError` — the model answered, and
#   the answer is not the requested structure at all. Non-retryable: an identical
#   request produces an identical answer, and the provider's own bounded retry
#   already ran below this layer (AD-024).
# * `ClaimEvidenceError` — a **programming** error: evidence that reached
#   persistence without verifying. The frozen failure model requires that this be
#   impossible, so it is a type rather than a tolerance. A candidate claim whose
#   evidence does not verify is *not* this error — it is an expected adversarial
#   outcome, recorded as a typed rejection by
#   `app.domain.research.evidence.verify_claims`, never raised.
#
# All non-retryable, like the planning family: every one of these is a property
# of the input or of one response, not of provider availability. Provider
# failures (`LLMServiceError`, `LLMTimeoutError`, `LLMAuthenticationError`,
# `LLMResponseError`) are deliberately **not** re-typed here, so the task-level
# retry policy keeps reading the `retryable` flag it already understands.
#
# None of these derives from `ValueError`, for P3.6's reason: pydantic wraps a
# `ValueError` raised inside a validator into an opaque `ValidationError`, which
# would make each typed error uncatchable by name.


class ResearchSourceNotFoundError(ResearchError):
    """No source with that id exists, or it belongs to a different run.

    Named separately from :class:`ResearchRunNotFoundError` because a caller
    reaches a source either through a run it owns or by naming an id directly,
    and the second case is the interesting one: a source id from another run is
    refused rather than analysed, so a run can never read a source it does not
    own (SR-10, T-23).
    """

    retryable = False


class EvidenceError(ResearchError):
    """Base class for every per-source summarization / evidence failure."""

    retryable = False


class SourceAlreadyAnalyzedError(EvidenceError):
    """This source's evidence has already been persisted (B7, P3.9-D).

    Raised by ``SourceEvidenceService`` when the atomic claim it takes before
    writing (``SourceRepository.claim_unanalysed``) reports that the source already
    has a summary — which, because P3.7 writes the summary and every claim and
    citation in one transaction, means its evidence is already stored.

    A **refusal, not a defect**: with ``task_acks_late`` and
    ``task_reject_on_worker_lost`` a redelivered research task re-executes from the
    start, and re-running the evidence stage would write a second set of claims for
    the same source (audit § R.14). The caller records the source as already
    evidenced and moves on.

    Non-retryable for the obvious reason: the work is done.
    """

    retryable = False


class SourceContentEmptyError(EvidenceError):
    """The source has no usable persisted chunk to reason over (FR-23).

    Raised **before any model call**. A source with no chunks is a source P3.4
    found unusable; asking a model to summarize nothing would either cost money
    or produce an invented description, and the second is precisely the failure
    this stage exists to prevent. The caller records the source as unusable
    rather than retrying it.

    Non-retryable: the chunks are already persisted, so they will not appear.
    """


class SummaryInputError(EvidenceError):
    """The summarization request is unusable (invalid summarization input).

    A missing source id, an empty or control-character-bearing domain or title,
    or a passage with no readable text — all rejected before any model call, for
    the same reason the input length is: an unbounded prompt is both a cost and a
    context-window hazard, and untrusted text in the metadata is a prompt-shape
    hazard.

    The offending value is never included in the message. Source metadata is
    attacker-influenced text, and echoing it into a log line or an exception
    message is how fetched content ends up somewhere it should not (SR-7).
    """


class SummarySchemaError(EvidenceError):
    """The model answered with something that is not the summary structure.

    Malformed JSON, a JSON value that is not an object, a missing ``summary``
    key, or a key the contract does not define. A response carrying unknown keys
    is refused rather than tolerated: an instruction smuggled into the response
    under a key nobody reads is still content this stage would otherwise store,
    and the contract is closed, so refusing costs nothing honest.
    """


class SummaryValidationError(EvidenceError):
    """A parsed summary violates the summary contract.

    An empty summary, one longer than the configured bound, or a language tag
    that is not a BCP-47 style tag. Distinct from :class:`SummarySchemaError`
    because "the model wrote the wrong shape" and "the model wrote an unusable
    value" have different fixes, and FR-43 asks for distinguishable outcomes.
    """


class ClaimExtractionSchemaError(EvidenceError):
    """The model answered with something that is not the claims structure.

    Malformed JSON, a non-object, a missing or non-list ``claims`` key, or a
    claim entry that is not a JSON object, or an entry carrying a key the
    contract does not define.
    """


class ClaimStructureError(EvidenceError):
    """A claim entry cannot be read as a claim at all.

    Raised for an entry whose ``statement`` is not a string, or whose
    ``source_chunk_id`` is not an integer — types rather than values, because a
    type error means the response is not the requested structure, while a *value*
    problem (an empty statement, an unknown chunk, a quote that is not in the
    passage) is a rejected candidate, recorded with a reason by
    `verify_claims` and never raised.

    An id is not coerced from a string: ``"12"`` is not an id, and guessing at a
    model that mistyped one is how a citation gets pointed at the wrong passage.
    """


class ClaimEvidenceError(EvidenceError):
    """Evidence reached persistence without verifying (a defect, not an outcome).

    The frozen contract (§ 15.7, AD-033, SR-9) requires that a claim cannot be
    written without a passage that verifies against stored content. The
    application layer enforces the textual containment rule the database cannot
    (PostgreSQL cannot constrain one row's text against another's), so a
    violation here is a bug in the caller rather than a model failure.

    It exists as a type so the violation is loud and named. It is not a tolerance
    path: nothing catches it. Two concrete cases: a quote that is not present in
    the passage it names, and a citation created without a quote at all — the
    latter is refused by ``CitationRepositoryImpl.add`` before any row exists, and
    the ``citations.quote`` column is ``NOT NULL`` so no path can write one.
    """


# ---------------------------------------------------------------------------
# Passage/chunk production and ranking persistence (Phase 3, P3.9-A)
# ---------------------------------------------------------------------------
#
# Two families, one per prerequisite the P3.9 audit found missing (§ R.6, § R.9).
# Both are non-retryable and none of them derives from `ValueError`, for P3.6's
# reason: pydantic wraps a `ValueError` raised inside a validator into an opaque
# `ValidationError`, and `SourceRanking`'s invariants are enforced by validators
# precisely so that a caller catching a named error still can.
#
# The two errors that are *refusals* rather than defects are the interesting ones:
#
# * `ChunkReplaceBlockedError` — the source's existing passages are already
#   referenced by verified citations. `citations.chunk_id` is `ON DELETE CASCADE`
#   from `source_chunks`, so deleting them would silently delete the citations
#   that cite them. Refusing is the only behaviour that keeps AD-033's trust
#   chain intact; the stage cannot "fix" a citation by removing its evidence.
# * `DuplicateTargetError` — the duplicate target is not a source of the run
#   being written. Attaching a source to another run's row would make
#   `duplicate_of_id` a cross-run edge the schema cannot express, because the
#   foreign key is self-referential and has no `run_id` in it.


class ChunkingError(ResearchError):
    """Base class for every passage/chunk-production failure."""

    retryable = False


class ChunkLimitError(ChunkingError):
    """The extracted text would produce more passages than the structural ceiling.

    SR-5 requires chunk counts to be bounded, and the bound has to be structural
    rather than a function of how a page happens to be punctuated: a chunk is at
    most ``MAX_CHUNK_CHARS`` and at least ``MIN_CHUNK_CHARS``, so a page inside
    ``MAX_EXTRACTED_CHARS`` needs far fewer than the ceiling allows — reaching it
    means the text is a run of minimal-length sentences, which is not evidence
    worth 1,000 rows. Raised **before any row is written**, so the refusal leaves
    the source's existing passages untouched.
    """

    retryable = False


class ChunkReplaceBlockedError(ChunkingError):
    """Existing passages of this source are cited, so they cannot be replaced.

    ``citations.chunk_id`` is a foreign key into ``source_chunks`` with
    ``ON DELETE CASCADE``, so the database itself would remove every citation
    that cites a deleted passage. That is silent destruction of verified
    evidence: the citation row carrying the quote that justified a claim would
    simply be gone, and the claim it supported would remain (AD-033, SR-9).

    Refused rather than worked around, because the alternatives are worse: no
    constraint in this schema can re-point a citation at the replacement
    passage, and nothing in this stage may decide which claim survives. The
    caller records the source as already evidenced and moves on; re-chunking a
    source that has produced verified claims is an orchestration decision (a
    new run), not a rewrite of an existing one.
    """

    retryable = False


class SourceRankingError(ResearchError):
    """Base class for every ranking/deduplication persistence failure."""

    retryable = False


class SourceRankingValueError(SourceRankingError):
    """A ranking value violates a domain invariant or a database constraint.

    One branch, because the caller's fix is the same in every case: the value
    does not belong in the column it was aimed at. Invalid for a stated reason —
    a rank is a 1-based position, a quality score is bounded by the weighted sum
    the ranking policy can produce, a content hash is the 64-character SHA-256
    ``content_fingerprint`` returns, and a duplicate target is never the source
    itself.
    """

    retryable = False


class DuplicateTargetError(SourceRankingError):
    """The duplicate target is not a source of the run being written.

    ``sources.duplicate_of_id`` is a self-referential foreign key with ``ON
    DELETE SET NULL`` and no ``run_id`` in it, so the database will happily
    accept a source pointing at a row belonging to a *different* research run.
    That edge is not expressible in the schema and is not meaningful in the
    domain: a duplicate group is a set of candidates from one run's candidate
    list, so the relationship must stay inside it (FR-26).
    """

    retryable = False


# ---------------------------------------------------------------------------
# Cross-source synthesis and citation construction (Phase 3, P3.8)
# ---------------------------------------------------------------------------
#
# One branch per outcome the frozen failure model (§ 15.9) lists for synthesis
# plus the citation-integrity failures SR-9 names, and the split that matters is
# between three things a caller must never confuse:
#
# * `SynthesisInputError` — raised **before any model is called**. The run has no
#   verified evidence, or the objective/question is unusable, so nothing is spent
#   and nothing may be written. FR-41 in its synthesis form: a run with no
#   verified evidence has no answer, and inventing one is the failure this phase
#   exists to prevent.
# * `SynthesisSchemaError` / `CitationStructureError` / `ContradictionStructureError`
#   — the model answered, and the answer is not the requested structure at all.
#   Non-retryable: an identical request produces an identical answer, and the
#   provider's own bounded retry already ran below this layer (AD-024).
# * The `CitationIntegrityError` family — **the security property of this stage.**
#   The model proposed citation references, and at least one of them does not
#   resolve to persisted evidence *of this run*. Every branch is non-retryable,
#   because a fabricated citation is not a transient condition and re-asking
#   cannot make it true. Nothing here is stripped and returned anyway: the answer
#   is not constructed (§ 17, and the P3.8 brief's "fail closed").
#
# All non-retryable, for P3.6/P3.7's reason. Provider failures
# (`LLMServiceError`, `LLMTimeoutError`, `LLMAuthenticationError`,
# `LLMResponseError`) are deliberately **not** re-typed here, so the task-level
# retry policy keeps reading the ``retryable`` flag it already understands.
#
# None of these derives from `ValueError`: pydantic wraps a `ValueError` raised
# inside a validator into an opaque `ValidationError`, which would make each
# typed error uncatchable by name.


class CitationFailureKind(enum.StrEnum):
    """Why one proposed citation reference was refused.

    A closed set, because "the citations did not check out" is not a reportable
    outcome: an orchestrator has to be able to count them by category (FR-42),
    and a free-text reason could not be aggregated.

    The distinctions are the point. ``UNKNOWN_CITATION`` and
    ``UNSHOWN_CITATION`` are both refused, but they are different facts — the
    first means the model invented an id, the second that the budget truncated
    the evidence before the model saw it — and an operator reading a run summary
    needs to tell them apart to know whether the defect is adversarial or
    operational.
    """

    UNKNOWN_CITATION = "unknown_citation"
    """No registry entry has that id: the model invented it, or the run has none."""

    UNSHOWN_CITATION = "unshown_citation"
    """The id is a real citation of this run, but it was outside the bounded context
    the model was shown. The model may only reference what it was given."""

    FOREIGN_CITATION = "foreign_citation"
    """The citation exists, but its claim does not belong to this research run."""

    BROKEN_EVIDENCE_CHAIN = "broken_evidence_chain"
    """The persisted chain ``citation → claim → chunk → source → run`` does not hold:
    the passage is not this citation's source's, the source is not this run's, or
    the claim is not this run's. A defect in the stored data, not in the model."""

    QUOTE_NOT_IN_PASSAGE = "quote_not_in_passage"
    """The persisted quote is not a literal substring of the persisted passage text.

    Detected here as well as at P3.7's write time: the containment rule is not
    expressible in SQL, so this is the read-side re-check that a hand-edited row
    cannot pass unnoticed."""

    UNCITED_SECTION = "uncited_section"
    """A section carries no citation that resolves. Citation completeness (FR-36),
    decided at the section granularity P3.7 already established."""

    UNKNOWN_CLAIM = "unknown_claim"
    """A proposed contradiction names a claim id that is not in this run's evidence."""

    SELF_CONTRADICTION = "self_contradiction"
    """A proposed contradiction names the same claim on both sides."""


@dataclass(frozen=True)
class CitationFailure:
    """One refused citation reference, in a form an operator can aggregate.

    ``reference`` is always an **identifier** (a citation id or a claim id), never
    model prose and never a URL. The whole point of these errors is that a
    fabricated reference cannot leak attacker-chosen text into a log line or an
    exception message (SR-7); the offending *section* is identified by its index
    in the proposal, which the application assigned.
    """

    kind: CitationFailureKind
    detail: str
    section_index: int | None = None
    reference: int | None = None


class SynthesisError(ResearchError):
    """Base class for every synthesis-stage failure."""

    retryable = False


class SynthesisInputError(SynthesisError):
    """The synthesis request is unusable (invalid synthesis input).

    Raised **before any model is called**: the run has no persisted verified
    evidence to synthesize, or the question/objective is empty, over-long, or
    carries a control character. FR-41 in synthesis form — a run with no usable
    sources produces a clear failure, never an invented answer.

    The offending value is never included in the message: the question is user
    input and the objective is model output derived from it, so echoing either is
    how untrusted text reaches a log line (SR-7).
    """


class SynthesisSchemaError(SynthesisError):
    """The model answered with something that is not the synthesis structure.

    Malformed JSON, a JSON value that is not an object, a missing or non-list
    ``sections``, or a section entry carrying a key the contract does not define.
    An unknown key is refused rather than tolerated: an instruction smuggled into
    the response under a key nobody reads is still content this stage would
    otherwise store next to a real field, and the contract is closed.
    """


class SynthesisValidationError(SynthesisError):
    """A parsed proposal violates the synthesis contract.

    No sections at all, more sections than the structural ceiling, a section whose
    text is empty or over-long, a section citing more references than the ceiling,
    or a schema version this build cannot read. Distinct from
    :class:`SynthesisSchemaError` because "the model wrote the wrong shape" and
    "the model wrote an unusable value" have different fixes.
    """


class CitationStructureError(SynthesisError):
    """A citation reference cannot be read as a citation reference at all.

    Raised for a ``citation_ids`` value that is not a list, an entry that is not a
    JSON number, or a section carrying an unsupported field. An id is **not**
    coerced from a string: ``"12"`` is not an id, and guessing at one is how a
    citation gets pointed at the wrong passage — exactly the reasoning P3.7
    applied to ``source_chunk_id``.
    """


class CitationIntegrityError(SynthesisError):
    """A proposed citation reference did not resolve to evidence of this run.

    The security property of the stage (SR-9, § 15.7). The model may *propose*
    references; only this verification can accept them, and a failure here means
    **no answer is constructed at all** — never one with the bad reference
    removed and the rest presented as though it were complete.

    ``failures`` carries **every** reference that was refused, in the deterministic
    order the verifier examined them, so a caller can report a count by category
    rather than only the first. It is empty when the error is raised for a different
    reason, which is why the constructor takes an optional keyword and the class
    stays constructible from a single message.
    """

    def __init__(
        self, message: str = "", *, failures: tuple[CitationFailure, ...] = ()
    ) -> None:
        super().__init__(message)
        self.failures = failures

    def counts_by_kind(self) -> dict[CitationFailureKind, int]:
        """How many references were refused per category (FR-42)."""
        counts: dict[CitationFailureKind, int] = {}
        for failure in self.failures:
            counts[failure.kind] = counts.get(failure.kind, 0) + 1
        return counts


class UnknownCitationError(CitationIntegrityError):
    """The proposal cites an identifier that is not in this run's registry.

    The plain fabrication case: a fluent, plausible citation id that corresponds to
    nothing retrieved. Rejected, never resolved "because it exists somewhere".
    """


class UnshownCitationError(CitationIntegrityError):
    """The proposal cites a real citation of this run that the model was not shown.

    A real id is still refused: the synthesis budget bounds what the model read,
    so a reference to evidence outside that budget cannot have been read from the
    prompt and is as ungrounded as an invented one. Reported separately from
    :class:`UnknownCitationError` because it is an operational signal — a truncated
    context — rather than an adversarial one.
    """


class ForeignCitationError(CitationIntegrityError):
    """The citation exists, but its claim belongs to a different research run.

    Ownership is verified through the persisted relationship, not by trusting the
    id. SR-10 and § 15.7 together: a citation from another user's run must be
    unnameable here, and the only way to know that is to resolve the claim's run.
    """


class CitationChainError(CitationIntegrityError):
    """A citation's persisted evidence chain does not hold.

    ``citation → claim → source_chunk → source → research_run``: the passage must
    belong to the named source, the source to the named run, and the claim to the
    run being synthesized. A violation is a defect in stored data rather than a
    model outcome, and it is a type rather than a tolerance.
    """


class CitationQuoteError(CitationIntegrityError):
    """A persisted citation's quote is not inside its persisted passage.

    ``citations.quote`` was verified against ``source_chunks.text`` at P3.7's write
    time; this is the read-side re-check. The database cannot constrain one row's
    text against another's, so a hand-edited row is detectable here and nowhere
    else.
    """


class CitationCompletenessError(CitationIntegrityError):
    """A section of the answer carries no citation that resolves (FR-36).

    The deterministic completeness policy, stated rather than approximated: this
    stage does not attempt semantic truth detection. It requires, at the section
    granularity P3.7 already established, that every factual section names at
    least one citation reference that resolves to persisted evidence of this run.
    An unsupported machine-detectable reference, and a section with no reference,
    both fail closed.
    """


class ContradictionError(SynthesisError):
    """Base class for a refusal of a proposed contradiction report (FR-33, FR-34)."""

    def __init__(
        self, message: str = "", *, failures: tuple[CitationFailure, ...] = ()
    ) -> None:
        super().__init__(message)
        self.failures = failures

    def counts_by_kind(self) -> dict[CitationFailureKind, int]:
        counts: dict[CitationFailureKind, int] = {}
        for failure in self.failures:
            counts[failure.kind] = counts.get(failure.kind, 0) + 1
        return counts


class ContradictionStructureError(ContradictionError):
    """A proposed contradiction cannot be read as one.

    A ``claim_ids`` value that is not a list of exactly two JSON numbers, or an
    entry carrying a key the contract does not define.
    """


class ContradictionReferenceError(ContradictionError):
    """A proposed contradiction names claims that are not usable as one.

    Either an id that is not in this run's evidence, or the same claim on both
    sides. The conflict itself is a *qualification*, never an authority: refusing
    to report a disagreement changes what the answer says, so it is checked, and
    it cannot discard the claims involved.
    """


# ---------------------------------------------------------------------------
# Research orchestration (Phase 3, P3.9-D)
# ---------------------------------------------------------------------------
#
# Two branches the orchestrator needs in order to report honestly (FR-43,
# § R.10): "there was nothing to find" and "something went wrong while looking"
# are different facts, and a user who hears one when the other happened acts
# wrongly. `ResearchNoEvidenceError` is deliberately **not** folded into
# `SynthesisInputError`, which means "this specific run had no verifiable evidence
# at the moment it reached synthesis": a run whose sources all failed to fetch is
# a different, earlier fact, and a caller that wants to say "I couldn't retrieve
# anything" should not have to infer it from a synthesis stage it may not even
# have reached.
#
# `ResearchDeadlineError` exists so that the deadline path is classified *as a
# typed error* rather than as a bare Celery exception. That is what keeps AD-045's
# requirement that the cooperative deadline not be conflated with a retryable
# provider failure: the task's retry policy reads `retryable`, and `False` here is
# the whole content of the decision. It is raised **inside** the coroutine, at a
# stage boundary — never from a signal handler — because a signal-delivered
# exception would unwind the awaiting frame rather than the coroutine and leave
# the unit of work open (audit § S.2(2)).
#
# Neither derives from `ValueError`, for P3.6's reason: pydantic wraps a
# `ValueError` raised inside a validator into an opaque `ValidationError`, which
# would make each typed error uncatchable by name.


class ResearchDeadlineError(ResearchError):
    """The run exceeded its cooperative time budget (AD-045, FR-44, § 15.9).

    Non-retryable. A deadline that is met by re-running the pipeline fixes nothing:
    the run spends its budget again, and the retry allowance would then be the only
    thing bounding spend. AD-045 requires the run to stop and keep what it has.
    """

    retryable = False


class ResearchPipelineError(ResearchError):
    """The pipeline could not complete, for a reason that is not a stage defect.

    The one exception a stage may raise when an upstream failure leaves no honest
    answer and no retryable path: every source is unusable, no plan step produced
    any candidate, or a stage raised something outside the typed families this
    domain defines. It is deliberately distinct from ``SynthesisInputError`` (no
    evidence *at synthesis*) and from ``ResearchNoEvidenceError`` (nothing worth an
    answer anywhere).

    Retryable by default: unlike a validation failure, this usually means a provider
    or an environment was unavailable, and the task policy is what bounds the retry —
    not this class. A caller with a deeper diagnosis raises a more specific error
    instead. The message never reaches a user (§ 15.9).
    """

    retryable = True


class ResearchNoEvidenceError(ResearchError):
    """The run produced nothing worth an answer, anywhere (FR-41, SR-12).

    Raised when the pipeline completed its stages and there is no verified evidence
    — nothing was retrieved, every candidate failed, or every proposed claim was
    rejected. It is separate from ``SynthesisInputError``, which is raised *before*
    any model call because the citation registry was empty; a run that never got
    that far needs a different ending than one that arrived empty.

    Non-retryable: the same question, the same providers, and the same web produce
    the same nothing. The user is told so plainly rather than offered a retry that
    cannot succeed.
    """

    retryable = False


# --- synthesized-answer citation re-verification ---------------------------
#
# `SynthesizedAnswer.from_payload` is the one path that builds a trusted citation
# without the in-memory verification token, and it deliberately cannot fabricate one:
# it resolves each stored citation against a `CitationRegistry` the caller supplies,
# which is itself built from persisted rows. The registry is therefore the *only*
# authority for what a citation means — in memory and on read-back alike — and the
# three failures below are what a hand-edited row provokes.


class StoredCitationChainError(CitationIntegrityError):
    """A stored citation names a different claim, source, or passage than its registry entry.

    The read-side counterpart of :class:`CitationChainError`: the identifiers in the
    stored payload are individually real, but they do not agree with the persisted
    relationship. Refused field by field, so a row re-pointed at another passage is
    caught even though every id in it resolves.
    """


class StoredCitationQuoteError(CitationIntegrityError):
    """A stored citation carries a quote or a URL that is not the persisted evidence.

    Rule 5 and Rule 6 on the read path: a URL in the answer must be the persisted
    ``sources.url``, and a quote must be the verified passage. A stored answer whose
    URL differs from the source row's is exactly the fabricated-source failure the
    write path makes structurally impossible, found after the fact here.
    """
