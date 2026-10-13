"""Citation rendering for a verified research answer (FR-40, FR-37).

A verified :class:`~app.domain.research.synthesis.SynthesizedAnswer` in, ready-to-send
Telegram messages out. Pure functions: no I/O, no clock, no database, no provider call,
so the presentation boundary is testable in isolation.

**Why this module exists.** P3.8 deliberately stored a *structured* answer and refused
to render markers into it — ``SynthesizedAnswer.plain_text`` is ordinary prose with no
markers, because a rendered ``[1]`` in the stored text would make the stored value
disagree with the verified structure and would make FR-40 a property of the domain
module. Presentation was explicitly left to "a later stage". That stage is this one,
and until it existed the product's defining promise — an answer with traceable
citations — was not delivered to the user.

**What this module is not allowed to do.** It does not verify, re-resolve, or repair
anything. It renders only what ``verify_synthesis`` already accepted: the input is a
``SynthesizedAnswer``, which is constructible *only* through the verifier's
module-private token guard (``_Verified``), and every field it renders
(``source_url``, ``source_domain``, ``source_title``, ``location``, ``ordinal``) was
copied from persisted rows by :class:`~app.domain.research.synthesis.VerifiedCitation`.
There is no code path from model text to anything printed here. A citation that failed
to resolve never became part of an answer, so there is nothing here to strip (AD-041's
deliberate narrowing of § 15.7 is upstream of this module, not a concern of it).

**Marker placement is section-level, and deliberately so.** A ``[n]`` marker is
appended to the **end of the section it belongs to**. That is the granularity at which
support was actually verified: ``VerifiedSection`` binds its citations to a section,
and FR-36 is enforced at section granularity because nothing in the contract can
decide that an individual *sentence* follows from a passage (§ 19.3). Placing a marker
mid-paragraph would assert a sentence-level correspondence that no check established.

**Numbering is per source, not per citation.** Two citations from different passages
of one document are one source, and a numbered list that printed the same URL twice
would read as two independent references. Markers therefore index a *source*, and
several markers may name the same source; the list carries each URL exactly once.

**Escaping.** The bot's default parse mode is HTML (``Settings.TELEGRAM_PARSE_MODE``
defaults to ``"HTML"``), so every dynamic value rendered here is passed through
:func:`app.services.formatting.escape`. Retrieved page titles are untrusted web content
(SR-3); a title containing ``<`` must not become markup, and a URL containing ``&``
must not break message parsing.

**Length.** Telegram rejects messages above 4096 characters, so a rendered answer is
split into chunks that are each already within the limit
(:func:`app.services.formatting.telegram_chunk_limit`). The body and the reference
list are packed as **one** document, so a short answer and its sources arrive in a
single message, and a long one simply continues into the next. Chunks are packed on
line boundaries, so a citation marker, a source label, and its URL are never torn
apart by the splitter: nothing is silently dropped, no marker is ever orphaned from
the list that explains it, and no entry is duplicated by the split.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from app.domain.errors import CitationIntegrityError
from app.services.formatting import escape

if TYPE_CHECKING:  # pragma: no cover
    from app.domain.research.synthesis import SynthesizedAnswer

#: Header above the numbered reference list. Plain, so it reads the same in every
#: locale the answer itself is written in.
_SOURCES_HEADING = "Sources:"

#: Longest source title rendered before it is shortened. A page title is attacker- and
#: publisher-controlled; it is a label beside the URL, not the reference itself, so it
#: is bounded rather than allowed to dominate the message.
_MAX_TITLE_CHARS = 120


class RenderedSource(BaseModel):
    """One entry of the numbered reference list.

    Every field is derived from a persisted row by ``verify_synthesis``; nothing here
    is model-authored and nothing here is invented by the renderer.
    """

    model_config = ConfigDict(frozen=True)

    number: int
    title: str
    url: str
    domain: str
    locator: str


class RenderedAnswer(BaseModel):
    """The answer as sendable messages.

    ``body`` and ``sources`` are exposed separately so a test — and a future non-Telegram
    surface — can assert the mapping without parsing text. ``messages`` is the delivery
    contract: the ordered chunks to send, body first and the reference list after, each
    already within the platform's limit.
    """

    model_config = ConfigDict(frozen=True)

    body: str
    sources: tuple[RenderedSource, ...] = ()
    messages: tuple[str, ...] = Field(default_factory=tuple)

    @property
    def cited(self) -> bool:
        """Whether the rendered answer carries at least one reference."""
        return bool(self.sources)


def render_answer(
    answer: SynthesizedAnswer,
    *,
    limit: int,
) -> RenderedAnswer:
    """Render a verified answer into chunked, citation-marked Telegram messages.

    ``limit`` is the per-message character budget and is expected to come from
    :func:`app.services.formatting.telegram_chunk_limit` so the renderer and
    ``DeliveryService`` agree; a chunk the renderer produced at the limit is not
    re-split on the way out.

    Raises :class:`CitationIntegrityError` if the answer carries a citation with no
    usable URL. That cannot happen for an answer produced by ``verify_synthesis``
    (``source_url`` is a non-empty field copied from a persisted row), so the check is
    a boundary assertion rather than a repair: the renderer refuses to print a
    reference it cannot make honest instead of emitting a dangling entry or dropping
    content silently.
    """
    if limit <= 0:
        raise ValueError("limit must be positive")

    numbering, sources = _number_sources(answer)
    if sources:
        _assert_sources_renderable(sources)

    body = _render_body(answer, numbering)
    if not sources:
        # No verified reference exists, so none is implied. A ``SynthesizedAnswer``
        # cannot be constructed without one, but the renderer does not depend on
        # that: it renders what it was given and invents nothing.
        return RenderedAnswer(body=body, sources=(), messages=tuple(_pack(body, limit)))

    document = f"{body}\n\n{_render_sources(sources)}" if body else _render_sources(sources)
    return RenderedAnswer(
        body=body, sources=sources, messages=tuple(_pack(document, limit))
    )


# --- numbering ---------------------------------------------------------------


def _number_sources(
    answer: SynthesizedAnswer,
) -> tuple[dict[int, int], tuple[RenderedSource, ...]]:
    """Assign each distinct source a number, in first-appearance order.

    ``SynthesizedAnswer.citations()`` already walks sections then contradictions and
    collapses duplicate citation ids, so first appearance there is the order a reader
    meets the evidence in. It is called rather than reimplemented, so there is exactly
    one definition of "the citations this answer relies on" in the codebase. A second
    citation from the same document keeps the first number, which is what makes the
    list a list of *sources*.
    """
    numbers: dict[int, int] = {}
    rendered: list[RenderedSource] = []

    for citation in answer.citations():
        if citation.source_id in numbers:
            continue
        numbers[citation.source_id] = len(rendered) + 1
        rendered.append(
            RenderedSource(
                number=len(rendered) + 1,
                title=escape(_shorten_title(citation.source_title)),
                url=escape(citation.source_url),
                domain=escape(citation.source_domain),
                locator=escape(citation.locator),
            )
        )

    return numbers, tuple(rendered)


def _shorten_title(title: str | None) -> str:
    """A bounded label beside the URL; the URL is the actual reference."""
    cleaned = (title or "").strip()
    if not cleaned:
        return "(untitled source)"
    if len(cleaned) <= _MAX_TITLE_CHARS:
        return cleaned
    return cleaned[: _MAX_TITLE_CHARS - 1].rstrip() + "…"


def _assert_sources_renderable(sources: tuple[RenderedSource, ...]) -> None:
    """Refuse to render a reference list that cannot be made honest."""
    for source in sources:
        if not source.url.strip():
            raise CitationIntegrityError(
                "a verified citation has no retrievable URL to render"
            )


# --- body --------------------------------------------------------------------


def _render_body(answer: SynthesizedAnswer, numbering: dict[int, int]) -> str:
    """The answer prose with a marker run at the end of each section."""
    blocks: list[str] = []

    for section in answer.sections:
        markers = _markers_for(section.source_ids, numbering)
        block = escape(section.heading) if section.heading else ""
        if block and section.text:
            block = f"{block}\n{escape(section.text)}"
        elif section.text:
            block = escape(section.text)
        if markers:
            block = f"{block}\n{markers}" if block else markers
        if block:
            blocks.append(block)

    for conflict in answer.contradictions:
        markers = _markers_for(
            tuple(dict.fromkeys(c.source_id for c in conflict.citations)), numbering
        )
        note = escape(conflict.note) if conflict.note else ""
        block = f"{note}\n{markers}" if note and markers else (note or markers)
        if block:
            blocks.append(block)

    return "\n\n".join(blocks)


def _markers_for(source_ids: tuple[int, ...], numbering: dict[int, int]) -> str:
    """``[1] [2]`` for the sources this section cites, in the order it cites them.

    Markers are separated by single spaces so each one is its own whitespace-delimited
    token: a line-wrapping splitter breaks on spaces and therefore cannot tear a marker
    in half or leave half of one behind.
    """
    numbers = [numbering[sid] for sid in source_ids if sid in numbering]
    # First occurrence wins, matching the list's own ordering.
    ordered = list(dict.fromkeys(numbers))
    return " ".join(f"[{number}]" for number in ordered)


# --- source list -------------------------------------------------------------


def _render_sources(sources: tuple[RenderedSource, ...]) -> str:
    """The numbered reference list: one label line and one URL line per source."""
    lines = [_SOURCES_HEADING]
    for source in sources:
        label = f"[{source.number}] {source.title}"
        if source.domain:
            label = f"{label} — {source.domain}"
        if source.locator:
            label = f"{label} ({source.locator})"
        lines.append(label)
        lines.append(source.url)
    return "\n".join(lines)


# --- chunking ----------------------------------------------------------------


def _pack(text: str, limit: int) -> list[str]:
    """Pack ``text`` into chunks of at most ``limit`` characters.

    Lines are the unit: a chunk is filled with whole lines, so a citation marker, a
    source label, and its URL are never split across two messages. A single line
    longer than the limit is the only case that is broken up, which is the same
    content-preserving rule ``split_for_telegram`` already applies (P0-9: a long answer
    must not fail, and must not be silently truncated).

    ``split_for_telegram`` is not reused here because it packs greedily across
    newlines and will split mid-token when a line is over-long, which is precisely the
    orphaning this module exists to prevent. The final chunks are still within the
    limit ``DeliveryService`` applies, so delivery does not re-split them.
    """
    if not text:
        return []

    chunks: list[str] = []
    current = ""

    for line in text.split("\n"):
        for piece in _fit_line(line, limit):
            candidate = f"{current}\n{piece}" if current else piece
            if len(candidate) > limit:
                if current:
                    chunks.append(current)
                    current = ""
                # ``piece`` is at most ``limit`` on its own, so it can start a chunk.
                current = piece
            else:
                current = candidate

    if current:
        chunks.append(current)
    return chunks


def _fit_line(line: str, limit: int) -> list[str]:
    """One line as the pieces that fit a chunk.

    A line within the limit is one piece. A longer line is broken on spaces where
    possible, so words stay intact; a run with no space at all (a very long URL) is
    broken by length, which is unavoidable and still lossless.
    """
    if len(line) <= limit:
        return [line]

    pieces: list[str] = []
    remaining = line
    while len(remaining) > limit:
        window = remaining[:limit]
        cut = window.rfind(" ")
        if cut <= 0:
            pieces.append(remaining[:limit])
            remaining = remaining[limit:]
            continue
        pieces.append(remaining[:cut])
        remaining = remaining[cut + 1 :]
    if remaining:
        pieces.append(remaining)
    return pieces