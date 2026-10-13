"""Claim extraction with programmatic evidence verification (P3.7, FR-32, AD-033).

The stage's central claim: **no evidence → no accepted claim**. These tests walk
that statement in both directions — every way a claim *can* be accepted, and every
way it cannot — and then check that only the accepted ones reach the database.

Three halves:

* the **adapter** (``LLMClaimExtractor``) — what it accepts from a model, what it
  refuses, and that it returns *candidates*, never claims;
* the **verifier through the service** — the acceptance and rejection outcomes,
  including windowed extraction across a long source;
* **persistence** — that a persisted claim always arrives with its passage.
"""

from __future__ import annotations

import pytest

from app.domain.chat import Role
from app.domain.errors import (
    ClaimEvidenceError,
    ClaimExtractionSchemaError,
    ClaimStructureError,
    LLMServiceError,
    SourceContentEmptyError,
)
from app.domain.research.evidence import (
    ClaimExtraction,
    EvidenceRejection,
    SourceContent,
)
from app.domain.research.ports import ClaimExtractor
from app.infrastructure.evidence.analyzer import (
    LLMClaimExtractor,
    create_claim_extractor,
)
from app.infrastructure.evidence.prompts import (
    CLAIM_EXTRACTION_PROMPT_VERSION,
    CLAIM_EXTRACTION_SYSTEM_PROMPT,
    build_claim_system_prompt,
)
from app.services.research.evidence import SourceEvidenceService
from tests.research_support import (
    ENGLISH_PASSAGE,
    MIXED_PASSAGE,
    OTHER_PASSAGE,
    PERSIAN_PASSAGE,
    RecordingLLM,
    claim,
    claims_response,
    seed_source,
    summary_response,
    uow_over,
)

ENGLISH_QUOTE = "recorded a forty percent increase across the sampled districts"
ENGLISH_STATEMENT = "The survey recorded a forty percent increase."
PERSIAN_QUOTE = "افزایش چهل درصدی را در نمونه‌های شهری"
PERSIAN_STATEMENT = "پژوهشگران افزایش چهل درصدی را گزارش کردند."
MIXED_QUOTE = "field survey منتشر شده و رشد ۴۰ درصدی"


class _FixedSummarizer:
    """A summarizer that never calls a provider, for the extraction tests."""

    def __init__(self, text: str = "A summary.") -> None:
        self.text = text
        self.sources: list[SourceContent] = []

    async def summarize(self, source: SourceContent):
        from app.domain.research.evidence import SourceSummary

        self.sources.append(source)
        return SourceSummary(source_id=source.source_id, text=self.text)


def content(*chunks, source_id: int = 1) -> SourceContent:
    from app.domain.research.evidence import EvidenceChunk

    return SourceContent(
        source_id=source_id,
        domain="example.com",
        title="A page",
        chunks=chunks
        or (EvidenceChunk(chunk_id=10, ordinal=0, text=ENGLISH_PASSAGE),),
    )


# --- the adapter ------------------------------------------------------------


class TestClaimExtractorAdapter:
    async def test_a_well_formed_response_becomes_candidates(self, settings):
        llm = RecordingLLM([claims_response(claim(ENGLISH_STATEMENT, 10, ENGLISH_QUOTE))])
        result = await LLMClaimExtractor(llm, settings).extract(content())
        assert len(result.candidates) == 1
        assert result.candidates[0].source_chunk_id == 10
        assert result.candidates[0].quote == ENGLISH_QUOTE

    async def test_the_extractor_returns_candidates_not_claims(self, settings):
        """The port cannot return an accepted claim; verification is elsewhere."""
        from app.domain.research.evidence import VerifiedClaim

        llm = RecordingLLM([claims_response(claim(ENGLISH_STATEMENT, 10, ENGLISH_QUOTE))])
        extraction = await LLMClaimExtractor(llm, settings).extract(content())
        assert not any(isinstance(c, VerifiedClaim) for c in extraction.candidates)
        assert not hasattr(extraction, "accepted")

    async def test_the_two_messages_are_instructions_then_data(self, settings):
        llm = RecordingLLM([claims_response()])
        await LLMClaimExtractor(llm, settings).extract(content())
        messages = llm.calls[0]["messages"]
        assert [m.role for m in messages] == [Role.SYSTEM, Role.USER]
        assert messages[0].content == CLAIM_EXTRACTION_SYSTEM_PROMPT.format(
            passage_count=1, max_claims=settings.CLAIM_MAX_CLAIMS
        )

    async def test_the_prompt_carries_the_claim_ceiling_and_the_passage_ids(self, settings):
        llm = RecordingLLM([claims_response()])
        await LLMClaimExtractor(llm, settings).extract(content())
        assert f"0 and {settings.CLAIM_MAX_CLAIMS}" in llm.calls[0]["messages"][0].content
        assert "id=10" in llm.calls[0]["messages"][1].content

    async def test_exactly_one_provider_call_per_window(self, settings):
        llm = RecordingLLM([claims_response(claim(ENGLISH_STATEMENT, 10, ENGLISH_QUOTE))])
        await LLMClaimExtractor(llm, settings).extract(content())
        assert llm.call_count == 1

    async def test_the_configured_model_and_token_bound_are_used(self, settings):
        llm = RecordingLLM([claims_response()])
        await LLMClaimExtractor(llm, settings).extract(content())
        assert llm.calls[0]["max_tokens"] == settings.CLAIM_MAX_TOKENS
        assert llm.calls[0]["model"] == settings.LLM_MODEL

    async def test_an_empty_claim_list_is_a_valid_response(self, settings):
        llm = RecordingLLM([claims_response()])
        result = await LLMClaimExtractor(llm, settings).extract(content())
        assert result.is_empty

    async def test_a_fenced_response_is_accepted(self, settings):
        llm = RecordingLLM(['```json\n{"claims": []}\n```'])
        assert (await LLMClaimExtractor(llm, settings).extract(content())).is_empty

    @pytest.mark.parametrize(
        "reply",
        [
            "",
            "   ",
            "no json here",
            '["a list"]',
            '"a string"',
            '{"summary": "not the claims contract"}',
            '{"claims": "a string"}',
            '{"claims": 5}',
            '{"claims": [], "verified": true}',
        ],
    )
    async def test_a_response_that_is_not_the_envelope_is_refused(self, settings, reply):
        llm = RecordingLLM([reply])
        with pytest.raises(ClaimExtractionSchemaError):
            await LLMClaimExtractor(llm, settings).extract(content())
        assert llm.call_count == 1, "a malformed answer must not be retried here"

    @pytest.mark.parametrize(
        "entry",
        [
            "a string",
            5,
            [claim(ENGLISH_STATEMENT, 10, ENGLISH_QUOTE)],
            {**claim(ENGLISH_STATEMENT, 10, ENGLISH_QUOTE), "verified": True},
            {**claim(ENGLISH_STATEMENT, 10, ENGLISH_QUOTE), "confidence": 0.99},
            {**claim(ENGLISH_STATEMENT, 10, ENGLISH_QUOTE), "source_id": 1},
        ],
    )
    async def test_an_entry_that_is_not_a_claim_is_refused(self, settings, entry):
        llm = RecordingLLM([claims_response(entry)])
        with pytest.raises(ClaimStructureError):
            await LLMClaimExtractor(llm, settings).extract(content())

    async def test_a_string_chunk_id_is_rejected_rather_than_coerced(self, settings):
        llm = RecordingLLM([claims_response(claim(ENGLISH_STATEMENT, "10", ENGLISH_QUOTE))])
        result = await LLMClaimExtractor(llm, settings).extract(content())
        assert result.candidates[0].source_chunk_id is None

    async def test_provider_failures_propagate_untouched(self, settings):
        error = LLMServiceError("provider down")
        llm = RecordingLLM([], error=error)
        with pytest.raises(LLMServiceError) as excinfo:
            await LLMClaimExtractor(llm, settings).extract(content())
        assert excinfo.value.retryable == error.retryable

    def test_the_adapter_satisfies_the_port(self, settings):
        assert isinstance(LLMClaimExtractor(RecordingLLM([]), settings), ClaimExtractor)

    def test_the_factory_uses_the_injected_provider(self, settings):
        llm = RecordingLLM([])
        assert create_claim_extractor(settings, llm)._llm is llm

    def test_the_prompt_is_versioned(self):
        assert CLAIM_EXTRACTION_PROMPT_VERSION == "claim-extraction-v1"

    def test_the_claim_system_prompt_builder_takes_no_source_content(self):
        import inspect

        parameters = inspect.signature(build_claim_system_prompt).parameters
        assert sorted(p.name for p in parameters.values()) == ["max_claims", "passage_count"]


# --- verification through the service --------------------------------------


def service_for(db, settings, extractor, summarizer=None):
    return SourceEvidenceService(
        settings=settings,
        summarizer=summarizer or _FixedSummarizer(),
        extractor=extractor,
        uow_factory=uow_over(db),
    )


class TestClaimExtraction:
    async def test_a_supported_claim_is_accepted_with_its_passage(self, db, make_settings):
        settings = make_settings()
        user, run, source, rows, *_ = await seed_source(db, telegram_user_id=7201)
        llm = RecordingLLM(
            [claims_response(claim(ENGLISH_STATEMENT, rows[0].id, ENGLISH_QUOTE))]
        )
        result = await service_for(
            db, settings, LLMClaimExtractor(llm, settings)
        ).analyze_source(run_id=run.id, user_id=user.id, source_id=source.id)
        assert result.claim_count == 1
        assert result.claims[0].evidence.chunk_id == rows[0].id
        assert result.claims[0].evidence.quote == ENGLISH_QUOTE
        assert result.rejected_count == 0

    async def test_several_claims_are_accepted(self, db, make_settings):
        settings = make_settings()
        user, run, source, rows, *_ = await seed_source(db, telegram_user_id=7202)
        llm = RecordingLLM(
            [
                claims_response(
                    claim("The survey recorded a forty percent increase.", rows[0].id, ENGLISH_QUOTE),
                    claim("The methodology section was published.", rows[0].id, "the published methodology section"),
                )
            ]
        )
        result = await service_for(
            db, settings, LLMClaimExtractor(llm, settings)
        ).analyze_source(run_id=run.id, user_id=user.id, source_id=source.id)
        assert result.claim_count == 2
        assert [c.statement for c in result.claims] == [
            "The survey recorded a forty percent increase.",
            "The methodology section was published.",
        ]

    async def test_claims_can_point_at_different_passages_of_one_source(self, db, make_settings):
        settings = make_settings()
        user, run, source, rows, *_ = await seed_source(
            db, telegram_user_id=7203, passages=(ENGLISH_PASSAGE, PERSIAN_PASSAGE)
        )
        llm = RecordingLLM(
            [
                claims_response(
                    claim(ENGLISH_STATEMENT, rows[0].id, ENGLISH_QUOTE),
                    claim(PERSIAN_STATEMENT, rows[1].id, PERSIAN_QUOTE),
                )
            ]
        )
        result = await service_for(
            db, settings, LLMClaimExtractor(llm, settings)
        ).analyze_source(run_id=run.id, user_id=user.id, source_id=source.id)
        assert [c.evidence.chunk_id for c in result.claims] == [rows[0].id, rows[1].id]
        assert result.claims[1].evidence.quote == PERSIAN_QUOTE

    async def test_a_mixed_language_passage_yields_a_mixed_language_claim(self, db, make_settings):
        settings = make_settings()
        user, run, source, rows, *_ = await seed_source(
            db, telegram_user_id=7204, passages=(MIXED_PASSAGE,)
        )
        llm = RecordingLLM([claims_response(claim("این گزارش میدانی است.", rows[0].id, MIXED_QUOTE))])
        result = await service_for(
            db, settings, LLMClaimExtractor(llm, settings)
        ).analyze_source(run_id=run.id, user_id=user.id, source_id=source.id)
        assert result.claim_count == 1
        assert result.claims[0].statement == "این گزارش میدانی است."
        assert "۴۰" in result.claims[0].evidence.quote

    async def test_an_invalid_chunk_id_is_rejected(self, db, make_settings):
        settings = make_settings()
        user, run, source, _rows, *_ = await seed_source(db, telegram_user_id=7205)
        llm = RecordingLLM([claims_response(claim(ENGLISH_STATEMENT, 999999, ENGLISH_QUOTE))])
        result = await service_for(
            db, settings, LLMClaimExtractor(llm, settings)
        ).analyze_source(run_id=run.id, user_id=user.id, source_id=source.id)
        assert result.claim_count == 0
        assert result.rejections_by_reason() == {EvidenceRejection.UNKNOWN_CHUNK: 1}

    async def test_a_quote_that_is_not_in_the_passage_is_rejected(self, db, make_settings):
        """A paraphrase is not evidence, however close it reads."""
        settings = make_settings()
        user, run, source, rows, *_ = await seed_source(db, telegram_user_id=7206)
        paraphrase = "the article basically says growth happened across the districts"
        llm = RecordingLLM([claims_response(claim("Growth happened.", rows[0].id, paraphrase))])
        result = await service_for(
            db, settings, LLMClaimExtractor(llm, settings)
        ).analyze_source(run_id=run.id, user_id=user.id, source_id=source.id)
        assert result.claim_count == 0
        assert result.rejections_by_reason() == {EvidenceRejection.QUOTE_NOT_FOUND: 1}

    async def test_an_empty_quote_is_rejected(self, db, make_settings):
        settings = make_settings()
        user, run, source, rows, *_ = await seed_source(db, telegram_user_id=7207)
        llm = RecordingLLM([claims_response(claim(ENGLISH_STATEMENT, rows[0].id, ""))])
        result = await service_for(
            db, settings, LLMClaimExtractor(llm, settings)
        ).analyze_source(run_id=run.id, user_id=user.id, source_id=source.id)
        assert result.claim_count == 0
        assert result.rejections_by_reason() == {EvidenceRejection.EMPTY_QUOTE: 1}

    async def test_an_empty_statement_is_rejected(self, db, make_settings):
        settings = make_settings()
        user, run, source, rows, *_ = await seed_source(db, telegram_user_id=7208)
        llm = RecordingLLM([claims_response(claim("", rows[0].id, ENGLISH_QUOTE))])
        result = await service_for(
            db, settings, LLMClaimExtractor(llm, settings)
        ).analyze_source(run_id=run.id, user_id=user.id, source_id=source.id)
        assert result.claim_count == 0
        assert result.rejections_by_reason() == {EvidenceRejection.EMPTY_STATEMENT: 1}

    async def test_a_claim_missing_its_evidence_field_is_rejected(self, db, make_settings):
        settings = make_settings()
        user, run, source, _rows, *_ = await seed_source(db, telegram_user_id=7209)
        llm = RecordingLLM([claims_response({"statement": ENGLISH_STATEMENT})])
        result = await service_for(
            db, settings, LLMClaimExtractor(llm, settings)
        ).analyze_source(run_id=run.id, user_id=user.id, source_id=source.id)
        assert result.claim_count == 0
        assert result.rejections_by_reason() == {EvidenceRejection.MISSING_CHUNK_REFERENCE: 1}

    async def test_fabricated_evidence_is_rejected(self, db, make_settings):
        """Plausible, well-formed, and not in the page: still rejected."""
        settings = make_settings()
        user, run, source, rows, *_ = await seed_source(db, telegram_user_id=7210)
        llm = RecordingLLM(
            [
                claims_response(
                    claim(
                        "A peer-reviewed meta-analysis found a 91% reduction.",
                        rows[0].id,
                        "a peer-reviewed meta-analysis found a 91% reduction in errors",
                    )
                )
            ]
        )
        result = await service_for(
            db, settings, LLMClaimExtractor(llm, settings)
        ).analyze_source(run_id=run.id, user_id=user.id, source_id=source.id)
        assert result.claim_count == 0
        assert result.rejections_by_reason() == {EvidenceRejection.QUOTE_NOT_FOUND: 1}

    async def test_a_chunk_of_another_source_in_the_same_run_is_rejected(self, db, make_settings):
        settings = make_settings()
        user, run, source, _rows, other_source, other_rows = await seed_source(
            db, telegram_user_id=7211, extra_source=True
        )
        llm = RecordingLLM([claims_response(claim(ENGLISH_STATEMENT, other_rows[0].id, OTHER_PASSAGE[:40]))])
        result = await service_for(
            db, settings, LLMClaimExtractor(llm, settings)
        ).analyze_source(run_id=run.id, user_id=user.id, source_id=source.id)
        assert result.claim_count == 0
        assert result.rejections_by_reason() == {EvidenceRejection.UNKNOWN_CHUNK: 1}
        assert other_source.id != source.id

    async def test_an_extractor_that_forges_evidence_cannot_persist_it(self, db, make_settings):
        """Verification lives in the service, not behind the port.

        A port implementation that returns something claiming to be verified gets no
        such benefit: the service decides, from the passage text, every time.
        """
        settings = make_settings()

        class _Forging:
            async def extract(self, source: SourceContent):
                return ClaimExtraction.from_payload(
                    {
                        "claims": [
                            claim(
                                "The report found a 91% reduction in errors.",
                                next(iter(source.chunk_ids)),
                                "The report found a 91% reduction in errors.",
                            )
                        ]
                    },
                    source_id=source.source_id,
                )

        user, run, source, _rows, *_ = await seed_source(db, telegram_user_id=7212)
        result = await service_for(
            db, settings, _Forging()
        ).analyze_source(run_id=run.id, user_id=user.id, source_id=source.id)
        assert result.claim_count == 0
        assert result.rejections_by_reason() == {EvidenceRejection.QUOTE_NOT_FOUND: 1}

    async def test_one_bad_claim_does_not_cost_the_verified_ones(self, db, make_settings):
        settings = make_settings()
        user, run, source, rows, *_ = await seed_source(db, telegram_user_id=7213)
        llm = RecordingLLM(
            [
                claims_response(
                    claim(ENGLISH_STATEMENT, rows[0].id, ENGLISH_QUOTE),
                    claim("Fabricated claim.", rows[0].id, "a sentence that appears nowhere"),
                    claim("Wrong passage.", 999999, ENGLISH_QUOTE),
                    claim("Also verified.", rows[0].id, "the published methodology section"),
                )
            ]
        )
        result = await service_for(
            db, settings, LLMClaimExtractor(llm, settings)
        ).analyze_source(run_id=run.id, user_id=user.id, source_id=source.id)
        assert result.claim_count == 2
        assert result.rejected_count == 2

    async def test_a_source_with_no_supportable_claims_keeps_its_summary(self, db, make_settings):
        """Zero accepted claims is a visible outcome, not a discarded source."""
        settings = make_settings()
        user, run, source, rows, *_ = await seed_source(db, telegram_user_id=7214)
        llm = RecordingLLM([claims_response(claim("Fabricated.", rows[0].id, "not in the page"))])
        service = service_for(
            db, settings, LLMClaimExtractor(llm, settings), _FixedSummarizer("Kept summary.")
        )
        result = await service.analyze_source(run_id=run.id, user_id=user.id, source_id=source.id)
        await db.commit()
        assert result.summary.text == "Kept summary."
        assert result.claim_count == 0

    async def test_a_windowed_source_keeps_each_claims_own_passage(self, db, make_settings):
        """Several windows, several calls, and every claim keeps its own passage id.

        The passage ids are read back out of the prompts rather than assumed, so the
        test states the real property: each window shows its own passages, the
        windows between them cover the source exactly once, and each claim's
        evidence points inside the window that produced it.
        """
        import re

        padding = " Additional methodological detail follows. " * 4
        passages = (ENGLISH_PASSAGE, PERSIAN_PASSAGE, MIXED_PASSAGE)
        settings = make_settings(CLAIM_MAX_INPUT_TOKENS=700, CLAIM_MAX_WINDOWS=4)
        user, run, source, rows, *_ = await seed_source(
            db,
            telegram_user_id=7215,
            passages=tuple(text + padding for text in passages),
        )
        llm = RecordingLLM(
            [
                claims_response(claim(ENGLISH_STATEMENT, rows[0].id, ENGLISH_QUOTE)),
                claims_response(claim(PERSIAN_STATEMENT, rows[1].id, PERSIAN_QUOTE)),
                claims_response(claim("این گزارش میدانی است.", rows[2].id, MIXED_QUOTE)),
            ]
        )
        service = service_for(db, settings, LLMClaimExtractor(llm, settings))
        result = await service.analyze_source(run_id=run.id, user_id=user.id, source_id=source.id)

        windows = [
            {int(found) for found in re.findall(r"<<<PASSAGE id=(\d+)>>>", call["messages"][1].content)}
            for call in llm.calls
        ]
        assert llm.call_count == 3, "each passage needs its own window at this budget"
        assert [len(window) for window in windows] == [1, 1, 1]
        assert sorted(chunk for window in windows for chunk in window) == sorted(
            row.id for row in rows
        ), "the windows between them must cover the source exactly once"
        assert [c.evidence.chunk_id for c in result.claims] == [rows[0].id, rows[1].id, rows[2].id]
        assert result.windows == llm.call_count

    async def test_a_passage_beyond_the_window_ceiling_is_counted_not_silently_dropped(
        self, db, make_settings
    ):
        padding = " Additional methodological detail follows. " * 4
        passages = tuple(text + padding for text in (ENGLISH_PASSAGE, PERSIAN_PASSAGE, MIXED_PASSAGE))
        settings = make_settings(CLAIM_MAX_INPUT_TOKENS=700, CLAIM_MAX_WINDOWS=2)
        user, run, source, _rows, *_ = await seed_source(
            db, telegram_user_id=7221, passages=passages
        )
        llm = RecordingLLM([claims_response(), claims_response()])
        result = await service_for(
            db, settings, LLMClaimExtractor(llm, settings)
        ).analyze_source(run_id=run.id, user_id=user.id, source_id=source.id)

        assert result.windows == 2
        assert result.claim_count == 0
        assert result.claim_passages == 2, "only the passages that fitted were examined"
        assert result.claim_truncated is True, "unexamined evidence must be visible"

    async def test_no_provider_call_happens_for_an_empty_source(self, db, make_settings):
        from sqlalchemy import delete

        from app.db.models import SourceChunkModel

        settings = make_settings()
        user, run, source, _rows, *_ = await seed_source(db, telegram_user_id=7216)
        await db.execute(delete(SourceChunkModel).where(SourceChunkModel.source_id == source.id))
        await db.commit()

        llm = RecordingLLM([summary_response("never"), claims_response()])
        service = service_for(db, settings, LLMClaimExtractor(llm, settings))
        with pytest.raises(SourceContentEmptyError):
            await service.analyze_source(run_id=run.id, user_id=user.id, source_id=source.id)
        assert llm.call_count == 0

    async def test_a_claim_that_fails_verification_is_never_persisted(self, db, make_settings):
        settings = make_settings()
        user, run, source, rows, *_ = await seed_source(db, telegram_user_id=7217)
        llm = RecordingLLM(
            [claims_response(claim("Fabricated claim here.", rows[0].id, "nowhere in the page"))]
        )
        await service_for(db, settings, LLMClaimExtractor(llm, settings)).analyze_source(
            run_id=run.id, user_id=user.id, source_id=source.id
        )
        await db.commit()

        from app.repositories.research import ClaimRepositoryImpl

        assert await ClaimRepositoryImpl(db).list_for_run(run.id) == []

    async def test_the_service_refuses_to_persist_evidence_it_cannot_verify(self, db, make_settings):
        """The pre-persistence re-check is a real gate, not decoration.

        ``VerifiedClaim`` is a frozen value type, so a caller could hand the service
        one built by hand. This asserts the second gate catches it.
        """
        from app.domain.research.evidence import EvidenceReference, VerifiedClaim

        settings = make_settings()
        _user, _run, source, rows, *_ = await seed_source(db, telegram_user_id=7218)

        forged = [
            VerifiedClaim(
                statement="A claim with invented evidence.",
                evidence=EvidenceReference(
                    source_id=source.id,
                    chunk_id=rows[0].id,
                    ordinal=rows[0].ordinal,
                    quote="this text is not in the passage at all",
                ),
            )
        ]
        service = SourceEvidenceService(
            settings=settings,
            summarizer=_FixedSummarizer(),
            extractor=_Nothing(),
            uow_factory=uow_over(db),
        )
        with pytest.raises(ClaimEvidenceError):
            await service._assert_evidence_is_real(forged, _content_for(source.id, rows))


class _Nothing:
    async def extract(self, source: SourceContent):
        return ClaimExtraction(source_id=source.source_id, candidates=())


def _content_for(source_id: int, rows) -> SourceContent:
    from app.domain.research.evidence import EvidenceChunk

    return SourceContent(
        source_id=source_id,
        domain="example.com",
        title=None,
        chunks=tuple(
            EvidenceChunk(chunk_id=r.id, ordinal=r.ordinal, text=r.text) for r in rows
        ),
    )

