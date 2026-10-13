"""Passage/chunk production against a real PostgreSQL (P3.9-A — FR-22, AD-033).

The chunk producer is only half a guarantee until it is in the database. § 14
requires that a claim's evidence resolves to a passage of the run's own source,
so what this file asserts is the persistence half: what is written, what is
replaced, what is refused, and what survives a failure.

Covered here:

* the **round-trip** — English, Persian, and mixed text, with NFC and ZWNJ intact;
* **idempotency** — a re-run converges instead of adding rows;
* **replacement** — changed content leaves no stale passage behind;
* **atomicity** — a failure mid-replacement rolls the whole thing back, including
  the delete, so the previous passage set is restored rather than emptied;
* **ownership** (SR-10) — another user's run, and another run's source, are
  refused; one source's passages are never touched through another's;
* **citation safety** — a source whose passages are already cited is refused, and
  the citation that made it so survives;
* **downstream compatibility** — the passages the service writes are exactly what
  ``SourceEvidenceService`` reads, and a claim extracted from them verifies.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.domain.errors import (
    ChunkReplaceBlockedError,
    ResearchRunNotFoundError,
    ResearchSourceNotFoundError,
)
from app.domain.research.chunking import MAX_CHUNK_CHARS
from app.domain.research.extraction import ExtractedContent
from app.domain.research.types import SourceChunk
from app.repositories.research import (
    CitationRepositoryImpl,
    ClaimRepositoryImpl,
    ResearchRunRepositoryImpl,
    SourceChunkRepositoryImpl,
    SourceRepositoryImpl,
)
from app.repositories.users import UserRepositoryImpl
from app.services.research.chunking import SourceChunkService
from tests.conftest import run_on_process_loop
from tests.research_support import (
    ENGLISH_PASSAGE,
    PERSIAN_PASSAGE,
    RecordingLLM,
    claim,
    claims_response,
    uow_over,
)

CHUNK_COUNT = 4
#: Long enough that the chunker must open several passages, and built from whole
#: sentences so the boundaries are sentence boundaries.
MULTI_PASSAGE_TEXT = " ".join(
    f"The {ordinal}th field survey recorded a change across the sampled districts."
    for ordinal in range(1, 61)
)
PERSIAN_TEXT = " ".join(
    f"پژوهشگران در سال {ordinal}۳ افزایش چهل درصدی را در نمونه‌های شهری گزارش کردند. "
    for ordinal in range(1, 41)
)
MIXED_TEXT = (
    "این گزارش بر پایهٔ field survey منتشر شده و رشد ۴۰ درصدی را نشان می‌دهد. "
    * 20
)
ENGLISH_QUOTE = "recorded a forty percent increase across the sampled districts"
PERSIAN_QUOTE = "افزایش چهل درصدی را در نمونه‌های شهری گزارش کردند"


def result_ordinals(chunks) -> list[int]:
    return [chunk.ordinal for chunk in chunks]


def _content(text: str, url: str = "https://example.com/page") -> ExtractedContent:
    return ExtractedContent(url=url, text=text)


def _service(session) -> SourceChunkService:
    return SourceChunkService(uow_factory=uow_over(session))


async def _seed(db, telegram_user_id: int, *, url: str = "https://example.com/page"):
    """One user, one run, one source — the minimum the producer needs."""
    user = await UserRepositoryImpl(db).get_or_create(telegram_user_id, None)
    run = await ResearchRunRepositoryImpl(db).create(user.id, "What did the survey find?")
    source = await SourceRepositoryImpl(db).add(run.id, url, "example.com", title="A page")
    await db.commit()
    return user, run, source


async def _chunks(db, source_id: int) -> list[SourceChunk]:
    return list(await SourceChunkRepositoryImpl(db).list_for_source(source_id))


# --- the round-trip ---------------------------------------------------------


class TestRoundTrip:
    async def test_short_content_produces_the_expected_passage_set(self, db):
        user, run, source = await _seed(db, 9101)
        result = await _service(db).chunk_source(
            run_id=run.id,
            user_id=user.id,
            source_id=source.id,
            content=_content(ENGLISH_PASSAGE),
        )
        stored = await _chunks(db, source.id)
        assert [chunk.text for chunk in stored] == [ENGLISH_PASSAGE]
        assert [chunk.text for chunk in result.chunks] == [ENGLISH_PASSAGE]
        assert result.chunks[0].id == stored[0].id
        assert result.source_id == source.id
        assert result.run_id == run.id
        assert not result.is_empty

    async def test_long_content_produces_bounded_deterministic_passages(self, db):
        user, run, source = await _seed(db, 9102)
        result = await _service(db).chunk_source(
            run_id=run.id,
            user_id=user.id,
            source_id=source.id,
            content=_content(MULTI_PASSAGE_TEXT),
        )
        stored = await _chunks(db, source.id)
        assert len(stored) == CHUNK_COUNT
        assert all(len(chunk.text) <= MAX_CHUNK_CHARS for chunk in stored)
        assert [chunk.ordinal for chunk in stored] == list(range(CHUNK_COUNT))
        assert [chunk.id for chunk in stored] == [chunk.id for chunk in result.chunks]

        # Determinism: the same content through a second source gives the same
        # boundaries and the same ordinals.
        other = await SourceRepositoryImpl(db).add(
            run.id, "https://example.com/other", "example.com"
        )
        await db.commit()
        repeat = await _service(db).chunk_source(
            run_id=run.id,
            user_id=user.id,
            source_id=other.id,
            content=_content(MULTI_PASSAGE_TEXT),
        )
        assert [chunk.text for chunk in repeat.chunks] == [
            chunk.text for chunk in result.chunks
        ]
        assert [chunk.ordinal for chunk in repeat.chunks] == list(range(CHUNK_COUNT))

    async def test_ordinals_are_dense_and_the_read_order_is_explicit(self, db):
        user, run, source = await _seed(db, 9103)
        await _service(db).chunk_source(
            run_id=run.id,
            user_id=user.id,
            source_id=source.id,
            content=_content(MULTI_PASSAGE_TEXT),
        )
        stored = await _chunks(db, source.id)
        assert [chunk.ordinal for chunk in stored] == list(range(len(stored)))
        assert result_ordinals(stored) == sorted(result_ordinals(stored))

    async def test_persian_text_round_trips_unchanged(self, db):
        user, run, source = await _seed(db, 9104)
        await _service(db).chunk_source(
            run_id=run.id,
            user_id=user.id,
            source_id=source.id,
            content=_content(PERSIAN_TEXT),
        )
        stored = await _chunks(db, source.id)
        assert stored
        joined = "".join(chunk.text for chunk in stored)
        assert "".join(joined.split()) == "".join(PERSIAN_TEXT.split())
        assert "نمونه‌های" in joined  # ZWNJ survives

    async def test_mixed_and_nfc_text_round_trips_unchanged(self, db):
        user, run, source = await _seed(db, 9105)
        decomposed = "cafe\u0301 with an accent. " + MIXED_TEXT
        await _service(db).chunk_source(
            run_id=run.id,
            user_id=user.id,
            source_id=source.id,
            content=_content(decomposed),
        )
        stored = await _chunks(db, source.id)
        assert stored
        assert stored[0].text.startswith("caf\u00e9")
        assert any("۴۰" in chunk.text for chunk in stored)

    async def test_offsets_index_the_stored_text(self, db):
        """FR-37's locator: a passage's offsets locate its own text."""
        user, run, source = await _seed(db, 9106)
        await _service(db).chunk_source(
            run_id=run.id,
            user_id=user.id,
            source_id=source.id,
            content=_content(MULTI_PASSAGE_TEXT),
        )
        normalized = " ".join(MULTI_PASSAGE_TEXT.split())
        for chunk in await _chunks(db, source.id):
            assert normalized[chunk.char_start : chunk.char_end] == chunk.text
            assert chunk.char_end - chunk.char_start == len(chunk.text)

    async def test_the_result_identifies_the_persisted_passages(self, db):
        user, run, source = await _seed(db, 9107)
        result = await _service(db).chunk_source(
            run_id=run.id,
            user_id=user.id,
            source_id=source.id,
            content=_content(MULTI_PASSAGE_TEXT),
        )
        assert result.chunk_ids == tuple(chunk.id for chunk in await _chunks(db, source.id))
        assert len(set(result.chunk_ids)) == len(result.chunk_ids)
        assert result.ordinals == tuple(range(CHUNK_COUNT))


class TestEmpty:
    async def test_empty_content_writes_no_passage(self, db):
        user, run, source = await _seed(db, 9110)
        result = await _service(db).chunk_source(
            run_id=run.id,
            user_id=user.id,
            source_id=source.id,
            content=_content(""),
        )
        assert result.is_empty
        assert result.chunks == ()
        assert await _chunks(db, source.id) == []

    async def test_whitespace_only_content_writes_no_passage(self, db):
        user, run, source = await _seed(db, 9111)
        result = await _service(db).chunk_source(
            run_id=run.id,
            user_id=user.id,
            source_id=source.id,
            content=_content("   \n\t  \n "),
        )
        assert result.is_empty
        assert await _chunks(db, source.id) == []

    async def test_an_empty_extraction_does_not_remove_what_was_there(self, db):
        """A page that parses to nothing must not delete usable evidence."""
        user, run, source = await _seed(db, 9112)
        await _service(db).chunk_source(
            run_id=run.id,
            user_id=user.id,
            source_id=source.id,
            content=_content(ENGLISH_PASSAGE),
        )
        before = await _chunks(db, source.id)
        result = await _service(db).chunk_source(
            run_id=run.id,
            user_id=user.id,
            source_id=source.id,
            content=_content("   "),
        )
        assert result.is_empty
        assert await _chunks(db, source.id) == before


class TestIdempotency:
    async def test_a_re_run_does_not_duplicate_rows(self, db):
        user, run, source = await _seed(db, 9120)
        first = await _service(db).chunk_source(
            run_id=run.id,
            user_id=user.id,
            source_id=source.id,
            content=_content(MULTI_PASSAGE_TEXT),
        )
        second = await _service(db).chunk_source(
            run_id=run.id,
            user_id=user.id,
            source_id=source.id,
            content=_content(MULTI_PASSAGE_TEXT),
        )
        stored = await _chunks(db, source.id)
        assert len(stored) == CHUNK_COUNT
        assert [chunk.text for chunk in first.chunks] == [chunk.text for chunk in second.chunks]
        assert second.replaced_count == CHUNK_COUNT
        assert first.replaced_count == 0

    async def test_changed_content_leaves_no_stale_passage(self, db):
        user, run, source = await _seed(db, 9121)
        await _service(db).chunk_source(
            run_id=run.id,
            user_id=user.id,
            source_id=source.id,
            content=_content(MULTI_PASSAGE_TEXT),
        )
        changed = await _service(db).chunk_source(
            run_id=run.id,
            user_id=user.id,
            source_id=source.id,
            content=_content(PERSIAN_TEXT),
        )
        stored = await _chunks(db, source.id)
        assert [chunk.text for chunk in stored] == [chunk.text for chunk in changed.chunks]
        assert not any("field survey" in chunk.text for chunk in stored)
        assert [chunk.ordinal for chunk in stored] == list(range(len(stored)))

    async def test_a_shorter_replacement_drops_the_extra_passages(self, db):
        user, run, source = await _seed(db, 9122)
        await _service(db).chunk_source(
            run_id=run.id,
            user_id=user.id,
            source_id=source.id,
            content=_content(MULTI_PASSAGE_TEXT),
        )
        await _service(db).chunk_source(
            run_id=run.id,
            user_id=user.id,
            source_id=source.id,
            content=_content(ENGLISH_PASSAGE),
        )
        stored = await _chunks(db, source.id)
        assert len(stored) == 1


class TestAtomicity:
    def test_a_failed_replacement_restores_the_previous_passage_set(
        self, clean_database
    ):
        """The delete is part of the transaction, so a mid-run failure undoes it.

        Real commits matter here, which is why this runs on ``clean_database``
        rather than the per-test-rollback ``db`` fixture: the ``db`` fixture holds
        an outer transaction that would hide the difference between a rolled-back
        delete and a delete that never happened.
        """

        async def scenario() -> None:
            async with clean_database.sessionmaker() as session:
                user, run, source = await _seed(session, 9130)
                await _service(session).chunk_source(
                    run_id=run.id,
                    user_id=user.id,
                    source_id=source.id,
                    content=_content(ENGLISH_PASSAGE),
                )
                original = await _chunks(session, source.id)
                assert original

                service = SourceChunkService(
                    uow_factory=lambda: _FailingChunksUoW(session)
                )
                with pytest.raises(RuntimeError):
                    await service.chunk_source(
                        run_id=run.id,
                        user_id=user.id,
                        source_id=source.id,
                        content=_content(MULTI_PASSAGE_TEXT),
                    )
                await session.commit()

                # Committed state, read fresh: neither the new passages nor the
                # delete survived, so the source is exactly as it was.
                stored = await _chunks(session, source.id)
                assert [chunk.text for chunk in stored] == [
                    chunk.text for chunk in original
                ]

        run_on_process_loop(scenario())


class _FailingChunksUoW:
    """A unit of work whose passage writes fail on the second insert.

    Real repositories throughout — only ``source_chunks.add`` is wrapped — so the
    delete that precedes it really was issued inside the transaction. That is
    what makes this a test of atomicity rather than of a mock.
    """

    def __init__(self, session) -> None:
        self.research_runs = ResearchRunRepositoryImpl(session)
        self.sources = SourceRepositoryImpl(session)
        self.source_chunks = _FailingChunks(SourceChunkRepositoryImpl(session))
        self.claims = ClaimRepositoryImpl(session)
        self.citations = CitationRepositoryImpl(session)
        self._session = session

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if exc_type is None:
            await self._session.commit()
        else:
            await self._session.rollback()
        return None


class _FailingChunks:
    def __init__(self, inner: SourceChunkRepositoryImpl) -> None:
        self._inner = inner
        self._adds = 0

    async def add(self, source_id, ordinal, text_, char_start, char_end):
        self._adds += 1
        if self._adds == 2:
            raise RuntimeError("injected failure after the first passage")
        return await self._inner.add(source_id, ordinal, text_, char_start, char_end)

    def __getattr__(self, name):
        return getattr(self._inner, name)


class TestOwnership:
    async def test_another_users_run_is_refused(self, db):
        from app.repositories.users import UserRepositoryImpl as Users

        _owner, run, source = await _seed(db, 9140)
        intruder = await Users(db).get_or_create(9141, None)
        await db.commit()
        with pytest.raises(ResearchRunNotFoundError):
            await _service(db).chunk_source(
                run_id=run.id,
                user_id=intruder.id,
                source_id=source.id,
                content=_content(ENGLISH_PASSAGE),
            )
        assert await _chunks(db, source.id) == []

    async def test_a_source_of_another_run_is_refused(self, db):
        """Same user, different run: the source is not in the run being written."""
        user = await UserRepositoryImpl(db).get_or_create(9142, None)
        runs = ResearchRunRepositoryImpl(db)
        sources = SourceRepositoryImpl(db)
        mine = await runs.create(user.id, "my question")
        theirs = await runs.create(user.id, "their question")
        source = await sources.add(theirs.id, "https://example.com/x", "example.com")
        await db.commit()
        with pytest.raises(ResearchSourceNotFoundError):
            await _service(db).chunk_source(
                run_id=mine.id,
                user_id=user.id,
                source_id=source.id,
                content=_content(ENGLISH_PASSAGE),
            )
        assert await _chunks(db, source.id) == []

    async def test_one_sources_replacement_leaves_the_other_untouched(self, db):
        user, run, first = await _seed(db, 9143, url="https://example.com/one")
        second = await SourceRepositoryImpl(db).add(
            run.id, "https://example.com/two", "example.com"
        )
        await db.commit()
        service = _service(db)
        for source in (first, second):
            await service.chunk_source(
                run_id=run.id,
                user_id=user.id,
                source_id=source.id,
                content=_content(ENGLISH_PASSAGE),
            )
        untouched = [chunk.text for chunk in await _chunks(db, second.id)]

        await service.chunk_source(
            run_id=run.id,
            user_id=user.id,
            source_id=first.id,
            content=_content(PERSIAN_TEXT),
        )

        assert [chunk.text for chunk in await _chunks(db, second.id)] == untouched
        assert await _chunks(db, first.id) != untouched

    async def test_the_delete_is_scoped_by_source_id(self, db):
        """The repository statement cannot reach another source's passages."""
        _user, run, first = await _seed(db, 9144, url="https://example.com/one")
        second = await SourceRepositoryImpl(db).add(
            run.id, "https://example.com/two", "example.com"
        )
        await db.commit()
        chunks = SourceChunkRepositoryImpl(db)
        await chunks.add(first.id, 0, "mine", 0, 4)
        await chunks.add(second.id, 0, "theirs", 0, 6)
        await db.commit()

        removed = await chunks.delete_for_source(first.id)
        assert removed == 1
        assert await _chunks(db, first.id) == []
        assert [chunk.text for chunk in await _chunks(db, second.id)] == ["theirs"]


class TestCitationSafety:
    async def test_a_source_whose_passages_are_cited_is_not_re_chunked(self, db):
        """AD-033: the database would cascade the citations away with the passages."""
        _user, run, source = await _seed(db, 9150)
        service = _service(db)
        result = await service.chunk_source(
            run_id=run.id,
            user_id=_user.id,
            source_id=source.id,
            content=_content(ENGLISH_PASSAGE),
        )
        chunk = result.chunks[0]
        claim_row = await ClaimRepositoryImpl(db).add(run.id, "A supported statement.")
        await CitationRepositoryImpl(db).add(
            claim_row.id,
            source.id,
            chunk.id,
            ENGLISH_QUOTE,
            location="chunk 0",
        )
        await db.commit()

        with pytest.raises(ChunkReplaceBlockedError):
            await service.chunk_source(
                run_id=run.id,
                user_id=_user.id,
                source_id=source.id,
                content=_content(PERSIAN_TEXT),
            )

        # Not replaced, and the citation that blocked it is still there with its
        # quote — the evidence chain is intact.
        assert [chunk.text for chunk in await _chunks(db, source.id)] == [ENGLISH_PASSAGE]
        citations = await CitationRepositoryImpl(db).list_for_claim(claim_row.id)
        assert len(citations) == 1
        assert citations[0].chunk_id == chunk.id
        assert citations[0].quote

    async def test_another_sources_citations_do_not_block_this_one(self, db):
        """The guard is per source, not per run."""
        _user, run, cited = await _seed(db, 9151, url="https://example.com/cited")
        mine = await SourceRepositoryImpl(db).add(
            run.id, "https://example.com/mine", "example.com"
        )
        await db.commit()
        service = _service(db)
        result = await service.chunk_source(
            run_id=run.id, user_id=_user.id, source_id=cited.id, content=_content(ENGLISH_PASSAGE)
        )
        claim_row = await ClaimRepositoryImpl(db).add(run.id, "A supported statement.")
        await CitationRepositoryImpl(db).add(
            claim_row.id, cited.id, result.chunks[0].id, ENGLISH_QUOTE, location="chunk 0"
        )
        await db.commit()

        await service.chunk_source(
            run_id=run.id, user_id=_user.id, source_id=mine.id, content=_content(PERSIAN_TEXT)
        )
        assert await _chunks(db, mine.id)


class TestDownstreamCompatibility:
    async def test_the_persisted_passages_are_what_the_evidence_stage_reads(
        self, db, make_settings
    ):
        """The producer's output is P3.7's input, with no adapter in between.

        Drives the real ``SourceEvidenceService`` over passages this stage wrote
        and asserts a claim was born with evidence from one of them — the path
        the audit found unexercised (§ R.6, § R.7).
        """
        from app.domain.research.evidence import SourceSummary
        from app.infrastructure.evidence.analyzer import LLMClaimExtractor
        from app.services.research.evidence import SourceEvidenceService

        user, run, source = await _seed(db, 9160)
        produced = await _service(db).chunk_source(
            run_id=run.id,
            user_id=user.id,
            source_id=source.id,
            content=_content(ENGLISH_PASSAGE),
        )
        passage = produced.chunks[0]
        await db.commit()

        class _Summarizer:
            async def summarize(self, source):
                return SourceSummary(source_id=source.source_id, text="A summary.")

        llm = RecordingLLM(
            [
                claims_response(
                    claim(
                        "The survey recorded a forty percent increase.",
                        passage.id,
                        ENGLISH_QUOTE,
                    )
                )
            ]
        )
        service = SourceEvidenceService(
            settings=make_settings(),
            summarizer=_Summarizer(),
            extractor=LLMClaimExtractor(llm, make_settings()),
            uow_factory=uow_over(db),
        )
        result = await service.analyze_source(
            run_id=run.id, user_id=user.id, source_id=source.id
        )

        assert result.claim_count == 1
        assert result.claims[0].evidence.chunk_id == passage.id
        assert result.claims[0].evidence.ordinal == 0
        assert result.claims[0].evidence.quote in passage.text

    async def test_a_persian_passage_supports_a_persian_claim(self, db, make_settings):
        from app.domain.research.evidence import SourceSummary
        from app.infrastructure.evidence.analyzer import LLMClaimExtractor
        from app.services.research.evidence import SourceEvidenceService

        user, run, source = await _seed(db, 9161, url="https://example.com/fa")
        produced = await _service(db).chunk_source(
            run_id=run.id,
            user_id=user.id,
            source_id=source.id,
            content=_content(PERSIAN_PASSAGE),
        )
        passage = produced.chunks[0]
        await db.commit()

        class _Summarizer:
            async def summarize(self, source):
                return SourceSummary(source_id=source.source_id, text="چکیده.")

        llm = RecordingLLM(
            [
                claims_response(
                    claim(
                        "پژوهشگران افزایش چهل درصدی را گزارش کردند.",
                        passage.id,
                        PERSIAN_QUOTE,
                    )
                )
            ]
        )
        service = SourceEvidenceService(
            settings=make_settings(),
            summarizer=_Summarizer(),
            extractor=LLMClaimExtractor(llm, make_settings()),
            uow_factory=uow_over(db),
        )
        result = await service.analyze_source(
            run_id=run.id, user_id=user.id, source_id=source.id
        )
        assert result.claim_count == 1
        assert result.claims[0].evidence.quote in passage.text

    async def test_the_chunk_uniqueness_still_holds_after_a_replacement(self, db):
        """``uq_source_chunks_source_id_ordinal`` is untouched by replacement."""
        user, run, source = await _seed(db, 9162)
        service = _service(db)
        for _ in range(3):
            await service.chunk_source(
                run_id=run.id,
                user_id=user.id,
                source_id=source.id,
                content=_content(MULTI_PASSAGE_TEXT),
            )
        rows = (
            await db.execute(
                text(
                    "SELECT source_id, ordinal, count(*) FROM source_chunks "
                    "WHERE source_id = :sid GROUP BY source_id, ordinal"
                ),
                {"sid": source.id},
            )
        ).all()
        assert rows and all(count == 1 for _sid, _ordinal, count in rows)
