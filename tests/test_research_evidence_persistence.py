"""Summary and evidence persistence against a real PostgreSQL (P3.7).

The evidence decision is only half a guarantee until it is in the database: § 14
requires that a persisted claim cannot exist without a source, a passage, a
statement, and a passage quote, and that the passage belongs to the claim's own
source. So these tests run against the migrated schema, not a fake.

Covered here:

* the **round-trip** of a summary and of a claim with its quote, including Persian;
* the **FK and ownership** behaviour, including the composite constraint that makes
  a cross-source citation uninsertable;
* **atomicity** — a write that fails partway leaves no claims, no citations, and no
  summary behind;
* **transaction rollback** and **deletion cascade** with the new columns present;
* **ownership** (SR-10): a foreign run or a foreign source is refused before a model
  is called, and a summary cannot be written to another run.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.domain.errors import (
    ClaimEvidenceError,
    LLMServiceError,
    ResearchRunNotFoundError,
    ResearchSourceNotFoundError,
)
from app.domain.research.evidence import SourceSummary, normalize_evidence_text
from app.infrastructure.evidence.analyzer import LLMClaimExtractor
from app.repositories.research import (
    CitationRepositoryImpl,
    ClaimRepositoryImpl,
    SourceRepositoryImpl,
)
from app.services.research.evidence import SourceEvidenceService, load_source_evidence
from tests.conftest import run_on_process_loop
from tests.research_support import (
    ENGLISH_PASSAGE,
    PERSIAN_PASSAGE,
    RecordingLLM,
    claim,
    claims_response,
    seed_source,
    uow_over,
)

ENGLISH_QUOTE = "recorded a forty percent increase across the sampled districts"
ENGLISH_STATEMENT = "The survey recorded a forty percent increase."
PERSIAN_QUOTE = "افزایش چهل درصدی را در نمونه‌های شهری"
PERSIAN_STATEMENT = "پژوهشگران افزایش چهل درصدی را گزارش کردند."


class _FixedSummarizer:
    def __init__(self, text: str = "A summary.") -> None:
        self.text = text

    async def summarize(self, source):
        return SourceSummary(source_id=source.source_id, text=self.text)


async def analyze(db, settings, replies, *, telegram_user_id, passages=(ENGLISH_PASSAGE,), summarizer=None):
    user, run, source, rows, *_ = await seed_source(
        db, telegram_user_id=telegram_user_id, passages=passages
    )
    llm = RecordingLLM(replies)
    service = SourceEvidenceService(
        settings=settings,
        summarizer=summarizer or _FixedSummarizer(),
        extractor=LLMClaimExtractor(llm, settings),
        uow_factory=uow_over(db),
    )
    result = await service.analyze_source(run_id=run.id, user_id=user.id, source_id=source.id)
    return result, user, run, source, rows


class _FailingCitationsUoW:
    """A unit of work whose *second* citation write fails.

    Real repositories throughout — only the citation repository is wrapped — so the
    claim row that precedes it really is written to the transaction before the
    failure. That is what makes this a test of atomicity rather than of a mock.
    """

    def __init__(self, session) -> None:
        from app.repositories.research import (
            CitationRepositoryImpl,
            ClaimRepositoryImpl,
            ResearchRunRepositoryImpl,
            SourceChunkRepositoryImpl,
            SourceRepositoryImpl,
        )

        self.research_runs = ResearchRunRepositoryImpl(session)
        self.sources = SourceRepositoryImpl(session)
        self.source_chunks = SourceChunkRepositoryImpl(session)
        self.claims = ClaimRepositoryImpl(session)
        self._citations = CitationRepositoryImpl(session)
        self._writes = 0
        self._session = session

        citations = self

        class _Citations:
            async def add(self, claim_id, source_id, chunk_id, quote, *, location=None):
                citations._writes += 1
                if citations._writes == 2:
                    raise ClaimEvidenceError("injected failure after one citation")
                return await citations._citations.add(
                    claim_id, source_id, chunk_id, quote, location=location
                )

        self.citations = _Citations()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if exc_type is None:
            await self._session.commit()
        else:
            await self._session.rollback()
        return None


# --- the round-trip ---------------------------------------------------------


class TestSummaryPersistence:
    async def test_the_summary_survives_the_database(self, db, make_settings):
        settings = make_settings()
        result, user, run, source, _rows = await analyze(
            db, settings, [claims_response()], telegram_user_id=7401
        )
        await db.commit()

        stored = (await load_source_evidence(run.id, user.id, source.id, uow_over(db)))[1]
        assert stored == result.summary.text

    async def test_a_persian_summary_survives_as_persian(self, db, make_settings):
        persian = "این صفحه یک گزارش میدانی دربارهٔ رشد شهری است."
        settings = make_settings()
        _result, user, run, source, _rows = await analyze(
            db,
            settings,
            [claims_response()],
            telegram_user_id=7402,
            passages=(PERSIAN_PASSAGE,),
            summarizer=_FixedSummarizer(persian),
        )
        await db.commit()

        stored = (await load_source_evidence(run.id, user.id, source.id, uow_over(db)))[1]
        assert stored == persian
        assert "\\u" not in stored, "Persian must not be stored as escapes"

    async def test_a_summary_is_null_before_the_stage_runs(self, db, make_settings):
        user, run, source, _rows, *_ = await seed_source(db, telegram_user_id=7403)
        row = (await db.execute(text("SELECT summary FROM sources WHERE id = :i"), {"i": source.id})).scalar_one()
        assert row is None
        assert (await load_source_evidence(run.id, user.id, source.id, uow_over(db)))[1] is None

    async def test_a_summary_cannot_be_written_to_a_source_of_another_run(self, db):
        """The write is scoped by ``run_id`` as well as ``source_id`` (SR-10)."""
        _owner, owner_run, owner_source, *_ = await seed_source(db, telegram_user_id=7404)
        _u2, _other_run, other_source, *_ = await seed_source(
            db, telegram_user_id=7405, question="another question"
        )
        await SourceRepositoryImpl(db).set_summary(
            other_source.id, owner_run.id, "a summary written through the wrong run"
        )
        await db.commit()

        assert (
            await SourceRepositoryImpl(db).get(other_source.id)
        ).summary is None, "a mismatched run id must write nothing"
        assert (
            await SourceRepositoryImpl(db).get(owner_source.id)
        ).summary is None


class TestClaimPersistence:
    async def test_an_accepted_claim_is_persisted_with_its_passage(self, db, make_settings):
        settings = make_settings()
        user, run, source, rows, *_ = await seed_source(db, telegram_user_id=7410)
        llm = RecordingLLM([claims_response(claim(ENGLISH_STATEMENT, rows[0].id, ENGLISH_QUOTE))])
        service = SourceEvidenceService(
            settings=settings,
            summarizer=_FixedSummarizer(),
            extractor=LLMClaimExtractor(llm, settings),
            uow_factory=uow_over(db),
        )
        await service.analyze_source(run_id=run.id, user_id=user.id, source_id=source.id)
        await db.commit()

        claims = await ClaimRepositoryImpl(db).list_for_run(run.id)
        assert [c.text for c in claims] == [ENGLISH_STATEMENT]
        assert claims[0].status.value == "supported"
        assert claims[0].confidence is None, "nothing in P3.7 measures confidence"

        citations = await CitationRepositoryImpl(db).list_for_claim(claims[0].id)
        assert len(citations) == 1
        assert citations[0].source_id == source.id
        assert citations[0].chunk_id == rows[0].id
        assert citations[0].quote == ENGLISH_QUOTE
        assert citations[0].location == f"chunk {rows[0].ordinal}"

    async def test_the_persian_claim_and_quote_survive_intact(self, db, make_settings):
        settings = make_settings()
        user, run, source, rows, *_ = await seed_source(
            db, telegram_user_id=7411, passages=(PERSIAN_PASSAGE,)
        )
        llm = RecordingLLM([claims_response(claim(PERSIAN_STATEMENT, rows[0].id, PERSIAN_QUOTE))])
        service = SourceEvidenceService(
            settings=settings,
            summarizer=_FixedSummarizer(),
            extractor=LLMClaimExtractor(llm, settings),
            uow_factory=uow_over(db),
        )
        await service.analyze_source(run_id=run.id, user_id=user.id, source_id=source.id)
        await db.commit()

        claims = await ClaimRepositoryImpl(db).list_for_run(run.id)
        assert claims[0].text == PERSIAN_STATEMENT
        citations = await CitationRepositoryImpl(db).list_for_claim(claims[0].id)
        assert citations[0].quote == PERSIAN_QUOTE
        assert "نمونه‌های" in citations[0].quote, "ZWNJ must survive the round-trip"

    async def test_several_claims_are_persisted_in_a_stable_order(self, db, make_settings):
        settings = make_settings()
        user, run, source, rows, *_ = await seed_source(db, telegram_user_id=7412)
        llm = RecordingLLM(
            [
                claims_response(
                    claim(ENGLISH_STATEMENT, rows[0].id, ENGLISH_QUOTE),
                    claim("The methodology section was published.", rows[0].id, "the published methodology section"),
                )
            ]
        )
        service = SourceEvidenceService(
            settings=settings,
            summarizer=_FixedSummarizer(),
            extractor=LLMClaimExtractor(llm, settings),
            uow_factory=uow_over(db),
        )
        await service.analyze_source(run_id=run.id, user_id=user.id, source_id=source.id)
        await db.commit()

        first = [c.text for c in await ClaimRepositoryImpl(db).list_for_run(run.id)]
        assert first == [
            ENGLISH_STATEMENT,
            "The methodology section was published.",
        ], "claim order follows the model's order, which is reproducible for one input"

    async def test_a_rejected_claim_is_not_persisted_at_all(self, db, make_settings):
        settings = make_settings()
        user, run, source, rows, *_ = await seed_source(db, telegram_user_id=7413)
        llm = RecordingLLM(
            [claims_response(claim("Fabricated claim here.", rows[0].id, "nowhere in the passage"))]
        )
        service = SourceEvidenceService(
            settings=settings,
            summarizer=_FixedSummarizer(),
            extractor=LLMClaimExtractor(llm, settings),
            uow_factory=uow_over(db),
        )
        result = await service.analyze_source(run_id=run.id, user_id=user.id, source_id=source.id)
        await db.commit()
        assert result.claim_count == 0
        assert await ClaimRepositoryImpl(db).list_for_run(run.id) == []
        assert (
            await db.execute(text("SELECT count(*) FROM citations"))
        ).scalar_one() == 0

    async def test_no_persisted_claim_lacks_a_citation(self, db, make_settings):
        """The invariant this schema cannot express as a constraint, asserted."""
        settings = make_settings()
        user, run, source, rows, *_ = await seed_source(db, telegram_user_id=7414)
        llm = RecordingLLM(
            [
                claims_response(
                    claim(ENGLISH_STATEMENT, rows[0].id, ENGLISH_QUOTE),
                    claim("Also supported here.", rows[0].id, "the published methodology section"),
                )
            ]
        )
        service = SourceEvidenceService(
            settings=settings,
            summarizer=_FixedSummarizer(),
            extractor=LLMClaimExtractor(llm, settings),
            uow_factory=uow_over(db),
        )
        await service.analyze_source(run_id=run.id, user_id=user.id, source_id=source.id)
        await db.commit()

        orphans = (
            await db.execute(
                text(
                    "SELECT c.id FROM claims c LEFT JOIN citations ci ON ci.claim_id = c.id "
                    "WHERE ci.id IS NULL"
                )
            )
        ).scalars().all()
        assert orphans == []

    async def test_no_persisted_citation_lacks_a_quote(self, db, make_settings):
        settings = make_settings()
        user, run, source, rows, *_ = await seed_source(db, telegram_user_id=7415)
        llm = RecordingLLM([claims_response(claim(ENGLISH_STATEMENT, rows[0].id, ENGLISH_QUOTE))])
        service = SourceEvidenceService(
            settings=settings,
            summarizer=_FixedSummarizer(),
            extractor=LLMClaimExtractor(llm, settings),
            uow_factory=uow_over(db),
        )
        await service.analyze_source(run_id=run.id, user_id=user.id, source_id=source.id)
        await db.commit()

        empties = (
            await db.execute(
                text("SELECT id FROM citations WHERE quote IS NULL OR btrim(quote) = ''")
            )
        ).scalars().all()
        assert empties == []

    async def test_every_stored_quote_is_present_in_its_passage(self, db, make_settings):
        """The containment invariant, re-checked against the rows themselves.

        The database cannot express it, so the assertion lives here: for every
        citation that exists, its quote is a literal substring of the normalized
        text of the passage it names.
        """
        settings = make_settings()
        user, run, source, rows, *_ = await seed_source(db, telegram_user_id=7416)
        llm = RecordingLLM(
            [
                claims_response(
                    claim(ENGLISH_STATEMENT, rows[0].id, ENGLISH_QUOTE),
                    claim(PERSIAN_STATEMENT, rows[0].id, "۱۴۰۳ افزایش چهل درصدی"),
                )
            ]
        )
        service = SourceEvidenceService(
            settings=settings,
            summarizer=_FixedSummarizer(),
            extractor=LLMClaimExtractor(llm, settings),
            uow_factory=uow_over(db),
        )
        await service.analyze_source(run_id=run.id, user_id=user.id, source_id=source.id)
        await db.commit()

        pairs = (
            await db.execute(text("SELECT quote, text FROM citations JOIN source_chunks ON source_chunks.id = citations.chunk_id"))
        ).all()
        assert len(pairs) == 1  # the Persian quote is not in the English passage
        quote, passage = pairs[0]
        assert quote in normalize_evidence_text(passage)

    async def test_a_citation_cannot_name_another_sources_passage(self, db, make_settings):
        """``source_chunk.source_id == citation.source_id``, enforced by the database."""
        _user, run, source, _rows, other_source, other_rows = await seed_source(
            db, telegram_user_id=7417, extra_source=True
        )
        claim_row = await ClaimRepositoryImpl(db).add(run.id, "A claim text")

        with pytest.raises(IntegrityError):
            async with db.begin_nested():
                await CitationRepositoryImpl(db).add(
                    claim_row.id, source.id, other_rows[0].id, "their passage text"
                )
        assert other_source.id != source.id

    async def test_a_citation_cannot_be_written_without_a_quote(self, db):
        from app.repositories.research import CitationRepositoryImpl as Citations

        _user, run, source, rows, *_ = await seed_source(db, telegram_user_id=7418)
        claim_row = await ClaimRepositoryImpl(db).add(run.id, "A claim text")

        with pytest.raises(ClaimEvidenceError):
            await Citations(db).add(claim_row.id, source.id, rows[0].id, "")

        with pytest.raises(IntegrityError):
            async with db.begin_nested():
                await db.execute(
                    text(
                        "INSERT INTO citations (claim_id, source_id, chunk_id, location) "
                        "VALUES (:c, :s, :k, NULL)"
                    ),
                    {"c": claim_row.id, "s": source.id, "k": rows[0].id},
                )


# --- transactions -----------------------------------------------------------


class TestTransactionSemantics:
    def test_a_failure_after_the_first_claim_persists_nothing(
        self, clean_database, make_settings
    ):
        """Atomicity: the whole result commits or none of it does.

        Uses the ``clean_database`` fixture rather than ``db`` because the point is
        what a **rollback** leaves behind, and the ``db`` fixture holds an outer
        transaction that would hide the difference between a rolled-back write and a
        write that was never issued. Real commits matter here, so the body runs on
        the process's long-lived loop, as ``test_data_deletion`` does.

        The fault is injected into the *second* citation write, which is the last
        thing the stage does — so by then the summary and one claim row exist in the
        transaction. A non-atomic implementation would commit them.
        """
        settings = make_settings()

        async def scenario() -> None:
            async with clean_database.sessionmaker() as session:
                user, run, source, rows, *_ = await seed_source(
                    session, telegram_user_id=7420
                )
                llm = RecordingLLM(
                    [
                        claims_response(
                            claim(ENGLISH_STATEMENT, rows[0].id, ENGLISH_QUOTE),
                            claim(
                                "Also supported here.",
                                rows[0].id,
                                "the published methodology section",
                            ),
                        )
                    ]
                )
                service = SourceEvidenceService(
                    settings=settings,
                    summarizer=_FixedSummarizer("A summary that must not survive."),
                    extractor=LLMClaimExtractor(llm, settings),
                    uow_factory=lambda: _FailingCitationsUoW(session),
                )
                with pytest.raises(ClaimEvidenceError):
                    await service.analyze_source(
                        run_id=run.id, user_id=user.id, source_id=source.id
                    )
                await session.commit()

                # Committed state, read fresh: the stage wrote a summary and one
                # claim row inside the transaction, and none of it is here.
                assert await ClaimRepositoryImpl(session).list_for_run(run.id) == []
                assert (
                    await SourceRepositoryImpl(session).get(source.id)
                ).summary is None
                for table in ("claims", "citations"):
                    remaining = (
                        await session.execute(text(f"SELECT count(*) FROM {table}"))
                    ).scalar_one()
                    assert remaining == 0, f"{table} rows survived the rollback"

        run_on_process_loop(scenario())

    async def test_a_provider_failure_mid_way_writes_nothing(self, db, make_settings):
        """The second window's provider call fails: nothing is written at all.

        Verification and persistence happen after every window, so a mid-run failure
        cannot leave a partial claim set.
        """
        padding = " Additional methodological detail follows. " * 4
        settings = make_settings(CLAIM_MAX_INPUT_TOKENS=700, CLAIM_MAX_WINDOWS=4)
        user, run, source, rows, *_ = await seed_source(
            db,
            telegram_user_id=7421,
            passages=(ENGLISH_PASSAGE + padding, PERSIAN_PASSAGE + padding),
        )
        llm = RecordingLLM(
            [claims_response(claim(ENGLISH_STATEMENT, rows[0].id, ENGLISH_QUOTE))]
        )

        class _FailsOnSecondWindow:
            """Accepts the first window, fails the second.

            A mid-run provider failure is the interesting case: the first window's
            claims have already been *accepted* when the run dies, so a
            non-atomic implementation would persist them.
            """

            def __init__(self) -> None:
                self.calls = 0

            async def extract(self, source):
                self.calls += 1
                if self.calls == 2:
                    raise LLMServiceError("provider failed mid-run")
                return await LLMClaimExtractor(llm, settings).extract(source)

        extractor = _FailsOnSecondWindow()
        service = SourceEvidenceService(
            settings=settings,
            summarizer=_FixedSummarizer(),
            extractor=extractor,
            uow_factory=uow_over(db),
        )
        with pytest.raises(LLMServiceError):
            await service.analyze_source(run_id=run.id, user_id=user.id, source_id=source.id)
        await db.commit()

        assert extractor.calls == 2, "the second window really was attempted"
        assert await ClaimRepositoryImpl(db).list_for_run(run.id) == []
        assert (await load_source_evidence(run.id, user.id, source.id, uow_over(db)))[1] is None


# --- ownership --------------------------------------------------------------


class TestOwnership:
    async def test_analyzing_another_users_run_writes_nothing(self, db, make_settings):
        settings = make_settings()
        owner, run, source, _rows, *_ = await seed_source(db, telegram_user_id=7430)
        from app.repositories.users import UserRepositoryImpl

        intruder = await UserRepositoryImpl(db).get_or_create(7431, None)
        await db.commit()

        llm = RecordingLLM([claims_response()])
        service = SourceEvidenceService(
            settings=settings,
            summarizer=_FixedSummarizer(),
            extractor=LLMClaimExtractor(llm, settings),
            uow_factory=uow_over(db),
        )
        with pytest.raises(ResearchRunNotFoundError):
            await service.analyze_source(
                run_id=run.id, user_id=intruder.id, source_id=source.id
            )
        await db.commit()

        assert llm.call_count == 0
        assert (await load_source_evidence(run.id, owner.id, source.id, uow_over(db)))[1] is None

    async def test_another_runs_source_is_refused(self, db, make_settings):
        settings = make_settings()
        user, run, _source, _rows, *_ = await seed_source(db, telegram_user_id=7432)
        _u2, _other_run, foreign_source, *_ = await seed_source(
            db, telegram_user_id=7433, question="a different question"
        )

        llm = RecordingLLM([claims_response()])
        service = SourceEvidenceService(
            settings=settings,
            summarizer=_FixedSummarizer(),
            extractor=LLMClaimExtractor(llm, settings),
            uow_factory=uow_over(db),
        )
        with pytest.raises(ResearchSourceNotFoundError):
            await service.analyze_source(
                run_id=run.id, user_id=user.id, source_id=foreign_source.id
            )
        assert llm.call_count == 0


# --- deletion and migrations ------------------------------------------------


class TestDeletionStillCascades:
    async def test_deleting_the_user_removes_summaries_claims_and_citations(self, db, make_settings):
        settings = make_settings()
        user, run, source, rows, *_ = await seed_source(db, telegram_user_id=7440)
        llm = RecordingLLM([claims_response(claim(ENGLISH_STATEMENT, rows[0].id, ENGLISH_QUOTE))])
        service = SourceEvidenceService(
            settings=settings,
            summarizer=_FixedSummarizer(),
            extractor=LLMClaimExtractor(llm, settings),
            uow_factory=uow_over(db),
        )
        await service.analyze_source(run_id=run.id, user_id=user.id, source_id=source.id)
        await db.commit()

        assert (
            await db.execute(
                text(
                    "SELECT (SELECT count(*) FROM sources WHERE summary IS NOT NULL), "
                    "(SELECT count(*) FROM citations WHERE quote IS NOT NULL)"
                )
            )
        ).one() == (1, 1)

        await db.execute(text("DELETE FROM users WHERE id = :i"), {"i": user.id})
        await db.commit()

        for table in ("sources", "source_chunks", "claims", "citations", "research_runs"):
            remaining = (
                await db.execute(text(f"SELECT count(*) FROM {table}"))
            ).scalar_one()
            assert remaining == 0, f"{table} rows survived the cascade"


def rows_placeholder_id(rows):
    """The persisted passage ids, so a claim can address real rows."""
    return rows[0].id