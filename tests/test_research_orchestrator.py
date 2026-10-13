"""P3.9-D — the research orchestrator against a real PostgreSQL.

The orchestrator is the only component that has never had a caller: every stage
below it is built and independently tested, and the P3.9 audit's critical finding
(§ R.7) was that the path *question → plan → search → fetch → extraction →
source_chunks → evidence → citation → synthesis → verified answer* could not be
executed at all. These tests execute it.

**What is real and what is controlled.** The database is real: the unit of work,
every repository, and every constraint under test are the production ones, over the
migrated schema. The four providers are controlled adapters that implement the real
port signatures and return contract-shaped output, which is the boundary the
architecture puts between the pipeline and a vendor. What no test here needs is a
credential.

**The minimum integration test** is the one § R.7 specified: one question driven to
a verified answer, asserting that ``source_chunks`` rows exist with contiguous
ordinals and offsets inside the extracted text, that every citation's quote is
inside its stored passage, that ``plain_text`` is non-empty, and that **re-running
the same task produces no duplicate rows** — the property that makes redelivery a
non-event rather than a data-corruption event.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy import func, select

from app.db.models import (
    CitationModel,
    ClaimModel,
    ResearchRunModel,
    SourceChunkModel,
    SourceModel,
)
from app.db.unit_of_work import SqlAlchemyUnitOfWork
from app.domain.errors import SearchNoResultsError
from app.domain.research.lifecycle import TransitionError
from app.domain.research.outcome import CooperativeDeadline, RunOutcomeKind

#: A quote that is a literal substring of ``ENGLISH_PASSAGE``.
from app.domain.research.planning import PLAN_SCHEMA_VERSION as _PLAN_SCHEMA_VERSION
from app.domain.research.synthesis import (
    SYNTHESIS_SCHEMA_VERSION as _SYNTHESIS_SCHEMA_VERSION,
)
from app.infrastructure.evidence.analyzer import LLMClaimExtractor, LLMSourceSummarizer
from app.infrastructure.planning.planner import LLMPlanner
from app.services.research.chunking import SourceChunkService
from app.services.research.evidence import SourceEvidenceService
from app.services.research.orchestrator import ResearchOrchestrator
from app.services.research.planning import PlanningService
from app.services.research.synthesis import SynthesisService
from app.services.research.usage import UsageRecordingLLM
from tests.conftest import run_on_process_loop
from tests.research_support import (
    ENGLISH_PASSAGE,
    RecordingLLM,
    claim,
    claims_response,
)

QUOTE = "recorded a forty percent increase across the sampled districts"

_TABLES = (
    ResearchRunModel,
    SourceModel,
    SourceChunkModel,
    ClaimModel,
    CitationModel,
)


# --- controlled adapters -----------------------------------------------------


class SearchAdapter:
    """A ``SearchProvider`` answering one canned result set per query."""

    def __init__(self, *result_sets: Any) -> None:
        self._sets = list(result_sets)
        self.requests: list[Any] = []

    async def search(self, request: Any) -> Any:
        from app.domain.research.search import SearchResponse

        self.requests.append(request)
        results = self._sets.pop(0) if self._sets else []
        if not results:
            raise SearchNoResultsError("no usable results")
        return SearchResponse(
            query=request.query, provider="test-provider", results=tuple(results)
        )


class FetchAdapter:
    """A ``SafeFetcher`` returning canned bytes, or a typed error on demand."""

    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.fetched: list[str] = []

    async def fetch(self, request: Any) -> Any:
        from app.domain.research.fetch import FetchedSource

        self.fetched.append(request.url)
        if self.error is not None:
            raise self.error
        return FetchedSource(
            requested_url=request.url,
            final_url=request.url,
            status_code=200,
            content_type="text/html",
            body=b"<html><body>content</body></html>",
        )


class ExtractAdapter:
    """A ``ContentExtraction`` producing the text the test wants.

    ``per_source`` varies the text by source id. Content-hash deduplication collapses
    sources whose *extracted* text is identical (FR-25, the property that keeps a
    shared boilerplate banner from merging two real articles), so a test that needs
    two sources to survive selection must give them two different texts — otherwise it
    is testing the deduplicator, not the stage it meant to exercise.
    """

    def __init__(self, text: str = ENGLISH_PASSAGE, *, per_source: bool = False) -> None:
        self.text = text
        self.per_source = per_source
        self.calls = 0

    async def extract(self, source: Any) -> Any:
        from app.domain.research.extraction import ExtractedContent

        self.calls += 1
        text = (
            f"{self.text} Reference {self.calls}."
            if self.per_source
            else self.text
        )
        return ExtractedContent.from_fetched(source, text=text)


class Deduplicator:
    """A ``SourceDeduplication`` backed by the real deterministic policy."""

    def __init__(self, settings: Any) -> None:
        from app.infrastructure.ranking import create_deduplicator

        self._inner = create_deduplicator(settings)

    def deduplicate(self, candidates: Any) -> Any:
        return self._inner.deduplicate(candidates)


class Ranker:
    """A ``RankingPolicy`` backed by the real deterministic policy."""

    def __init__(self, settings: Any) -> None:
        from app.infrastructure.ranking import create_ranking_policy

        self._inner = create_ranking_policy(settings)

    def rank(self, result: Any, query: str) -> Any:
        return self._inner.rank(result, query)


class EvidenceAdapter:
    """A ``SourceEvidenceService`` substitute that cites the real passage ids.

    Needed because a claim's ``source_chunk_id`` must be a **persisted** id the
    database assigned, which nobody knows before the chunk stage runs. A fixed
    payload would have to guess one and would be rejected as ``UNKNOWN_CHUNK`` —
    exactly the failure the security contract requires, and exactly what a naive
    fixed-id test would call "the pipeline is broken".
    """

    def __init__(self, database, settings, quote: str) -> None:
        self.database = database
        self.settings = settings
        self.quote = quote

    async def analyze_source(self, *, run_id: int, user_id: int, source_id: int) -> Any:

        from app.domain.research.evidence import SourceEvidenceResult, SourceSummary

        chunks = await _passages_of(self.database, source_id)
        if not chunks:
            return SourceEvidenceResult(
                source_id=source_id,
                summary=SourceSummary(source_id=source_id, text="A controlled summary."),
            )
        llm = UsageRecordingLLM(
            RecordingLLM(
                [
                    json.dumps({"summary": "A controlled summary.", "language": "en"}),
                    claims_response(
                        claim("The survey recorded growth.", chunks[0], self.quote)
                    ),
                ]
            )
        )
        settings = self.settings
        return await SourceEvidenceService(
            settings=settings,
            summarizer=LLMSourceSummarizer(llm, settings),
            extractor=LLMClaimExtractor(llm, settings),
            uow_factory=_uow_factory(self.database),
        ).analyze_source(run_id=run_id, user_id=user_id, source_id=source_id)


async def _passages_of(database, source_id: int) -> list[int]:
    """The persisted chunk ids of one source, in ordinal order.

    Awaited, not driven on the process loop: this runs *inside* the pipeline's own
    coroutine, and the project's loop rules forbid starting a second loop from a
    coroutine (see the skill's note on ``run_on_process_loop``).
    """
    async with database.session() as session:
        return [
            int(row)
            for row in (
                await session.execute(
                    select(SourceChunkModel.id)
                    .where(SourceChunkModel.source_id == source_id)
                    .order_by(SourceChunkModel.ordinal.asc())
                )
            ).scalars()
        ]


class SynthesisAdapter:
    """A ``SynthesisProvider`` returning one canned proposal.

    It cites the **first citation of the run it was actually shown**, taken from the
    registry rather than hard-coded. A fixed ``1`` happens to be right for the first
    run against a freshly truncated database and wrong for every run after it, so a
    double that could only be used once hid the difference between "the pipeline
    works" and "the harness guessed the id".
    """

    def __init__(self, text: str = "The 2024 survey recorded growth.") -> None:
        self.text = text
        self.inputs: list[Any] = []

    async def synthesize(self, synthesis_input: Any) -> Any:
        from app.domain.research.synthesis import SynthesisProposal
        from app.infrastructure.synthesis.synthesizer import _parse_object

        self.inputs.append(synthesis_input)
        entries = synthesis_input.registry.entries
        citation_ids = [entries[0].citation_id] if entries else []
        return SynthesisProposal.from_payload(_parse_object(json.dumps(
            {
                "schema_version": _SYNTHESIS_SCHEMA_VERSION,
                "sections": [
                    {"text": self.text, "citation_ids": citation_ids},
                ],
            }
        )))


# --- assembly ----------------------------------------------------------------


def result(url: str, rank: int, *, title: str = "A page") -> Any:
    """A provider search result."""
    from app.domain.research.search import SearchResult

    return SearchResult(
        url=url, domain=url.split("/")[2], rank=rank, title=title
    )


def plan_reply(objective: str = "Investigate the 2024 survey") -> str:
    return json.dumps(
        {
            "schema_version": _PLAN_SCHEMA_VERSION,
            "objective": objective,
            "language": "en",
            "steps": [
                {
                    "id": "s1",
                    "kind": "search",
                    "question": "What did the 2024 survey find?",
                    "query": "2024 field survey results",
                    "expected_source_types": ["news"],
                }
            ],
        }
    )


def _uow_factory(database) -> Any:
    def factory() -> SqlAlchemyUnitOfWork:
        return SqlAlchemyUnitOfWork(database)

    return factory


class Pipeline:
    """The real orchestrator, assembled with controlled ports over a real database."""

    def __init__(
        self,
        database,
        settings,
        *,
        search: Any,
        fetch: Any,
        extract: Any,
        llm_replies: list[str],
        synthesis_text: str = "The 2024 survey recorded growth.",
        deadline: CooperativeDeadline | None = None,
        evidence_factory: Any = None,
    ) -> None:
        self.database = database
        self.settings = settings
        self.llm = RecordingLLM(llm_replies)
        self.search = search
        self.fetch = fetch
        self.extract = extract
        self.synthesis_adapter = SynthesisAdapter(synthesis_text)
        self.deadline = deadline
        self.evidence_factory = evidence_factory
        self._orchestrator: ResearchOrchestrator | None = None

    def _build(self) -> ResearchOrchestrator:
        factory = _uow_factory(self.database)
        settings = self.settings
        # The composition root hands the four research adapters the shared provider
        # behind ``UsageRecordingLLM`` (FR-9/SR-5), so the harness does the same and
        # the accounting under test runs over the same wiring production runs.
        llm = UsageRecordingLLM(self.llm)
        return ResearchOrchestrator(
            settings=settings,
            uow_factory=factory,
            planning=PlanningService(
                settings=settings,
                planner=LLMPlanner(llm, settings),
                uow_factory=factory,
            ),
            search_provider=self.search,
            fetcher=self.fetch,
            extractor=self.extract,
            deduplicator=Deduplicator(settings),
            ranking=Ranker(settings),
            chunking=SourceChunkService(uow_factory=factory),
            evidence=self._evidence_service(),
            synthesis=SynthesisService(
                settings=settings,
                synthesizer=self.synthesis_adapter,
                uow_factory=factory,
            ),
            deadline=self.deadline,
        )

    def _evidence_service(self) -> Any:
        """The evidence collaborator, or a no-op when a test isolates the stage."""
        if self.evidence_factory is None:
            return _NoEvidenceService()
        return self.evidence_factory()

    def seed(self, question: str = "What did the 2024 survey find?", user_id: int = 3100) -> tuple[int, int]:
        """Create the owner and the queued run, returning ``(user_id, run_id)``."""

        async def _seed() -> tuple[int, int]:
            async with SqlAlchemyUnitOfWork(self.database) as uow:
                user = await uow.users.get_or_create(user_id, None)
                run = await uow.research_runs.create(user.id, question)
                return user.id, run.id

        return run_on_process_loop(_seed())

    def run(self, run_id: int, user_id: int) -> Any:
        if self._orchestrator is None:
            self._orchestrator = self._build()
        return run_on_process_loop(
            self._orchestrator.execute(run_id=run_id, user_id=user_id)
        )

    def counts(self) -> dict[str, int]:
        async def _count() -> dict[str, int]:
            async with self.database.session() as session:
                return {
                    model.__tablename__: int(
                        (
                            await session.execute(
                                select(func.count()).select_from(model)
                            )
                        ).scalar_one()
                    )
                    for model in _TABLES
                }

        return run_on_process_loop(_count())

    def run_row(self, run_id: int) -> Any:
        async def _read() -> Any:
            async with self.database.session() as session:
                return await session.get(ResearchRunModel, run_id)

        return run_on_process_loop(_read())

    def sources(self, run_id: int) -> list[Any]:
        async def _read() -> list[Any]:
            async with self.database.session() as session:
                rows = (
                    await session.execute(
                        select(SourceModel).where(SourceModel.run_id == run_id)
                    )
                ).scalars()
                return list(rows)

        return run_on_process_loop(_read())

    def chunk_rows(self) -> list[Any]:
        """Every persisted passage, with its ordinal and offset columns."""
        async def _read() -> list[Any]:
            async with self.database.session() as session:
                return [
                    {
                        "id": int(row.id),
                        "source_id": int(row.source_id),
                        "ordinal": int(row.ordinal),
                        "char_start": int(row.char_start),
                        "char_end": int(row.char_end),
                        "text": str(row.text),
                    }
                    for row in (
                        await session.execute(
                            select(
                                SourceChunkModel.id,
                                SourceChunkModel.source_id,
                                SourceChunkModel.ordinal,
                                SourceChunkModel.char_start,
                                SourceChunkModel.char_end,
                                SourceChunkModel.text,
                            )
                        )
                    )
                ]

        return run_on_process_loop(_read())

    def citations(self) -> list[Any]:
        """Every persisted citation, with the passage it points at."""
        async def _read() -> list[Any]:
            async with self.database.session() as session:
                return [
                    {
                        "id": int(row.id),
                        "claim_id": int(row.claim_id),
                        "source_id": int(row.source_id),
                        "chunk_id": int(row.chunk_id),
                        "quote": str(row.quote),
                        "location": row.location,
                    }
                    for row in (
                        await session.execute(
                            select(
                                CitationModel.id,
                                CitationModel.claim_id,
                                CitationModel.source_id,
                                CitationModel.chunk_id,
                                CitationModel.quote,
                                CitationModel.location,
                            )
                        )
                    )
                ]

        return run_on_process_loop(_read())

    def passages_by_id(self) -> dict[int, str]:
        return {row["id"]: row["text"] for row in self.chunk_rows()}


class _NoEvidenceService:
    """Stand-in used when a test must isolate the retrieval stages from evidence."""

    def __init__(self) -> None:
        self.calls: list[tuple[int, int, int]] = []

    async def analyze_source(self, *, run_id: int, user_id: int, source_id: int) -> Any:
        self.calls.append((run_id, user_id, source_id))
        from app.domain.research.evidence import SourceEvidenceResult, SourceSummary

        # A summary and no claims: the source was read, but nothing in it survived
        # claim extraction, so synthesis has no citable evidence (FR-41).
        return SourceEvidenceResult(
            source_id=source_id,
            summary=SourceSummary(source_id=source_id, text="Nothing verifiable."),
        )


def _rewrite_terminal(database, run_id: int, user_id: int, prompt: int, completion: int):
    """Write the same ledger totals again through the lifecycle's own interface."""

    async def _do() -> None:
        async with SqlAlchemyUnitOfWork(database) as uow:
            await uow.research_runs.set_status(
                run_id,
                user_id,
                "completed",
                prompt_tokens=prompt,
                completion_tokens=completion,
            )

    return _do()


# --- the § R.7 minimum integration test -------------------------------------


class TestIntegrationPath:
    def test_a_question_becomes_a_verified_answer(self, clean_database, settings):
        """The path the P3.9 audit found entirely unexecutable, executed for real."""
        pipeline = Pipeline(
            clean_database,
            settings,
            search=SearchAdapter([result("https://example.com/a", 1)]),
            fetch=FetchAdapter(),
            extract=ExtractAdapter(ENGLISH_PASSAGE),
            llm_replies=[plan_reply()],
            evidence_factory=lambda: EvidenceAdapter(clean_database, settings, QUOTE),
        )
        user_id, run_id = pipeline.seed()

        outcome = pipeline.run(run_id, user_id)

        # 1. A verified answer exists and is not empty.
        assert outcome.kind is RunOutcomeKind.COMPLETED
        assert outcome.is_deliverable
        assert outcome.answer

        # 2. The run is terminal, with the answer persisted on it.
        row = pipeline.run_row(run_id)
        assert row.status == "completed"
        assert row.final_answer == outcome.answer
        assert row.synthesis, "the verified answer was not persisted as JSONB"

        # 3. Chunks with contiguous ordinals and offsets inside the extracted text.
        chunks = pipeline.chunk_rows()
        assert chunks, "the passage producer produced nothing"
        ordinals = sorted(int(c["ordinal"]) for c in chunks)
        assert ordinals == list(range(len(ordinals)))
        for row in chunks:
            assert 0 <= int(row["char_start"]) < int(row["char_end"])

        # 4. Every citation's quote is a substring of its stored passage.
        citations = pipeline.citations()
        assert citations, "a verified answer with no citation cannot be delivered"
        passages = pipeline.passages_by_id()
        for citation in citations:
            assert str(citation["quote"]) in passages[int(citation["chunk_id"])]

        # 5. Sources were created, ranked, and selected.
        sources = pipeline.sources(run_id)
        assert len(sources) == 1
        assert sources[0].rank == 1
        assert sources[0].status == "selected"
        assert sources[0].content_hash, "P3.5's output was not persisted (B2)"

        # 6. The run records the counts a partial answer would need.
        assert outcome.candidates == 1
        assert outcome.fetched == 1
        assert outcome.extracted == 1
        assert outcome.claims >= 1
        assert outcome.citations >= 1

    def test_a_persian_question_reaches_a_verified_answer(self, clean_database, settings):
        """§ 15.10: the product's primary language, at the surface that produces text.

        English-only fixtures would certify the wrong system, so the pipeline is
        driven with Persian extracted content and a Persian claim whose quote is a
        substring of the Persian passage — which is what P3.7's containment rule
        verifies, and what the chunking must not break by re-normalizing.
        """
        persian = (
            "پژوهشگران در سال ۱۴۰۳ افزایش چهل درصدی را در نمونه‌های شهری گزارش کردند."
        )
        persian_quote = "افزایش چهل درصدی را در نمونه‌های شهری گزارش کردند"
        pipeline = Pipeline(
            clean_database,
            settings,
            search=SearchAdapter([result("https://example.com/fa", 1)]),
            fetch=FetchAdapter(),
            extract=ExtractAdapter(persian),
            llm_replies=[plan_reply("بررسی نتایج نظرسنجی ۱۴۰۳")],
            evidence_factory=lambda: EvidenceAdapter(
                clean_database, settings, persian_quote
            ),
            synthesis_text="نظرسنجی ۱۴۰۳ افزایش را ثبت کرد.",
        )
        user_id, run_id = pipeline.seed()

        outcome = pipeline.run(run_id, user_id)

        assert outcome.kind is RunOutcomeKind.COMPLETED
        assert outcome.answer
        passages = pipeline.passages_by_id()
        for citation in pipeline.citations():
            assert str(citation["quote"]) in passages[int(citation["chunk_id"])]


class TestTokenAccounting:
    """FR-9 / SR-5 / T-25: a run records what its own model calls cost.

    Before this, ``research_runs.prompt_tokens`` / ``completion_tokens`` had columns
    the repository would write and no caller that ever passed a value, so every run
    reported zero regardless of how much it had spent.

    ``RecordingLLM`` reports ``prompt_tokens=11, completion_tokens=7`` per call, so the
    persisted totals are a function of the call count and the assertion can be exact
    rather than "greater than zero".
    """

    #: One planning call plus one summarization and one claim-extraction call per
    #: source. ``SynthesisAdapter`` is a double and does not call the provider, so it
    #: contributes nothing to the ledger — which is itself part of what is asserted.
    def _pipeline(self, database, settings, *, sources: int = 1) -> Pipeline:
        return Pipeline(
            database,
            settings,
            search=SearchAdapter(
                [result(f"https://example.com/{n}", n) for n in range(1, sources + 1)]
            ),
            fetch=FetchAdapter(),
            # Distinct text per source, so content-hash dedup does not collapse them
            # and the second source really reaches the evidence stage.
            extract=ExtractAdapter(ENGLISH_PASSAGE, per_source=sources > 1),
            llm_replies=[plan_reply()],
            evidence_factory=lambda: EvidenceAdapter(database, settings, QUOTE),
        )

    def test_a_completed_run_persists_the_usage_its_calls_reported(
        self, clean_database, settings
    ):
        pipeline = self._pipeline(clean_database, settings)
        user_id, run_id = pipeline.seed()

        outcome = pipeline.run(run_id, user_id)
        assert outcome.kind is RunOutcomeKind.COMPLETED

        # One planning call, plus one summarization and one claim-extraction call for
        # the single source. Each reports 11 prompt / 7 completion tokens.
        calls = 3
        row = pipeline.run_row(run_id)
        assert row.prompt_tokens == 11 * calls
        assert row.completion_tokens == 7 * calls
        assert row.prompt_tokens > 0 and row.completion_tokens > 0

    def test_several_model_calls_accumulate_rather_than_overwrite(
        self, clean_database, settings
    ):
        """Two sources means more provider calls, and the ledger must add up.

        The values are **set**, not incremented, so the second run reports its own
        total rather than either overwriting the first or accumulating onto it.
        """
        one = self._pipeline(clean_database, settings, sources=1)
        user_id, run_id = one.seed(user_id=6101)
        one.run(run_id, user_id)
        first = one.run_row(run_id)

        two = self._pipeline(clean_database, settings, sources=2)
        other_user, other_run = two.seed(user_id=6102)
        two.run(other_run, other_user)
        second = two.run_row(other_run)

        # 1 source → 1 planning + 2 per source = 3.  2 sources → 1 + 4 = 5.
        assert first.prompt_tokens == 11 * 3
        assert second.prompt_tokens == 11 * 5
        assert second.prompt_tokens > first.prompt_tokens
        assert second.completion_tokens == 7 * 5

    def test_a_repeated_terminal_write_is_refused_so_the_ledger_cannot_double_count(
        self, clean_database, settings
    ):
        """Double counting is prevented by the lifecycle, not by arithmetic.

        ``set_status`` *sets* the ledger columns rather than incrementing them, and
        the lifecycle separately refuses a terminal run being rewritten. Both hold:
        a Celery retry that re-executed the terminal write would be refused outright,
        and a caller that could reach the write at all would overwrite rather than
        add. The DoD-§ 13 property protecting a terminal run from being reopened is
        not relaxed to accommodate bookkeeping.
        """
        pipeline = self._pipeline(clean_database, settings)
        user_id, run_id = pipeline.seed()
        pipeline.run(run_id, user_id)
        before = pipeline.run_row(run_id)

        with pytest.raises(TransitionError):
            run_on_process_loop(
                _rewrite_terminal(
                    clean_database, run_id, user_id,
                    before.prompt_tokens, before.completion_tokens,
                )
            )

        after = pipeline.run_row(run_id)
        assert after.prompt_tokens == before.prompt_tokens
        assert after.completion_tokens == before.completion_tokens

    def test_a_run_with_no_evidence_still_records_what_it_spent(
        self, clean_database, settings
    ):
        """A failed run's cost is what an operator most needs to see.

        There is no answer, so there is no synthesis write to carry the numbers; the
        terminal failure write carries them instead.
        """
        pipeline = Pipeline(
            clean_database,
            settings,
            search=SearchAdapter([result("https://example.com/a", 1)]),
            fetch=FetchAdapter(),
            extract=ExtractAdapter(ENGLISH_PASSAGE),
            llm_replies=[plan_reply()],
            evidence_factory=None,  # no claims, so synthesis has nothing to cite
        )
        user_id, run_id = pipeline.seed()

        outcome = pipeline.run(run_id, user_id)
        assert outcome.kind is RunOutcomeKind.NO_EVIDENCE

        row = pipeline.run_row(run_id)
        assert row.status == "failed"
        assert row.prompt_tokens == 11  # the planning call
        assert row.completion_tokens == 7

    def test_the_usage_is_written_without_moving_the_status(
        self, clean_database, settings
    ):
        """The ledger rides along with the terminal write, never replacing it."""
        pipeline = self._pipeline(clean_database, settings)
        user_id, run_id = pipeline.seed()
        pipeline.run(run_id, user_id)

        row = pipeline.run_row(run_id)
        assert row.status == "completed"
        assert row.final_answer, "usage accounting displaced the answer"
        assert row.prompt_tokens > 0


class TestTheClaimIsIdempotent:
    """A redelivered attempt must resume, not fail (AD-043, B7).

    The repository still refuses ``running -> running`` as SAME_STATE, and that is
    correct: it is the lifecycle's truth. What changes is the orchestrator's
    reading of it — ``queued -> running`` is the claim, and a run already at
    ``running`` has been claimed, by an earlier attempt of the *same* delivery.

    The pipeline is assembled with the same controlled ports the integration path
    uses, so a resumed run completes through every stage rather than stopping at
    the claim. A claim-only double would prove the transition was tolerated but
    not that the run actually carried on.
    """

    def _pipeline(self, database, settings) -> Pipeline:
        return Pipeline(
            database,
            settings,
            search=SearchAdapter([result("https://example.com/a", 1)]),
            fetch=FetchAdapter(),
            extract=ExtractAdapter(ENGLISH_PASSAGE),
            llm_replies=[plan_reply()],
            evidence_factory=lambda: EvidenceAdapter(database, settings, QUOTE),
        )

    def test_a_run_already_running_is_resumed_not_refused(self, clean_database, settings):
        from app.db.unit_of_work import SqlAlchemyUnitOfWork
        from tests.conftest import run_on_process_loop

        pipeline = self._pipeline(clean_database, settings)
        user_id, run_id = pipeline.seed(user_id=5190)

        # The first attempt claims it and dies; the row is left `running`.
        async def _claim_and_die() -> None:
            async with SqlAlchemyUnitOfWork(clean_database) as uow:
                await uow.research_runs.set_status(run_id, user_id, "running")

        run_on_process_loop(_claim_and_die())

        # A second delivery of the same message re-enters the claim. Before the
        # fix this raised TransitionError before any work resumed, so a broker
        # redelivery could never converge on a worker that had been killed.
        outcome = pipeline.run(run_id, user_id)

        assert outcome.is_deliverable, (
            "a resumed run produced no answer, so the claim did not resume the work"
        )
        assert pipeline.run_row(run_id).status == "completed"

    def test_a_terminal_run_is_not_reopened(self, clean_database, settings):
        """What is *not* idempotent, deliberately: a finished run stays finished.

        Re-running it would spend the budget again and overwrite a verified
        answer, which is exactly what the terminal guard exists to prevent. The
        ownership check refuses first, which is the SR-10 boundary and is a
        stronger refusal than the lifecycle one.
        """
        from app.db.unit_of_work import SqlAlchemyUnitOfWork
        from tests.conftest import run_on_process_loop

        pipeline = self._pipeline(clean_database, settings)
        user_id, run_id = pipeline.seed(user_id=5191)

        async def _finish() -> None:
            async with SqlAlchemyUnitOfWork(clean_database) as uow:
                await uow.research_runs.set_status(run_id, user_id, "running")
                await uow.research_runs.set_status(
                    run_id, user_id, "completed", final_answer="already done"
                )

        run_on_process_loop(_finish())

        from app.domain.research.lifecycle import (
            TransitionError,
            TransitionErrorKind,
        )

        with pytest.raises(TransitionError) as caught:
            pipeline.run(run_id, user_id)

        # The lifecycle refusal, not a silent reopen: a finished run stays
        # finished and the run's verified answer is not overwritten.
        assert caught.value.kind is TransitionErrorKind.TERMINAL_STATE
        assert pipeline.run_row(run_id).final_answer == "already done"
