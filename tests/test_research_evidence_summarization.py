"""Per-source summarization (P3.7, FR-31).

Two halves:

* the **adapter**, driven by a fake Phase 2 provider, covering the response
  contract: what it accepts, what it refuses, and how many calls it makes;
* the **service**, against real PostgreSQL repositories, covering the ordering
  properties that matter for FR-31 — one source at a time, no cross-source
  content, no fetch, no tools, nothing written on failure, and ownership settled
  before anything is spent.

The evidence side of the stage lives in ``test_research_evidence_claims.py`` and
the injection boundary in ``test_research_evidence_security.py``.
"""

from __future__ import annotations

import pytest

from app.domain.chat import Role
from app.domain.errors import (
    LLMServiceError,
    LLMTimeoutError,
    ResearchRunNotFoundError,
    ResearchSourceNotFoundError,
    SourceContentEmptyError,
    SummarySchemaError,
)
from app.domain.research.evidence import (
    EvidenceChunk,
    SourceContent,
    SourceSummary,
)
from app.domain.research.ports import SourceSummarizer
from app.infrastructure.evidence.analyzer import (
    LLMSourceSummarizer,
    create_source_summarizer,
)
from app.infrastructure.evidence.prompts import (
    SOURCE_DATA_CLOSE,
    SOURCE_DATA_OPEN,
    SUMMARIZATION_PROMPT_VERSION,
    SUMMARIZATION_SYSTEM_PROMPT,
    build_summary_system_prompt,
    prompt_fingerprint,
)
from app.services.research.evidence import SourceEvidenceService, load_source_evidence
from tests.research_support import (
    ENGLISH_PASSAGE,
    PERSIAN_PASSAGE,
    RecordingLLM,
    claim,
    claims_response,
    seed_source,
    summary_response,
    uow_over,
)

# A quote that is a literal substring of ``ENGLISH_PASSAGE``, and a statement for a
# claim citing it. Defined locally rather than shared, because the P3.7 modules each
# assert on the same passage and a shared constant would let a change in one module
# silently weaken another.
ENGLISH_QUOTE = "recorded a forty percent increase across the sampled districts"
ENGLISH_STATEMENT = "The survey recorded a forty percent increase."


def content(*chunks: EvidenceChunk, source_id: int = 1, domain: str = "example.com", title: str | None = "A field survey") -> SourceContent:
    return SourceContent(
        source_id=source_id, domain=domain, title=title, chunks=chunks or (EvidenceChunk(chunk_id=10, ordinal=0, text=ENGLISH_PASSAGE),)
    )


def summarizer(replies: list[str], settings, **kwargs) -> tuple[LLMSourceSummarizer, RecordingLLM]:
    llm = RecordingLLM(replies, **kwargs)
    return LLMSourceSummarizer(llm, settings), llm


# --- the adapter ------------------------------------------------------------


class TestSummarizerAdapter:
    async def test_a_valid_response_becomes_a_summary_of_that_source(self, settings):
        adapter, _llm = summarizer([summary_response("The page describes one survey.", "en")], settings)
        result = await adapter.summarize(content())
        assert result.text == "The page describes one survey."
        assert result.language == "en"
        assert result.source_id == 1

    async def test_the_source_id_comes_from_the_content_not_the_response(self, settings):
        adapter, _ = summarizer([summary_response("A summary.")], settings)
        assert (await adapter.summarize(content(source_id=42))).source_id == 42

    async def test_a_response_that_names_its_own_source_is_refused(self, settings):
        """The contract has no ``source_id`` field, so a page cannot claim one."""
        adapter, _ = summarizer(['{"summary": "s", "source_id": 999}'], settings)
        with pytest.raises(SummarySchemaError):
            await adapter.summarize(content(source_id=42))

    async def test_the_two_messages_are_instructions_then_data(self, settings):
        adapter, llm = summarizer([summary_response("A summary.")], settings)
        await adapter.summarize(content())
        messages = llm.calls[0]["messages"]
        assert [m.role for m in messages] == [Role.SYSTEM, Role.USER]
        assert messages[0].content == SUMMARIZATION_SYSTEM_PROMPT.format(passage_count=1)

    async def test_the_prompt_carries_the_passage_text_and_its_id(self, settings):
        adapter, llm = summarizer([summary_response("A summary.")], settings)
        await adapter.summarize(content())
        user = llm.calls[0]["messages"][1].content
        assert ENGLISH_PASSAGE in user
        assert "id=10" in user, "the model must be shown which id it may cite"
        assert SOURCE_DATA_OPEN in user and SOURCE_DATA_CLOSE in user

    async def test_exactly_one_provider_call_per_summary(self, settings):
        adapter, llm = summarizer([summary_response("A summary.")], settings)
        await adapter.summarize(content())
        assert llm.call_count == 1

    async def test_the_configured_model_and_token_bound_are_used(self, settings):
        adapter, llm = summarizer([summary_response("A summary.")], settings)
        await adapter.summarize(content())
        assert llm.calls[0]["model"] == settings.LLM_MODEL
        assert llm.calls[0]["max_tokens"] == settings.SUMMARY_MAX_TOKENS
        assert llm.calls[0]["temperature"] == settings.LLM_TEMPERATURE

    async def test_a_persian_source_produces_a_persian_summary(self, settings):
        persian = "این صفحه یک گزارش میدانی دربارهٔ رشد شهری است."
        adapter, _ = summarizer([summary_response(persian, "fa")], settings)
        result = await adapter.summarize(
            content(EvidenceChunk(chunk_id=11, ordinal=0, text=PERSIAN_PASSAGE), source_id=3)
        )
        assert result.text == persian
        assert result.language == "fa"

    async def test_a_fenced_response_is_accepted(self, settings):
        adapter, _ = summarizer(['```json\n{"summary": "A summary."}\n```'], settings)
        assert (await adapter.summarize(content())).text == "A summary."

    @pytest.mark.parametrize(
        "reply",
        [
            "",
            "   ",
            "not json at all",
            '["a list"]',
            '"a string"',
            "Here is the summary you asked for: {}",
            '{"language": "en"}',
            '{"summary": "s", "extra": true}',
            '{"summary": "s", "verified": true}',
        ],
    )
    async def test_a_response_that_is_not_the_contract_is_refused(self, settings, reply):
        adapter, llm = summarizer([reply], settings)
        with pytest.raises(SummarySchemaError):
            await adapter.summarize(content())
        assert llm.call_count == 1, "a malformed answer must not be retried here"

    async def test_prose_around_the_json_is_refused(self, settings):
        adapter, _ = summarizer(['{"summary": "s"}\nHope this helps!'], settings)
        with pytest.raises(SummarySchemaError):
            await adapter.summarize(content())

    async def test_an_unusable_summary_value_is_a_validation_error(self, settings):
        adapter, _ = summarizer(['{"summary": "   "}'], settings)
        from app.domain.errors import SummaryValidationError

        with pytest.raises(SummaryValidationError):
            await adapter.summarize(content())

    @pytest.mark.parametrize(
        "error", [LLMTimeoutError("slow"), LLMServiceError("boom"), LLMServiceError("auth")]
    )
    async def test_provider_failures_propagate_untouched(self, settings, error):
        """Re-typing them would lose the ``retryable`` flag the task policy reads."""
        adapter, _ = summarizer([], settings, error=error)
        with pytest.raises(type(error)) as excinfo:
            await adapter.summarize(content())
        assert excinfo.value.retryable == error.retryable

    def test_the_adapter_satisfies_the_port(self, settings):
        llm = RecordingLLM([])
        assert isinstance(LLMSourceSummarizer(llm, settings), SourceSummarizer)

    def test_the_factory_uses_the_injected_provider(self, settings):
        llm = RecordingLLM([])
        adapter = create_source_summarizer(settings, llm)
        assert isinstance(adapter, LLMSourceSummarizer)
        assert adapter._llm is llm, "the stage must not build a second provider client"

    def test_the_prompt_is_versioned_and_fingerprinted(self):
        assert SUMMARIZATION_PROMPT_VERSION == "source-summarization-v1"
        digest = prompt_fingerprint(SUMMARIZATION_SYSTEM_PROMPT)
        assert len(digest) == 16 and digest == prompt_fingerprint(SUMMARIZATION_SYSTEM_PROMPT)

    def test_the_system_prompt_builder_takes_no_source_content(self):
        import inspect

        parameters = inspect.signature(build_summary_system_prompt).parameters
        assert [p.name for p in parameters.values()] == ["passage_count"]
        assert parameters["passage_count"].kind is inspect.Parameter.KEYWORD_ONLY


# --- the service ------------------------------------------------------------


def service_for(db, settings, summarizer_obj, extractor_obj):
    return SourceEvidenceService(
        settings=settings,
        summarizer=summarizer_obj,
        extractor=extractor_obj,
        uow_factory=uow_over(db),
    )


class _NoClaims:
    """An extractor that finds nothing, for the summarization tests.

    It answers for whatever source it is handed, so a hard-coded id cannot make a
    summarization test fail for the wrong reason.
    """

    def __init__(self) -> None:
        self.sources: list[SourceContent] = []

    async def extract(self, source: SourceContent):
        from app.domain.research.evidence import ClaimExtraction

        self.sources.append(source)
        return ClaimExtraction(source_id=source.source_id, candidates=())


class TestSummarizationService:
    async def test_the_summary_of_one_source_is_persisted(self, db, make_settings):
        user, run, source, _rows, _other, _other_rows = await seed_source(db, telegram_user_id=7101)
        llm = RecordingLLM([summary_response("The page describes one survey.")])
        service = service_for(
            db, make_settings(), LLMSourceSummarizer(llm, make_settings()), _NoClaims()
        )

        result = await service.analyze_source(run_id=run.id, user_id=user.id, source_id=source.id)
        await db.commit()

        assert result.summary.text == "The page describes one survey."
        assert (await load_source_evidence(run.id, user.id, source.id, uow_over(db)))[1] == (
            "The page describes one survey."
        )

    async def test_a_persian_summary_round_trips_through_the_database(self, db, make_settings):
        persian = "این صفحه یک گزارش میدانی دربارهٔ رشد شهری است."
        user, run, source, _rows, *_ = await seed_source(
            db, telegram_user_id=7102, passages=(PERSIAN_PASSAGE,), title="گزارش پژوهش"
        )
        llm = RecordingLLM([summary_response(persian, "fa")])
        service = service_for(
            db, make_settings(), LLMSourceSummarizer(llm, make_settings()), _NoClaims()
        )

        await service.analyze_source(run_id=run.id, user_id=user.id, source_id=source.id)
        await db.commit()
        loaded, stored = await load_source_evidence(run.id, user.id, source.id, uow_over(db))
        assert stored == persian, "Persian must be stored as Persian, not escaped"
        assert loaded.summary == persian
        assert "نمونه‌های" in loaded.title or loaded.title is not None

    async def test_only_the_asked_source_is_sent_to_the_model(self, db, make_settings):
        user, run, source, rows, other_source, other_rows = await seed_source(
            db,
            telegram_user_id=7103,
            passages=(ENGLISH_PASSAGE, PERSIAN_PASSAGE),
            extra_source=True,
        )
        llm = RecordingLLM([summary_response("One page.")])
        service = service_for(
            db, make_settings(), LLMSourceSummarizer(llm, make_settings()), _NoClaims()
        )

        await service.analyze_source(run_id=run.id, user_id=user.id, source_id=source.id)
        sent = llm.calls[0]["messages"][1].content
        assert all(row.text in sent for row in rows)
        assert not any(row.text in sent for row in other_rows)
        assert f"id={other_rows[0].id}" not in sent, "another source's passage id leaked"
        assert other_source.id != source.id

    async def test_a_second_source_is_summarized_from_its_own_passages_only(self, db, make_settings):
        user, run, _source, rows, other_source, other_rows = await seed_source(
            db, telegram_user_id=7104, passages=(ENGLISH_PASSAGE,), extra_source=True
        )
        llm = RecordingLLM([summary_response("Second page.")])
        service = service_for(
            db, make_settings(), LLMSourceSummarizer(llm, make_settings()), _NoClaims()
        )

        result = await service.analyze_source(
            run_id=run.id, user_id=user.id, source_id=other_source.id
        )
        sent = llm.calls[0]["messages"][1].content
        assert result.source_id == other_source.id
        assert result.summary.source_id == other_source.id
        assert all(row.text in sent for row in other_rows)
        assert not any(row.text in sent for row in rows)

    async def test_an_empty_source_never_reaches_the_model(self, db, make_settings):
        user, run, source, _rows, *_ = await seed_source(
            db, telegram_user_id=7105, passages=(ENGLISH_PASSAGE,)
        )
        # Delete the passages: P3.4 found the source unusable, and P3.7 must not
        # invent a summary for it.
        from sqlalchemy import delete

        from app.db.models import SourceChunkModel

        await db.execute(delete(SourceChunkModel).where(SourceChunkModel.source_id == source.id))
        await db.commit()

        llm = RecordingLLM([summary_response("never used")])
        service = service_for(
            db, make_settings(), LLMSourceSummarizer(llm, make_settings()), _NoClaims()
        )
        with pytest.raises(SourceContentEmptyError):
            await service.analyze_source(run_id=run.id, user_id=user.id, source_id=source.id)
        assert llm.call_count == 0

    async def test_an_empty_source_writes_nothing(self, db, make_settings):
        user, run, source, _rows, *_ = await seed_source(db, telegram_user_id=7106)
        from sqlalchemy import delete

        from app.db.models import SourceChunkModel

        await db.execute(delete(SourceChunkModel).where(SourceChunkModel.source_id == source.id))
        await db.commit()

        llm = RecordingLLM([summary_response("never used")])
        service = service_for(
            db, make_settings(), LLMSourceSummarizer(llm, make_settings()), _NoClaims()
        )
        with pytest.raises(SourceContentEmptyError):
            await service.analyze_source(run_id=run.id, user_id=user.id, source_id=source.id)
        await db.commit()

        assert (await load_source_evidence(run.id, user.id, source.id, uow_over(db)))[1] is None

    async def test_a_foreign_run_is_refused_before_any_model_call(self, db, make_settings):
        owner, run, source, _rows, *_ = await seed_source(db, telegram_user_id=7107)
        from app.repositories.users import UserRepositoryImpl

        intruder = await UserRepositoryImpl(db).get_or_create(7108, None)
        await db.commit()

        llm = RecordingLLM([summary_response("never used")])
        service = service_for(
            db, make_settings(), LLMSourceSummarizer(llm, make_settings()), _NoClaims()
        )
        with pytest.raises(ResearchRunNotFoundError):
            await service.analyze_source(
                run_id=run.id, user_id=intruder.id, source_id=source.id
            )
        assert llm.call_count == 0
        assert owner.id != intruder.id

    async def test_a_source_from_another_run_is_refused_before_any_model_call(self, db, make_settings):
        user, run, _source, _rows, *_ = await seed_source(db, telegram_user_id=7109)
        _u2, _other_run, foreign_source, *_rest = await seed_source(
            db, telegram_user_id=7110, question="a different question"
        )

        llm = RecordingLLM([summary_response("never used")])
        service = service_for(
            db, make_settings(), LLMSourceSummarizer(llm, make_settings()), _NoClaims()
        )
        with pytest.raises(ResearchSourceNotFoundError):
            await service.analyze_source(
                run_id=run.id, user_id=user.id, source_id=foreign_source.id
            )
        assert llm.call_count == 0

    async def test_a_provider_failure_writes_no_summary(self, db, make_settings):
        user, run, source, _rows, *_ = await seed_source(db, telegram_user_id=7111)
        llm = RecordingLLM([], error=LLMServiceError("provider down"))
        service = service_for(
            db, make_settings(), LLMSourceSummarizer(llm, make_settings()), _NoClaims()
        )
        with pytest.raises(LLMServiceError):
            await service.analyze_source(run_id=run.id, user_id=user.id, source_id=source.id)
        await db.commit()

        assert (await load_source_evidence(run.id, user.id, source.id, uow_over(db)))[1] is None

    async def test_a_malformed_response_writes_no_summary_and_is_not_retried(self, db, make_settings):
        user, run, source, _rows, *_ = await seed_source(db, telegram_user_id=7112)
        llm = RecordingLLM(["not json"])
        service = service_for(
            db, make_settings(), LLMSourceSummarizer(llm, make_settings()), _NoClaims()
        )
        with pytest.raises(SummarySchemaError):
            await service.analyze_source(run_id=run.id, user_id=user.id, source_id=source.id)
        await db.commit()

        assert llm.call_count == 1
        assert (await load_source_evidence(run.id, user.id, source.id, uow_over(db)))[1] is None

    async def test_a_long_source_gets_a_bounded_summary_and_says_it_was_bounded(
        self, db, make_settings
    ):
        """Truncation is recorded, never silent: a summary of half a source must say so."""
        long_passages = tuple(
            f"passage number {i}: {'detail ' * 20}" for i in range(8)
        )
        user, run, source, rows, *_ = await seed_source(
            db, telegram_user_id=7113, passages=long_passages
        )
        settings = make_settings(SUMMARY_MAX_INPUT_TOKENS=900)
        llm = RecordingLLM([summary_response("Partial summary.")])
        service = service_for(
            db, settings, LLMSourceSummarizer(llm, settings), _NoClaims()
        )

        result = await service.analyze_source(run_id=run.id, user_id=user.id, source_id=source.id)
        sent = llm.calls[0]["messages"][1].content
        assert result.summary_truncated is True
        assert 0 < result.summary_passages < len(rows)
        assert sum(1 for row in rows if row.text in sent) == result.summary_passages

    async def test_a_short_source_is_not_marked_truncated(self, db, make_settings):
        user, run, source, rows, *_ = await seed_source(db, telegram_user_id=7120)
        llm = RecordingLLM([summary_response("Whole-source summary.")])
        service = service_for(
            db, make_settings(), LLMSourceSummarizer(llm, make_settings()), _NoClaims()
        )

        result = await service.analyze_source(run_id=run.id, user_id=user.id, source_id=source.id)
        assert result.summary_truncated is False
        assert result.summary_passages == len(rows)

    async def test_the_service_holds_no_session_across_a_model_call(self, db, make_settings):
        """The read transaction closes before the provider is called.

        A slow provider must not keep a database connection or an open read
        snapshot: the pool is small by design and a research run makes several
        calls per source.
        """
        user, run, source, _rows, *_ = await seed_source(db, telegram_user_id=7114)
        events: list[str] = []

        class _ObservingSummarizer:
            async def summarize(self, source_content: SourceContent) -> SourceSummary:
                # Nothing is open in the test session: a write here would fail if
                # the service still held its read transaction.
                events.append("called")
                return SourceSummary(source_id=source_content.source_id, text="s")

        class _ObservingExtractor:
            async def extract(self, source_content: SourceContent):
                from app.domain.research.evidence import ClaimExtraction

                return ClaimExtraction(source_id=source_content.source_id, candidates=())

        service = SourceEvidenceService(
            settings=make_settings(),
            summarizer=_ObservingSummarizer(),
            extractor=_ObservingExtractor(),
            uow_factory=uow_over(db),
        )
        await service.analyze_source(run_id=run.id, user_id=user.id, source_id=source.id)
        assert events == ["called"]

    async def test_a_second_analysis_is_refused_rather_than_appending_claims(
        self, db, make_settings
    ):
        """Re-execution is refused, so no claim or citation is appended (B7).

        P3.8-era behaviour asked the stage to *replace* the summary on a second
        call. That is precisely what § R.14 forbids: because each duplicate claim
        row has its own id, appending a second set of claims and citations for the
        same source is invisible to ``uq_citations_claim_source_chunk`` — the
        duplication would never be caught by the schema and would have to be
        discovered by a reader.

        The refusal is therefore the correct *implemented* behaviour, not a
        regression: a redelivered task must converge, and "the claim set is written
        once" is the invariant that converges.
        """
        from app.domain.errors import SourceAlreadyAnalyzedError
        from app.repositories.research import ClaimRepositoryImpl

        user, run, source, rows, *_ = await seed_source(db, telegram_user_id=7115)
        llm = RecordingLLM(
            [
                summary_response("First summary."),
                claims_response(claim(ENGLISH_STATEMENT, rows[0].id, ENGLISH_QUOTE)),
            ]
        )
        service = service_for(
            db, make_settings(), LLMSourceSummarizer(llm, make_settings()), _NoClaims()
        )

        await service.analyze_source(run_id=run.id, user_id=user.id, source_id=source.id)
        await db.commit()
        before = await ClaimRepositoryImpl(db).list_for_run(run.id)
        calls_before = llm.call_count

        # A second attempt makes **no** provider call, so a redelivery cannot even
        # buy the summary and claims it is about to be refused.
        with pytest.raises(SourceAlreadyAnalyzedError):
            await service.analyze_source(run_id=run.id, user_id=user.id, source_id=source.id)
        await db.commit()

        assert llm.call_count == calls_before, "the redelivery re-ran the evidence stage"
        _loaded, summary = await load_source_evidence(
            run.id, user.id, source.id, uow_over(db)
        )
        assert summary == "First summary.", "the refusal overwrote nothing"
        assert await ClaimRepositoryImpl(db).list_for_run(run.id) == before

    async def test_loading_an_un_analyzed_source_returns_no_summary_rather_than_failing(self, db):
        user, run, source, _rows, *_ = await seed_source(db, telegram_user_id=7116)
        loaded, summary = await load_source_evidence(run.id, user.id, source.id, uow_over(db))
        assert loaded.id == source.id
        assert summary is None, "a source that has not been analyzed is a normal state"

    async def test_loading_a_foreign_run_is_refused(self, db):
        _user, run, source, _rows, *_ = await seed_source(db, telegram_user_id=7117)
        from app.repositories.users import UserRepositoryImpl

        intruder = await UserRepositoryImpl(db).get_or_create(7118, None)
        await db.commit()

        with pytest.raises(ResearchRunNotFoundError):
            await load_source_evidence(run.id, intruder.id, source.id, uow_over(db))

    async def test_summarizing_does_not_change_the_source_status(self, db, make_settings):
        """P3.7 describes a source; it does not promote, rank, or select it."""
        user, run, source, _rows, *_ = await seed_source(db, telegram_user_id=7119)
        llm = RecordingLLM([summary_response("A summary."), claims_response()])
        service = service_for(
            db, make_settings(), LLMSourceSummarizer(llm, make_settings()), _NoClaims()
        )

        await service.analyze_source(run_id=run.id, user_id=user.id, source_id=source.id)
        await db.commit()
        stored = await load_source_evidence(run.id, user.id, source.id, uow_over(db))
        assert stored[0].status.value == source.status.value
        assert stored[0].rank == source.rank
        assert stored[0].quality_score == source.quality_score