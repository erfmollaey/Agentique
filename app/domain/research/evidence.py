"""Per-source summarization and evidence-grounded claim extraction (Phase 3, P3.7).

Two responsibilities, one source at a time (FR-31, FR-32):

```text
one persisted Source + its persisted SourceChunk rows
        ↓
   SourceSummarizer   → SourceSummary          (about the source, nothing else)
   ClaimExtractor     → ClaimExtraction        (candidate claims, unverified)
   verify_claims()    → ClaimVerification      (accepted + rejected, with reasons)
```

**The invariant this module exists to make true:**

```text
Claim
  ↓
SourceChunk                      (a persisted row, from this source, this run)
  ↓
exact passage / quote             (present in that chunk's stored text)
  ↓
original Source                   (owned by that chunk, enforced by the database)
```

A model saying a claim is supported is **not** evidence. Nothing in this module
trusts the model about anything, including its own output shape: the model
produces candidate text, and :func:`verify_claims` decides what is admissible by
comparing the quoted passage against stored chunk text. There is no model
self-certification, no second model asked to adjudicate the first model's work,
and no similarity score standing in for a substring.

**Per-source, deliberately.** Nothing here reads a second source. There is no
cross-source comparison, no synthesis, and no contradiction detection — those are
FR-33/FR-34 and P3.8. A summary describes one source and claims are extracted
from one source, which is what makes "which source supports this?" a question with
a single, checkable answer.

**Boundaries this module holds** (§ 15.3, AD-032, AD-033):

* no I/O, no clock, no randomness, no network, no SQLAlchemy, no provider SDK, no
  HTTP client. It is a pure decision function plus validated value types, so the
  entire trust decision is testable with no infrastructure;
* the only authority for "which chunk may a claim cite" is the chunk set supplied
  for the current source. A reference to an id that exists elsewhere in the
  database is **not** resolved against the database — see
  :func:`verify_claims` for why;
* normalization is explicitly minimal (:func:`normalize_evidence_text`) so that
  Persian, ZWNJ, Arabic-Indic digits, and punctuation survive intact.

A note on what the model may influence: the *statement* and the *quote* are model
text, and both are normalized and validated, but neither is trusted. Everything
that makes a claim admissible is decided by this module.
"""

from __future__ import annotations

import enum
import unicodedata
from collections.abc import Iterable, Sequence
from typing import Any, Protocol, Self, runtime_checkable

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from app.domain.errors import (
    ClaimExtractionSchemaError,
    ClaimStructureError,
    EvidenceError,
    SourceContentEmptyError,
    SummaryInputError,
    SummarySchemaError,
    SummaryValidationError,
)

#: Version of the summary *structure*, not of the prompt that produces it. Bump on
#: a breaking shape change so a summary persisted by an older build is
#: recognizable rather than misread. The prompt carries its own version
#: (``app.infrastructure.evidence.prompts.SUMMARIZATION_PROMPT_VERSION``); the two
#: change for different reasons, as P3.6 recorded for the plan.
SUMMARY_SCHEMA_VERSION = "source-summary-v1"

#: Version of the claim-extraction *structure*. Same reasoning, separate constant.
CLAIM_EXTRACTION_SCHEMA_VERSION = "claim-extraction-v1"

#: Upper bound on a generated summary. A summary is a description of one source,
#: not an essay about a topic; past this it is either a synthesis (out of scope)
#: or a repetition of the source text.
MAX_SUMMARY_CHARS = 2_000

#: Upper bound on one claim statement. A claim is one assertion.
MAX_STATEMENT_CHARS = 500

#: Upper bound on a quoted passage. Bounds what one candidate can carry into a
#: prompt and into a row; long quotes are a summarization failure, not evidence.
MAX_QUOTE_CHARS = 1_200

#: Lower bound on a quoted passage, and the reason it exists.
#:
#: A substring test alone is satisfied by "the", "2024", or "است" — text that
#: appears in the passage and supports nothing. Requiring roughly three words
#: removes that trivially-verifiable case without tokenizing, so the bound means
#: the same thing in Persian, English, and mixed text: a passage too short to
#: carry an assertion cannot support one. Measured in characters because
#: character count is the only script-agnostic measure available without
#: introducing a tokenizer this project does not have.
MIN_QUOTE_CHARS = 16

#: Structural ceiling on claims in one extraction response. The operating bound is
#: configuration (``CLAIM_MAX_CLAIMS``); this is the value configuration cannot
#: raise, so a misconfiguration cannot make one response unbounded.
MAX_CLAIMS_CEILING = 20

#: Upper bound on source metadata. Both fields are attacker-influenced (they come
#: from a fetched page), so both are bounded before they reach a prompt.
MAX_TITLE_CHARS = 300
MAX_DOMAIN_CHARS = 255

#: Estimated tokens reserved per call for the system prompt, the delimiters, and
#: the metadata block, so a window budget bounds the whole request and not only
#: the passage text inside it. A deliberate margin over the current prompts: a
#: window that overruns the real context is the failure this prevents, and
#: under-filling a window costs one extra call, which is cheap and bounded.
PROMPT_OVERHEAD_TOKENS = 600

#: Estimated tokens reserved per passage for its label and delimiters.
PASSAGE_OVERHEAD_TOKENS = 12

#: Control characters tolerated inside passage text. Whitespace is handled by the
#: normalizer; everything else in Unicode category ``Cc`` is refused, because it
#: carries no linguistic content and can be used to disguise one passage boundary
#: as another. Format characters (``Cf`` — ZWNJ, ZWJ, bidi marks) are **not** in
#: this set: they are load-bearing for Persian and must survive (AD-031, § 15.10).
_ALLOWED_TEXT_CONTROLS = frozenset("\t\n\r")


def _has_forbidden_control(value: str) -> bool:
    """Whether ``value`` contains a control character the contract refuses."""
    return any(
        unicodedata.category(ch) == "Cc" and ch not in _ALLOWED_TEXT_CONTROLS
        for ch in value
    )


def normalize_evidence_text(value: str) -> str:
    """NFC-normalize, collapse whitespace runs, and strip the ends.

    The **entire** normalization policy for evidence text, stated once so the
    contract is auditable and testable rather than implied by whatever each
    comparison happens to do:

    * **NFC** — a Persian vowel written either way agrees with the other, and a
      decomposed Latin character matches its composed form. Composition does not
      change meaning.
    * **Whitespace runs collapse to one space**, ends stripped — this removes line
      wrapping and the newlines a passage boundary introduces, so a model that
      quotes across a wrapped line still matches. It does not change which words
      are present or in what order.

    What is deliberately **not** done, because each of these changes meaning:

    * no case folding — Persian has no case, and lowercasing would rewrite an
      English term the source chose;
    * no transliteration, translation, or script conversion;
    * no punctuation, digit, or diacritic stripping — Persian punctuation,
      Arabic-Indic digits, and ZWNJ are content, not formatting;
    * no tokenization, stemming, or fuzzy matching — there is no similarity step
      anywhere in the verification path, by design (§ 13).

    The result is that a quote is compared against the **stored** passage text
    with only this normalization applied, and the string persisted as the quote is
    the normalized form, so it remains a literal substring of the normalized
    passage.
    """
    if not isinstance(value, str):
        return ""
    return " ".join(unicodedata.normalize("NFC", value).split())


class EvidenceChunk(BaseModel):
    """One persisted passage of one source, as the evidence stage sees it.

    ``chunk_id`` is the *pipeline-assigned* identity of a ``source_chunks`` row.
    It is never taken from page content: a page that says "I am passage 99" has no
    effect here, because the model may only cite an id the application supplied in
    the prompt (§ 15.6).
    """

    model_config = ConfigDict(frozen=True)

    chunk_id: int
    """The persisted ``source_chunks.id``. Positive, and unique within a source."""

    ordinal: int
    """The passage's position in the source, from ``source_chunks.ordinal``."""

    text: str
    """The stored passage text, verbatim.

    Kept exactly as persisted rather than normalized at construction: the quote
    comparison must run against what is *stored*, and normalizing here would make
    the contract invisible — a reader could not tell whether a match came from the
    text or from the normalizer. :attr:`normalized_text` is the normalized view
    that :func:`verify_claims` compares against.
    """

    @field_validator("chunk_id")
    @classmethod
    def _chunk_id_positive(cls, value: int) -> int:
        if value <= 0:
            raise EvidenceError("a passage must have a positive chunk id")
        return value

    @field_validator("ordinal")
    @classmethod
    def _ordinal_non_negative(cls, value: int) -> int:
        if value < 0:
            raise EvidenceError("a passage ordinal must be >= 0")
        return value

    @field_validator("text", mode="before")
    @classmethod
    def _text_readable(cls, value: object) -> str:
        if not isinstance(value, str):
            raise EvidenceError("passage text must be a string")
        if _has_forbidden_control(value):
            raise EvidenceError("passage text must not contain control characters")
        if not normalize_evidence_text(value):
            # Whitespace-only text has no quotable passage, so a claim citing it
            # could never verify. Rejecting it here keeps the empty-content path a
            # typed error at the boundary rather than a stream of quote rejections.
            raise EvidenceError("passage text must contain readable characters")
        return value

    @property
    def normalized_text(self) -> str:
        """The passage as :func:`normalize_evidence_text` defines it."""
        return normalize_evidence_text(self.text)

    def contains(self, quote: str) -> bool:
        """Whether ``quote`` occurs verbatim in this passage's normalized text."""
        return bool(quote) and quote in self.normalized_text


class ChunkWindow(BaseModel):
    """A budget-bounded group of consecutive passages of one source.

    The unit the model is shown. Windows exist because a long source cannot fit in
    one request (SR-5, FR-44), and because dropping evidence silently would make
    claims unverifiable — so what did not fit is **counted**, never discarded
    quietly (§ 16).

    ``dropped`` counts passages of *this source* that were not shown, whether
    because the budget ran out or because the window ceiling was reached. A caller
    that reports a coverage number must report this one.
    """

    model_config = ConfigDict(frozen=True)

    chunks: tuple[EvidenceChunk, ...]
    """Consecutive passages in ``(ordinal, chunk_id)`` order. Never empty."""

    index: int = 0
    """Position of this window in the source's window sequence, from zero."""

    window_count: int = 1
    """How many windows this source was split into."""

    dropped: int = 0
    """Passages of this source that no window showed."""

    @field_validator("chunks")
    @classmethod
    def _not_empty(cls, value: tuple[EvidenceChunk, ...]) -> tuple[EvidenceChunk, ...]:
        if not value:
            raise SummaryInputError("a window must contain at least one passage")
        return value

    @field_validator("index")
    @classmethod
    def _index_non_negative(cls, value: int) -> int:
        if value < 0:
            raise SummaryInputError("window index must be >= 0")
        return value

    @field_validator("window_count")
    @classmethod
    def _window_count_positive(cls, value: int) -> int:
        if value < 1:
            raise SummaryInputError("window_count must be at least 1")
        return value

    @field_validator("dropped")
    @classmethod
    def _dropped_non_negative(cls, value: int) -> int:
        if value < 0:
            raise SummaryInputError("dropped passage count must be >= 0")
        return value

    @property
    def truncated(self) -> bool:
        """Whether any passage of this source went unshown."""
        return self.dropped > 0

    @property
    def char_count(self) -> int:
        return sum(len(chunk.text) for chunk in self.chunks)


@runtime_checkable
class TokenCounter(Protocol):
    """The one method the window planner needs from a token estimator.

    Declared locally rather than imported from :mod:`app.domain.ports`: that
    module imports this package, so importing it back would be a cycle. The
    implementation used in production is Phase 2's
    ``CharRatioTokenEstimator``, so the *estimator* is the existing one — this is
    only the shape the planner depends on.
    """

    def count(self, text: str) -> int: ...


class SourceContent(BaseModel):
    """The untrusted evidence for one source, as one model call will see it.

    Deliberately constructed **per call** rather than once per source: the
    summarizer receives the passages that fit its budget and the claim extractor
    receives one window, so a long source is never presented whole (§ 16). The
    ``source_id`` travels with the content so a returned summary or candidate set
    cannot be attributed to another source.

    **Every field except the identifiers is untrusted.** ``title`` and ``domain``
    came from a fetched page. They are normalized and bounded here, and the prompt
    places them inside a delimited data region — they are never instructions
    (AD-032).
    """

    model_config = ConfigDict(frozen=True)

    source_id: int
    """The persisted ``sources.id``. Positive."""

    domain: str
    """The source's domain. Untrusted text, bounded and control-free.

    Whitespace inside it is collapsed rather than refused, so no line break can
    survive into the prompt; every *other* control character is refused outright.
    """

    title: str | None = None
    """The source's title. Untrusted text, or ``None`` when the page had none."""

    chunks: tuple[EvidenceChunk, ...]
    """The passages for this call, canonically ordered. At least one."""

    @field_validator("source_id")
    @classmethod
    def _source_id_positive(cls, value: int) -> int:
        if value <= 0:
            raise SummaryInputError("source id must be a positive integer")
        return value

    @field_validator("domain", mode="before")
    @classmethod
    def _domain_present(cls, value: object) -> str:
        if not isinstance(value, str):
            raise SummaryInputError("source domain must be a string")
        if _has_forbidden_control(value):
            # Not echoed: a domain is attacker-supplied and a control character in
            # a log line is how terminal and log-injection payloads travel (SR-7).
            raise SummaryInputError("source domain must not contain control characters")
        cleaned = normalize_evidence_text(value)
        if not cleaned:
            raise SummaryInputError("source domain must not be empty")
        if len(cleaned) > MAX_DOMAIN_CHARS:
            raise SummaryInputError(
                f"source domain must be at most {MAX_DOMAIN_CHARS} characters"
            )
        return cleaned

    @field_validator("title", mode="before")
    @classmethod
    def _title_optional(cls, value: object) -> str | None:
        """Normalize the title, or drop it.

        Dropped rather than refused when it is absent or empty: a page without a
        title is ordinary, and P3.4 already allows one. Refusing would make an
        unusable title take down a source that had perfectly good passages.
        """
        if value is None:
            return None
        if not isinstance(value, str):
            return None
        if _has_forbidden_control(value):
            raise SummaryInputError("source title must not contain control characters")
        cleaned = normalize_evidence_text(value)
        if not cleaned:
            return None
        if len(cleaned) > MAX_TITLE_CHARS:
            raise SummaryInputError(
                f"source title must be at most {MAX_TITLE_CHARS} characters"
            )
        return cleaned

    @field_validator("chunks", mode="before")
    @classmethod
    def _chunks_canonical(cls, value: object) -> object:
        """Build the passage list in canonical order and reject duplicates.

        Ordering happens here rather than being left to the caller so that every
        consumer — prompt, window planner, verifier, persistence — sees the same
        order for the same source, which is what makes the stage's output
        reproducible. The key is ``(ordinal, chunk_id)``: ordinal is the source's
        own order, and the id breaks ties deterministically rather than by input
        order.
        """
        if isinstance(value, (str, bytes, dict)):
            raise EvidenceError("passages must be a list of EvidenceChunk")
        if value is None:
            value = []
        if not isinstance(value, Iterable):
            raise EvidenceError("passages must be a list of EvidenceChunk")
        items = [
            item if isinstance(item, EvidenceChunk) else EvidenceChunk.model_validate(item)
            for item in value
        ]
        if not items:
            raise SourceContentEmptyError(
                "the source has no persisted passage to reason over"
            )

        seen_ids: set[int] = set()
        seen_ordinals: set[int] = set()
        for chunk in items:
            if chunk.chunk_id in seen_ids:
                raise EvidenceError(f"duplicate passage id {chunk.chunk_id}")
            if chunk.ordinal in seen_ordinals:
                raise EvidenceError(f"duplicate passage ordinal {chunk.ordinal}")
            seen_ids.add(chunk.chunk_id)
            seen_ordinals.add(chunk.ordinal)

        return sorted(items, key=lambda chunk: (chunk.ordinal, chunk.chunk_id))

    @property
    def chunk_ids(self) -> frozenset[int]:
        """The ids a claim may cite from this source. The only such authority."""
        return frozenset(chunk.chunk_id for chunk in self.chunks)

    @property
    def char_count(self) -> int:
        return sum(len(chunk.text) for chunk in self.chunks)

    @property
    def passage_count(self) -> int:
        return len(self.chunks)

    def window(self, chunks: Sequence[EvidenceChunk]) -> SourceContent:
        """The same source restricted to ``chunks``, for one bounded call."""
        return SourceContent(
            source_id=self.source_id,
            domain=self.domain,
            title=self.title,
            chunks=tuple(chunks),
        )


def plan_chunk_windows(
    chunks: Sequence[EvidenceChunk],
    *,
    max_tokens: int,
    counter: TokenCounter,
    max_windows: int = 1,
) -> tuple[ChunkWindow, ...]:
    """Pack consecutive passages into at most ``max_windows`` budget-bounded windows.

    Deterministic by construction: passages are consumed in ``(ordinal, chunk_id)``
    order — re-established here, so the result never depends on how the caller
    happened to list them — greedily, with no randomness, no clock, and no
    dictionary iteration order involved. The same input always produces the same
    windows.

    Three rules, each of which exists to avoid a specific dishonesty:

    1. **A passage is never split.** Windows hold whole passages, so a claim's
       quote can always be checked against a passage that exists in full.
    2. **A passage that alone exceeds the budget still gets a window.** Dropping
       it would remove evidence and quietly reduce coverage; showing it oversized
       is the honest failure, and the caller can see it in ``window.char_count``.
    3. **What does not fit is counted, not discarded.** Passages beyond
       ``max_windows`` are reported in the final window's ``dropped``, so a
       truncated source is visible as truncated.

    ``max_windows`` is a cost bound (each window is one provider call), which is
    why it is a parameter rather than a consequence of the token budget.
    """
    if max_tokens < 1:
        raise EvidenceError("max_tokens must be at least 1")
    if max_windows < 1:
        raise EvidenceError("max_windows must be at least 1")

    ordered = sorted(chunks, key=lambda item: (item.ordinal, item.chunk_id))
    content_budget = max(1, max_tokens - PROMPT_OVERHEAD_TOKENS)

    windows: list[list[EvidenceChunk]] = []
    current: list[EvidenceChunk] = []
    used = 0

    def _flush() -> None:
        nonlocal current, used
        if current:
            windows.append(current)
        current = []
        used = 0

    for chunk in ordered:
        cost = counter.count(chunk.text) + PASSAGE_OVERHEAD_TOKENS
        if current and used + cost > content_budget:
            if len(windows) + 1 >= max_windows:
                # This passage would need another window and there is none left, so
                # it — and everything after it — becomes counted, uncovered
                # evidence. Breaking rather than flushing is what keeps the window
                # count inside the ceiling.
                break
            _flush()
        current.append(chunk)
        used += cost

    _flush()
    if not windows:
        raise SourceContentEmptyError("the source has no persisted passage to reason over")

    packed_total = sum(len(window) for window in windows)
    dropped = len(ordered) - packed_total

    return tuple(
        ChunkWindow(
            chunks=tuple(window),
            index=index,
            window_count=len(windows),
            dropped=dropped if index == len(windows) - 1 else 0,
        )
        for index, window in enumerate(windows)
    )


class SourceSummary(BaseModel):
    """One source's summary (FR-31).

    **A summary is not evidence.** It is model prose about one source, with no
    passage behind any sentence of it, and nothing downstream may cite it. Its
    only role is to describe what the source says; the claims are what carry
    evidence (§ 15).

    ``source_id`` is filled in by the application from the content it asked about,
    never from the model's response, so a summary cannot be attributed to a source
    the caller did not supply.
    """

    model_config = ConfigDict(frozen=True)

    schema_version: str = SUMMARY_SCHEMA_VERSION
    source_id: int
    text: str
    language: str | None = None
    """BCP-47 style hint for the summary's language, when the model stated one.

    A hint for a later renderer, never a transformation: nothing translates,
    transliterates, or rewrites the text because of it.
    """

    @field_validator("source_id")
    @classmethod
    def _source_id_positive(cls, value: int) -> int:
        if value <= 0:
            raise SummaryValidationError("summary source id must be positive")
        return value

    @field_validator("text", mode="before")
    @classmethod
    def _text_present(cls, value: object) -> str:
        if not isinstance(value, str):
            raise SummaryValidationError("summary text must be a string")
        cleaned = normalize_evidence_text(value)
        if not cleaned:
            raise SummaryValidationError("summary text must not be empty")
        if len(cleaned) > MAX_SUMMARY_CHARS:
            raise SummaryValidationError(
                f"summary must be at most {MAX_SUMMARY_CHARS} characters "
                f"(got {len(cleaned)})"
            )
        return cleaned

    @field_validator("language", mode="before")
    @classmethod
    def _language_tag(cls, value: object) -> str | None:
        """Validate as a BCP-47 style tag, mirroring ``ResearchPlan.language``.

        The same rule as P3.6 on purpose: a value a later stage might forward to
        a provider should be checked here rather than failing there.
        """
        if value is None:
            return None
        if not isinstance(value, str):
            raise SummaryValidationError("summary language must be a string")
        tag = normalize_evidence_text(value).lower()
        if not tag:
            return None
        if not all(part.isalnum() for part in tag.split("-")) or len(tag) > 12:
            raise SummaryValidationError(
                "summary language must be a BCP-47 style tag, e.g. 'fa' or 'en-GB'"
            )
        return tag

    @classmethod
    def from_payload(cls, payload: object, *, source_id: int) -> SourceSummary:
        """Rebuild a summary from a validated mapping.

        ``source_id`` is supplied by the caller, not read from ``payload``: source
        identity is pipeline-assigned, so a page cannot decide which source its
        own content belongs to (§ 15.6).
        """
        if not isinstance(payload, dict):
            raise SummaryValidationError("a summary payload must be a JSON object")

        version = payload.get("schema_version")
        if version is not None and version != SUMMARY_SCHEMA_VERSION:
            raise SummaryValidationError(
                f"summary schema version {version!r} cannot be read by this build "
                f"(expected {SUMMARY_SCHEMA_VERSION!r})"
            )

        text = payload.get("summary")
        if text is None:
            raise SummarySchemaError(
                "summary response has no 'summary' field; it does not describe the source"
            )
        return cls(
            source_id=source_id,
            text=text,
            language=payload.get("language"),
            schema_version=SUMMARY_SCHEMA_VERSION,
        )

    def to_payload(self) -> dict[str, Any]:
        """The summary as a JSON-compatible mapping, in a fixed key order."""
        return {
            "schema_version": self.schema_version,
            "source_id": self.source_id,
            "summary": self.text,
            "language": self.language,
        }


class ClaimCandidate(BaseModel):
    """One claim the model proposed, before any evidence check.

    Constructed **leniently**: a missing statement becomes ``""``, a missing
    quote becomes ``""``, and a non-integer chunk reference becomes ``None``,
    rather than raising. That is deliberate and is the reason
    :class:`~app.domain.errors.ClaimStructureError` is reserved for the cases
    where the entry is not readable as a claim *at all*.

    Every candidate travels through exactly one path — :func:`verify_claims` —
    and every unusable value ends up as a recorded rejection with a reason. A
    model that returns one unusable claim out of six therefore costs one claim,
    not the whole source, and the cost is visible rather than silent.
    """

    model_config = ConfigDict(frozen=True)

    index: int
    """Position in the model's list, from zero. Preserved for reporting."""

    statement: str = ""
    source_chunk_id: int | None = None
    quote: str = ""

    @field_validator("index")
    @classmethod
    def _index_non_negative(cls, value: int) -> int:
        if value < 0:
            raise ClaimStructureError("claim index must be >= 0")
        return value

    @field_validator("statement", mode="before")
    @classmethod
    def _statement_normalized(cls, value: object) -> str:
        if value is None:
            return ""
        if not isinstance(value, str):
            # Not raised here: a non-string statement is an empty one as far as
            # verification is concerned, and it is rejected with a reason.
            return ""
        return normalize_evidence_text(value)

    @field_validator("quote", mode="before")
    @classmethod
    def _quote_normalized(cls, value: object) -> str:
        if value is None:
            return ""
        if not isinstance(value, str):
            return ""
        return normalize_evidence_text(value)

    @field_validator("source_chunk_id", mode="before")
    @classmethod
    def _chunk_id_or_none(cls, value: object) -> int | None:
        """Accept an integer id, or ``None``.

        ``bool`` is excluded explicitly: ``True`` is an ``int`` in Python and
        would silently become chunk 1, which is exactly the kind of coercion this
        stage must not perform on a value that becomes an evidence link.
        """
        if value is None or isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        return None

    @classmethod
    def from_entry(cls, entry: dict[str, Any], *, index: int) -> ClaimCandidate:
        """Read one claim entry leniently.

        Only *type* failures are the caller's problem (see
        :func:`ClaimExtraction.from_payload`); value problems become rejections.
        """
        return cls(
            index=index,
            statement=entry.get("statement") or "",
            source_chunk_id=entry.get("source_chunk_id"),
            quote=entry.get("quote") or "",
        )

    @property
    def is_usable(self) -> bool:
        """Whether every field needed to attempt verification is present."""
        return bool(self.statement and self.quote and self.source_chunk_id is not None)


class EvidenceRejection(enum.StrEnum):
    """Why one candidate claim did not become a claim.

    A closed set, because "some candidates were dropped" is not a reportable
    outcome: an orchestrator has to be able to say how many were rejected and why
    (FR-42), and a free-text reason would make that impossible to aggregate.
    """

    EMPTY_STATEMENT = "empty_statement"
    OVERLONG_STATEMENT = "overlong_statement"
    MISSING_CHUNK_REFERENCE = "missing_chunk_reference"
    EMPTY_QUOTE = "empty_quote"
    OVERLONG_QUOTE = "overlong_quote"
    SHORT_QUOTE = "short_quote"
    UNKNOWN_CHUNK = "unknown_chunk"
    QUOTE_NOT_FOUND = "quote_not_found"
    DUPLICATE_CLAIM = "duplicate_claim"


class RejectedClaim(BaseModel):
    """One candidate that failed verification, with the reason.

    Persisted nowhere and delivered nowhere; it exists so the count of
    unsupported claims is reportable rather than inferred from a shortfall.
    """

    model_config = ConfigDict(frozen=True)

    index: int
    reason: EvidenceRejection
    statement: str = ""
    source_chunk_id: int | None = None


class EvidenceReference(BaseModel):
    """A verified link from one claim to one passage of one source.

    Built **only** by :func:`verify_claims`, and every field in it is a fact the
    application established: the ids come from the passage set the application
    supplied, and the quote was found in the stored text. This is the object that
    becomes a persisted citation (§ 14), and the absence of a way to build one by
    hand is the point.
    """

    model_config = ConfigDict(frozen=True)

    source_id: int
    chunk_id: int
    ordinal: int
    """The passage's position within the source, carried so the citation can name
    a location without re-reading the passage (FR-37)."""

    quote: str
    """The verified passage, in normalized form.

    A literal substring of ``normalize_evidence_text(chunk.text)`` — which is the
    strongest statement about a quote this architecture can make, and it is
    checked by :func:`verify_claims` before the object exists.
    """

    @property
    def location(self) -> str:
        """The human-renderable locator stored on the citation."""
        return f"chunk {self.ordinal}"

    def verify_against(self, chunk: EvidenceChunk) -> bool:
        """Whether this reference still matches ``chunk``.

        The post-condition check: persistence calls it so that a
        :class:`VerifiedClaim` constructed by anything other than the verifier
        cannot reach a row.
        """
        return (
            chunk.chunk_id == self.chunk_id
            and chunk.ordinal == self.ordinal
            and chunk.contains(self.quote)
        )


class VerifiedClaim(BaseModel):
    """One claim with evidence that was checked programmatically (FR-32).

    A claim and its evidence are produced together or not at all. There is no
    intermediate "claim awaiting evidence", because that state is what
    AD-033 forbids.
    """

    model_config = ConfigDict(frozen=True)

    statement: str
    evidence: EvidenceReference

    @field_validator("statement", mode="before")
    @classmethod
    def _statement_normalized(cls, value: object) -> str:
        if not isinstance(value, str):
            raise EvidenceError("claim statement must be a string")
        cleaned = normalize_evidence_text(value)
        if not cleaned:
            raise EvidenceError("claim statement must not be empty")
        if len(cleaned) > MAX_STATEMENT_CHARS:
            raise EvidenceError(
                f"claim statement must be at most {MAX_STATEMENT_CHARS} characters"
            )
        return cleaned


class ClaimExtraction(BaseModel):
    """The candidate claims one extraction call produced (FR-32, pre-verification).

    Unverified by construction. Holding candidates rather than claims is what makes
    "no evidence → no accepted claim" a structural property instead of a
    convention: an extraction cannot be persisted, because it is not a claim.
    """

    model_config = ConfigDict(frozen=True)

    schema_version: str = CLAIM_EXTRACTION_SCHEMA_VERSION
    source_id: int
    candidates: tuple[ClaimCandidate, ...]

    @field_validator("source_id")
    @classmethod
    def _source_id_positive(cls, value: int) -> int:
        if value <= 0:
            raise EvidenceError("claim extraction source id must be positive")
        return value

    @field_validator("candidates", mode="before")
    @classmethod
    def _candidates_list(cls, value: object) -> object:
        if isinstance(value, (str, bytes, dict)):
            raise ClaimStructureError("claims must be a list of claim objects")
        return value

    @model_validator(mode="after")
    def _bounded(self) -> Self:
        if len(self.candidates) > MAX_CLAIMS_CEILING:
            raise ClaimStructureError(
                f"a claim extraction must contain at most {MAX_CLAIMS_CEILING} "
                f"claims (got {len(self.candidates)})"
            )
        return self

    @classmethod
    def from_payload(cls, payload: object, *, source_id: int) -> ClaimExtraction:
        """Read a model response into candidates.

        Three kinds of failure, and the split matters:

        * the **envelope** is wrong (not an object, no ``claims``, ``claims`` is
          not a list) → ``ClaimExtractionSchemaError``, because there is nothing to
          salvage and the response is not the requested structure;
        * an **entry** is not a JSON object, or carries a key the contract does
          not define → ``ClaimStructureError``;
        * an entry's **values** are missing, empty, or wrong in a way verification
          can judge → a candidate, which verification will reject with a reason.

        Unknown keys are refused rather than ignored. The contract is closed, and
        a key nobody reads is still content this stage would store as a statement
        or a quote somewhere else in the same response.
        """
        if not isinstance(payload, dict):
            raise ClaimExtractionSchemaError(
                "claim extraction response must be a JSON object"
            )

        version = payload.get("schema_version")
        if version is not None and version != CLAIM_EXTRACTION_SCHEMA_VERSION:
            raise ClaimExtractionSchemaError(
                f"claim schema version {version!r} cannot be read by this build "
                f"(expected {CLAIM_EXTRACTION_SCHEMA_VERSION!r})"
            )

        if "claims" not in payload:
            raise ClaimExtractionSchemaError(
                "claim extraction response has no 'claims' field; it does not "
                "describe any claim"
            )
        entries = payload["claims"]
        if not isinstance(entries, list):
            raise ClaimExtractionSchemaError("'claims' must be a list of claim objects")

        candidates: list[ClaimCandidate] = []
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                raise ClaimStructureError(
                    f"claim at index {index} must be a JSON object"
                )
            unknown = sorted(set(entry) - _CLAIM_ENTRY_KEYS)
            if unknown:
                raise ClaimStructureError(
                    f"claim at index {index} carries unsupported field(s): "
                    f"{', '.join(unknown)}"
                )
            candidates.append(ClaimCandidate.from_entry(entry, index=index))

        return cls(
            schema_version=CLAIM_EXTRACTION_SCHEMA_VERSION,
            source_id=source_id,
            candidates=tuple(candidates),
        )

    @property
    def is_empty(self) -> bool:
        """Whether the model proposed no claims.

        A legitimate outcome, not a failure: a page can contain nothing a claim
        could be drawn from. It is a *visible* emptiness — the extraction carries
        zero candidates and zero accepted claims — rather than a failure that
        would discard the source's summary.
        """
        return not self.candidates

    def to_payload(self) -> dict[str, Any]:
        """The extraction as a JSON-compatible mapping, in a fixed key order."""
        return {
            "schema_version": self.schema_version,
            "source_id": self.source_id,
            "claims": [
                {
                    "statement": candidate.statement,
                    "source_chunk_id": candidate.source_chunk_id,
                    "quote": candidate.quote,
                }
                for candidate in self.candidates
            ],
        }


class ClaimVerification(BaseModel):
    """The outcome of verifying one extraction: what was accepted, and what was not."""

    model_config = ConfigDict(frozen=True)

    source_id: int
    accepted: tuple[VerifiedClaim, ...] = ()
    rejected: tuple[RejectedClaim, ...] = ()

    @property
    def accepted_count(self) -> int:
        return len(self.accepted)

    @property
    def rejected_count(self) -> int:
        return len(self.rejected)

    @property
    def has_rejections(self) -> bool:
        return bool(self.rejected)

    def rejections_by_reason(self) -> dict[EvidenceRejection, int]:
        """Counts per rejection reason, for reporting (FR-42)."""
        counts: dict[EvidenceRejection, int] = {}
        for rejection in self.rejected:
            counts[rejection.reason] = counts.get(rejection.reason, 0) + 1
        return counts


#: The exact keys a claim entry may carry. Closed on purpose; see
#: :meth:`ClaimExtraction.from_payload`.
_CLAIM_ENTRY_KEYS = frozenset({"statement", "source_chunk_id", "quote"})


def verify_claims(
    extraction: ClaimExtraction, source: SourceContent
) -> ClaimVerification:
    """Decide which candidate claims are backed by evidence they can prove.

    The only path from a candidate to a :class:`VerifiedClaim`, and the whole
    security argument of FR-32. For each candidate, in order:

    1. **Is there a statement?** Empty or over-long → ``EMPTY_STATEMENT`` /
       ``OVERLONG_STATEMENT``.
    2. **Is there a passage reference, and is it an integer?** Absent →
       ``MISSING_CHUNK_REFERENCE``. (A non-integer is rejected in
       :meth:`ClaimExtraction.from_payload`, before it reaches here.)
    3. **Is the passage one of *this source's* passages?** Anything else →
       ``UNKNOWN_CHUNK``.

       The passage set supplied for this source is the **only** authority, and the
       database is deliberately not consulted to classify a reference. A model
       naming chunk 41 of some other source, or a chunk that does not exist, is
       refused identically: resolving the id "because it exists" is precisely the
       behaviour that lets a claim cite evidence from outside the source it claims
       to describe. The pipeline-assigned set is what makes source identity
       unforgeable (§ 15.6).
    4. **Is the quote present in that passage's stored text?** Under
       :func:`normalize_evidence_text` and nothing else → ``QUOTE_NOT_FOUND``
       otherwise. No similarity, no embedding, no model adjudication: a paraphrase
       is not evidence, and an ellipsis joining two fragments is not a passage.
    5. **Is this claim already accepted?** Same statement and same passage →
       ``DUPLICATE_CLAIM``; the first occurrence wins, so the accepted set is
       order-stable.

    Accepted claims are returned in candidate order, which is the model's order
    within the window. Over the whole source, the service concatenates windows in
    index order, so the persisted claim order is reproducible from the same input.

    No candidate can raise here. Rejection is the normal outcome for an
    adversarial or careless model, and one bad claim must not cost the verified
    claims beside it.
    """
    if extraction.source_id != source.source_id:
        # Two different sources in one call. A wiring defect, not a model outcome:
        # verification compares a candidate set against the passage set it was
        # built for, and silently verifying across the two would attribute a claim
        # to a source nobody supplied passages for.
        raise EvidenceError(
            "claim extraction and source content refer to different sources"
        )

    by_id = {chunk.chunk_id: chunk for chunk in source.chunks}
    accepted: list[VerifiedClaim] = []
    rejected: list[RejectedClaim] = []
    seen: set[tuple[str, int]] = set()

    for candidate in extraction.candidates:
        reason = _rejection_reason(candidate)
        chunk: EvidenceChunk | None = None
        if reason is None and candidate.source_chunk_id is not None:
            # A reference is only resolved against this source's passage set. The
            # database is never consulted to decide whether the id exists elsewhere.
            chunk = by_id.get(candidate.source_chunk_id)

        if reason is None and chunk is not None:
            if not chunk.contains(candidate.quote):
                reason = EvidenceRejection.QUOTE_NOT_FOUND
            else:
                key = (candidate.statement, chunk.chunk_id)
                if key in seen:
                    reason = EvidenceRejection.DUPLICATE_CLAIM
                else:
                    seen.add(key)
                    accepted.append(
                        VerifiedClaim(
                            statement=candidate.statement,
                            evidence=EvidenceReference(
                                source_id=source.source_id,
                                chunk_id=chunk.chunk_id,
                                ordinal=chunk.ordinal,
                                quote=candidate.quote,
                            ),
                        )
                    )
                    continue

        rejected.append(
            RejectedClaim(
                index=candidate.index,
                reason=reason or EvidenceRejection.UNKNOWN_CHUNK,
                statement=candidate.statement,
                source_chunk_id=candidate.source_chunk_id,
            )
        )

    return ClaimVerification(
        source_id=source.source_id,
        accepted=tuple(accepted),
        rejected=tuple(rejected),
    )


def _rejection_reason(candidate: ClaimCandidate) -> EvidenceRejection | None:
    """The first structural reason a candidate cannot be verified, or ``None``."""
    if not candidate.statement:
        return EvidenceRejection.EMPTY_STATEMENT
    if len(candidate.statement) > MAX_STATEMENT_CHARS:
        return EvidenceRejection.OVERLONG_STATEMENT
    if candidate.source_chunk_id is None:
        return EvidenceRejection.MISSING_CHUNK_REFERENCE
    if not candidate.quote:
        return EvidenceRejection.EMPTY_QUOTE
    if len(candidate.quote) > MAX_QUOTE_CHARS:
        return EvidenceRejection.OVERLONG_QUOTE
    if len(candidate.quote) < MIN_QUOTE_CHARS:
        return EvidenceRejection.SHORT_QUOTE
    return None


class SourceEvidenceResult(BaseModel):
    """What the evidence stage produced for one source.

    A result, not a verdict: ``rejected`` is part of it, so a stage that drops
    claims reports them rather than presenting a thinner answer as though nothing
    had been refused (FR-42, SR-12).
    """

    model_config = ConfigDict(frozen=True)

    source_id: int
    summary: SourceSummary
    claims: tuple[VerifiedClaim, ...] = ()
    rejected: tuple[RejectedClaim, ...] = ()
    windows: int = 1
    """Provider calls made for claim extraction (summary excluded)."""

    summary_passages: int = 1
    """Passages the summary was produced from."""

    summary_truncated: bool = False
    """Whether passages were not shown to the summarizer (FR-42 honesty)."""

    claim_passages: int = 1
    """Passages that at least one claim-extraction window showed."""

    claim_truncated: bool = False
    """Whether passages were not shown to the claim extractor.

    A source whose evidence could not all be examined is a *different* result from
    one whose evidence was fully examined and yielded nothing, and the caller has to
    be able to tell them apart (FR-42, SR-12). Reported per stage because the two
    budgets differ, so one can be complete while the other is not.
    """

    @property
    def claim_count(self) -> int:
        return len(self.claims)

    @property
    def rejected_count(self) -> int:
        return len(self.rejected)

    def rejections_by_reason(self) -> dict[EvidenceRejection, int]:
        counts: dict[EvidenceRejection, int] = {}
        for rejection in self.rejected:
            counts[rejection.reason] = counts.get(rejection.reason, 0) + 1
        return counts


__all__ = [
    "CLAIM_EXTRACTION_SCHEMA_VERSION",
    "MAX_CLAIMS_CEILING",
    "MAX_DOMAIN_CHARS",
    "MAX_QUOTE_CHARS",
    "MAX_STATEMENT_CHARS",
    "MAX_SUMMARY_CHARS",
    "MAX_TITLE_CHARS",
    "MIN_QUOTE_CHARS",
    "PASSAGE_OVERHEAD_TOKENS",
    "PROMPT_OVERHEAD_TOKENS",
    "SUMMARY_SCHEMA_VERSION",
    "ChunkWindow",
    "ClaimCandidate",
    "ClaimExtraction",
    "ClaimStructureError",
    "ClaimVerification",
    "EvidenceChunk",
    "EvidenceReference",
    "EvidenceRejection",
    "RejectedClaim",
    "SourceContent",
    "SourceContentEmptyError",
    "SourceEvidenceResult",
    "SourceSummary",
    "SummaryInputError",
    "SummarySchemaError",
    "SummaryValidationError",
    "TokenCounter",
    "VerifiedClaim",
    "normalize_evidence_text",
    "plan_chunk_windows",
    "verify_claims",
]