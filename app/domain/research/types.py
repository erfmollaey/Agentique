"""Research Engine domain types (Phase 3, P3.1).

Pure types with no I/O: the contract between persistence and the later
Research Engine stages (search, fetch, extract, synthesize). No SQLAlchemy,
FastAPI, aiogram, Celery, Redis, HTTP, or provider SDK is imported here, so
the domain stays testable without infrastructure.

The five persisted entities mirror the frozen data model (§ 15.8, AD-034):
``ResearchRun``, ``Source``, ``SourceChunk``, ``Claim``, ``Citation``.
Transient pipeline concepts (``ResearchQuery``, ``SearchResult``,
``FetchResult``, ``ResearchReport``) are deliberately *not* tables and not
modeled as persisted records — a search result is a ``Source`` at
``PENDING`` status, a fetch outcome is source state, and the report is the
run's ``final_answer``.
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from app.domain.errors import SourceRankingValueError


class ResearchRunStatus(enum.StrEnum):
    """Lifecycle of one research execution (§ 15.1: queued → running → terminal)."""

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"

    @classmethod
    def coerce(cls, value: object) -> ResearchRunStatus:
        if isinstance(value, cls):
            return value
        try:
            return cls(str(value).strip().lower())
        except ValueError as exc:
            raise ValueError(f"unknown research run status: {value!r}") from exc

    @property
    def terminal(self) -> bool:
        return self in {
            ResearchRunStatus.COMPLETED,
            ResearchRunStatus.PARTIAL,
            ResearchRunStatus.FAILED,
        }


class SourceStatus(enum.StrEnum):
    """Where a source is in the fetch → extract → select lifecycle."""

    PENDING = "pending"
    FETCHED = "fetched"
    FETCH_FAILED = "fetch_failed"
    EXTRACTED = "extracted"
    UNUSABLE = "unusable"
    DUPLICATE = "duplicate"
    EXCLUDED = "excluded"
    SELECTED = "selected"

    @classmethod
    def coerce(cls, value: object) -> SourceStatus:
        if isinstance(value, cls):
            return value
        try:
            return cls(str(value).strip().lower())
        except ValueError as exc:
            raise ValueError(f"unknown source status: {value!r}") from exc


class ClaimStatus(enum.StrEnum):
    """Support verdict for a claim. Unsupported claims must be detectable (FR-39)."""

    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    CONTRADICTED = "contradicted"

    @classmethod
    def coerce(cls, value: object) -> ClaimStatus:
        if isinstance(value, cls):
            return value
        try:
            return cls(str(value).strip().lower())
        except ValueError as exc:
            raise ValueError(f"unknown claim status: {value!r}") from exc


def _non_empty(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("must be a non-empty string")
    return value


class ResearchRun(BaseModel):
    """One research execution, owned by exactly one user (SR-10)."""

    model_config = ConfigDict(from_attributes=True)

    id: int | None = None
    user_id: int
    conversation_id: int | None = None
    question: str
    request_key: str | None = None
    """The identity of the request that created this run (B7, P3.9-D).

    A digest derived from the Telegram command's own identifiers, in the same
    spirit as AD-017's ``messages.turn_id``, and guarded by
    ``uq_research_runs_user_id_request_key``. A redelivered update or a retried
    enqueue recomputes the same value and collides in the database, which is what
    makes "one logical run per user request" a constraint rather than a
    convention.

    ``None`` means the run has no derivable request identity — a caller with no
    Telegram ``message_id``, or a row that predates the ``/research`` command.
    Such a run is not deduplicated, and because PostgreSQL treats NULLs as
    distinct inside a unique constraint it never blocks another keyless run.
    """
    plan: dict[str, Any] | None = None
    synthesis: dict[str, Any] | None = None
    """The run's **verified** synthesis (P3.8).

    Nullable because a run is created at intake, planned at P3.6, and only reaches a
    cited answer at P3.8 — every earlier state legitimately has none.

    A raw mapping here, not a domain type, for the same reason ``plan`` is: this is a
    persistence-facing record, and the run row is read before any registry exists to
    re-verify a ``SynthesizedAnswer`` against. Reading it back as a trusted answer
    happens through ``app.services.research.synthesis.load_synthesis``, which
    re-verifies every citation against the run's registry — a stored payload is
    checked, never trusted (SR-9).
    """
    provider: str | None = None
    status: ResearchRunStatus = ResearchRunStatus.QUEUED
    failure_summary: str | None = None
    final_answer: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None
    completed_at: datetime | None = None

    @field_validator("question")
    @classmethod
    def _question_present(cls, value: str) -> str:
        return _non_empty(value)

    @field_validator("status", mode="before")
    @classmethod
    def _coerce_status(cls, value: object) -> ResearchRunStatus:
        return ResearchRunStatus.coerce(value)


class Source(BaseModel):
    """A normalized source discovered for a run (FR-10, FR-11)."""

    model_config = ConfigDict(from_attributes=True)

    id: int | None = None
    run_id: int
    url: str
    domain: str
    title: str | None = None
    snippet: str | None = None
    status: SourceStatus = SourceStatus.PENDING
    failure_reason: str | None = None
    content_hash: str | None = None
    rank: int | None = None
    quality_score: float | None = None
    duplicate_of_id: int | None = None
    summary: str | None = None
    """The source's own summary (FR-31), written by P3.7.

    Nullable because a source is persisted long before it is summarized: P3.2–P3.5
    create and rank sources without ever calling a model. **A summary is not
    evidence** — no claim may cite it, and its text is model prose about the
    source, carrying no passage behind any sentence of it (AD-033).
    """
    retrieved_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @field_validator("url", "domain")
    @classmethod
    def _identity_present(cls, value: str) -> str:
        return _non_empty(value)

    @field_validator("status", mode="before")
    @classmethod
    def _coerce_status(cls, value: object) -> SourceStatus:
        return SourceStatus.coerce(value)


class SourceChunk(BaseModel):
    """A persisted evidence passage from a source (FR-22).

    ``ordinal`` orders the chunks of one source; ``char_start``/``char_end``
    locate the passage in the source's extracted text, which is what makes a
    citation resolvable to a specific location (FR-37).
    """

    model_config = ConfigDict(from_attributes=True)

    id: int | None = None
    source_id: int
    ordinal: int
    text: str
    char_start: int
    char_end: int
    created_at: datetime | None = None

    @field_validator("text")
    @classmethod
    def _text_present(cls, value: str) -> str:
        return _non_empty(value)

    @field_validator("ordinal", "char_start", "char_end")
    @classmethod
    def _non_negative(cls, value: int) -> int:
        if value < 0:
            raise ValueError("must be >= 0")
        return value


class SourceRanking(BaseModel):
    """The ranking and deduplication metadata one source's row must carry.

    The persistence projection of P3.5's output. It is deliberately **not** a
    ``RankedResult``: the ranking stage works over ``SourceCandidate`` values that
    have no persisted identity, and turning one into a row needs three things
    this type exists to state — the persisted ``source_id`` it belongs to, the
    ``duplicate_of_id`` edge (which is a relationship between *persisted rows*,
    so it can only be resolved after both exist), and which of the four values is
    being written at all.

    **Every field is written, and every field is required.** There is no
    "leave unchanged" default, because a default would make ``None`` ambiguous:
    is it "no duplicate target" or "do not touch the duplicate target"? Those are
    different facts and the first one is the ordinary case. A caller that has
    nothing to say about a column passes ``None`` and the column becomes NULL —
    which is the correct meaning for a source nobody else duplicates.

    The invariants are enforced by validators, not by the caller:

    * ``rank`` is a 1-based position (P3.5's ``RankedResult.position``), because
      ``list_for_run`` and ``list_citable_for_run`` both order by
      ``sources.rank ASC NULLS LAST`` and a 0 would sort ahead of every real
      position;
    * ``quality_score`` is bounded by ``[0.0, 1.0]``, the same bounds
      ``RankingConfig.min_quality_score`` uses, because every ranking feature is
      normalized into that range and the score is their weighted sum;
    * ``content_hash`` is the 64-character lowercase hex SHA-256
      ``content_fingerprint`` returns — ``sources.content_hash`` is ``String(64)``,
      so a longer or non-hex value is a truncation or a fabrication;
    * ``duplicate_of_id`` is never the source itself: a source is not its own
      duplicate, and a self-edge would make the self-referential foreign key
      describe nothing.
    """

    model_config = ConfigDict(frozen=True)

    source_id: int
    rank: int | None
    quality_score: float | None
    content_hash: str | None
    duplicate_of_id: int | None

    @field_validator("source_id")
    @classmethod
    def _source_id_positive(cls, value: int) -> int:
        if value < 1:
            raise SourceRankingValueError("a source id must be a positive integer")
        return value

    @field_validator("rank")
    @classmethod
    def _rank_is_position(cls, value: int | None) -> int | None:
        if value is None:
            return None
        if value < 1:
            raise SourceRankingValueError(
                "a source rank is a 1-based position and cannot be below 1"
            )
        return value

    @field_validator("quality_score")
    @classmethod
    def _quality_score_bounded(cls, value: float | None) -> float | None:
        if value is None:
            return None
        if not 0.0 <= value <= 1.0:
            raise SourceRankingValueError(
                "a source quality score must be within [0.0, 1.0]"
            )
        return value

    @field_validator("content_hash")
    @classmethod
    def _content_hash_is_sha256(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
            raise SourceRankingValueError(
                "a source content hash must be the 64-character hexadecimal "
                "SHA-256 the ranking stage computes"
            )
        return value

    @field_validator("duplicate_of_id")
    @classmethod
    def _duplicate_target_positive(cls, value: int | None) -> int | None:
        if value is None:
            return None
        if value < 1:
            raise SourceRankingValueError(
                "a duplicate target must be a positive source id"
            )
        return value

    @model_validator(mode="after")
    def _duplicate_target_is_another_source(self) -> SourceRanking:
        if self.duplicate_of_id is not None and self.duplicate_of_id == self.source_id:
            raise SourceRankingValueError(
                "a source is not its own duplicate target"
            )
        return self


class Claim(BaseModel):
    """A claim extracted for a run, with its support verdict (FR-32, FR-39)."""

    model_config = ConfigDict(from_attributes=True)

    id: int | None = None
    run_id: int
    text: str
    status: ClaimStatus = ClaimStatus.UNSUPPORTED
    confidence: float | None = None
    created_at: datetime | None = None

    @field_validator("text")
    @classmethod
    def _text_present(cls, value: str) -> str:
        return _non_empty(value)

    @field_validator("status", mode="before")
    @classmethod
    def _coerce_status(cls, value: object) -> ClaimStatus:
        return ClaimStatus.coerce(value)


class Citation(BaseModel):
    """The programmatic claim → evidence relationship (FR-36, FR-37).

    Every citation points at a persisted source and chunk, so an answer can
    never cite content that this run did not retrieve (SR-9). ``location`` is
    a human-renderable locator within the source.

    ``quote`` is the verified passage itself: the exact text found in
    ``source_chunks.text`` for ``chunk_id``, in normalized form (see
    ``app.domain.research.evidence.normalize_evidence_text``). It is required,
    and the column is ``NOT NULL``, because a citation without the passage that
    justifies it is the exact failure AD-033 exists to prevent — the link would
    exist, but nothing would prove what it links. The database cannot constrain
    one row's text against another's, so the containment check
    ``quote ∈ normalize(chunk.text)`` is enforced by
    ``app.domain.research.evidence.verify_claims`` immediately before the row is
    written, and re-checked against the passage before persistence
    (P3.7).
    """

    model_config = ConfigDict(from_attributes=True)

    id: int | None = None
    claim_id: int
    source_id: int
    chunk_id: int
    quote: str
    """The passage that supports the claim. Non-empty; required."""

    location: str | None = None
    created_at: datetime | None = None

    @field_validator("quote")
    @classmethod
    def _quote_present(cls, value: str) -> str:
        return _non_empty(value)
