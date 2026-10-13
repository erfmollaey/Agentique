"""Passage/chunk production service (Phase 3, P3.9-A — FR-22, AD-033).

The application-layer step between one source's extracted content and the
persisted ``SourceChunk`` rows P3.7 reads. It owns the ordering of the operations
and nothing else: settle ownership, plan the passages, replace the source's
passages in one transaction.

```text
selected Source + its ExtractedContent (P3.4)
    → chunk_source_text()          → PassageSpan tuple   (pure, deterministic)
    → one transaction:
        is any passage already cited?      → refuse if so
        delete_for_source()                → the previous passage set
        source_chunks.add() per span       → ordinal, text, char_start, char_end
```

**Why this is a service and not a repository call.** The passages are derived
data: the same normalized text always produces the same spans, and re-deriving
them must converge rather than duplicate. That makes the *decision* (boundaries,
order, bounds) a domain concern and the *write* a persistence concern, and the
service is the seam that owns the transaction spanning both.

**Idempotency is replacement, and replacement is guarded.** Re-execution is an
expected condition, not a hypothetical: ``task_acks_late=True`` with
``task_reject_on_worker_lost=True`` means a hard-killed research task is
redelivered and re-executes from the start (audit § R.11, § R.14). Appending
would collide with ``uq_source_chunks_source_id_ordinal`` and abort the run; the
producer therefore replaces the source's own passage set inside one unit of work,
so a redelivery converges on the same rows.

The guard is the part that is easy to get wrong. ``citations.chunk_id`` is
``ON DELETE CASCADE`` from ``source_chunks``, so deleting a passage that a
verified citation points at would silently delete the citation — the quote that
justified the claim, gone, with the claim still standing. No constraint in this
schema can refuse it, so the service checks before it writes and raises
:class:`~app.domain.errors.ChunkReplaceBlockedError`. A source whose evidence
has already been cited is not re-chunked; producing different passages for it is
a new run's business, not a rewrite of an existing one.

**Empty content writes nothing.** P3.4 already classifies "reached but unusable"
as a typed outcome (``ExtractedContent.is_empty``), and a page that yields no
readable text has no passage to produce. The service returns an empty result and
leaves the source exactly as it was rather than deleting rows it cannot replace:
there is nothing to gain and evidence to lose.

**What this service does not do.** It does not fetch, extract, rank, summarize,
verify claims, synthesize, render, or schedule. It does not touch another
source's passages, another run's sources, or any column other than the four on
``source_chunks``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.domain.errors import (
    ChunkReplaceBlockedError,
    ResearchRunNotFoundError,
    ResearchSourceNotFoundError,
)
from app.domain.research.chunking import (
    MAX_CHUNK_CEILING,
    MAX_CHUNK_CHARS,
    MIN_CHUNK_CHARS,
    PassageSpan,
    chunk_source_text,
    covers_text,
    normalized_source_text,
)
from app.domain.research.extraction import ExtractedContent
from app.domain.research.types import Source, SourceChunk

log = logging.getLogger(__name__)


class SourceChunkSet(BaseModel):
    """What the chunk stage persisted for one source.

    A result, not a verdict. ``chunks`` carries the **persisted** ``SourceChunk``
    rows with their database-assigned ids, which is what the next stage needs to
    identify the passages it may read: P3.7 loads them through
    ``SourceChunkRepository.list_for_source`` and hands them to the evidence
    stage as ``EvidenceChunk`` values, so the ids are the pipeline-assigned
    evidence identity (AD-033) and nothing downstream re-derives them.

    ``replaced_count`` records how many previous passages the replacement
    removed — ``0`` on a first run, and the previous count on a re-run. It is
    here so a caller can tell "this source was chunked" from "this source was
    re-chunked", which is the difference between a fresh run and a redelivery
    (audit § R.14).
    """

    model_config = ConfigDict(frozen=True)

    source_id: int
    run_id: int
    chunks: tuple[SourceChunk, ...] = ()
    normalized_char_count: int = 0
    replaced_count: int = 0

    @property
    def is_empty(self) -> bool:
        """Whether this source produced no passage at all.

        The honest state for an extraction P3.4 classified as empty: no evidence
        exists, and nothing was written pretending otherwise.
        """
        return not self.chunks

    @property
    def chunk_ids(self) -> tuple[int, ...]:
        """The persisted passage ids, in ordinal order."""
        return tuple(chunk.id for chunk in self.chunks if chunk.id is not None)

    @property
    def ordinals(self) -> tuple[int, ...]:
        """The passage ordinals, always ``0 .. n-1``."""
        return tuple(chunk.ordinal for chunk in self.chunks)


class SourceChunkService:
    """Turn one source's extracted content into its persisted passages."""

    def __init__(self, *, uow_factory: Callable[[], Any]) -> None:
        self._uow_factory = uow_factory

    async def chunk_source(
        self,
        *,
        run_id: int,
        user_id: int,
        source_id: int,
        content: ExtractedContent,
    ) -> SourceChunkSet:
        """Persist ``content`` as the passages of ``source_id`` (FR-22).

        Ownership is settled before anything is derived or written: the run is
        read through ``get_for_user`` and the source must belong to *that* run, so
        a caller that named a source from another user's run — or another run of
        the same user — is refused before a row exists (SR-10, T-23). This is the
        same two-step the evidence and synthesis services use, because the
        repository's ``sources.get`` is not user-scoped and the caller has to
        close that gap.

        The extracted content's own ``url`` is deliberately **not** compared
        against the source's stored URL: ``ExtractedContent.url`` is the
        ``FetchedSource.final_url`` — the redirect destination that answered —
        while the source row keeps the URL search returned. They differ on every
        redirect, and requiring agreement would refuse legitimate passages.

        Raises, rather than returning an empty or partial result:

        * :class:`ResearchRunNotFoundError` — the run is not this user's;
        * :class:`ResearchSourceNotFoundError` — the source is not in this run;
        * :class:`~app.domain.errors.ChunkLimitError` — the text would produce
          more passages than the structural ceiling allows. Raised before the
          write, so the source keeps whatever passages it had;
        * :class:`~app.domain.errors.ChunkReplaceBlockedError` — a verified
          citation already points at one of this source's passages, so replacing
          them would destroy it;
        * :class:`~app.domain.errors.PersistenceError` (via the driver) — a
          database failure rolls the whole replacement back.

        Nothing is written on any of those. The passages that exist when the
        call returns are the passages the text produces, or the passages that
        were already there.
        """
        source = await self._load_owned_source(run_id, user_id, source_id)

        spans: tuple[PassageSpan, ...] = chunk_source_text(content.text)
        normalized_chars = len(normalized_source_text(content.text))
        if not spans:
            # P3.4's typed "reached but unusable" outcome. No passage is
            # manufactured, and none is deleted either: there is nothing to
            # replace them with, and the source may still be re-extracted.
            log.info(
                "no passages produced: source=%d run=%d extracted_chars=%d",
                source_id,
                run_id,
                normalized_chars,
            )
            return SourceChunkSet(
                source_id=source_id,
                run_id=run_id,
                chunks=(),
                normalized_char_count=0,
                replaced_count=0,
            )

        async with self._uow_factory() as uow:
            cited = await uow.citations.count_for_source(source_id)
            if cited:
                # The database would cascade these citations away with the
                # passages they cite. Refusing is the only behaviour that keeps
                # the trust chain intact; see ChunkReplaceBlockedError.
                raise ChunkReplaceBlockedError(
                    f"source {source_id} of run {run_id} has {cited} citation(s) "
                    f"pointing at its existing passages; replacing them would "
                    f"delete those citations"
                )
            replaced = await uow.source_chunks.delete_for_source(source_id)
            chunks = [
                await uow.source_chunks.add(
                    source_id, span.ordinal, span.text, span.char_start, span.char_end
                )
                for span in spans
            ]

        log.info(
            "passages persisted: source=%d run=%d chunks=%d normalized_chars=%d "
            "replaced=%d domain=%s",
            source_id,
            run_id,
            len(chunks),
            normalized_chars,
            replaced,
            source.domain,
        )
        return SourceChunkSet(
            source_id=source_id,
            run_id=run_id,
            chunks=tuple(chunks),
            normalized_char_count=normalized_chars,
            replaced_count=replaced,
        )

    # --- internals -----------------------------------------------------------

    async def _load_owned_source(
        self, run_id: int, user_id: int, source_id: int
    ) -> Source:
        """Resolve the run and the source in one short read transaction.

        Closed before anything is written, so the write transaction holds no
        read snapshot across it. A source id from any other run is
        indistinguishable from one that does not exist — not "found, then
        filtered" — which is the same rule the evidence stage applies.
        """
        async with self._uow_factory() as uow:
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
            return source


__all__ = [
    "MAX_CHUNK_CEILING",
    "MAX_CHUNK_CHARS",
    "MIN_CHUNK_CHARS",
    "SourceChunkService",
    "SourceChunkSet",
    "covers_text",
]
