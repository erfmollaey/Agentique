"""Cross-source synthesis and citation construction (Phase 3, P3.8 — FR-33 … FR-39).

The stage that turns a run's verified claims into an answer, and the stage where
the central invariant of the phase lives:

```text
Fetched Source
    ↓
Extracted Content
    ↓
SourceChunk                  (persisted passage)
    ↓
VerifiedClaim                (P3.7: statement + EvidenceReference)
    ↓
Citation row + Source row    (persisted; the citation registry's authority)
    ↓
SynthesisProposal            (model output: text + citation *references*)
    ↓
Programmatic verification    ← this module
    ↓
SynthesizedAnswer            (constructible only after verification succeeds)
```

**The LLM may propose citations. It may never establish that one is valid.** The
model's contribution is an answer's prose plus a list of *identifiers*; every field
of every trusted citation — the quote, the location, the source URL, the title, the
domain, the passage ordinal — is read from persisted rows inside this module and
never from model output. The proposal schema has no ``url``, ``quote``, ``source``,
``confidence``, or ``verified`` field to put a fabricated one in, and unknown keys
are refused rather than ignored.

Three structural consequences, all tested:

* :class:`SynthesizedAnswer` **cannot be constructed directly**. Its only trusted
  constructor is :func:`verify_synthesis`; the alternative, :meth:`from_payload`,
  re-verifies a stored answer against the registry before returning it, so a
  hand-edited ``research_runs.synthesis`` row cannot be read back as trustworthy.
* A fabricated reference does not get stripped so the rest can be delivered — the
  answer is not built. FR-36's completeness is enforced at the **section**
  granularity P3.7 established, and an unresolved reference anywhere fails closed.
* URL and quote validation are not separate mechanisms from existence validation.
  They are the same mechanism read from the other side: the *only* URL and the
  *only* quote a citation can carry are the ones on its persisted row.

**Contradictions are preserved, never reconciled (FR-33, FR-34).** The model may
propose that two verified claims disagree; the application checks that both ids are
real claims of this run and keeps **both** sides with **all** their citations, plus
the conflict itself, in the answer and in the persisted payload. No model output can
remove a claim, silence a source, or select a winner — a disagreement label is a
qualification, not an authority. There is no contradiction *ontology* here: no
negation, no modal logic, no similarity threshold. Just a symmetric relation over
real claim ids, which is the strongest statement this schema can support.

**Boundaries this module holds** (§ 15.3, AD-032, AD-033):

* no I/O, no clock, no randomness, no network, no SQLAlchemy, no provider SDK, no
  HTTP client. The whole trust decision is testable with no infrastructure;
* the registry is a **projection of the existing ``citations`` table**, not a second
  citation identity system: ``citation_id`` *is* ``citations.id``;
* normalization is P3.7's, by import: :func:`app.domain.research.evidence.normalize_evidence_text`
  is used unmodified, so a quote is compared against stored passage text with the
  same NFC + whitespace-collapse semantics and no case folding, no transliteration,
  and no similarity step anywhere in the verification path;
* English-first, Persian-supported. Nothing is translated, transliterated, or
  lower-cased; ZWNJ, Arabic-Indic digits, and Persian punctuation survive the prompt,
  the comparison, the answer, and the round-trip.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable, Sequence
from typing import Any, Final, Protocol, Self, TypeVar, runtime_checkable

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from app.domain.errors import (
    CitationChainError,
    CitationCompletenessError,
    CitationFailure,
    CitationFailureKind,
    CitationIntegrityError,
    CitationQuoteError,
    CitationStructureError,
    ContradictionReferenceError,
    ContradictionStructureError,
    ForeignCitationError,
    StoredCitationChainError,
    StoredCitationQuoteError,
    SynthesisSchemaError,
    SynthesisValidationError,
    UnknownCitationError,
    UnshownCitationError,
)
from app.domain.research.evidence import (
    MIN_QUOTE_CHARS,
    normalize_evidence_text,
)
from app.domain.research.planning import MAX_OBJECTIVE_CHARS

#: Version of the *answer* structure, not of the prompt that produces it. Bump on a
#: breaking shape change so an answer persisted by an older build is recognizable
#: rather than misread — the same reasoning P3.6 applied to the plan and P3.7 to
#: the claim extraction. The prompt carries its own version
#: (``app.infrastructure.synthesis.prompts.SYNTHESIS_PROMPT_VERSION``).
SYNTHESIS_SCHEMA_VERSION = "research-synthesis-v1"

#: Structural ceilings. Every one of these is also expressible as configuration
#: (``SYNTHESIS_MAX_*``); these are the values configuration cannot raise, so a
#: misconfigured deployment cannot make one response unbounded.

MAX_SECTIONS_CEILING = 20
"""Sections in one answer. A section is the citation-completeness unit (FR-36),
so this is what bounds how many units must each carry a citation."""

MAX_CITATIONS_PER_SECTION_CEILING = 20
"""Citations one section may name. Bounds the output of the completeness check and
the size of the persisted payload, independently of the output token budget."""

MAX_EVIDENCE_CEILING = 120
"""Evidence items offered to one synthesis call."""

MAX_CONTRADICTIONS_CEILING = 20
"""Conflict pairs one response may report."""

#: Length bounds. A section is a paragraph; a heading is a label; a conflict note
#: is one sentence of qualification. All are model prose and are normalized the
#: same way P3.7 normalizes a statement.
MAX_SECTION_HEADING_CHARS = 200
MAX_SECTION_TEXT_CHARS = 2_000
MAX_CONTRADICTION_NOTE_CHARS = 400

#: Bound on the run question carried into the prompt. The question is untrusted user
#: text, so it is bounded on the way in exactly as P3.6 bounded it for planning.
MAX_QUESTION_CHARS = 2_000

#: Estimated tokens reserved per call for the system prompt, the delimiters, and the
#: evidence block header, so an input budget bounds the whole request rather than
#: only the evidence inside it. The same reasoning, and a deliberate margin over, the
#: P3.7 constant: under-filling costs one bounded call, over-filling overruns the
#: context window.
SYNTHESIS_PROMPT_OVERHEAD_TOKENS = 700

#: Estimated tokens reserved per evidence item for its label, delimiters, and the
#: citation-id list.
EVIDENCE_OVERHEAD_TOKENS = 40

#: The exact keys the response envelope may carry. Closed, like every model-facing
#: contract in Phase 3: an unrecognised key is content this stage would otherwise
#: store next to a real field.
_SYNTHESIS_KEYS = frozenset({"schema_version", "sections", "contradictions"})
_SECTION_KEYS = frozenset({"heading", "text", "citation_ids"})
_CONTRADICTION_KEYS = frozenset({"claim_ids", "note"})

#: Control characters tolerated inside model prose. Whitespace is handled by the
#: normalizer; everything else in Unicode category ``Cc`` is refused, because it can
#: be used to disguise one section boundary as another. Format characters (``Cf`` —
#: ZWNJ, ZWJ, bidi marks) are **not** in this set: they are load-bearing for Persian
#: and must survive (§ 15.10).
_ALLOWED_TEXT_CONTROLS = frozenset("\t\n\r")


@runtime_checkable
class TokenCounter(Protocol):
    """The one method the context planner needs from a token estimator.

    Declared locally for the same reason as
    :class:`app.domain.research.evidence.TokenCounter`: this module must not import
    :mod:`app.domain.ports`, which imports this package. The implementation used in
    production is Phase 2's ``CharRatioTokenEstimator``, so the *estimator* is the
    existing one — this is only the shape the planner depends on.
    """

    def count(self, text: str) -> int: ...


def _has_forbidden_control(value: str) -> bool:
    """Whether ``value`` contains a control character the contract refuses."""
    return any(
        unicodedata.category(ch) == "Cc" and ch not in _ALLOWED_TEXT_CONTROLS
        for ch in value
    )


def _normalize_prose(value: str, *, max_chars: int, what: str) -> str:
    """P3.7's normalization, applied to model prose, with a length bound.

    NFC + whitespace collapse only. No case folding (Persian has no case, and
    lowercasing would rewrite an English term the source chose), no transliteration,
    no punctuation or digit stripping (Persian punctuation, Arabic-Indic digits and
    ZWNJ are content).
    """
    if not isinstance(value, str):
        raise SynthesisValidationError(f"{what} must be a string")
    cleaned = normalize_evidence_text(value)
    if not cleaned:
        raise SynthesisValidationError(f"{what} must not be empty")
    if len(cleaned) > max_chars:
        raise SynthesisValidationError(
            f"{what} must be at most {max_chars} characters (got {len(cleaned)})"
        )
    return cleaned


def _language_tag(value: object) -> str | None:
    """Validate a BCP-47 style language tag, mirroring ``ResearchPlan.language``.

    The same rule as P3.6 and P3.7 on purpose: a value a renderer might forward
    should be checked here rather than failing there.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise SynthesisValidationError("language must be a string")
    tag = normalize_evidence_text(value).lower()
    if not tag:
        return None
    if not all(part.isalnum() for part in tag.split("-")) or len(tag) > 12:
        raise SynthesisValidationError(
            "language must be a BCP-47 style tag, e.g. 'fa' or 'en-GB'"
        )
    return tag


def _as_int(value: object) -> int | None:
    """Accept a JSON integer, or ``None``.

    ``bool`` is excluded explicitly: ``True`` is an ``int`` in Python and would
    silently become citation 1, which is exactly the kind of coercion this stage
    must not perform on a value that becomes an evidence link.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


# --- the citation registry ---------------------------------------------------


class CitationEntry(BaseModel):
    """One persisted citation of one run, resolved through its whole evidence chain.

    This is the **registry record**: what the citation is *provably* about, read
    from persisted rows and assembled by the repository in one join. It exists for
    one reason — to make "which evidence does this reference mean?" a question with
    a single, checkable answer that has nothing to do with the model.

    Every field except the identity is a fact about stored data:

    * ``quote`` is ``citations.quote``, which P3.7 verified against
      ``source_chunks.text`` at write time and which :meth:`quote_is_real` re-checks
      here, so a hand-edited row cannot pass unnoticed (SR-9);
    * ``source_url``, ``source_domain``, ``source_title`` come from the ``sources``
      row. **No URL in the final result is ever model text** (Rule 5);
    * ``passage_text`` is the stored ``source_chunks.text``, kept so containment can
      be re-checked without a second query. It is *not* rendered into the prompt —
      the quote is, and the quote is a substring of it.

    Built by :meth:`CitationRegistry.from_rows`, which is the only constructor, and
    which refuses an entry whose stored chain does not hold.
    """

    model_config = ConfigDict(frozen=True)

    citation_id: int
    """``citations.id``. The only identifier a model may reference."""

    claim_id: int
    """``citations.claim_id`` — the claim whose statement this evidence supports."""

    claim_text: str
    """``claims.text``, normalized. The proposition the citation is evidence *for*."""

    source_id: int
    """``sources.id``, as named by the citation row."""

    chunk_source_id: int
    """``source_chunks.source_id`` — the source the *passage* actually belongs to.

    Read separately from ``source_id`` on purpose. The two being equal is Rule 3's
    chain requirement, and the composite foreign key
    ``fk_citations_chunk_id_source_id_source_chunks`` (P3.7) enforces it at write time.
    Carrying both values lets the domain **re-check** it rather than assume it: an entry
    whose passage belongs to a different source than the citation names is refused
    before it can be offered to a model, which is the same defence-in-depth shape as
    the quote-containment re-check.

    It costs one column in the registry read and buys a check that does not depend on
    the database constraint remaining in place.
    """

    chunk_id: int
    ordinal: int
    """Position of the passage within its source, for the citation's locator."""

    quote: str
    """The verified passage, as stored on the citation row."""

    passage_text: str
    """``source_chunks.text``, as stored. The authority ``quote`` is checked against."""

    location: str | None = None
    """``citations.location``, the human-renderable locator. Never model text."""

    source_url: str
    """``sources.url``. The persisted URL, and the only URL a citation can carry."""

    source_domain: str
    """``sources.domain``. Untrusted text (a page chose it), bounded and normalized."""

    source_title: str | None = None
    """``sources.title``. Untrusted text, or ``None``. Never model text."""

    source_rank: int | None = None
    """``sources.rank`` — P3.5's ordering, or ``None`` for an unranked source.

    An input to the canonical ordering below, never a claim about authority: rank is
    a retrieval heuristic, and the registry does not treat it as one.
    """

    def quote_is_real(self) -> bool:
        """Whether the stored quote is a literal substring of the stored passage.

        The same test P3.7 applied at write time, re-applied at read time under the
        same normalization. It is the read-side half of a rule the database cannot
        express, and it is why ``passage_text`` is on the entry.
        """
        if not self.quote:
            return False
        # Both sides normalized, so a quote carrying the passage's own line wrapping
        # still matches. The length bound is measured on the normalized form, which is
        # the form that would be persisted.
        normalized = normalize_evidence_text(self.quote)
        if len(normalized) < MIN_QUOTE_CHARS:
            return False
        return normalized in normalize_evidence_text(self.passage_text)

    def locator(self) -> str:
        """A stable, human-renderable locator for this citation.

        Derived from the persisted ordinal, so it cannot be model text. Presentation
        belongs to a later stage; this is the *value*, not its formatting (FR-37).
        """
        return self.location or f"chunk {self.ordinal}"

    @property
    def chain_is_sound(self) -> bool:
        """Whether the passage really belongs to the source this citation names.

        Rule 3's ``citation → chunk → source`` link, checked on the values rather than
        assumed from the foreign key. ``chunk_source_id`` is read from the
        ``source_chunks`` row and ``source_id`` from the ``sources`` row, so this
        cannot be satisfied by a citation pointing at a consistent-but-wrong pair.
        """
        return self.chunk_source_id == self.source_id

    @property
    def order_key(self) -> tuple[int, int, int, int, int]:
        """The canonical position of this evidence in a synthesis context.

        Explicit and total, so the same run always yields the same context (§ 20):

        1. P3.5's ``sources.rank``, ranked sources first and ascending;
        2. ``sources.id`` — stable, and not dependent on insertion order;
        3. the passage's ``ordinal`` — the source's own reading order;
        4. ``source_chunks.id`` — ties within one passage;
        5. ``citations.id`` — a source may hold several claims on one passage.

        No database ordering, no set iteration, no hash order, no clock.
        """
        return (
            1 if self.source_rank is None else 0,
            self.source_rank or 0,
            self.source_id,
            self.ordinal,
            self.citation_id,
        )


class CitationRegistry(BaseModel):
    """Every citable citation of one research run, canonically ordered.

    **Not a second citation identity system.** ``citation_id`` is ``citations.id``;
    the registry is an in-memory projection of the table P3.1 created and P3.7
    constrained, read in one join across
    ``citations → claims → source_chunks → sources``. Nothing is duplicated, no
    table is added, and an id that is absent here is an id this run never persisted
    — which is what makes "unknown citation" constructible rather than merely
    discouraged.

    The registry is scoped to exactly one ``run_id``, and the join is scoped by
    ``claims.run_id``. That is Rule 2 of the trust chain expressed structurally: a
    citation belonging to another run is **not in this registry**, so it cannot be
    resolved here even though the id may name a real row elsewhere.
    """

    model_config = ConfigDict(frozen=True)

    run_id: int
    entries: tuple[CitationEntry, ...] = ()
    """Canonically ordered by :attr:`CitationEntry.order_key`."""

    @field_validator("run_id")
    @classmethod
    def _run_id_positive(cls, value: int) -> int:
        if value <= 0:
            raise SynthesisValidationError("citation registry run id must be positive")
        return value

    @field_validator("entries", mode="before")
    @classmethod
    def _entries_canonical(cls, value: object) -> object:
        """Validate every entry's stored chain, dedupe, and impose the canonical order.

        Done at construction rather than per lookup so that a registry holding a
        broken entry cannot exist at all: an entry whose passage is not its source's,
        or whose source is not this run's, or whose quote is not in its passage, is a
        persistence defect, and refusing to build the registry makes it loud instead
        of quietly producing an answer that cites it.
        """
        if isinstance(value, (str, bytes, dict)):
            raise SynthesisValidationError("registry entries must be a list of entries")
        if value is None:
            return []
        if not isinstance(value, Iterable):
            raise SynthesisValidationError("registry entries must be a list of entries")
        items = [
            item if isinstance(item, CitationEntry) else CitationEntry.model_validate(item)
            for item in value
        ]

        seen: dict[int, CitationEntry] = {}
        for entry in items:
            if entry.citation_id in seen:
                raise SynthesisValidationError(
                    f"duplicate citation id {entry.citation_id} in the registry"
                )
            # The quote must be inside the passage, under P3.7's normalization and
            # nothing else. The repository already refuses to return a row that fails
            # this, so it is defence in depth — and the stronger statement: a registry
            # that could hold such an entry would offer a model evidence whose quote is
            # not in it, which is the fabrication this stage exists to prevent (SR-9).
            if not entry.chain_is_sound:
                raise CitationChainError(
                    f"citation {entry.citation_id} names source {entry.source_id} but "
                    f"its passage belongs to source {entry.chunk_source_id}"
                )
            if not entry.quote_is_real():
                raise CitationQuoteError(
                    f"citation {entry.citation_id} does not carry a passage that is "
                    "present in its stored source passage"
                )
            seen[entry.citation_id] = entry

        return sorted(items, key=lambda entry: entry.order_key)

    def entry(self, citation_id: int) -> CitationEntry | None:
        """The entry for ``citation_id``, or ``None``.

        Linear over a handful of entries, for P3.6's reason: a cached index on a
        frozen model would need invalidating for no measurable gain.
        """
        for entry in self.entries:
            if entry.citation_id == citation_id:
                return entry
        return None

    @property
    def citation_ids(self) -> frozenset[int]:
        """Every citable id of this run."""
        return frozenset(entry.citation_id for entry in self.entries)

    @property
    def claim_ids(self) -> frozenset[int]:
        """Every claim with at least one citation of this run.

        The authority for a proposed contradiction: a conflict between a real claim
        and a fabricated one is not a conflict, it is a fabrication.
        """
        return frozenset(entry.claim_id for entry in self.entries)

    @property
    def source_ids(self) -> tuple[int, ...]:
        """Distinct sources cited, in canonical evidence order."""
        seen: list[int] = []
        for entry in self.entries:
            if entry.source_id not in seen:
                seen.append(entry.source_id)
        return tuple(seen)

    def __len__(self) -> int:
        return len(self.entries)


class SynthesisContext(BaseModel):
    """The bounded slice of the registry one synthesis call may be shown.

    Deterministic: the highest-ranked prefix of the canonical order, so truncation
    drops the *least* relevant evidence rather than an arbitrary set, and the same
    registry always produces the same context (§ 20, § 21).

    ``dropped`` counts evidence the call did not see. It is **counted, never
    discarded quietly**: a caller that reports coverage must report this, and a
    citation to an entry that was dropped is refused as ``UNSHOWN_CITATION`` rather
    than accepted on the strength of existing somewhere.
    """

    model_config = ConfigDict(frozen=True)

    entries: tuple[CitationEntry, ...] = ()
    total: int = 0
    dropped: int = 0

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.total < len(self.entries):
            raise SynthesisValidationError(
                "a synthesis context cannot contain more entries than the registry"
            )
        if self.dropped != self.total - len(self.entries):
            raise SynthesisValidationError(
                "dropped evidence count must be the registry size minus the shown entries"
            )
        return self

    @property
    def citation_ids(self) -> frozenset[int]:
        """The ids the model was shown. The only ids it may reference (Rule 7)."""
        return frozenset(entry.citation_id for entry in self.entries)

    @property
    def truncated(self) -> bool:
        """Whether any citable evidence went unshown (FR-42 honesty)."""
        return self.dropped > 0

    @property
    def source_count(self) -> int:
        return len({entry.source_id for entry in self.entries})


def plan_synthesis_context(
    registry: CitationRegistry,
    *,
    max_tokens: int,
    counter: TokenCounter,
    max_items: int,
) -> SynthesisContext:
    """Bound what one synthesis call may be shown, deterministically.

    ``verified claims + compact evidence + source metadata``, never whole fetched
    pages: the evidence item carries the verified claim's statement and the verified
    quote, and the passage text itself is *not* rendered — the quote is a substring
    of it, so nothing is lost that a citation could need.

    Deterministic by construction: entries are taken from the registry's canonical
    order (re-established here, so a caller that rebuilt one differently cannot
    change the outcome), greedily, with no randomness, no clock, and no set
    iteration. The estimate deliberately **over-counts** — it sums the statement,
    the quote, the title, and the domain, then adds
    :data:`EVIDENCE_OVERHEAD_TOKENS`, and the rendered form also carries labels and
    citation-id lists — because a context that overruns the window is the failure
    this prevents, while under-filling costs nothing the caller can see
    (``dropped``) and is therefore recoverable.

    Rules, each avoiding a specific dishonesty:

    1. **An evidence item is never split.** A claim and its quote travel together, so
       a quote the model read is a quote that can be checked.
    2. **An item that alone exceeds the budget is still offered.** Dropping it would
       remove evidence and quietly shrink the answer's coverage; showing it oversized
       is the honest failure and is visible in the context.
    3. **What does not fit is counted** in ``dropped``, and the count is carried by
       the service into the result.
    """
    if max_tokens < 1:
        raise SynthesisValidationError("max_tokens must be at least 1")
    if max_items < 1:
        raise SynthesisValidationError("max_items must be at least 1")

    ordered = sorted(registry.entries, key=lambda entry: entry.order_key)
    content_budget = max(1, max_tokens - SYNTHESIS_PROMPT_OVERHEAD_TOKENS)

    shown: list[CitationEntry] = []
    used = 0
    for entry in ordered:
        if len(shown) >= max_items:
            break
        cost = (
            counter.count(entry.claim_text)
            + counter.count(entry.quote)
            + counter.count(entry.source_domain)
            + counter.count(entry.source_title or "")
            + EVIDENCE_OVERHEAD_TOKENS
        )
        if shown and used + cost > content_budget:
            break
        shown.append(entry)
        used += cost

    return SynthesisContext(
        entries=tuple(shown),
        total=len(ordered),
        dropped=len(ordered) - len(shown),
    )


class SynthesisInput(BaseModel):
    """Everything one synthesis call needs, and nothing it does not.

    Built by the application from persisted rows only: the run's question, the plan's
    objective, the language hint the plan recorded, the registry of this run, and the
    bounded slice of it the call may be shown.

    ``registry`` is the **authority** (what exists in this run);
    ``context`` is the **allowance** (what this call was shown). Verification needs
    both: a reference that resolves in the registry but is outside the context is
    refused, because the model could not have read it.
    """

    model_config = ConfigDict(frozen=True)

    run_id: int
    question: str
    objective: str | None = None
    language: str | None = None
    registry: CitationRegistry
    context: SynthesisContext

    @field_validator("run_id")
    @classmethod
    def _run_id_positive(cls, value: int) -> int:
        if value <= 0:
            raise SynthesisValidationError("synthesis run id must be positive")
        return value

    @field_validator("question", mode="before")
    @classmethod
    def _question_bounded(cls, value: object) -> str:
        if not isinstance(value, str):
            raise SynthesisValidationError("research question must be a string")
        return _normalize_prose(
            value, max_chars=MAX_QUESTION_CHARS, what="research question"
        )

    @field_validator("objective", mode="before")
    @classmethod
    def _objective_bounded(cls, value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise SynthesisValidationError("research objective must be a string")
        return _normalize_prose(
            value, max_chars=MAX_OBJECTIVE_CHARS, what="research objective"
        )

    @field_validator("language", mode="before")
    @classmethod
    def _language_valid(cls, value: object) -> str | None:
        return _language_tag(value)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.registry.run_id != self.run_id:
            raise SynthesisValidationError(
                "the citation registry belongs to a different research run"
            )
        if not self.registry.entries:
            # No verified evidence ⇒ no answer. Refused here, before any model call,
            # so a run with nothing retrieved cannot spend money or invent one.
            raise SynthesisValidationError(
                "a synthesis input needs at least one persisted, verified citation"
            )
        if not self.context.entries:
            raise SynthesisValidationError(
                "a synthesis input needs at least one evidence item to show"
            )
        shown = self.context.citation_ids
        for entry in self.context.entries:
            if entry.citation_id not in self.registry.citation_ids:
                raise SynthesisValidationError(
                    f"evidence {entry.citation_id} is not in this run's registry"
                )
        if self.context.total != len(self.registry.entries):
            raise SynthesisValidationError(
                "the context must report the registry's full evidence count, so an "
                "unshown remainder is visible rather than invisible"
            )
        assert shown  # guaranteed by the non-empty check above
        return self

    @property
    def evidence_count(self) -> int:
        return len(self.context.entries)


# --- the model's proposal ----------------------------------------------------


class CitationReference(BaseModel):
    """One citation identifier the model proposed.

    **A reference, not evidence.** It is built from a model response and it carries
    one integer and nothing else — there is no field here for a quote, a URL, a
    source, or a title, which is the structural reason those can never be model text.
    The integer is not trusted either: ``int`` or ``None``, never coerced from a
    string, because guessing at a mistyped id is how a citation points at the wrong
    passage (P3.7's reasoning, applied to the same kind of value).
    """

    model_config = ConfigDict(frozen=True)

    index: int
    """Position of this reference inside its section, from zero. Preserved for
    reporting; the *answer's* order comes from the registry, not from this."""

    citation_id: int | None = None
    """The proposed ``citations.id``. ``None`` when the response did not carry an
    integer, in which case verification refuses the reference."""

    @field_validator("index")
    @classmethod
    def _index_non_negative(cls, value: int) -> int:
        if value < 0:
            raise CitationStructureError("citation reference index must be >= 0")
        return value


class ProposedSection(BaseModel):
    """One section of the answer, before any citation is checked.

    Constructed **leniently** on the values and strictly on the shape, which is
    P3.7's envelope/entry/value split: an envelope that is not the requested
    structure is a ``SynthesisSchemaError``, an entry that is not a section object
    or carries an unknown key is a ``SynthesisSchemaError``, and an entry's *values*
    (missing text, no citations, an invented id) become findings verification
    reports rather than exceptions that discard the answer beside them.

    Nothing here is trusted. ``text`` is model prose; ``citations`` is a list of
    integers to be resolved against the registry.
    """

    model_config = ConfigDict(frozen=True)

    index: int
    heading: str = ""
    text: str = ""
    citations: tuple[CitationReference, ...] = ()

    @field_validator("index")
    @classmethod
    def _index_non_negative(cls, value: int) -> int:
        if value < 0:
            raise SynthesisValidationError("section index must be >= 0")
        return value

    @property
    def proposed_ids(self) -> tuple[int | None, ...]:
        return tuple(reference.citation_id for reference in self.citations)


class ProposedContradiction(BaseModel):
    """One conflict the model reported between two verified claims (FR-33, FR-34).

    A *qualification*, never an authority. Building one says "these two claims look
    inconsistent"; it cannot remove either claim, silence either source, or select a
    winner. Verification only checks that both ids are real claims of this run, and
    then keeps **both sides with all their citations**.

    ``note`` is model prose, normalized and bounded, and it is stored as part of the
    answer — which is model prose already, like every other sentence of the answer.
    It grants no authority: nothing reads it back as evidence.
    """

    model_config = ConfigDict(frozen=True)

    index: int
    left_claim_id: int | None = None
    right_claim_id: int | None = None
    note: str = ""

    @field_validator("index")
    @classmethod
    def _index_non_negative(cls, value: int) -> int:
        if value < 0:
            raise ContradictionStructureError("contradiction index must be >= 0")
        return value

    @property
    def claim_id_pair(self) -> tuple[int | None, int | None]:
        return (self.left_claim_id, self.right_claim_id)


class SynthesisProposal(BaseModel):
    """What the model proposed: answer prose plus citation *identifiers*.

    Untrusted by construction. Holding references rather than citations is what makes
    "a citation exists only if the application resolved it" a structural property
    instead of a convention: a proposal cannot be persisted, because it is not an
    answer.

    Parsing follows P3.7's three-way split:

    * **envelope** wrong (not an object, no ``sections``, ``sections`` not a list) →
      :class:`SynthesisSchemaError`;
    * **entry** not an object, or carrying a key the contract does not define →
      :class:`SynthesisSchemaError` / :class:`CitationStructureError` for the
      ``citation_ids`` value's type;
    * **values** unusable (empty text, an invented id, no citation at all) → a
      finding :func:`verify_synthesis` reports, or a
      :class:`SynthesisValidationError` for a property of the proposal as a whole.
    """

    model_config = ConfigDict(frozen=True)

    schema_version: str = SYNTHESIS_SCHEMA_VERSION
    sections: tuple[ProposedSection, ...] = ()
    contradictions: tuple[ProposedContradiction, ...] = ()

    @field_validator("sections", mode="before")
    @classmethod
    def _sections_is_a_list(cls, value: object) -> object:
        if isinstance(value, (str, bytes, dict)):
            raise SynthesisSchemaError("sections must be a list of section objects")
        return value

    @field_validator("contradictions", mode="before")
    @classmethod
    def _contradictions_is_a_list(cls, value: object) -> object:
        if value is None:
            return []
        if isinstance(value, (str, bytes, dict)):
            raise SynthesisSchemaError(
                "contradictions must be a list of contradiction objects"
            )
        return value

    @model_validator(mode="after")
    def _bounded(self) -> Self:
        if len(self.sections) > MAX_SECTIONS_CEILING:
            raise SynthesisValidationError(
                f"a synthesis proposal must contain at most {MAX_SECTIONS_CEILING} "
                f"sections (got {len(self.sections)})"
            )
        if len(self.contradictions) > MAX_CONTRADICTIONS_CEILING:
            raise SynthesisValidationError(
                f"a synthesis proposal must report at most "
                f"{MAX_CONTRADICTIONS_CEILING} contradictions "
                f"(got {len(self.contradictions)})"
            )
        return self

    @classmethod
    def from_payload(cls, payload: object) -> SynthesisProposal:
        """Read a model response into a proposal of untrusted sections and references.

        ``source_id``-style cross-checking is absent by construction: the proposal
        names citations and claim ids only, and the ``source_id`` of an evidence
        chain is read from the registry row, never from the response. A response that
        *does* carry a ``source_id``, ``url``, ``quote``, or ``verified`` key is
        refused, which is the structural reason a model cannot self-certify a
        citation.
        """
        if not isinstance(payload, dict):
            raise SynthesisSchemaError("synthesis response must be a JSON object")

        version = payload.get("schema_version")
        if version is not None and version != SYNTHESIS_SCHEMA_VERSION:
            raise SynthesisSchemaError(
                f"synthesis schema version {version!r} cannot be read by this build "
                f"(expected {SYNTHESIS_SCHEMA_VERSION!r})"
            )

        unknown = sorted(set(payload) - _SYNTHESIS_KEYS)
        if unknown:
            raise SynthesisSchemaError(
                f"synthesis response carries unsupported field(s): {', '.join(unknown)}"
            )

        if "sections" not in payload:
            raise SynthesisSchemaError(
                "synthesis response has no 'sections' field; it states nothing"
            )
        raw_sections = payload["sections"]
        if not isinstance(raw_sections, list):
            raise SynthesisSchemaError("'sections' must be a list of section objects")

        sections: list[ProposedSection] = []
        for index, entry in enumerate(raw_sections):
            if not isinstance(entry, dict):
                raise SynthesisSchemaError(f"section at index {index} must be an object")
            unknown_entry = sorted(set(entry) - _SECTION_KEYS)
            if unknown_entry:
                raise SynthesisSchemaError(
                    f"section at index {index} carries unsupported field(s): "
                    f"{', '.join(unknown_entry)}"
                )
            sections.append(_read_section(entry, index=index))

        contradictions: list[ProposedContradiction] = []
        raw_contradictions = payload.get("contradictions") or []
        for index, entry in enumerate(raw_contradictions):
            if not isinstance(entry, dict):
                raise ContradictionStructureError(
                    f"contradiction at index {index} must be an object"
                )
            unknown_entry = sorted(set(entry) - _CONTRADICTION_KEYS)
            if unknown_entry:
                raise ContradictionStructureError(
                    f"contradiction at index {index} carries unsupported field(s): "
                    f"{', '.join(unknown_entry)}"
                )
            left, right = _read_claim_pair(entry.get("claim_ids"))
            note = entry.get("note")
            contradictions.append(
                ProposedContradiction(
                    index=index,
                    left_claim_id=left,
                    right_claim_id=right,
                    note=note if isinstance(note, str) else "",
                )
            )

        return cls(
            schema_version=SYNTHESIS_SCHEMA_VERSION,
            sections=tuple(sections),
            contradictions=tuple(contradictions),
        )

    @property
    def is_empty(self) -> bool:
        """Whether the model stated nothing at all.

        Not a successful synthesis: there is no answer to validate and no citation
        to check, so :func:`verify_synthesis` refuses it with
        :class:`SynthesisValidationError` rather than returning an empty answer that
        looks like a finding.
        """
        return not self.sections

    def to_payload(self) -> dict[str, Any]:
        """The proposal as a JSON-compatible mapping, in a fixed key order.

        Never persisted: the stored result is the *verified* answer, and keeping the
        proposal out of the payload means there is no path by which an unvalidated
        citation could be read back as research output.
        """
        return {
            "schema_version": self.schema_version,
            "sections": [
                {
                    "heading": section.heading,
                    "text": section.text,
                    "citation_ids": [
                        reference.citation_id for reference in section.citations
                    ],
                }
                for section in self.sections
            ],
            "contradictions": [
                {
                    "claim_ids": [proposal.left_claim_id, proposal.right_claim_id],
                    "note": proposal.note,
                }
                for proposal in self.contradictions
            ],
        }


def _read_section(entry: dict[str, Any], *, index: int) -> ProposedSection:
    """Read one section entry: shape strictly, values leniently."""
    heading = entry.get("heading")
    text = entry.get("text")
    if heading is not None and not isinstance(heading, str):
        raise SynthesisSchemaError(f"section {index}: 'heading' must be a string")
    if text is not None and not isinstance(text, str):
        raise SynthesisSchemaError(f"section {index}: 'text' must be a string")

    raw_ids = entry.get("citation_ids")
    references: list[CitationReference] = []
    if raw_ids is None:
        raw_ids = []
    if not isinstance(raw_ids, list):
        raise CitationStructureError(
            f"section {index}: 'citation_ids' must be a list of citation numbers"
        )
    for position, raw in enumerate(raw_ids):
        # A non-integer id is a *shape* failure, not a value to verify: the model did
        # not return the requested structure, and guessing at an id is the one thing
        # that must never happen to a value that becomes an evidence link.
        if not isinstance(raw, int) or isinstance(raw, bool):
            raise CitationStructureError(
                f"section {index}: citation id at position {position} must be a JSON "
                f"number, not {type(raw).__name__}"
            )
        references.append(CitationReference(index=position, citation_id=raw))

    return ProposedSection(
        index=index,
        heading=normalize_evidence_text(heading) if isinstance(heading, str) else "",
        text=normalize_evidence_text(text) if isinstance(text, str) else "",
        citations=tuple(references),
    )


def _read_claim_pair(raw: object) -> tuple[int | None, int | None]:
    """Read a ``[left, right]`` claim-id pair strictly.

    A pair of exactly two JSON numbers or nothing: an id is not coerced from a
    string, and a three-element list is not "the first two" — both are how a
    conflict report turns into something the caller did not write.
    """
    if raw is None:
        # An entry with no pair is a shape failure. Reading it as ``(None, None)`` and
        # refusing it later would report a *citation* problem for a malformed report,
        # which is a different thing from an unresolvable reference.
        raise ContradictionStructureError(
            "'claim_ids' is required on a reported contradiction"
        )
    if not isinstance(raw, list) or len(raw) != 2:
        raise ContradictionStructureError(
            "'claim_ids' must be a list of exactly two claim numbers"
        )
    for item in raw:
        if not isinstance(item, int) or isinstance(item, bool):
            raise ContradictionStructureError(
                f"claim id {item!r} must be a JSON number, not {type(item).__name__}"
            )
    return (int(raw[0]), int(raw[1]))


# --- the verified answer -----------------------------------------------------

#: Private token proving a value came from :func:`verify_synthesis`. A plain
#: ``object()`` and module-private: nothing in a JSON payload, a hand-built mapping,
#: or a model response can reproduce this identity, so the guard cannot be forged by
#: supplying a field — the check is against the object, not a key's presence.
_VERIFICATION_TOKEN: Final = object()

_VERIFIED_KEY: Final = "_verified_by"


class _Verified(BaseModel):
    """Base for the trusted types, which cannot be built outside verification.

    ``model_validator(mode="before")`` runs for ``__init__`` **and** for
    ``model_validate``, so closing the constructor here closes both. Pydantic's
    ``model_construct`` bypasses validation entirely by design; that is recorded as
    a known limitation rather than pretended away.
    """

    model_config = ConfigDict(frozen=True)

    @model_validator(mode="before")
    @classmethod
    def _only_through_verification(cls, data: Any) -> Any:
        if isinstance(data, dict) and data.get(_VERIFIED_KEY) is _VERIFICATION_TOKEN:
            return {key: value for key, value in data.items() if key != _VERIFIED_KEY}
        raise CitationChainError(
            f"{cls.__name__} may only be constructed by verify_synthesis(); a "
            "citation is established programmatically, never by a caller"
        )


def _verified(cls: type[_T], **fields: Any) -> _T:
    """Build one trusted type. Module-private, and reachable only from verification."""
    return cls.model_validate({_VERIFIED_KEY: _VERIFICATION_TOKEN, **fields})


_T = TypeVar("_T", bound="_Verified")


class VerifiedCitation(_Verified):
    """One citation of the final answer, every field read from persisted rows.

    The trusted counterpart of :class:`CitationReference`. It is constructed **only**
    by :func:`verify_synthesis` (or by :meth:`SynthesizedAnswer.from_payload`, after
    re-checking the stored answer against the registry), and the absence of any other
    way to build one is the point: a caller cannot assert a citation.

    ``source_url``, ``quote``, ``location``, ``source_title``, ``source_domain``, and
    ``ordinal`` are copied from :class:`CitationEntry`, which the repository built
    from persisted rows. Rules 5 and 6 are satisfied here by construction rather than
    by a check: there is no code path by which model text reaches these fields,
    because the proposal schema has nowhere to put it.
    """

    citation_id: int
    claim_id: int
    source_id: int
    chunk_id: int
    ordinal: int
    quote: str
    passage_text: str = ""
    location: str | None = None
    source_url: str
    source_domain: str
    source_title: str | None = None
    claim_text: str = ""

    @property
    def locator(self) -> str:
        """The persisted, human-renderable location (FR-37)."""
        return self.location or f"chunk {self.ordinal}"

    @classmethod
    def _from_entry(cls, entry: CitationEntry) -> VerifiedCitation:
        return _verified(
            cls,
            citation_id=entry.citation_id,
            claim_id=entry.claim_id,
            source_id=entry.source_id,
            chunk_id=entry.chunk_id,
            ordinal=entry.ordinal,
            quote=entry.quote,
            passage_text=entry.passage_text,
            location=entry.location,
            source_url=entry.source_url,
            source_domain=entry.source_domain,
            source_title=entry.source_title,
            claim_text=entry.claim_text,
        )

    def to_payload(self) -> dict[str, Any]:
        """The citation as a JSON-compatible mapping, in a fixed key order.

        Every value is persisted evidence, so the mapping is safe to store and safe
        to re-read — :meth:`SynthesizedAnswer.from_payload` re-verifies each entry
        against the registry rather than trusting these fields.
        """
        return {
            "citation_id": self.citation_id,
            "claim_id": self.claim_id,
            "source_id": self.source_id,
            "chunk_id": self.chunk_id,
            "ordinal": self.ordinal,
            "quote": self.quote,
            "location": self.locator,
            "source_url": self.source_url,
            "source_domain": self.source_domain,
            "source_title": self.source_title,
        }


class VerifiedSection(_Verified):
    """One section of the final answer with the citations that were resolved for it.

    A section exists only with at least one resolved citation (FR-36), and its
    ``citations`` are in the **registry's canonical order**, not the order the model
    listed them. The model's listing order therefore cannot influence the stored
    result, and duplicate references collapse deterministically on first occurrence.
    """

    index: int
    heading: str
    text: str
    citations: tuple[VerifiedCitation, ...]

    @model_validator(mode="after")
    def _carries_evidence(self) -> Self:
        if not self.citations:
            raise CitationCompletenessError(
                f"section {self.index} carries no citation that resolves"
            )
        return self

    @property
    def citation_ids(self) -> tuple[int, ...]:
        return tuple(citation.citation_id for citation in self.citations)

    @property
    def source_ids(self) -> tuple[int, ...]:
        seen: list[int] = []
        for citation in self.citations:
            if citation.source_id not in seen:
                seen.append(citation.source_id)
        return tuple(seen)

    def to_payload(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "heading": self.heading,
            "text": self.text,
            "citations": [citation.to_payload() for citation in self.citations],
        }


class VerifiedContradiction(_Verified):
    """One reported disagreement, with **both** sides kept (FR-33, FR-34).

    ``citations`` holds the citations of *both* claims, so the reader of the answer
    can see exactly which sources disagree — "Source A says X, Source B says Y" is
    representable, and neither side can be erased by anything in this structure.

    Symmetry is deliberate. ``claims.status`` has a ``contradicted`` value, and it is
    *not* used to mark a losing side: that would be silently reconciling the conflict
    (FR-34). A claim in conflict stays ``supported`` — which is true, its quote is in
    its passage — and the conflict is reported here, where both sides appear.
    """

    left_claim_id: int
    right_claim_id: int
    note: str = ""
    citations: tuple[VerifiedCitation, ...]

    @model_validator(mode="after")
    def _distinct_sides(self) -> Self:
        if self.left_claim_id == self.right_claim_id:
            raise ContradictionReferenceError(
                "a claim cannot contradict itself"
            )
        return self

    @property
    def citation_ids(self) -> tuple[int, ...]:
        return tuple(citation.citation_id for citation in self.citations)

    def to_payload(self) -> dict[str, Any]:
        return {
            "left_claim_id": self.left_claim_id,
            "right_claim_id": self.right_claim_id,
            "note": self.note,
            "citations": [citation.to_payload() for citation in self.citations],
        }


class SynthesizedAnswer(_Verified):
    """The final, evidence-backed research answer (FR-35 … FR-39).

    **The only trusted research output of P3.8**, and constructible only by
    :func:`verify_synthesis`. That is enforced, not documented: the class inherits
    :class:`_Verified`, whose ``mode="before"`` validator refuses any construction
    that does not carry this module's private token — for ``SynthesizedAnswer(...)``
    and for ``SynthesizedAnswer.model_validate(...)`` alike. A hand-built object
    cannot reach a row.

    What it holds is deliberately structured rather than rendered (§ 15): sections
    with structured citations, and conflicts with both sides' citations. There is no
    Markdown, no ``[1]`` numbering, and no Telegram formatting here — presentation is
    a later stage's responsibility, and a rendered string cannot be un-rendered for
    verification. :attr:`plain_text` exists for the ``research_runs.final_answer``
    column and is ordinary prose with no citation markers in it.

    Every citation in here was resolved against this run's registry, and every quote
    was re-checked against its stored passage. Nothing in this object was written by
    a model except the prose of the sections and the conflict notes.
    """

    run_id: int
    question: str
    objective: str | None = None
    language: str | None = None
    sections: tuple[VerifiedSection, ...] = ()
    contradictions: tuple[VerifiedContradiction, ...] = ()
    evidence_offered: int = 0
    """Evidence items the synthesis call was shown, for coverage reporting (FR-42)."""

    evidence_available: int = 0
    """Citable evidence this run held. ``offered < available`` means the answer was
    synthesized from a truncated context, and a caller that reports coverage must
    report that."""

    @model_validator(mode="after")
    def _non_empty(self) -> Self:
        if not self.sections:
            raise SynthesisValidationError(
                "a synthesized answer must contain at least one cited section"
            )
        return self

    @property
    def citation_count(self) -> int:
        return len(self.cited_citation_ids)

    @property
    def cited_citation_ids(self) -> tuple[int, ...]:
        """Every distinct citation the answer relies on, in first-appearance order."""
        seen: list[int] = []
        for section in self.sections:
            for citation in section.citations:
                if citation.citation_id not in seen:
                    seen.append(citation.citation_id)
        return tuple(seen)

    @property
    def cited_claim_ids(self) -> tuple[int, ...]:
        """Every distinct claim the answer relies on, in first-appearance order."""
        seen: list[int] = []
        for section in self.sections:
            for citation in section.citations:
                if citation.claim_id not in seen:
                    seen.append(citation.claim_id)
        return tuple(seen)

    @property
    def cited_source_ids(self) -> tuple[int, ...]:
        """Every distinct source the answer relies on, in first-appearance order."""
        seen: list[int] = []
        for section in self.sections:
            for citation in section.citations:
                if citation.source_id not in seen:
                    seen.append(citation.source_id)
        return tuple(seen)

    @property
    def source_count(self) -> int:
        """How many distinct sources contributed evidence (FR-33 cross-source)."""
        return len(self.cited_source_ids)

    @property
    def has_contradictions(self) -> bool:
        return bool(self.contradictions)

    @property
    def evidence_truncated(self) -> bool:
        """Whether the answer was synthesized from a truncated evidence context."""
        return self.evidence_offered < self.evidence_available

    def citations(self) -> tuple[VerifiedCitation, ...]:
        """Every distinct citation of the answer, in first-appearance order."""
        by_id: dict[int, VerifiedCitation] = {}
        for section in self.sections:
            for citation in section.citations:
                by_id.setdefault(citation.citation_id, citation)
        for conflict in self.contradictions:
            for citation in conflict.citations:
                by_id.setdefault(citation.citation_id, citation)
        return tuple(by_id[citation_id] for citation_id in by_id)

    @property
    def plain_text(self) -> str:
        """The answer as ordinary prose, for the ``final_answer`` column.

        Headings and section text, separated by a blank line, with **no citation
        markers**. The markers are the structured ``citations`` on each section; a
        rendered ``[1]`` here would make the stored text disagree with the verified
        structure and would make FR-40 (length-bounded rendering) a property of this
        module, which it is not.
        """
        blocks: list[str] = []
        for section in self.sections:
            # A heading sits directly above its own text; sections are separated by a
            # blank line. Both are ordinary prose, so the stored ``final_answer``
            # reads as an answer rather than as a serialized structure.
            blocks.append("\n".join(part for part in (section.heading, section.text) if part))
        return "\n\n".join(blocks)

    def to_payload(self) -> dict[str, Any]:
        """The answer as a JSON-compatible mapping, in a fixed key order.

        This is what ``research_runs.synthesis`` stores, and it is **only** reachable
        from a verified answer: there is no payload method on the proposal, so the raw
        model response has no route into persistence (§ 16).
        """
        return {
            "schema_version": SYNTHESIS_SCHEMA_VERSION,
            "run_id": self.run_id,
            "question": self.question,
            "objective": self.objective,
            "language": self.language,
            "evidence_offered": self.evidence_offered,
            "evidence_available": self.evidence_available,
            "sections": [section.to_payload() for section in self.sections],
            "contradictions": [
                conflict.to_payload() for conflict in self.contradictions
            ],
        }

    @classmethod
    def from_payload(
        cls, payload: dict[str, Any], *, registry: CitationRegistry
    ) -> SynthesizedAnswer:
        """Rebuild an answer from what the database stored, re-verified.

        Re-validation rather than trust, following ``ResearchPlan.from_payload`` and
        ``SourceSummary.from_payload``: a stored answer is read back only if every one
        of its citations still resolves to persisted evidence of ``registry.run_id``,
        still names the same claim, source, and passage, and still carries a quote
        present in that passage. A hand-edited row, a row written by an older build,
        or a truncated write fails here with a typed error instead of surfacing as a
        trustworthy answer later.

        The model-supplied prose is re-bounded but not re-judged — it was accepted
        once by :func:`verify_synthesis` under the same bounds, and re-running a
        language judgement on stored text would only add a way to reject an honest
        answer.
        """
        if not isinstance(payload, dict):
            raise SynthesisValidationError("a stored synthesis must be a JSON object")

        version = payload.get("schema_version")
        if version is not None and version != SYNTHESIS_SCHEMA_VERSION:
            raise SynthesisValidationError(
                f"stored synthesis schema version {version!r} cannot be read by this "
                f"build (expected {SYNTHESIS_SCHEMA_VERSION!r})"
            )

        run_id = payload.get("run_id")
        if not isinstance(run_id, int) or isinstance(run_id, bool) or run_id <= 0:
            raise SynthesisValidationError("stored synthesis has no valid run id")
        if registry.run_id != run_id:
            raise SynthesisValidationError(
                "the citation registry does not belong to the stored synthesis's run"
            )

        raw_sections = payload.get("sections")
        if not isinstance(raw_sections, list) or not raw_sections:
            raise SynthesisValidationError(
                "a stored synthesis must contain at least one section"
            )
        if len(raw_sections) > MAX_SECTIONS_CEILING:
            raise SynthesisValidationError(
                f"a stored synthesis must contain at most {MAX_SECTIONS_CEILING} sections"
            )

        sections: list[VerifiedSection] = []
        for position, entry in enumerate(raw_sections):
            if not isinstance(entry, dict):
                raise SynthesisValidationError(
                    f"stored section at index {position} must be an object"
                )
            raw_citations = entry.get("citations")
            if not isinstance(raw_citations, list) or not raw_citations:
                raise CitationCompletenessError(
                    f"stored section at index {position} carries no citation"
                )
            if len(raw_citations) > MAX_CITATIONS_PER_SECTION_CEILING:
                raise SynthesisValidationError(
                    f"stored section at index {position} carries more than "
                    f"{MAX_CITATIONS_PER_SECTION_CEILING} citations"
                )
            citations: list[VerifiedCitation] = []
            for raw in raw_citations:
                if not isinstance(raw, dict):
                    raise SynthesisValidationError(
                        f"stored citation at section {position} must be an object"
                    )
                citations.append(_citation_from_payload(raw, registry=registry))
            # Deduplicated in place, first occurrence winning, exactly as the
            # in-memory verifier does — so a stored answer read back has the same
            # shape it had when it was verified.
            ordered: list[VerifiedCitation] = []
            seen: set[int] = set()
            for citation in citations:
                if citation.citation_id in seen:
                    continue
                seen.add(citation.citation_id)
                ordered.append(citation)
            sections.append(
                _verified(
                    VerifiedSection,
                    index=position,
                    # A heading is optional, so an absent one reads as "" rather than
                    # as a corrupt row — mirroring the proposal, where a section need
                    # not have a heading at all.
                    heading=_stored_prose(
                        entry.get("heading"),
                        MAX_SECTION_HEADING_CHARS,
                        "heading",
                        allow_empty=True,
                    ),
                    text=_stored_prose(
                        entry.get("text"), MAX_SECTION_TEXT_CHARS, "section text"
                    ),
                    citations=tuple(ordered),
                )
            )

        conflicts: list[VerifiedContradiction] = []
        raw_conflicts = payload.get("contradictions")
        if raw_conflicts is None:
            raw_conflicts = []
        if not isinstance(raw_conflicts, list):
            raise SynthesisValidationError("stored contradictions must be a list")
        if len(raw_conflicts) > MAX_CONTRADICTIONS_CEILING:
            raise SynthesisValidationError(
                f"a stored synthesis must report at most {MAX_CONTRADICTIONS_CEILING} "
                "contradictions"
            )
        for position, entry in enumerate(raw_conflicts):
            if not isinstance(entry, dict):
                raise SynthesisValidationError(
                    f"stored contradiction at index {position} must be an object"
                )
            left = entry.get("left_claim_id")
            right = entry.get("right_claim_id")
            for value in (left, right):
                if not isinstance(value, int) or isinstance(value, bool):
                    raise ContradictionReferenceError(
                        f"stored contradiction at index {position} names an invalid "
                        "claim id"
                    )
            raw_citations = entry.get("citations") or []
            if not isinstance(raw_citations, list):
                raise SynthesisValidationError(
                    f"stored contradiction at index {position} carries invalid citations"
                )
            conflicts.append(
                _verified(
                    VerifiedContradiction,
                    left_claim_id=left,
                    right_claim_id=right,
                    note=_stored_prose(
                        entry.get("note"),
                        MAX_CONTRADICTION_NOTE_CHARS,
                        "contradiction note",
                        allow_empty=True,
                    ),
                    citations=tuple(
                        _citation_from_payload(raw, registry=registry)
                        for raw in raw_citations
                    ),
                )
            )

        question = _stored_prose(payload.get("question"), MAX_QUESTION_CHARS, "question")
        objective_raw = payload.get("objective")
        objective = (
            _stored_prose(objective_raw, MAX_OBJECTIVE_CHARS, "objective")
            if objective_raw is not None
            else None
        )
        offered = payload.get("evidence_offered")
        available = payload.get("evidence_available")
        return _verified(
            cls,
            run_id=run_id,
            question=question,
            objective=objective,
            language=_language_tag(payload.get("language")),
            sections=tuple(sections),
            contradictions=tuple(conflicts),
            evidence_offered=offered if isinstance(offered, int) else 0,
            evidence_available=available if isinstance(available, int) else 0,
        )


def _stored_prose(value: object, max_chars: int, what: str, *, allow_empty: bool = False) -> str:
    """Re-bound stored prose, mirroring the proposal's normalization."""
    if value is None:
        if allow_empty:
            return ""
        raise SynthesisValidationError(f"stored synthesis is missing its {what}")
    if not isinstance(value, str):
        raise SynthesisValidationError(f"stored synthesis {what} must be a string")
    cleaned = normalize_evidence_text(value)
    if not cleaned and not allow_empty:
        raise SynthesisValidationError(f"stored synthesis {what} must not be empty")
    if len(cleaned) > max_chars:
        raise SynthesisValidationError(
            f"stored synthesis {what} must be at most {max_chars} characters"
        )
    return cleaned


def _citation_from_payload(payload: dict[str, Any], *, registry: CitationRegistry) -> VerifiedCitation:
    """Re-verify one stored citation against the registry, field by field.

    Existence, ownership, the evidence chain, and quote containment are all decided
    here, from persisted data — the same decisions
    :func:`verify_synthesis` makes, against the same authority. Every identifier and
    every content field in the payload is compared with the registry's entry, so a
    row edited to point at a different passage, a different source, or a different
    claim is refused even though each id on its own is real.
    """
    citation_id = payload.get("citation_id")
    if not isinstance(citation_id, int) or isinstance(citation_id, bool):
        raise SynthesisValidationError("stored citation has no valid citation id")

    entry = registry.entry(citation_id)
    if entry is None:
        raise UnknownCitationError(
            f"stored citation {citation_id} is not a citation of this research run"
        )

    for field, expected in (
        ("claim_id", entry.claim_id),
        ("source_id", entry.source_id),
        ("chunk_id", entry.chunk_id),
        ("ordinal", entry.ordinal),
    ):
        stored = payload.get(field)
        if stored is not None and stored != expected:
            raise StoredCitationChainError(
                f"stored citation {citation_id} names a different {field} than the "
                "persisted evidence"
            )

    stored_quote = payload.get("quote")
    if stored_quote is not None and normalize_evidence_text(stored_quote) != entry.quote:
        raise StoredCitationQuoteError(
            f"stored citation {citation_id} carries a quote that is not the verified "
            "passage"
        )

    stored_url = payload.get("source_url")
    if stored_url is not None and stored_url != entry.source_url:
        raise StoredCitationQuoteError(
            f"stored citation {citation_id} carries a URL that is not the persisted "
            "source URL"
        )

    # Re-stated here so this function is honest on its own terms rather than relying
    # on the registry's constructor having done it.
    if not entry.chain_is_sound:
        raise CitationChainError(
            f"citation {citation_id} names source {entry.source_id} but its passage "
            f"belongs to source {entry.chunk_source_id}"
        )
    if not entry.quote_is_real():
        raise CitationQuoteError(
            f"citation {citation_id} does not carry a passage present in its stored "
            "source passage"
        )

    return VerifiedCitation._from_entry(entry)


# --- verification ------------------------------------------------------------

#: Which typed error each integrity failure raises. Kept as an explicit table so the
#: mapping is one readable object rather than a chain of ``if`` statements, and so a
#: test can assert the whole mapping at once.
_ERROR_FOR_KIND: Final[dict[CitationFailureKind, type[CitationIntegrityError]]] = {
    CitationFailureKind.UNKNOWN_CITATION: UnknownCitationError,
    CitationFailureKind.UNSHOWN_CITATION: UnshownCitationError,
    CitationFailureKind.FOREIGN_CITATION: ForeignCitationError,
    CitationFailureKind.BROKEN_EVIDENCE_CHAIN: CitationChainError,
    CitationFailureKind.QUOTE_NOT_IN_PASSAGE: CitationQuoteError,
    CitationFailureKind.UNCITED_SECTION: CitationCompletenessError,
}


def verify_synthesis(
    proposal: SynthesisProposal, synthesis_input: SynthesisInput
) -> SynthesizedAnswer:
    """Turn a proposal into a trusted answer, or refuse it.

    **The only path from a :class:`SynthesisProposal` to a
    :class:`SynthesizedAnswer`**, and the whole security argument of P3.8. For each
    proposed reference, in proposal order:

    1. **Is it an identifier at all?** Parsing already refused a non-integer id
       (``CitationStructureError``), so this is always an integer here.
    2. **Does it exist in this run's registry?** → ``UNKNOWN_CITATION`` otherwise. The
       registry is scoped by ``claims.run_id``, so a citation of another run is not in
       it: ownership is verified through the persisted relationship, never by trusting
       the id.
    3. **Was it in the bounded context the model was shown?** → ``UNSHOWN_CITATION``
       otherwise. A real id the call never received cannot have been read from the
       prompt, so it is as ungrounded as an invented one.
    4. **Does the stored chain still hold?** → ``BROKEN_EVIDENCE_CHAIN`` otherwise:
       the passage must be this citation's source's, and the quote must be inside that
       passage (``QUOTE_NOT_IN_PASSAGE``). The registry proved this at construction;
       it is re-stated here so the guarantee belongs to *this* function.
    5. **Is the section left with any citation at all?** → ``UNCITED_SECTION``
       otherwise (FR-36).

    And for each proposed contradiction: both claim ids must be claims of this run's
    registry and must differ, else ``UNKNOWN_CLAIM`` / ``SELF_CONTRADICTION``. The
    conflict is then recorded with **both** sides' citations.

    **Every failure is collected and the whole answer is refused.** The typed error
    carries the full ordered list of findings, so a caller can report counts by
    category, but no partial answer is returned: a fabricated citation is an attack
    signal, and delivering the rest of a proposal that contained one is exactly the
    "apparently valid answer" § 15.7 and SR-9 forbid. Nothing is stripped.

    **Deterministic order** (§ 20): sections in proposal order, references in the
    order the model listed them, contradictions after the sections. The *answer's*
    citation order is the registry's canonical order, so the model's listing order
    cannot influence what is stored. Duplicate references collapse on first
    occurrence and are counted, not treated as an error — repeating a real citation is
    harmless, and failing closed on it would let a model veto its own answer.
    """
    if proposal.is_empty:
        raise SynthesisValidationError(
            "the model proposed no sections; there is no answer to validate"
        )

    registry = synthesis_input.registry
    shown = synthesis_input.context.citation_ids

    failures: list[CitationFailure] = []
    sections: list[VerifiedSection] = []

    for section in proposal.sections:
        text = _require_section_text(section)
        heading = _require_section_heading(section)
        citations, section_failures = _resolve_section(section, registry, shown)
        failures.extend(section_failures)
        if citations:
            sections.append(
                _verified(
                    VerifiedSection,
                    index=section.index,
                    heading=heading,
                    text=text,
                    citations=citations,
                )
            )

    conflicts, conflict_failures = _resolve_contradictions(
        proposal.contradictions, registry
    )
    failures.extend(conflict_failures)

    if failures:
        _raise_for(failures, "citation references did not resolve to evidence of this run")

    if not sections:
        # Unreachable while completeness is enforced above, and asserted anyway so a
        # future edit to the check cannot silently produce an empty trusted answer.
        raise SynthesisValidationError(
            "no section of the proposal carried a citation that resolves"
        )

    return _verified(
        SynthesizedAnswer,
        run_id=synthesis_input.run_id,
        question=synthesis_input.question,
        objective=synthesis_input.objective,
        language=synthesis_input.language,
        sections=tuple(sections),
        contradictions=tuple(conflicts),
        evidence_offered=len(synthesis_input.context.entries),
        evidence_available=synthesis_input.context.total,
    )


def _require_section_text(section: ProposedSection) -> str:
    """A section's prose, bounded. A proposal-level property, so it raises directly."""
    if not section.text:
        raise SynthesisValidationError(
            f"section {section.index} states no text"
        )
    return _normalize_prose(
        section.text, max_chars=MAX_SECTION_TEXT_CHARS, what="section text"
    )


def _require_section_heading(section: ProposedSection) -> str:
    """A section's heading, bounded and optional — a section need not have one."""
    if not section.heading:
        return ""
    return _normalize_prose(
        section.heading, max_chars=MAX_SECTION_HEADING_CHARS, what="section heading"
    )


def _resolve_section(
    section: ProposedSection,
    registry: CitationRegistry,
    shown: frozenset[int],
) -> tuple[tuple[VerifiedCitation, ...], tuple[CitationFailure, ...]]:
    """Resolve one section's references, returning its citations and its findings."""
    if len(section.citations) > MAX_CITATIONS_PER_SECTION_CEILING:
        raise SynthesisValidationError(
            f"section {section.index} names more than "
            f"{MAX_CITATIONS_PER_SECTION_CEILING} citations"
        )

    failures: list[CitationFailure] = []
    resolved: list[VerifiedCitation] = []
    seen: set[int] = set()

    for reference in section.citations:
        citation_id = reference.citation_id
        if citation_id is None:  # pragma: no cover - parsing refuses this first
            failures.append(
                CitationFailure(
                    kind=CitationFailureKind.UNKNOWN_CITATION,
                    detail="citation reference carries no identifier",
                    section_index=section.index,
                )
            )
            continue

        entry = registry.entry(citation_id)
        if entry is None:
            # Absent from a registry that is scoped to this run: either invented, or
            # belonging to another run. Both are refused identically — resolving it
            # "because it exists elsewhere" is precisely the behaviour that lets an
            # answer cite evidence from outside the run that retrieved it.
            failures.append(
                CitationFailure(
                    kind=CitationFailureKind.UNKNOWN_CITATION,
                    detail="no citation of this run has that identifier",
                    section_index=section.index,
                    reference=citation_id,
                )
            )
            continue

        if citation_id not in shown:
            failures.append(
                CitationFailure(
                    kind=CitationFailureKind.UNSHOWN_CITATION,
                    detail="the citation was outside the evidence shown for this call",
                    section_index=section.index,
                    reference=citation_id,
                )
            )
            continue

        if not entry.chain_is_sound:
            failures.append(
                CitationFailure(
                    kind=CitationFailureKind.BROKEN_EVIDENCE_CHAIN,
                    detail="the citation's passage belongs to a different source than "
                    "the citation names",
                    section_index=section.index,
                    reference=citation_id,
                )
            )
            continue

        if not entry.quote_is_real():
            failures.append(
                CitationFailure(
                    kind=CitationFailureKind.QUOTE_NOT_IN_PASSAGE,
                    detail="the stored quote is not present in the stored passage",
                    section_index=section.index,
                    reference=citation_id,
                )
            )
            continue

        if citation_id in seen:
            # Deterministic duplicate handling: first occurrence wins, the rest are
            # dropped from the section, and the section keeps its citation count.
            continue
        seen.add(citation_id)
        resolved.append(VerifiedCitation._from_entry(entry))

    resolved.sort(key=lambda citation: registry.entry(citation.citation_id).order_key)  # type: ignore[union-attr]

    if not resolved:
        failures.append(
            CitationFailure(
                kind=CitationFailureKind.UNCITED_SECTION,
                detail="the section carries no citation that resolves",
                section_index=section.index,
            )
        )

    return tuple(resolved), tuple(failures)


def _resolve_contradictions(
    proposed: Sequence[ProposedContradiction],
    registry: CitationRegistry,
) -> tuple[tuple[VerifiedContradiction, ...], tuple[CitationFailure, ...]]:
    """Resolve proposed conflicts, keeping **both** sides with all their citations.

    Nothing is discarded here, ever. A conflict that cannot be checked is refused as
    a finding, and the claims involved remain fully citable through the registry: the
    check governs whether the answer may *report* a disagreement, never which
    evidence the answer may use (FR-33, FR-34).
    """
    failures: list[CitationFailure] = []
    resolved: list[VerifiedContradiction] = []
    claim_ids = registry.claim_ids
    seen_pairs: set[frozenset[int]] = set()

    for proposal in proposed:
        left, right = proposal.claim_id_pair
        if left is None or right is None:
            failures.append(
                CitationFailure(
                    kind=CitationFailureKind.UNKNOWN_CLAIM,
                    detail="the reported conflict names no claim on one side",
                    section_index=proposal.index,
                )
            )
            continue
        if left == right:
            failures.append(
                CitationFailure(
                    kind=CitationFailureKind.SELF_CONTRADICTION,
                    detail="the reported conflict names one claim on both sides",
                    section_index=proposal.index,
                    reference=left,
                )
            )
            continue
        missing = [claim_id for claim_id in (left, right) if claim_id not in claim_ids]
        if missing:
            failures.append(
                CitationFailure(
                    kind=CitationFailureKind.UNKNOWN_CLAIM,
                    detail="the reported conflict names a claim with no citation in "
                    "this run",
                    section_index=proposal.index,
                    reference=missing[0],
                )
            )
            continue

        pair = frozenset((left, right))
        if pair in seen_pairs:
            # The same conflict reported twice is one conflict. Deterministic: the
            # first occurrence wins, keeping the answer's stored form independent of
            # how many times the model repeated itself.
            continue
        seen_pairs.add(pair)

        citations = tuple(
            VerifiedCitation._from_entry(entry)
            for entry in registry.entries
            if entry.claim_id in (left, right)
        )
        note = _normalize_prose(
            proposal.note or "these sources disagree",
            max_chars=MAX_CONTRADICTION_NOTE_CHARS,
            what="contradiction note",
        )
        resolved.append(
            _verified(
                VerifiedContradiction,
                left_claim_id=min(left, right),
                right_claim_id=max(left, right),
                note=note,
                citations=citations,
            )
        )

    resolved.sort(key=lambda conflict: (conflict.left_claim_id, conflict.right_claim_id))
    return tuple(resolved), tuple(failures)


def _raise_for(failures: Sequence[CitationFailure], summary: str) -> None:
    """Raise the typed error for the first finding, carrying every finding.

    The *type* comes from the first failure in deterministic order, so a caller can
    ``except`` the specific category; the *list* is complete, so a caller that reports
    counts can. Both are needed and neither alone would do.
    """
    first = failures[0]
    ordered = tuple(failures)
    # A reported contradiction is a different class of finding from a bad citation
    # reference, and has its own base type, so a caller can catch "the answer's
    # citations did not resolve" separately from "the reported conflict did not".
    if first.kind in (CitationFailureKind.UNKNOWN_CLAIM, CitationFailureKind.SELF_CONTRADICTION):
        raise ContradictionReferenceError(
            f"a reported contradiction did not resolve: {first.detail}", failures=ordered
        )
    error_cls = _ERROR_FOR_KIND[first.kind]
    raise error_cls(f"{summary}: {first.detail}", failures=ordered)


__all__ = [
    "EVIDENCE_OVERHEAD_TOKENS",
    "MAX_CITATIONS_PER_SECTION_CEILING",
    "MAX_CONTRADICTIONS_CEILING",
    "MAX_EVIDENCE_CEILING",
    "MAX_QUESTION_CHARS",
    "MAX_SECTIONS_CEILING",
    "MAX_SECTION_HEADING_CHARS",
    "MAX_SECTION_TEXT_CHARS",
    "SYNTHESIS_PROMPT_OVERHEAD_TOKENS",
    "SYNTHESIS_SCHEMA_VERSION",
    "CitationEntry",
    "CitationFailure",
    "CitationFailureKind",
    "CitationReference",
    "CitationRegistry",
    "ProposedContradiction",
    "ProposedSection",
    "SynthesisContext",
    "SynthesisInput",
    "SynthesisProposal",
    "SynthesizedAnswer",
    "TokenCounter",
    "VerifiedCitation",
    "VerifiedContradiction",
    "VerifiedSection",
    "normalize_evidence_text",
    "plan_synthesis_context",
    "verify_synthesis",
]