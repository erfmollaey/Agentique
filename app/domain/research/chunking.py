"""Passage/chunk production: normalized text → ordered, bounded passages.

Responsibility 14 of the frozen pipeline (§ 15.2) and FR-22. This module owns
the *decision* half of turning ``ExtractedContent`` (P3.4) into the
``SourceChunk`` rows P3.7 consumes: where the boundaries are, how long a passage
may be, and what ``ordinal`` / ``char_start`` / ``char_end`` mean. The write
half is :class:`app.services.research.chunking.SourceChunkService`, which is the
only caller that persists.

Pure by construction: no clock, no randomness, no network, no LLM, no database,
no ORM import. The same text and the same bound always produce the same
passages, which is what makes re-chunking a source idempotent rather than
merely repeatable.

**``ExtractedContent`` ≠ ``SourceChunk`` ≠ ``Source``.** P3.4's module states
the boundary and this one closes it: the extracted text is one normalized
string, a passage is a positioned slice of it, and a source is the persisted row
that owns both.

The three guarantees, each stated because each is testable:

1. **Bounded.** No passage exceeds ``MAX_CHUNK_CHARS`` and none is shorter than
   ``MIN_CHUNK_CHARS`` unless it cannot be merged with a neighbour. Both bounds
   are *reused* from :mod:`app.domain.research.evidence` rather than invented
   here — see their definitions for why the quote bounds are the right ones.
2. **Coverage-preserving.** The passages partition the normalized text: they are
   ordered and non-overlapping, the only characters that fall between two of
   them are whitespace, and the last one ends at the end of the text. Every
   non-whitespace character therefore appears in exactly one passage, in the same
   order, and nothing is mutated. Checked by :func:`covers_text`.
3. **Stable for verification.** A passage's stored text is *already* in the form
   :func:`~app.domain.research.evidence.normalize_evidence_text` produces, which
   is the only normalization P3.7 applies before checking that a quote is a
   literal substring. So a quote read out of a passage matches the passage as
   stored, with no second normalization step that could disagree.

**No overlap.** Overlapping passages would put the same text in two rows, which
means one quote can verify against two different ``chunk_id`` values — two
different evidence identities for one passage, which is the ambiguity AD-033
exists to remove. Sequence splitting is not used either: it needs a model, and
a boundary that depends on a model is not reproducible.

**Paragraph structure is honoured only when the input still carries it.**
``ExtractedContent.from_fetched`` whitespace-collapses the body it is given, so
a paragraph break and a line wrap arrive here identically and the sentence rule
below is what actually splits real pages. A caller that supplies text with
blank lines still gets paragraph boundaries respected, because splitting them
first costs nothing and is strictly closer to the source's own structure.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from app.domain.errors import ChunkLimitError
from app.domain.research.evidence import (
    MAX_QUOTE_CHARS,
    MIN_QUOTE_CHARS,
    normalize_evidence_text,
)

#: Upper bound on one passage, taken directly from P3.7's bound on one quoted
#: passage (``MAX_QUOTE_CHARS``). Reusing it is not a convenience: a quote the
#: model draws from a passage can then never exceed the bound
#: :func:`~app.domain.research.evidence.verify_claims` already enforces, so a
#: passage is by construction quotable in full rather than a fragment that has
#: to be cut. It is also a sane evidence unit — roughly 1,200 characters — for a
#: window planner that packs whole passages into a bounded token budget.
MAX_CHUNK_CHARS = MAX_QUOTE_CHARS

#: Lower bound on one passage, also reused: P3.7's ``MIN_QUOTE_CHARS``, the
#: shortest passage a claim may quote. A chunk shorter than it could never
#: support a verified claim, so persisting one would create an evidence unit
#: that cannot be used. Merging it into its neighbour costs nothing and removes
#: the possibility.
MIN_CHUNK_CHARS = MIN_QUOTE_CHARS

#: Structural ceiling on passages per source (SR-5: bounded chunk counts). The
#: ordinary case is far below it — ``MAX_EXTRACTED_CHARS / MAX_CHUNK_CHARS`` is
#: 84 — so this ceiling only bites on pathological input, and it exists so that
#: a bound on *rows* is a property of the schema and the contract rather than of
#: how a page happens to be punctuated.
MAX_CHUNK_CEILING = 1_000

#: Characters that end a sentence, and therefore a preferred boundary.
#:
#: ``.`` ``!`` ``?`` cover Latin script; ``؟`` (U+061F) and ``۔`` (U+06D4) are
#: the Arabic-script question mark and full stop, and ``。`` (U+3002) the
#: ideographic one. Nothing else is listed on purpose: a separator that is not a
#: terminator (``؛`` Arabic semicolon, ``,``) is not a boundary, and treating it
#: as one would split sentences mid-clause for no gain.
_SENTENCE_TERMINATORS = frozenset(".!?؟۔。")

#: A blank line: two newlines with only horizontal whitespace between them.
#: ``\s`` rather than an explicit class so tabs, form feeds, and CRLF variants
#: all count, and ``\n\s*\n`` so three blank lines are one separator.
_PARAGRAPH_SEPARATOR = re.compile(r"\n\s*\n")


class PassageSpan(BaseModel):
    """One planned passage: its ordinal and its position in the normalized text.

    A *plan*, not a row. ``ordinal`` is assigned here so that the persisted
    order is a property of the text rather than of insertion order, which is
    what makes a re-run converge on the same ``source_chunks`` rows instead of
    colliding with the previous ones.

    ``char_start`` / ``char_end`` are offsets into the **normalized** extracted
    text (``normalize_evidence_text(content.text)``), with
    ``normalized[char_start:char_end] == text``. In production the two are the
    same string, because P3.4's ``from_fetched`` already applies exactly this
    normalization; measuring against the normalized form means the invariant
    holds for any input, including one built directly.
    """

    model_config = ConfigDict(frozen=True)

    ordinal: int
    char_start: int
    char_end: int
    text: str

    @field_validator("ordinal", "char_start")
    @classmethod
    def _non_negative(cls, value: int) -> int:
        if value < 0:
            raise ValueError("passage ordinal and offsets must be >= 0")
        return value

    @field_validator("text")
    @classmethod
    def _text_present(cls, value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("a passage must carry text")
        if len(value) > MAX_CHUNK_CHARS:
            raise ValueError(f"a passage must be at most {MAX_CHUNK_CHARS} characters")
        return value

    @model_validator(mode="after")
    def _offsets_bound_exactly_the_text(self) -> PassageSpan:
        """``normalized[char_start:char_end] == text`` is an invariant.

        Checked here rather than in a field validator because a field validator
        cannot see the text — ``text`` is declared after the offsets, so
        ``info.data`` does not hold it yet. Getting it wrong would let a passage
        claim a position in the source that is not the position of its own text,
        which is exactly the offset drift that breaks FR-37's locator.
        """
        if self.char_end < self.char_start:
            raise ValueError("char_end must not precede char_start")
        if self.char_end - self.char_start != len(self.text):
            raise ValueError("a passage's offsets must bound exactly its text")
        return self


def normalized_source_text(text: str) -> str:
    """The extracted text as the chunker measures it.

    Exactly :func:`~app.domain.research.evidence.normalize_evidence_text` — NFC,
    whitespace runs collapsed to one space, ends stripped. Named separately
    because it is also the string ``char_start`` / ``char_end`` index into, and a
    caller comparing offsets needs to know which string that is.
    """
    return normalize_evidence_text(text)


def chunk_source_text(
    text: str, *, max_chars: int = MAX_CHUNK_CHARS
) -> tuple[PassageSpan, ...]:
    """Plan the passages of one source's extracted text (FR-22).

    Deterministic, bounded, and coverage-preserving; the three properties are
    stated on the module and asserted by the tests. The algorithm, in order:

    1. **Normalize** with the one rule P3.7 also applies, so a passage is
       stored in the exact form a quote is compared against.
    2. **Split on blank lines** into paragraphs when the input has them.
    3. **Split each paragraph into sentences** at a terminator followed by a
       space or by the end of the paragraph. A terminator inside a word
       (``e.g.``) is not a boundary, so a word is never cut at punctuation.
    4. **Pack whole sentences greedily** up to ``max_chars``, so a passage holds
       as many complete sentences as fit and no sentence is ever split when it
       fits in a passage of its own.
    5. **Split an oversized sentence** at word boundaries, and only if a single
       word is itself longer than ``max_chars``, split that word at exactly
       ``max_chars``. Unbroken text stays bounded; it is never silently dropped.
    6. **Merge any passage below** ``MIN_CHUNK_CHARS`` into its neighbour when
       the result still fits.

    Raises :class:`~app.domain.errors.ChunkLimitError` when the result would
    exceed ``MAX_CHUNK_CEILING`` — before anything is written, so the refusal
    leaves whatever passages exist untouched.

    Empty or whitespace-only text yields ``()``: there is no passage to produce,
    and a passage with no readable text could never support a verified claim
    (P3.7 refuses it at the boundary). The caller decides what an empty
    extraction means; this function never manufactures evidence for it.
    """
    normalized = normalized_source_text(text)
    if not normalized:
        return ()

    units = _sentence_units(text, normalized)
    packed = _pack_units(units, normalized, max_chars)
    packed = _merge_short(packed, max_chars)

    if len(packed) > MAX_CHUNK_CEILING:
        raise ChunkLimitError(
            f"extracted text of {len(normalized)} normalized characters would "
            f"produce {len(packed)} passages, above the {MAX_CHUNK_CEILING} ceiling"
        )

    return tuple(
        PassageSpan(
            ordinal=ordinal,
            char_start=start,
            char_end=end,
            text=normalized[start:end],
        )
        for ordinal, (start, end) in enumerate(packed)
    )


def covers_text(passages: Sequence[PassageSpan], text: str) -> bool:
    """Whether ``passages`` cover ``text`` exactly: no gap, no overlap, no loss.

    The coverage contract as an executable check, so a test does not have to
    restate it. Coverage means the passages are ordered and non-overlapping,
    every character that falls *between* two of them is whitespace, and the last
    one ends at the end of the text. Together with
    ``normalized[char_start:char_end] == text``, that makes every non-whitespace
    character of the normalized text appear in exactly one passage, in the same
    order — which is the strongest statement this stage can make about not
    losing source text.
    """
    normalized = normalized_source_text(text)
    position = 0
    for passage in passages:
        if normalized[passage.char_start : passage.char_end] != passage.text:
            return False
        if passage.char_start < position:
            return False  # overlap, or out of order
        if normalized[position : passage.char_start].strip():
            return False  # a non-whitespace gap: text was dropped
        position = passage.char_end
    return position == len(normalized)


# --- internals ---------------------------------------------------------------


def _paragraphs(text: str) -> tuple[str, ...]:
    """The non-empty paragraphs of ``text``, each normalized.

    Returned in document order and normalized individually, so joining them
    with a single space reproduces ``normalize_evidence_text(text)`` — the
    property ``_sentence_units`` relies on to locate a paragraph in normalized
    coordinates.
    """
    parts = _PARAGRAPH_SEPARATOR.split(text or "")
    return tuple(
        paragraph for paragraph in map(normalize_evidence_text, parts) if paragraph
    )


def _sentence_units(original: str, normalized: str) -> tuple[_Unit, ...]:
    """Sentence units in normalized coordinates, never crossing a paragraph.

    The units the packer consumes. Each carries ``(start, end)`` into
    ``normalized`` plus whether it opens a paragraph. Consecutive units are
    separated by exactly one space (the paragraph join or the sentence join —
    both are a single space after normalization), and the union of the units plus
    those spaces is exactly ``normalized``.
    """
    units: list[_Unit] = []
    offset = 0
    for paragraph in _paragraphs(original):
        if offset:
            offset += 1  # the single space joining two paragraphs
        for index, (start, end) in enumerate(_sentence_spans(paragraph)):
            units.append(
                _Unit(
                    start=offset + start,
                    end=offset + end,
                    paragraph_start=index == 0,
                )
            )
        offset += len(paragraph)
    return tuple(units)


def _sentence_spans(paragraph: str) -> tuple[tuple[int, int], ...]:
    """Split one normalized paragraph into sentences.

    A boundary is a run of terminators followed by a space or by the end of the
    paragraph. Scanning past the run means ``?!`` and ``...`` end one sentence
    rather than three, and requiring a following space means a terminator inside
    a word — ``e.g.``, ``3.5`` — is not a boundary.
    """
    spans: list[tuple[int, int]] = []
    start = 0
    index = 0
    length = len(paragraph)
    while index < length:
        if paragraph[index] not in _SENTENCE_TERMINATORS:
            index += 1
            continue
        end = index
        while end + 1 < length and paragraph[end + 1] in _SENTENCE_TERMINATORS:
            end += 1
        if end + 1 < length and paragraph[end + 1] != " ":
            index += 1
            continue
        sentence_end = end + 1
        spans.append((start, sentence_end))
        start = sentence_end + 1 if sentence_end < length else sentence_end
        index = sentence_end
    if start < length:
        spans.append((start, length))
    return tuple(spans)


@dataclass(frozen=True, slots=True)
class _Unit:
    """One sentence of one paragraph, in normalized coordinates."""

    start: int
    end: int
    paragraph_start: bool


def _pack_units(
    units: Sequence[_Unit], normalized: str, max_chars: int
) -> list[tuple[int, int]]:
    """Pack whole sentence-units into spans of at most ``max_chars``.

    A paragraph boundary is a hard one, so a passage never spans two paragraphs
    when the input still has them; the size bound and the sentence rule decide
    everything inside a paragraph.
    """
    spans: list[tuple[int, int]] = []
    current_start = -1
    current_end = -1

    for unit in units:
        length = unit.end - unit.start
        if length > max_chars:
            # One sentence larger than a whole passage: it is emitted alone,
            # split at word boundaries, so it never drags a neighbour with it.
            if current_start >= 0:
                spans.append((current_start, current_end))
                current_start = -1
            spans.extend(_split_span(normalized, unit.start, unit.end, max_chars))
            continue
        if current_start >= 0 and (
            unit.paragraph_start
            or (current_end - current_start) + 1 + length > max_chars
        ):
            spans.append((current_start, current_end))
            current_start = -1
        if current_start < 0:
            current_start, current_end = unit.start, unit.end
        else:
            current_end = unit.end

    if current_start >= 0:
        spans.append((current_start, current_end))
    return spans


def _split_span(
    text: str, start: int, end: int, max_chars: int
) -> list[tuple[int, int]]:
    """Split one oversized span at word boundaries, hard-splitting only a word.

    The last space inside the window is preferred, so words stay whole. When the
    window contains no space the span is unbroken text and is cut at exactly
    ``max_chars`` — the one case where a word is split, and it is unavoidable if
    the bound is to hold at all.
    """
    pieces: list[tuple[int, int]] = []
    position = start
    while position < end:
        limit = min(position + max_chars, end)
        if limit >= end:
            pieces.append((position, end))
            break
        cut = text.rfind(" ", position, limit)
        if cut <= position:
            # No word boundary inside the window: unbroken text, cut exactly at
            # the bound. ``position`` advances to ``limit`` rather than past it,
            # because there is no separator character to skip.
            pieces.append((position, limit))
            position = limit
        else:
            pieces.append((position, cut))
            position = cut + 1  # skip the space itself
    return pieces


def _merge_short(
    spans: Sequence[tuple[int, int]], max_chars: int
) -> list[tuple[int, int]]:
    """Merge a passage below ``MIN_CHUNK_CHARS`` into its predecessor when it fits.

    Reached from two places: a hard split of unbroken text, whose final piece can
    be arbitrarily short, and a *paragraph* shorter than the minimum quotable
    passage — which is why this runs after the paragraph break rather than before
    it. The invariant "no passage is too short to quote" is the stronger of the
    two: a passage no claim may cite is a row that exists and can never be used,
    while merging two adjacent paragraphs costs nothing and loses no character.

    Merging keeps every character: the merged span covers its predecessor's start
    through this unit's end, and :func:`covers_text` still holds. A piece that
    cannot be merged because the neighbour is already full stays as it is, because
    it is still real, bounded, covered text.
    """
    merged: list[tuple[int, int]] = []
    for start, end in spans:
        if merged and end - start < MIN_CHUNK_CHARS:
            previous_start, _previous_end = merged[-1]
            if end - previous_start <= max_chars:
                merged[-1] = (previous_start, end)
                continue
        merged.append((start, end))
    return merged
