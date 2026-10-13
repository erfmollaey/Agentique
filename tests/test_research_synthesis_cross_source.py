"""Cross-source synthesis, contradiction handling, and deterministic ordering (P3.8).

P3.7 was deliberately single-source. These are the tests for the property that made
that necessary: **one answer drawn from several sources, in which conflicting
evidence survives** (FR-33, FR-34, T-17).

The three questions a research answer has to answer honestly are:

1. *which sources support what* — source identity is preserved per citation;
2. *where they agree* — several citations on one section, all of them real;
3. *where they disagree* — the conflict is reported, with both sides' evidence, and
   nothing is erased to make the answer tidier.

Ordering and the evidence budget are here too, because "the same inputs produce the
same synthesis context" is a property of the cross-source contract rather than of the
verifier alone.
"""

from __future__ import annotations

import pytest

from app.domain.errors import ContradictionReferenceError, UnknownCitationError
from app.domain.research.synthesis import (
    CitationEntry,
    CitationRegistry,
    SynthesisInput,
    SynthesisProposal,
    plan_synthesis_context,
    verify_synthesis,
)
from app.repositories.research import CitationRepositoryImpl
from app.services.context import CharRatioTokenEstimator
from app.services.research.synthesis import SynthesisService
from tests.research_support import (
    OTHER_PASSAGE,
    PERSIAN_PASSAGE,
    FixedSynthesizer,
    section,
    seed_verified_run,
    synthesis_response,
    uow_over,
)
from tests.research_support import contradiction as contradiction_entry

COUNTER = CharRatioTokenEstimator(4.0)
BASE_TELEGRAM_ID = 8400

SURVEY_A = (
    "The 2024 field survey recorded a forty percent increase across the sampled "
    "districts, according to the published methodology section."
)
SURVEY_B = (
    "The regional office published a second review and confirmed a forty percent "
    "increase across the same sampled districts."
)
BUDGET_C = (
    "This third page covers an unrelated regional budget report and lists its own "
    "methodology notes."
)
QUOTE_A = "recorded a forty percent increase across the sampled districts"
QUOTE_B = "published a second review and confirmed a forty percent increase"
QUOTE_C = "lists its own methodology notes"


def entry(
    citation_id: int,
    *,
    source_id: int,
    claim_id: int,
    rank: int | None,
    passage: str,
    quote: str,
    statement: str,
    domain: str,
    title: str,
    url: str,
    ordinal: int = 0,
) -> CitationEntry:
    return CitationEntry(
        citation_id=citation_id,
        claim_id=claim_id,
        claim_text=statement,
        source_id=source_id,
        chunk_source_id=source_id,
        chunk_id=citation_id + 1_000,
        ordinal=ordinal,
        quote=quote,
        passage_text=passage,
        location=f"chunk {ordinal}",
        source_url=url,
        source_domain=domain,
        source_title=title,
        source_rank=rank,
    )


def three_source_registry() -> CitationRegistry:
    return CitationRegistry(
        run_id=1,
        entries=(
            entry(
                10,
                source_id=1,
                claim_id=101,
                rank=0,
                passage=SURVEY_A,
                quote=QUOTE_A,
                statement="The survey recorded a forty percent increase.",
                domain="survey.example",
                title="Field survey",
                url="https://survey.example/a",
            ),
            entry(
                20,
                source_id=2,
                claim_id=202,
                rank=1,
                passage=SURVEY_B,
                quote=QUOTE_B,
                statement="The regional review confirmed the increase.",
                domain="office.example",
                title="Regional review",
                url="https://office.example/b",
            ),
            entry(
                30,
                source_id=3,
                claim_id=303,
                rank=2,
                passage=BUDGET_C,
                quote=QUOTE_C,
                statement="The budget page lists its own methodology.",
                domain="budget.example",
                title="Budget report",
                url="https://budget.example/c",
            ),
        ),
    )


def input_for(registry: CitationRegistry, **context) -> SynthesisInput:
    options = {"max_tokens": 8_000, "counter": COUNTER, "max_items": 30}
    options.update(context)
    return SynthesisInput(
        run_id=registry.run_id,
        question="What did the 2024 survey find?",
        objective="Establish what the surveys measured",
        language="en",
        registry=registry,
        context=plan_synthesis_context(registry, **options),
    )


# --- synthesis across several sources ----------------------------------------


class TestMultiSourceSynthesis:
    def test_three_sources_are_synthesized_into_one_answer(self):
        answer = verify_synthesis(
            SynthesisProposal.from_payload(
                {
                    "sections": [
                        section("Two surveys agree on the increase.", [10, 20]),
                        section("A third page is unrelated.", [30]),
                    ]
                }
            ),
            input_for(three_source_registry()),
        )
        assert answer.source_count == 3
        assert len(answer.sections) == 2

    def test_supporting_sources_are_preserved_per_citation(self):
        """"Source A supports X and Source B supports X" is representable."""
        answer = verify_synthesis(
            SynthesisProposal.from_payload(
                {"sections": [section("Both agree.", [10, 20])]}
            ),
            input_for(three_source_registry()),
        )
        found = answer.sections[0]
        assert found.source_ids == (1, 2)
        assert found.citations[0].source_domain == "survey.example"
        assert found.citations[1].source_domain == "office.example"

    def test_each_citation_keeps_its_own_quote_and_url(self):
        """Source identity is per citation, not per answer.

        Without this, three citations in one section would be indistinguishable and the
        reader could not tell which evidence said what.
        """
        answer = verify_synthesis(
            SynthesisProposal.from_payload(
                {"sections": [section("Both agree.", [10, 20, 30])]}
            ),
            input_for(three_source_registry()),
        )
        by_source = {c.source_id: c for c in answer.sections[0].citations}
        assert by_source[1].quote == QUOTE_A
        assert by_source[1].source_url == "https://survey.example/a"
        assert by_source[2].quote == QUOTE_B
        assert by_source[3].quote == QUOTE_C

    def test_a_section_may_cite_a_subset_of_the_sources(self):
        answer = verify_synthesis(
            SynthesisProposal.from_payload(
                {"sections": [section("Only the first.", [10])]}
            ),
            input_for(three_source_registry()),
        )
        assert answer.cited_source_ids == (1,)
        assert answer.cited_claim_ids == (101,)

    def test_the_answer_reports_how_many_sources_contributed(self):
        answer = verify_synthesis(
            SynthesisProposal.from_payload(
                {"sections": [section("Two agree.", [10, 20])]}
            ),
            input_for(three_source_registry()),
        )
        assert answer.source_count == 2

    def test_the_same_citation_may_back_two_sections(self):
        """A source supporting two statements is one citation cited twice."""
        answer = verify_synthesis(
            SynthesisProposal.from_payload(
                {
                    "sections": [
                        section("First point.", [10]),
                        section("Second point.", [10]),
                    ]
                }
            ),
            input_for(three_source_registry()),
        )
        assert answer.citation_count == 1
        assert len(answer.sections) == 2
        assert answer.citations()[0].citation_id == 10


# --- contradiction handling --------------------------------------------------


class TestContradictionHandling:
    def test_a_conflict_keeps_both_sides_with_their_citations(self):
        """FR-34: reported, not reconciled."""
        answer = verify_synthesis(
            SynthesisProposal.from_payload(
                {
                    "sections": [
                        section("The survey measured a rise.", [10]),
                        section("The review found something different.", [20]),
                    ],
                    "contradictions": [
                        contradiction_entry(
                            [101, 202], "The two pages do not agree about the figure."
                        )
                    ],
                }
            ),
            input_for(three_source_registry()),
        )
        conflict = answer.contradictions[0]
        assert {conflict.left_claim_id, conflict.right_claim_id} == {101, 202}
        assert {c.citation_id for c in conflict.citations} == {10, 20}

    def test_neither_side_is_discarded_from_the_answer(self):
        answer = verify_synthesis(
            SynthesisProposal.from_payload(
                {
                    "sections": [
                        section("A says X.", [10]),
                        section("B says Y.", [20]),
                    ],
                    "contradictions": [
                        contradiction_entry([101, 202], "They disagree.")
                    ],
                }
            ),
            input_for(three_source_registry()),
        )
        assert len(answer.sections) == 2
        assert answer.cited_claim_ids == (101, 202)

    def test_a_source_not_in_the_conflict_keeps_its_own_section(self):
        answer = verify_synthesis(
            SynthesisProposal.from_payload(
                {
                    "sections": [
                        section("A says X.", [10]),
                        section("B says Y.", [20]),
                        section("C is unrelated.", [30]),
                    ],
                    "contradictions": [contradiction_entry([101, 202], "They differ.")],
                }
            ),
            input_for(three_source_registry()),
        )
        assert len(answer.sections) == 3
        assert answer.cited_source_ids == (1, 2, 3)

    def test_two_separate_conflicts_are_both_recorded(self):
        answer = verify_synthesis(
            SynthesisProposal.from_payload(
                {
                    "sections": [
                        section("A.", [10]),
                        section("B.", [20]),
                        section("C.", [30]),
                    ],
                    "contradictions": [
                        contradiction_entry([101, 202], "First conflict."),
                        contradiction_entry([101, 303], "Second conflict."),
                    ],
                }
            ),
            input_for(three_source_registry()),
        )
        assert len(answer.contradictions) == 2
        pairs = {
            (c.left_claim_id, c.right_claim_id) for c in answer.contradictions
        }
        assert pairs == {(101, 202), (101, 303)}

    def test_a_conflict_cannot_duplicate_one_citation_into_a_phantom_side(self):
        """A conflict needs two real claims; it cannot invent a side."""
        with pytest.raises(ContradictionReferenceError):
            verify_synthesis(
                SynthesisProposal.from_payload(
                    {
                        "sections": [section("A.", [10])],
                        "contradictions": [contradiction_entry([101, 101], "Self.")],
                    }
                ),
                input_for(three_source_registry()),
            )

    def test_a_conflict_naming_an_absent_claim_refuses_the_answer(self):
        with pytest.raises(ContradictionReferenceError):
            verify_synthesis(
                SynthesisProposal.from_payload(
                    {
                        "sections": [section("A.", [10])],
                        "contradictions": [contradiction_entry([101, 999_999], "Fake.")],
                    }
                ),
                input_for(three_source_registry()),
            )

    def test_the_note_is_kept_as_the_readers_explanation(self):
        answer = verify_synthesis(
            SynthesisProposal.from_payload(
                {
                    "sections": [section("A.", [10]), section("B.", [20])],
                    "contradictions": [
                        contradiction_entry(
                            [101, 202],
                            "One reports forty percent, the other a flat result.",
                        )
                    ],
                }
            ),
            input_for(three_source_registry()),
        )
        assert (
            answer.contradictions[0].note
            == "One reports forty percent, the other a flat result."
        )

    def test_the_conflict_survives_the_payload_round_trip(self):
        registry = three_source_registry()
        answer = verify_synthesis(
            SynthesisProposal.from_payload(
                {
                    "sections": [section("A.", [10]), section("B.", [20])],
                    "contradictions": [contradiction_entry([101, 202], "They differ.")],
                }
            ),
            input_for(registry),
        )
        from app.domain.research.synthesis import SynthesizedAnswer

        rebuilt = SynthesizedAnswer.from_payload(answer.to_payload(), registry=registry)
        assert len(rebuilt.contradictions[0].citations) == 2


# --- deterministic ordering --------------------------------------------------


class TestDeterministicOrdering:
    def test_the_context_follows_rank_then_source_then_position(self):
        """P3.5's rank leads; ties break on stable ids, never on input order."""
        registry = three_source_registry()
        context = plan_synthesis_context(
            registry, max_tokens=8_000, counter=COUNTER, max_items=30
        )
        assert [e.citation_id for e in context.entries] == [10, 20, 30]

    def test_shuffling_the_registry_does_not_change_the_context(self):
        """Input-order independence: the same evidence always yields the same context."""
        registry = three_source_registry()
        entries = registry.entries
        forwards = plan_synthesis_context(
            CitationRegistry(run_id=1, entries=entries),
            max_tokens=8_000,
            counter=COUNTER,
            max_items=30,
        )
        backwards = plan_synthesis_context(
            CitationRegistry(run_id=1, entries=tuple(reversed(entries))),
            max_tokens=8_000,
            counter=COUNTER,
            max_items=30,
        )
        assert forwards.citation_ids == backwards.citation_ids

    def test_the_budget_keeps_the_highest_ranked_evidence(self):
        registry = three_source_registry()
        context = plan_synthesis_context(
            registry, max_tokens=8_000, counter=COUNTER, max_items=2
        )
        assert [e.citation_id for e in context.entries] == [10, 20]
        assert context.dropped == 1

    def test_the_answer_is_byte_identical_across_repeated_runs(self):
        registry = three_source_registry()
        synthesis_input = input_for(registry)
        payload = {
            "sections": [section("A.", [10]), section("B.", [20, 30])],
            "contradictions": [contradiction_entry([101, 202], "They differ.")],
        }
        results = [
            verify_synthesis(
                SynthesisProposal.from_payload(dict(payload)), synthesis_input
            ).to_payload()
            for _ in range(5)
        ]
        assert results[0] == results[1] == results[2] == results[3] == results[4]

    def test_a_missing_rank_is_ordered_after_ranked_sources(self):
        """An unranked source is still offered — last, never silently dropped."""
        registry = CitationRegistry(
            run_id=1,
            entries=(
                entry(
                    40,
                    source_id=4,
                    claim_id=404,
                    rank=None,
                    passage=BUDGET_C,
                    quote=QUOTE_C,
                    statement="Unranked.",
                    domain="x.example",
                    title="X",
                    url="https://x.example/",
                ),
                entry(
                    10,
                    source_id=1,
                    claim_id=101,
                    rank=0,
                    passage=SURVEY_A,
                    quote=QUOTE_A,
                    statement="Ranked.",
                    domain="y.example",
                    title="Y",
                    url="https://y.example/",
                ),
            ),
        )
        context = plan_synthesis_context(
            registry, max_tokens=8_000, counter=COUNTER, max_items=30
        )
        assert [e.citation_id for e in context.entries] == [10, 40]

    def test_two_passages_of_one_source_are_ordered_by_ordinal(self):
        registry = CitationRegistry(
            run_id=1,
            entries=(
                entry(
                    11,
                    source_id=1,
                    claim_id=101,
                    rank=0,
                    passage=SURVEY_B,
                    quote=QUOTE_B,
                    statement="Second passage.",
                    domain="a.example",
                    title="A",
                    url="https://a.example/",
                    ordinal=1,
                ),
                entry(
                    12,
                    source_id=1,
                    claim_id=102,
                    rank=0,
                    passage=SURVEY_A,
                    quote=QUOTE_A,
                    statement="First passage.",
                    domain="a.example",
                    title="A",
                    url="https://a.example/",
                    ordinal=0,
                ),
            ),
        )
        context = plan_synthesis_context(
            registry, max_tokens=8_000, counter=COUNTER, max_items=30
        )
        assert [e.citation_id for e in context.entries] == [12, 11]


# --- mixed-language cross-source synthesis -----------------------------------


class TestMultilingualCrossSource:
    def test_a_persian_and_an_english_source_synthesize_together(self):
        """English-first, Persian-supported: mixed runs are the normal case."""
        persian_entry = entry(
            50,
            source_id=5,
            claim_id=505,
            rank=0,
            passage=PERSIAN_PASSAGE,
            quote="افزایش چهل درصدی را در نمونه‌های شهری",
            statement="پژوهشگران افزایش چهل درصدی را گزارش کردند.",
            domain="fa.example",
            title="گزارش میدانی",
            url="https://fa.example/fa",
        )
        registry = CitationRegistry(run_id=1, entries=(persian_entry,))
        answer = verify_synthesis(
            SynthesisProposal.from_payload(
                {
                    "sections": [
                        section("یافته‌های کلیدی.", [50], heading="یافته‌ها")
                    ]
                }
            ),
            input_for(registry),
        )
        assert answer.source_count == 1
        assert answer.sections[0].heading == "یافته‌ها"
        assert "نمونه‌های" in answer.sections[0].citations[0].quote

    async def test_two_real_sources_synthesize_end_to_end(self, db, make_settings):
        """The same properties, against persisted rows from two real sources."""
        seeded = await seed_verified_run(db, BASE_TELEGRAM_ID + 1, second_source=(OTHER_PASSAGE,))
        entries = await CitationRepositoryImpl(db).list_citable_for_run(seeded["run"].id)
        assert len(entries) == 2
        assert {e.source_id for e in entries} == {seeded["sources"][0].id, seeded["sources"][1].id}

        first, second = (group[0].id for group in seeded["citations"])
        left, right = seeded["claims"][0][0].id, seeded["claims"][1][0].id
        llm = FixedSynthesizer(
            synthesis_response(
                section("The survey recorded a rise.", [first], "Survey"),
                section("The budget page lists its methodology.", [second], "Budget"),
                contradictions=[contradiction_entry([left, right], "They describe different things.")],
            )
        )
        service = SynthesisService(
            settings=make_settings(), synthesizer=llm, uow_factory=uow_over(db)
        )
        outcome = await service.synthesize_run(
            run_id=seeded["run"].id, user_id=seeded["user"].id
        )
        await db.commit()

        assert outcome.source_count == 2
        assert outcome.citation_count == 2
        assert outcome.contradiction_count == 1
        assert len(outcome.answer.sections) == 2

    async def test_conflicting_evidence_is_persisted_on_both_sides(self, db, make_settings):
        """The end-to-end version of FR-34: a conflict cannot be silently erased."""
        seeded = await seed_verified_run(db, BASE_TELEGRAM_ID + 2, second_source=(OTHER_PASSAGE,))
        left, right = seeded["claims"][0][0].id, seeded["claims"][1][0].id
        first, second = (group[0].id for group in seeded["citations"])
        llm = FixedSynthesizer(
            synthesis_response(
                section("A.", [first]),
                section("B.", [second]),
                contradictions=[contradiction_entry([left, right], "They disagree.")],
            )
        )
        service = SynthesisService(
            settings=make_settings(), synthesizer=llm, uow_factory=uow_over(db)
        )
        await service.synthesize_run(
            run_id=seeded["run"].id, user_id=seeded["user"].id
        )
        await db.commit()

        from app.services.research.synthesis import load_synthesis

        reloaded = await load_synthesis(seeded["run"].id, seeded["user"].id, uow_over(db))
        conflict = reloaded.contradictions[0]
        assert len(conflict.citations) == 2
        assert {c.claim_id for c in conflict.citations} == {left, right}
        assert len(reloaded.sections) == 2

    async def test_a_fabricated_cross_source_citation_is_refused_end_to_end(
        self, db, make_settings
    ):
        """The headline property, end to end.

        A fluent answer that cites a source this run never retrieved is refused, and
        nothing is written.
        """
        seeded = await seed_verified_run(db, BASE_TELEGRAM_ID + 3, second_source=(OTHER_PASSAGE,))
        first = seeded["citations"][0][0].id
        llm = FixedSynthesizer(
            synthesis_response(
                section("A peer-reviewed meta-analysis proved a 91% reduction.", [first + 500_000]),
                section("The survey recorded a rise.", [first]),
            )
        )
        service = SynthesisService(
            settings=make_settings(), synthesizer=llm, uow_factory=uow_over(db)
        )
        with pytest.raises(UnknownCitationError):
            await service.synthesize_run(
                run_id=seeded["run"].id, user_id=seeded["user"].id
            )
        await db.commit()

        from sqlalchemy import text

        assert (
            await db.execute(
                text("SELECT synthesis FROM research_runs WHERE id = :r"),
                {"r": seeded["run"].id},
            )
        ).scalar_one() is None