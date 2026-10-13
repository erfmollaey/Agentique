"""The synthesis domain contract (P3.8).

Pure tests: no database, no provider, no network. The trust chain is decided by
:func:`app.domain.research.synthesis.verify_synthesis`, so this module is where that
decision is tested on its own terms — every rule, every rejection, and the
structural properties that make the decision impossible to bypass.

What is asserted here:

1. the proposal and answer types: construction, closed schemas, lenient values,
   strict shapes;
2. the citation registry: canonical ordering, chain validation, run scoping;
3. verification: every acceptance and every refusal, including the fail-closed rule;
4. determinism: same input, same answer — and input order cannot change it;
5. multilingual content: Persian, mixed Persian/English, ZWNJ, Arabic-Indic digits;
6. the guarded constructors: a trusted answer cannot be built by a caller.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.domain.errors import (
    CitationChainError,
    CitationCompletenessError,
    CitationFailureKind,
    CitationQuoteError,
    CitationStructureError,
    ContradictionReferenceError,
    ContradictionStructureError,
    SynthesisSchemaError,
    SynthesisValidationError,
    UnknownCitationError,
    UnshownCitationError,
)
from app.domain.research.synthesis import (
    MAX_CITATIONS_PER_SECTION_CEILING,
    MAX_SECTIONS_CEILING,
    SYNTHESIS_SCHEMA_VERSION,
    CitationEntry,
    CitationRegistry,
    SynthesisContext,
    SynthesisInput,
    SynthesisProposal,
    SynthesizedAnswer,
    VerifiedCitation,
    VerifiedContradiction,
    VerifiedSection,
    plan_synthesis_context,
    verify_synthesis,
)
from app.services.context import CharRatioTokenEstimator

# --- fixtures ---------------------------------------------------------------

SURVEY = (
    "The 2024 field survey recorded a forty percent increase across the sampled "
    "districts, according to the published methodology section."
)
SURVEY_QUOTE = "recorded a forty percent increase across the sampled districts"
SURVEY_STATEMENT = "The survey recorded a forty percent increase."

BUDGET = (
    "This second page covers an unrelated regional budget report and lists its own "
    "methodology notes."
)
BUDGET_QUOTE = "lists its own methodology notes"
BUDGET_STATEMENT = "The regional budget page lists its own methodology."

PERSIAN = "پژوهشگران در سال ۱۴۰۳ افزایش چهل درصدی را در نمونه‌های شهری گزارش کردند."
PERSIAN_QUOTE = "افزایش چهل درصدی را در نمونه‌های شهری"
PERSIAN_STATEMENT = "پژوهشگران افزایش چهل درصدی را گزارش کردند."

MIXED = "این گزارش بر پایهٔ field survey منتشر شده و رشد ۴۰ درصدی را نشان می‌دهد."
MIXED_QUOTE = "بر پایهٔ field survey منتشر شده"

COUNTER = CharRatioTokenEstimator(4.0)


def entry(
    citation_id: int,
    *,
    source_id: int | None = None,
    claim_id: int | None = None,
    passage: str = SURVEY,
    quote: str = SURVEY_QUOTE,
    statement: str = SURVEY_STATEMENT,
    rank: int | None = 0,
    url: str | None = None,
    domain: str = "example.com",
    title: str | None = "A field survey",
    ordinal: int = 0,
    chunk_source_id: int | None = None,
) -> CitationEntry:
    resolved_source = source_id if source_id is not None else citation_id
    return CitationEntry(
        citation_id=citation_id,
        claim_id=claim_id if claim_id is not None else citation_id + 100,
        claim_text=statement,
        source_id=resolved_source,
        # Equal to ``source_id`` by default, because a sound entry's passage belongs to
        # the source its citation names. Only the chain tests pass a different value.
        chunk_source_id=(
            chunk_source_id if chunk_source_id is not None else resolved_source
        ),
        chunk_id=citation_id + 200,
        ordinal=ordinal,
        quote=quote,
        passage_text=passage,
        location=f"chunk {ordinal}",
        source_url=url or f"https://{domain}/{source_id or citation_id}",
        source_domain=domain,
        source_title=title,
        source_rank=rank,
    )


def two_source_registry() -> CitationRegistry:
    return CitationRegistry(
        run_id=1,
        entries=(
            entry(1, source_id=1, claim_id=101, rank=0, url="https://example.com/survey"),
            entry(
                2,
                source_id=2,
                claim_id=202,
                passage=BUDGET,
                quote=BUDGET_QUOTE,
                statement=BUDGET_STATEMENT,
                rank=1,
                url="https://second.example/other",
                domain="second.example",
                title="Another page",
            ),
        ),
    )


def build_input(
    registry: CitationRegistry | None = None,
    *,
    max_items: int = 30,
    max_tokens: int = 8_000,
) -> SynthesisInput:
    reg = registry if registry is not None else two_source_registry()
    return SynthesisInput(
        run_id=reg.run_id,
        question="What did the 2024 survey find?",
        objective="Establish what the survey measured",
        language="en",
        registry=reg,
        context=plan_synthesis_context(
            reg, max_tokens=max_tokens, counter=COUNTER, max_items=max_items
        ),
    )


def propose(payload: dict) -> SynthesisProposal:
    return SynthesisProposal.from_payload(payload)


# --- the proposal contract ---------------------------------------------------


class TestProposalContract:
    def test_a_well_formed_proposal_parses(self):
        proposal = propose(
            {
                "sections": [
                    {
                        "heading": "Findings",
                        "text": "The survey found a rise.",
                        "citation_ids": [1],
                    }
                ]
            }
        )
        assert len(proposal.sections) == 1
        assert proposal.sections[0].heading == "Findings"
        assert proposal.sections[0].citations[0].citation_id == 1

    def test_a_heading_is_optional(self):
        proposal = propose({"sections": [{"text": "Body only.", "citation_ids": [1]}]})
        assert proposal.sections[0].heading == ""

    def test_a_missing_citation_list_reads_as_no_references(self):
        """Lenient on the *value*: absence becomes an empty list, not an exception.

        The section is then refused by the completeness check, which is the error a
        caller actually needs to see — "a section citing nothing" rather than "the
        field you left out is missing".
        """
        proposal = propose({"sections": [{"text": "Uncited."}]})
        assert proposal.sections[0].citations == ()

    def test_a_non_object_response_is_refused(self):
        with pytest.raises(SynthesisSchemaError):
            propose(["not", "an", "object"])

    def test_a_missing_sections_field_is_refused(self):
        with pytest.raises(SynthesisSchemaError, match="no 'sections'"):
            propose({"contradictions": []})

    def test_sections_that_is_not_a_list_is_refused(self):
        with pytest.raises(SynthesisSchemaError):
            propose({"sections": "a string"})

    @pytest.mark.parametrize(
        "key,value",
        [
            ("url", "https://evil.example"),
            ("quote", "a fabricated quote"),
            ("source", "example.com"),
            ("verified", True),
            ("confidence", 0.99),
        ],
    )
    def test_self_certification_and_fabrication_fields_have_nowhere_to_go(self, key, value):
        """The structural reason a model cannot supply its own citation.

        There is no ``url``, ``quote``, ``source``, ``verified``, or ``confidence``
        field in this contract, at either level, and an unknown key is refused rather
        than ignored.
        """
        for payload in (
            {"sections": [{"text": "x", "citation_ids": [1]}], key: value},
            {"sections": [{"text": "x", "citation_ids": [1], key: value}]},
        ):
            with pytest.raises(SynthesisSchemaError, match="unsupported field"):
                propose(payload)

    def test_a_string_citation_id_is_refused_rather_than_coerced(self):
        """``"1"`` is not an id, and guessing at one is how a citation points wrong."""
        with pytest.raises(CitationStructureError, match="must be a JSON number"):
            propose({"sections": [{"text": "x", "citation_ids": ["1"]}]})

    def test_a_boolean_citation_id_is_refused(self):
        """``True`` is an ``int`` in Python and would silently become citation 1."""
        with pytest.raises(CitationStructureError):
            propose({"sections": [{"text": "x", "citation_ids": [True]}]})

    def test_a_non_list_citation_list_is_refused(self):
        with pytest.raises(CitationStructureError, match="must be a list"):
            propose({"sections": [{"text": "x", "citation_ids": 1}]})

    def test_a_non_string_section_text_is_refused(self):
        with pytest.raises(SynthesisSchemaError, match="'text' must be a string"):
            propose({"sections": [{"text": 42, "citation_ids": [1]}]})

    def test_more_sections_than_the_ceiling_is_refused(self):
        payload = {
            "sections": [
                {"text": f"Section {i}.", "citation_ids": [1]}
                for i in range(MAX_SECTIONS_CEILING + 1)
            ]
        }
        with pytest.raises(SynthesisValidationError, match="at most"):
            propose(payload)

    def test_more_citations_per_section_than_the_ceiling_is_refused(self):
        ids = list(range(1, MAX_CITATIONS_PER_SECTION_CEILING + 2))
        with pytest.raises(SynthesisValidationError, match="more than"):
            verify_synthesis(
                propose({"sections": [{"text": "Too many.", "citation_ids": ids}]}),
                build_input(),
            )

    def test_an_unreadable_schema_version_is_refused(self):
        with pytest.raises(SynthesisSchemaError, match="cannot be read by this build"):
            propose({"schema_version": "synthesis-v0", "sections": []})

    def test_the_schema_version_round_trips(self):
        proposal = propose({"schema_version": SYNTHESIS_SCHEMA_VERSION, "sections": []})
        assert proposal.schema_version == SYNTHESIS_SCHEMA_VERSION

    def test_a_proposal_serializes_in_a_fixed_key_order(self):
        payload = propose(
            {"sections": [{"text": "x", "citation_ids": [1]}]}
        ).to_payload()
        assert list(payload) == ["schema_version", "sections", "contradictions"]
        assert list(payload["sections"][0]) == ["heading", "text", "citation_ids"]

    def test_an_empty_proposal_is_reported_as_empty(self):
        assert propose({"sections": []}).is_empty

    def test_an_empty_proposal_produces_no_answer(self):
        """A model that stated nothing has not produced a finding.

        Returning an empty answer would present "the model said nothing" as "research
        found nothing", which is a different claim.
        """
        with pytest.raises(SynthesisValidationError, match="no sections"):
            verify_synthesis(propose({"sections": []}), build_input())


class TestContradictionContract:
    def test_a_well_formed_contradiction_parses(self):
        proposal = propose(
            {
                "sections": [{"text": "x", "citation_ids": [1]}],
                "contradictions": [{"claim_ids": [101, 202], "note": "They disagree."}],
            }
        )
        assert proposal.contradictions[0].claim_id_pair == (101, 202)
        assert proposal.contradictions[0].note == "They disagree."

    def test_a_claim_ids_list_must_hold_exactly_two_numbers(self):
        for bad in ([101], [101, 202, 303], [], "101,202"):
            with pytest.raises(ContradictionStructureError):
                propose(
                    {
                        "sections": [{"text": "x", "citation_ids": [1]}],
                        "contradictions": [{"claim_ids": bad}],
                    }
                )

    def test_a_string_claim_id_is_refused_rather_than_coerced(self):
        with pytest.raises(ContradictionStructureError, match="must be a JSON number"):
            propose(
                {
                    "sections": [{"text": "x", "citation_ids": [1]}],
                    "contradictions": [{"claim_ids": ["101", 202]}],
                }
            )

    def test_an_unknown_field_on_a_contradiction_is_refused(self):
        with pytest.raises(ContradictionStructureError, match="unsupported field"):
            propose(
                {
                    "sections": [{"text": "x", "citation_ids": [1]}],
                    "contradictions": [{"claim_ids": [101, 202], "winner": 101}],
                }
            )

    def test_contradictions_may_be_absent_entirely(self):
        proposal = propose({"sections": [{"text": "x", "citation_ids": [1]}]})
        assert proposal.contradictions == ()


# --- the registry ------------------------------------------------------------


class TestCitationRegistry:
    def test_entries_are_canonically_ordered_by_rank_then_source_then_position(self):
        registry = CitationRegistry(
            run_id=1,
            entries=(
                entry(3, source_id=3, rank=1),
                entry(1, source_id=1, rank=0),
                entry(2, source_id=1, rank=0, ordinal=1),
            ),
        )
        assert [e.citation_id for e in registry.entries] == [1, 2, 3]

    def test_a_ranked_source_precedes_an_unranked_one(self):
        registry = CitationRegistry(
            run_id=1,
            entries=(
                entry(2, source_id=2, rank=None),
                entry(1, source_id=1, rank=5),
            ),
        )
        assert [e.citation_id for e in registry.entries] == [1, 2], (
            "a source P3.5 ranked is evidence we measured; an unranked one is not"
        )

    def test_ordering_is_reestablished_regardless_of_input_order(self):
        forwards = CitationRegistry(
            run_id=1, entries=(entry(1, rank=0), entry(2, rank=1, source_id=2))
        )
        backwards = CitationRegistry(
            run_id=1, entries=(entry(2, rank=1, source_id=2), entry(1, rank=0))
        )
        assert [e.citation_id for e in forwards.entries] == [
            e.citation_id for e in backwards.entries
        ]

    def test_the_input_listing_order_cannot_change_the_order(self):
        items = [entry(i, source_id=i, rank=i) for i in (3, 1, 2)]
        registry = CitationRegistry(run_id=1, entries=tuple(items))
        assert [e.citation_id for e in registry.entries] == [1, 2, 3]

    def test_a_duplicate_citation_id_is_refused(self):
        with pytest.raises(SynthesisValidationError, match="duplicate citation id"):
            CitationRegistry(run_id=1, entries=(entry(1), entry(1, source_id=2)))

    def test_an_entry_whose_quote_is_absent_from_its_passage_is_refused(self):
        """Defence in depth: a row edited after the fact cannot enter the registry.

        P3.7 verified containment before writing, and the repository re-checks it when
        building the registry. A registry that could hold such an entry would offer a
        model evidence whose quote is not in it.
        """
        with pytest.raises(CitationQuoteError):
            CitationRegistry(
                run_id=1, entries=(entry(1, quote="not present in the passage at all"),)
            )

    def test_a_broken_chain_is_refused_even_when_the_quote_verifies(self):
        """Both halves are checked: a valid quote does not excuse a wrong source.

        Otherwise a citation naming source 1 while pointing at source 2's passage would
        pass on the strength of its quote alone, which is the cross-source evidence link
        § 14 forbids.
        """
        with pytest.raises(CitationChainError):
            CitationRegistry(
                run_id=1, entries=(entry(1, source_id=1, chunk_source_id=2),)
            )

    def test_an_entry_with_an_empty_quote_is_refused(self):
        with pytest.raises(CitationQuoteError):
            CitationRegistry(run_id=1, entries=(entry(1, quote=""),))

    def test_a_too_short_quote_is_refused(self):
        """P3.7's ``MIN_QUOTE_CHARS`` applies here too: ``"the"`` supports nothing."""
        with pytest.raises(CitationQuoteError):
            CitationRegistry(run_id=1, entries=(entry(1, quote="the"),))

    def test_the_registry_reports_its_claim_and_source_ids(self):
        registry = two_source_registry()
        assert registry.citation_ids == frozenset({1, 2})
        assert registry.claim_ids == frozenset({101, 202})
        assert registry.source_ids == (1, 2)
        assert len(registry) == 2

    def test_entry_lookup_is_exact(self):
        registry = two_source_registry()
        assert registry.entry(1) is not None
        assert registry.entry(999) is None

    def test_the_registry_is_frozen(self):
        """Frozen, so the canonical order cannot be edited after verification sees it."""
        registry = two_source_registry()
        with pytest.raises(ValidationError):
            registry.run_id = 2

    def test_a_registry_with_a_broken_entry_is_refused(self):
        """Rule 3's chain, checked on the values rather than assumed from the FK.

        The composite foreign key P3.7 added makes such a row uninsertable, so this is
        defence in depth: it holds even if that constraint were ever relaxed, and it
        turns a corrupted read into a loud refusal rather than evidence offered to a
        model.
        """
        with pytest.raises(CitationChainError, match="belongs to source"):
            CitationRegistry(
                run_id=1, entries=(entry(1, source_id=1, chunk_source_id=2),)
            )

    def test_a_sound_entry_reports_its_chain_as_sound(self):
        assert entry(1, source_id=7).chain_is_sound is True
        assert entry(1, source_id=7, chunk_source_id=8).chain_is_sound is False

    def test_an_empty_registry_is_constructible(self):
        """A run with no verified evidence is a state the registry can express.

        The *service* turns it into a typed failure; the type being able to hold it
        keeps that decision in the layer that owns the run.
        """
        assert len(CitationRegistry(run_id=1)) == 0

    def test_quote_containment_uses_p3_7_normalization_and_nothing_else(self):
        wrapped = CitationEntry(
            citation_id=1,
            claim_id=1,
            claim_text=SURVEY_STATEMENT,
            source_id=1,
            chunk_source_id=1,
            chunk_id=1,
            ordinal=0,
            quote="recorded a forty percent increase",
            passage_text="The survey\n  recorded   a forty percent increase today.",
            source_url="https://example.com/a",
            source_domain="example.com",
        )
        assert wrapped.quote_is_real(), "whitespace collapsing is P3.7's rule"

        cased = entry(1, quote="RECORDED A FORTY PERCENT INCREASE")
        assert not cased.quote_is_real(), "no case folding: that would rewrite the source"

    def test_a_locator_is_derived_from_persisted_data_not_model_text(self):
        item = entry(1, ordinal=4)
        assert item.locator() == "chunk 4"

    


# --- the synthesis input -----------------------------------------------------


class TestSynthesisInput:
    def test_a_valid_input_is_constructible(self):
        synthesis_input = build_input()
        assert synthesis_input.evidence_count == 2

    def test_an_empty_objective_is_refused(self):
        reg = two_source_registry()
        with pytest.raises(SynthesisValidationError, match="must not be empty"):
            SynthesisInput(
                run_id=1,
                question="q",
                objective="   ",
                registry=reg,
                context=plan_synthesis_context(
                    reg, max_tokens=8_000, counter=COUNTER, max_items=30
                ),
            )

    def test_an_empty_question_is_refused(self):
        reg = two_source_registry()
        with pytest.raises(SynthesisValidationError, match="must not be empty"):
            SynthesisInput(
                run_id=1,
                question="",
                registry=reg,
                context=plan_synthesis_context(
                    reg, max_tokens=8_000, counter=COUNTER, max_items=30
                ),
            )

    def test_an_overlong_question_is_refused(self):
        reg = two_source_registry()
        with pytest.raises(SynthesisValidationError, match="at most"):
            SynthesisInput(
                run_id=1,
                question="x" * 5_000,
                registry=reg,
                context=plan_synthesis_context(
                    reg, max_tokens=8_000, counter=COUNTER, max_items=30
                ),
            )

    def test_an_empty_registry_is_refused(self):
        """No verified evidence means no answer — refused before any model call."""
        with pytest.raises(SynthesisValidationError, match="at least one persisted"):
            SynthesisInput(
                run_id=1,
                question="q",
                registry=CitationRegistry(run_id=1),
                context=plan_synthesis_context(
                    CitationRegistry(run_id=1),
                    max_tokens=8_000,
                    counter=COUNTER,
                    max_items=30,
                ),
            )

    def test_a_registry_from_another_run_is_refused(self):
        reg = two_source_registry()
        with pytest.raises(SynthesisValidationError, match="different research run"):
            SynthesisInput(
                run_id=2,
                question="q",
                registry=reg,
                context=plan_synthesis_context(
                    reg, max_tokens=8_000, counter=COUNTER, max_items=30
                ),
            )

    def test_a_context_that_hides_the_remainder_is_refused(self):
        """Coverage must be visible: a caller cannot claim completeness it cannot see.

        ``context.total`` is what makes truncation reportable (FR-42). A context whose
        counts do not add up — here claiming to drop 98 items the registry does not
        contain — is refused rather than presented as a subset of the whole.
        """
        reg = two_source_registry()
        with pytest.raises(SynthesisValidationError, match="dropped evidence count"):
            SynthesisContext(entries=reg.entries, total=99, dropped=98)

    def test_a_context_that_does_not_cover_the_registry_is_refused(self):
        """The context's total must be the registry's real size.

        Without this, a caller could report ``total`` far smaller than the run
        actually holds and present an answer as exhaustive when it is not (FR-42).
        The context object itself is well-formed — one entry, none dropped — so this
        is the check that binds the two together.
        """
        reg = two_source_registry()
        partial = SynthesisContext(entries=reg.entries[:1], total=1, dropped=0)
        with pytest.raises(SynthesisValidationError, match="full evidence count"):
            SynthesisInput(run_id=1, question="q", registry=reg, context=partial)

    def test_an_empty_context_is_refused(self):
        reg = two_source_registry()
        with pytest.raises(SynthesisValidationError, match="at least one evidence item"):
            SynthesisInput(
                run_id=1,
                question="q",
                registry=reg,
                context=SynthesisContext(entries=(), total=2, dropped=2),
            )

    @pytest.mark.parametrize("tag", ["fa", "en-GB", "EN", None])
    def test_a_language_tag_is_accepted(self, tag):
        reg = two_source_registry()
        built = SynthesisInput(
            run_id=1,
            question="q",
            language=tag,
            registry=reg,
            context=plan_synthesis_context(
                reg, max_tokens=8_000, counter=COUNTER, max_items=30
            ),
        )
        assert built.language == (tag.lower() if isinstance(tag, str) else None)

    @pytest.mark.parametrize("tag", ["fa ir!", "a" * 40, "toolongtagvalue"])
    def test_an_unusable_language_tag_is_refused(self, tag):
        reg = two_source_registry()
        with pytest.raises(SynthesisValidationError, match="BCP-47"):
            SynthesisInput(
                run_id=1,
                question="q",
                language=tag,
                registry=reg,
                context=plan_synthesis_context(
                    reg, max_tokens=8_000, counter=COUNTER, max_items=30
                ),
            )


# --- the context budget ------------------------------------------------------


class TestContextBudget:
    def test_everything_fits_when_the_budget_is_large(self):
        context = plan_synthesis_context(
            two_source_registry(), max_tokens=8_000, counter=COUNTER, max_items=30
        )
        assert context.dropped == 0
        assert context.truncated is False
        assert context.source_count == 2

    def test_the_item_limit_drops_the_lowest_ranked_evidence_first(self):
        registry = CitationRegistry(
            run_id=1,
            entries=tuple(
                entry(i, source_id=i, rank=i, passage=f"passage {i} " * 40, quote=f"passage {i} " * 12)
                for i in range(5)
            ),
        )
        context = plan_synthesis_context(
            registry, max_tokens=100_000, counter=COUNTER, max_items=2
        )
        assert [e.citation_id for e in context.entries] == [0, 1]
        assert context.dropped == 3
        assert context.truncated is True

    def test_the_token_budget_bounds_the_context(self):
        registry = CitationRegistry(
            run_id=1,
            entries=tuple(
                entry(
                    i,
                    source_id=i,
                    rank=i,
                    passage="word " * 200,
                    quote="word " * 200,
                )
                for i in range(6)
            ),
        )
        context = plan_synthesis_context(
            registry, max_tokens=2_000, counter=COUNTER, max_items=30
        )
        assert context.total == 6
        assert len(context.entries) < 6, "the budget must bind"
        assert context.dropped == 6 - len(context.entries)

    def test_an_oversized_item_is_still_offered(self):
        """Dropping it would remove evidence and quietly shrink coverage.

        Showing it oversized is the honest failure, and the count tells the caller.
        """
        registry = CitationRegistry(
            run_id=1,
            entries=(
                entry(1, passage="word " * 5_000, quote="word " * 5_000),
                entry(2, source_id=2, rank=1),
            ),
        )
        context = plan_synthesis_context(
            registry, max_tokens=1_000, counter=COUNTER, max_items=30
        )
        assert [e.citation_id for e in context.entries] == [1]
        assert context.dropped == 1

    def test_the_context_is_deterministic(self):
        registry = two_source_registry()
        runs = [
            plan_synthesis_context(
                registry, max_tokens=2_000, counter=COUNTER, max_items=1
            )
            for _ in range(5)
        ]
        assert len({c.citation_ids for c in runs}) == 1

    def test_the_context_order_does_not_depend_on_the_input_order(self):
        items = [entry(i, source_id=i, rank=i) for i in (2, 0, 1)]
        forwards = CitationRegistry(run_id=1, entries=tuple(items))
        backwards = CitationRegistry(run_id=1, entries=tuple(reversed(items)))
        assert [
            plan_synthesis_context(
                forwards, max_tokens=8_000, counter=COUNTER, max_items=1
            ).citation_ids
        ] == [
            plan_synthesis_context(
                backwards, max_tokens=8_000, counter=COUNTER, max_items=1
            ).citation_ids
        ]

    @pytest.mark.parametrize("max_tokens,max_items", [(0, 5), (5, 0)])
    def test_a_nonsensical_budget_is_refused(self, max_tokens, max_items):
        with pytest.raises(SynthesisValidationError):
            plan_synthesis_context(
                two_source_registry(),
                max_tokens=max_tokens,
                counter=COUNTER,
                max_items=max_items,
            )

    def test_the_context_reports_the_ids_the_model_may_use(self):
        context = plan_synthesis_context(
            two_source_registry(), max_tokens=8_000, counter=COUNTER, max_items=1
        )
        assert len(context.citation_ids) == 1
        assert context.citation_ids <= two_source_registry().citation_ids


# --- verification ------------------------------------------------------------


class TestVerificationAccepts:
    def test_a_valid_proposal_becomes_an_answer(self):
        answer = verify_synthesis(
            propose(
                {
                    "sections": [
                        {"heading": "Findings", "text": "A rise.", "citation_ids": [1]},
                        {"heading": "Other", "text": "A budget page.", "citation_ids": [2]},
                    ]
                }
            ),
            build_input(),
        )
        assert answer.run_id == 1
        assert len(answer.sections) == 2
        assert answer.citation_count == 2
        assert answer.source_count == 2

    def test_one_section_may_cite_several_citations(self):
        answer = verify_synthesis(
            propose(
                {"sections": [{"text": "Both agree.", "citation_ids": [2, 1]}]}
            ),
            build_input(),
        )
        assert answer.sections[0].citation_ids == (1, 2)

    def test_a_section_may_repeat_a_citation_deterministically(self):
        """Duplicates collapse on first occurrence, and the answer is unchanged."""
        proposal = propose({"sections": [{"text": "x", "citation_ids": [1, 1, 1]}]})
        answer = verify_synthesis(proposal, build_input())
        assert answer.sections[0].citation_ids == (1,)

    def test_two_sections_may_cite_the_same_citation(self):
        answer = verify_synthesis(
            propose(
                {
                    "sections": [
                        {"text": "One.", "citation_ids": [1]},
                        {"text": "Two.", "citation_ids": [1]},
                    ]
                }
            ),
            build_input(),
        )
        assert answer.citation_count == 1, "distinct citations, not occurrences"
        assert len(answer.sections) == 2

    def test_the_answer_carries_the_question_objective_and_language(self):
        answer = verify_synthesis(
            propose({"sections": [{"text": "x", "citation_ids": [1]}]}), build_input()
        )
        assert answer.question == "What did the 2024 survey find?"
        assert answer.objective == "Establish what the survey measured"
        assert answer.language == "en"

    def test_coverage_counters_are_carried_for_reporting(self):
        answer = verify_synthesis(
            propose({"sections": [{"text": "x", "citation_ids": [1]}]}), build_input()
        )
        assert answer.evidence_offered == 2
        assert answer.evidence_available == 2
        assert answer.evidence_truncated is False

    def test_a_truncated_context_is_visible_on_the_answer(self):
        synthesis_input = build_input(max_items=1)
        answer = verify_synthesis(
            propose({"sections": [{"text": "x", "citation_ids": [1]}]}), synthesis_input
        )
        assert answer.evidence_offered == 1
        assert answer.evidence_available == 2
        assert answer.evidence_truncated is True

    def test_the_prose_is_normalized(self):
        answer = verify_synthesis(
            propose({"sections": [{"text": "  spaced   out  ", "citation_ids": [1]}]}),
            build_input(),
        )
        assert answer.sections[0].text == "spaced out"


class TestVerificationRefuses:
    def test_an_unknown_citation_is_refused(self):
        with pytest.raises(UnknownCitationError) as caught:
            verify_synthesis(
                propose({"sections": [{"text": "x", "citation_ids": [999]}]}),
                build_input(),
            )
        assert caught.value.failures[0].kind is CitationFailureKind.UNKNOWN_CITATION

    def test_a_foreign_run_citation_is_refused(self):
        """Rule 2: an id that exists elsewhere is *absent* from this run's registry.

        The registry is scoped by ``claims.run_id``, so a citation of another run
        cannot be resolved here at all — the check is structural, not a comparison.
        """
        with pytest.raises(UnknownCitationError) as caught:
            verify_synthesis(
                propose({"sections": [{"text": "x", "citation_ids": [4242]}]}),
                build_input(),
            )
        assert "not a citation of this run" in str(caught.value).lower() or (
            caught.value.failures[0].kind is CitationFailureKind.UNKNOWN_CITATION
        )

    def test_a_citation_outside_the_shown_context_is_refused(self):
        """A real citation the call never saw is as ungrounded as an invented one."""
        with pytest.raises(UnshownCitationError) as caught:
            verify_synthesis(
                propose({"sections": [{"text": "x", "citation_ids": [2]}]}),
                build_input(max_items=1),
            )
        assert caught.value.failures[0].kind is CitationFailureKind.UNSHOWN_CITATION

    def test_a_section_with_no_citation_is_refused(self):
        with pytest.raises(CitationCompletenessError):
            verify_synthesis(
                propose({"sections": [{"text": "Uncited assertion."}]}), build_input()
            )

    def test_a_section_whose_only_citation_is_fabricated_is_refused(self):
        with pytest.raises(UnknownCitationError):
            verify_synthesis(
                propose({"sections": [{"text": "Invented.", "citation_ids": [7, 8]}]}),
                build_input(),
            )

    def test_one_fabricated_citation_refuses_the_whole_answer(self):
        """§ 17: no state where a partially-deceptive proposal becomes an answer.

        The good section beside the fabricated one is not delivered. Stripping the bad
        reference and returning the rest is precisely the "apparently valid answer"
        SR-9 forbids.
        """
        with pytest.raises(UnknownCitationError):
            verify_synthesis(
                propose(
                    {
                        "sections": [
                            {"text": "Real and cited.", "citation_ids": [1]},
                            {"text": "Fabricated.", "citation_ids": [999]},
                        ]
                    }
                ),
                build_input(),
            )

    def test_every_failure_is_reported_not_only_the_first(self):
        """Every bad reference is counted, not just the one that raised.

        The count matters for FR-42: a caller reporting a failed run has to say *how
        many* references did not resolve and by category. Raising on the first and
        discarding the rest would make that impossible.

        Each section also contributes an ``UNCITED_SECTION`` finding, because a section
        whose only citation was fabricated is left with nothing — so six findings
        describe three bad references honestly rather than hiding the second half of
        the problem.
        """
        with pytest.raises(UnknownCitationError) as caught:
            verify_synthesis(
                propose(
                    {
                        "sections": [
                            {"text": "a", "citation_ids": [101]},
                            {"text": "b", "citation_ids": [102]},
                            {"text": "c", "citation_ids": [103]},
                        ]
                    }
                ),
                build_input(),
            )
        counts = caught.value.counts_by_kind()
        assert counts[CitationFailureKind.UNKNOWN_CITATION] == 3
        assert counts[CitationFailureKind.UNCITED_SECTION] == 3

    def test_failures_are_ordered_deterministically(self):
        """Section order first, then the order the model listed its references."""
        payload = {
            "sections": [
                {"text": "a", "citation_ids": [201, 101]},
                {"text": "b", "citation_ids": [301, 1]},
            ]
        }
        orders = []
        for _ in range(3):
            with pytest.raises(UnknownCitationError) as caught:
                verify_synthesis(propose(payload), build_input())
            orders.append(
                [(f.section_index, f.reference) for f in caught.value.failures]
            )
        assert orders[0] == orders[1] == orders[2]
        assert [(i, r) for i, r in orders[0] if r is not None] == [
            (0, 201),
            (0, 101),
            (1, 301),
        ]

    def test_an_empty_section_is_refused(self):
        with pytest.raises(SynthesisValidationError, match="states no text"):
            verify_synthesis(
                propose({"sections": [{"text": "   ", "citation_ids": [1]}]}), build_input()
            )

    def test_an_overlong_section_is_refused(self):
        with pytest.raises(SynthesisValidationError, match="at most"):
            verify_synthesis(
                propose({"sections": [{"text": "x" * 5_000, "citation_ids": [1]}]}),
                build_input(),
            )

    def test_an_overlong_heading_is_refused(self):
        with pytest.raises(SynthesisValidationError, match="heading"):
            verify_synthesis(
                propose(
                    {
                        "sections": [
                            {"heading": "h" * 500, "text": "x", "citation_ids": [1]}
                        ]
                    }
                ),
                build_input(),
            )

    def test_a_contradiction_naming_an_unknown_claim_is_refused(self):
        with pytest.raises(ContradictionReferenceError) as caught:
            verify_synthesis(
                propose(
                    {
                        "sections": [{"text": "x", "citation_ids": [1]}],
                        "contradictions": [{"claim_ids": [101, 9999]}],
                    }
                ),
                build_input(),
            )
        assert caught.value.failures[0].kind is CitationFailureKind.UNKNOWN_CLAIM

    def test_a_claim_cannot_contradict_itself(self):
        with pytest.raises(ContradictionReferenceError) as caught:
            verify_synthesis(
                propose(
                    {
                        "sections": [{"text": "x", "citation_ids": [1]}],
                        "contradictions": [{"claim_ids": [101, 101]}],
                    }
                ),
                build_input(),
            )
        assert caught.value.failures[0].kind is CitationFailureKind.SELF_CONTRADICTION

    def test_a_contradiction_with_no_claim_on_one_side_is_refused(self):
        """An absent pair is a shape failure at parsing, not a silent half-conflict."""
        with pytest.raises(ContradictionStructureError, match="is required"):
            propose(
                {
                    "sections": [{"text": "x", "citation_ids": [1]}],
                    "contradictions": [{"claim_ids": None}],
                }
            )

    def test_a_claim_never_loses_its_citations_to_a_reported_contradiction(self):
        """The conflict is refused as a *report*, not by deleting evidence.

        FR-34 says contradictions are reported rather than reconciled. When the
        report itself is uncheckable, the right answer is to refuse the report — not
        to drop a claim, which would be reconciling it by erasure.
        """
        synthesis_input = build_input()
        with pytest.raises(ContradictionReferenceError):
            verify_synthesis(
                propose(
                    {
                        "sections": [{"text": "x", "citation_ids": [1]}],
                        "contradictions": [{"claim_ids": [101, 9999]}],
                    }
                ),
                synthesis_input,
            )
        # Both claims are still citable evidence of this run.
        assert synthesis_input.registry.claim_ids == frozenset({101, 202})

    def test_a_section_citing_a_citation_of_a_section_it_did_not_see_is_refused(self):
        with pytest.raises(UnshownCitationError):
            verify_synthesis(
                propose(
                    {
                        "sections": [
                            {"text": "x", "citation_ids": [1]},
                            {"text": "y", "citation_ids": [2]},
                        ]
                    }
                ),
                build_input(max_items=1),
            )


# --- cross-source behaviour --------------------------------------------------


class TestCrossSourceSynthesis:
    def test_two_sources_are_synthesized_into_one_answer(self):
        answer = verify_synthesis(
            propose(
                {
                    "sections": [
                        {"text": "Source A measured a rise.", "citation_ids": [1]},
                        {"text": "Source B is a budget page.", "citation_ids": [2]},
                    ]
                }
            ),
            build_input(),
        )
        assert answer.source_count == 2
        assert answer.cited_source_ids == (1, 2)

    def test_source_identity_is_preserved_per_citation(self):
        answer = verify_synthesis(
            propose({"sections": [{"text": "x", "citation_ids": [1, 2]}]}), build_input()
        )
        first, second = answer.sections[0].citations
        assert first.source_id == 1
        assert first.source_domain == "example.com"
        assert second.source_id == 2
        assert second.source_domain == "second.example"

    def test_a_supporting_pair_keeps_both_citations_on_one_section(self):
        """"Source A supports X and Source B supports X" is representable."""
        answer = verify_synthesis(
            propose({"sections": [{"text": "Both measured a rise.", "citation_ids": [1, 2]}]}),
            build_input(),
        )
        assert len(answer.sections[0].citations) == 2
        assert answer.sections[0].source_ids == (1, 2)

    def test_a_conflict_keeps_both_sides_with_their_citations(self):
        """FR-34: the disagreement is preserved, with its evidence."""
        answer = verify_synthesis(
            propose(
                {
                    "sections": [{"text": "Sources disagree.", "citation_ids": [1]}],
                    "contradictions": [
                        {
                            "claim_ids": [101, 202],
                            "note": "One reports a rise, the other a flat budget.",
                        }
                    ],
                }
            ),
            build_input(),
        )
        assert answer.has_contradictions
        conflict = answer.contradictions[0]
        assert {conflict.left_claim_id, conflict.right_claim_id} == {101, 202}
        assert {c.claim_id for c in conflict.citations} == {101, 202}, (
            "both sides must keep their citations"
        )
        assert len(conflict.citations) == 2

    def test_a_conflict_does_not_remove_either_side_from_the_answer(self):
        answer = verify_synthesis(
            propose(
                {
                    "sections": [
                        {"text": "Source A says X.", "citation_ids": [1]},
                        {"text": "Source B says Y.", "citation_ids": [2]},
                    ],
                    "contradictions": [{"claim_ids": [101, 202], "note": "They differ."}],
                }
            ),
            build_input(),
        )
        assert len(answer.sections) == 2
        assert answer.cited_claim_ids == (101, 202)

    def test_a_repeated_conflict_is_recorded_once(self):
        answer = verify_synthesis(
            propose(
                {
                    "sections": [{"text": "x", "citation_ids": [1]}],
                    "contradictions": [
                        {"claim_ids": [101, 202]},
                        {"claim_ids": [202, 101]},
                    ],
                }
            ),
            build_input(),
        )
        assert len(answer.contradictions) == 1

    def test_a_conflict_is_stored_with_its_sides_in_a_canonical_order(self):
        """The pair is unordered, so the stored form must be too."""
        answer = verify_synthesis(
            propose(
                {
                    "sections": [{"text": "x", "citation_ids": [1]}],
                    "contradictions": [{"claim_ids": [202, 101]}],
                }
            ),
            build_input(),
        )
        conflict = answer.contradictions[0]
        assert (conflict.left_claim_id, conflict.right_claim_id) == (101, 202)

    def test_a_conflict_without_a_note_gets_a_neutral_one(self):
        answer = verify_synthesis(
            propose(
                {
                    "sections": [{"text": "x", "citation_ids": [1]}],
                    "contradictions": [{"claim_ids": [101, 202]}],
                }
            ),
            build_input(),
        )
        assert answer.contradictions[0].note == "these sources disagree"

    def test_the_answer_serializes_the_conflict_with_both_sides(self):
        answer = verify_synthesis(
            propose(
                {
                    "sections": [{"text": "x", "citation_ids": [1]}],
                    "contradictions": [{"claim_ids": [101, 202], "note": "They differ."}],
                }
            ),
            build_input(),
        )
        payload = answer.to_payload()
        assert payload["contradictions"][0]["left_claim_id"] == 101
        assert len(payload["contradictions"][0]["citations"]) == 2


# --- trusted values cannot be fabricated -------------------------------------


class TestTrustedTypesCannotBeConstructed:
    @pytest.mark.parametrize(
        "cls", [VerifiedCitation, VerifiedSection, VerifiedContradiction, SynthesizedAnswer]
    )
    def test_the_type_refuses_direct_construction(self, cls):
        """The core structural guarantee of the stage.

        ``SynthesizedAnswer`` is the trusted research output, and there is no
        ``__init__`` outside verification that can produce one. A caller cannot assert
        a citation; only ``verify_synthesis`` can.
        """
        with pytest.raises(CitationChainError, match="only be constructed"):
            cls()

    def test_model_validate_is_also_closed(self):
        """``model_validate`` bypasses ``__init__``, so the guard is a validator.

        A guard implemented only in ``__init__`` would be trivially routed around.
        """
        with pytest.raises(CitationChainError, match="only be constructed"):
            SynthesizedAnswer.model_validate({"run_id": 1, "question": "q"})

    def test_a_forged_verification_marker_does_not_work(self):
        """The check is against a module-private object identity, not a key.

        The marker key is visible in the source, so a guard that checked only for the
        key's presence would be forgeable from any payload.
        """
        with pytest.raises(CitationChainError):
            SynthesizedAnswer.model_validate(
                {"_verified_by": "anything at all", "run_id": 1, "question": "q"}
            )

    def test_a_section_cannot_be_built_without_citations(self):
        with pytest.raises(CitationChainError):
            VerifiedSection.model_validate({"_verified_by": object(), "index": 0})

    def test_the_verifier_is_the_only_documented_constructor(self):
        """Asserted on the signature, so a future refactor cannot quietly widen it."""
        parameters = inspect.signature(verify_synthesis).parameters
        assert list(parameters) == ["proposal", "synthesis_input"]

    def test_the_verifier_takes_no_optional_authority_argument(self):
        """It resolves against a registry, not against caller-supplied truth."""
        assert all(
            parameter.default is inspect.Parameter.empty
            for parameter in inspect.signature(verify_synthesis).parameters.values()
        )


# --- determinism -------------------------------------------------------------


class TestDeterminism:
    def test_the_same_input_yields_the_same_answer(self):
        synthesis_input = build_input()
        payload = {
            "sections": [
                {"text": "One.", "citation_ids": [1]},
                {"text": "Two.", "citation_ids": [2]},
            ]
        }
        results = [
            verify_synthesis(propose(payload), synthesis_input).to_payload()
            for _ in range(5)
        ]
        assert len({r["sections"][0]["citations"][0]["citation_id"] for r in results}) == 1
        assert results[0] == results[1] == results[4]

    def test_the_citation_order_is_the_registry_order_not_the_models(self):
        """The model's listing order cannot influence what is stored."""
        synthesis_input = build_input()
        forwards = verify_synthesis(
            propose({"sections": [{"text": "x", "citation_ids": [1, 2]}]}), synthesis_input
        )
        backwards = verify_synthesis(
            propose({"sections": [{"text": "x", "citation_ids": [2, 1]}]}), synthesis_input
        )
        assert forwards.sections[0].citation_ids == backwards.sections[0].citation_ids

    def test_the_registry_listing_order_does_not_change_the_answer(self):
        items = [
            entry(1, source_id=1, rank=0),
            entry(2, source_id=2, rank=1, passage=BUDGET, quote=BUDGET_QUOTE,
                  statement=BUDGET_STATEMENT, domain="second.example"),
        ]
        first = build_input(CitationRegistry(run_id=1, entries=tuple(items)))
        second = build_input(CitationRegistry(run_id=1, entries=tuple(reversed(items))))
        payload = {"sections": [{"text": "x", "citation_ids": [1, 2]}]}
        assert (
            verify_synthesis(propose(payload), first).to_payload()
            == verify_synthesis(propose(payload), second).to_payload()
        )

    def test_the_domain_uses_no_clock(self):
        """No wall-clock read: the same evidence always yields the same context.

        Parsed from the AST rather than grepped, so a computed clock call cannot hide.
        """
        import app.domain.research.synthesis as module

        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        clock_calls = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        assert clock_calls.isdisjoint({"now", "utcnow", "today"})

    def test_the_domain_imports_no_io_library(self):
        import app.domain.research.synthesis as module

        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                imported.add(node.module.split(".")[0])
        assert imported.isdisjoint(
            {
                "httpx",
                "aiohttp",
                "socket",
                "subprocess",
                "os",
                "sqlalchemy",
                "openai",
                "random",
                "time",
                "datetime",
            }
        ), f"unexpected import: {imported}"


# --- multilingual ------------------------------------------------------------


class TestMultilingualContent:
    def test_a_persian_answer_verifies(self):
        registry = CitationRegistry(
            run_id=1,
            entries=(
                entry(
                    1,
                    passage=PERSIAN,
                    quote=PERSIAN_QUOTE,
                    statement=PERSIAN_STATEMENT,
                    domain="fa.example",
                    title="گزارش میدانی",
                ),
            ),
        )
        answer = verify_synthesis(
            propose(
                {
                    "sections": [
                        {
                            "heading": "یافته‌ها",
                            "text": "پژوهشگران افزایش چهل درصدی را گزارش کردند.",
                            "citation_ids": [1],
                        }
                    ]
                }
            ),
            build_input(registry, max_items=10),
        )
        assert answer.sections[0].heading == "یافته‌ها"
        assert "نمونه‌های" in answer.sections[0].citations[0].quote, "ZWNJ must survive"

    def test_an_arabic_indic_digit_quote_verifies(self):
        registry = CitationRegistry(
            run_id=1,
            entries=(
                entry(
                    1,
                    passage=PERSIAN,
                    quote="سال ۱۴۰۳ افزایش چهل درصدی",
                    statement=PERSIAN_STATEMENT,
                    domain="fa.example",
                ),
            ),
        )
        answer = verify_synthesis(
            propose({"sections": [{"text": "گزارش.", "citation_ids": [1]}]}),
            build_input(registry, max_items=10),
        )
        assert "۱۴۰۳" in answer.sections[0].citations[0].quote

    def test_a_latin_digit_quote_against_persian_digits_does_not_verify(self):
        """Digits are content: a quote that transliterates them is not a quote."""
        with pytest.raises(CitationQuoteError):
            CitationRegistry(
                run_id=1,
                entries=(
                    entry(
                        1,
                        passage=PERSIAN,
                        quote="سال 1403 افزایش چهل درصدی",
                        statement=PERSIAN_STATEMENT,
                        domain="fa.example",
                    ),
                ),
            )

    def test_a_mixed_language_answer_verifies(self):
        registry = CitationRegistry(
            run_id=1,
            entries=(
                entry(
                    1,
                    passage=MIXED,
                    quote=MIXED_QUOTE,
                    statement="گزارش بر پایهٔ field survey.",
                    domain="fa.example",
                ),
            ),
        )
        answer = verify_synthesis(
            propose({"sections": [{"text": "x", "citation_ids": [1]}]}),
            build_input(registry, max_items=10),
        )
        assert "field survey" in answer.sections[0].citations[0].quote

    def test_the_question_is_not_lowercased(self):
        registry = two_source_registry()
        built = SynthesisInput(
            run_id=1,
            question="What Did The 2024 Survey FIND?",
            registry=registry,
            context=plan_synthesis_context(
                registry, max_tokens=8_000, counter=COUNTER, max_items=30
            ),
        )
        assert built.question == "What Did The 2024 Survey FIND?"

    def test_a_persian_question_survives(self):
        registry = two_source_registry()
        built = SynthesisInput(
            run_id=1,
            question="پژوهش ۱۴۰۳ چه چیزی یافت؟",
            language="fa",
            registry=registry,
            context=plan_synthesis_context(
                registry, max_tokens=8_000, counter=COUNTER, max_items=30
            ),
        )
        assert built.question == "پژوهش ۱۴۰۳ چه چیزی یافت؟"
        assert built.language == "fa"


# --- answer shape ------------------------------------------------------------


class TestAnswerShape:
    def _answer(self):
        return verify_synthesis(
            propose(
                {
                    "sections": [
                        {"heading": "A", "text": "First.", "citation_ids": [1]},
                        {"heading": "B", "text": "Second.", "citation_ids": [2]},
                    ],
                    "contradictions": [{"claim_ids": [101, 202], "note": "They differ."}],
                }
            ),
            build_input(),
        )

    def test_plain_text_has_no_citation_markers(self):
        text = self._answer().plain_text
        assert "[1]" not in text and "(1)" not in text
        assert "First." in text and "Second." in text

    def test_plain_text_keeps_the_heading_above_its_section(self):
        assert self._answer().plain_text == "A\nFirst.\n\nB\nSecond."

    def test_the_answer_is_frozen(self):
        """Frozen, so nothing downstream can edit a verified answer's citations."""
        with pytest.raises(ValidationError):
            self._answer().run_id = 2

    def test_the_payload_has_a_fixed_key_order(self):
        assert list(self._answer().to_payload()) == [
            "schema_version",
            "run_id",
            "question",
            "objective",
            "language",
            "evidence_offered",
            "evidence_available",
            "sections",
            "contradictions",
        ]

    def test_every_citation_payload_field_comes_from_persisted_data(self):
        """Rule 5 and Rule 6, observed on the stored shape.

        ``quote`` and ``source_url`` are in the payload, and both were read from
        persisted rows — there is no field in the proposal that could have supplied
        either.
        """
        entry_payload = self._answer().to_payload()["sections"][0]["citations"][0]
        assert set(entry_payload) == {
            "citation_id",
            "claim_id",
            "source_id",
            "chunk_id",
            "ordinal",
            "quote",
            "location",
            "source_url",
            "source_domain",
            "source_title",
        }
        assert entry_payload["quote"] == SURVEY_QUOTE
        assert entry_payload["source_url"] == "https://example.com/survey"

    def test_the_locator_is_the_persisted_location(self):
        payload = self._answer().to_payload()
        assert payload["sections"][0]["citations"][0]["location"] == "chunk 0"

    def test_the_distinct_citation_helper_merges_sections_and_conflicts(self):
        answer = self._answer()
        assert {c.citation_id for c in answer.citations()} == {1, 2}

    def test_an_answer_serializes_and_rebuilds_through_the_registry(self):
        answer = self._answer()
        rebuilt = SynthesizedAnswer.from_payload(
            answer.to_payload(), registry=two_source_registry()
        )
        assert rebuilt.to_payload() == answer.to_payload()


class TestStoredAnswerReverification:
    """A stored answer is re-checked, never trusted (§ 16, SR-9)."""

    def _payload(self):
        return verify_synthesis(
            propose(
                {
                    "sections": [
                        {"text": "One.", "citation_ids": [1]},
                        {"text": "Two.", "citation_ids": [2]},
                    ]
                }
            ),
            build_input(),
        ).to_payload()

    def test_a_clean_payload_rebuilds(self):
        rebuilt = SynthesizedAnswer.from_payload(
            self._payload(), registry=two_source_registry()
        )
        assert rebuilt.citation_count == 2

    def test_an_edited_citation_id_is_refused(self):
        payload = self._payload()
        payload["sections"][0]["citations"][0]["citation_id"] = 999
        with pytest.raises(UnknownCitationError):
            SynthesizedAnswer.from_payload(payload, registry=two_source_registry())

    def test_a_repointed_claim_id_is_refused(self):
        """Every id is individually real; they simply must agree with the evidence."""
        payload = self._payload()
        payload["sections"][0]["citations"][0]["claim_id"] = 202
        with pytest.raises(Exception, match="different claim_id"):
            SynthesizedAnswer.from_payload(payload, registry=two_source_registry())

    def test_a_repointed_source_id_is_refused(self):
        payload = self._payload()
        payload["sections"][0]["citations"][0]["source_id"] = 2
        with pytest.raises(Exception, match="different source_id"):
            SynthesizedAnswer.from_payload(payload, registry=two_source_registry())

    def test_a_repointed_chunk_id_is_refused(self):
        payload = self._payload()
        payload["sections"][0]["citations"][0]["chunk_id"] = 999
        with pytest.raises(Exception, match="different chunk_id"):
            SynthesizedAnswer.from_payload(payload, registry=two_source_registry())

    def test_a_fabricated_url_is_refused(self):
        """Rule 5 on the read path: a hand-edited URL is caught against the source row."""
        payload = self._payload()
        payload["sections"][0]["citations"][0]["source_url"] = "https://evil.example/x"
        with pytest.raises(Exception, match="not the persisted source URL"):
            SynthesizedAnswer.from_payload(payload, registry=two_source_registry())

    def test_a_fabricated_quote_is_refused(self):
        payload = self._payload()
        payload["sections"][0]["citations"][0]["quote"] = "a completely different quote"
        with pytest.raises(Exception, match="not the verified passage"):
            SynthesizedAnswer.from_payload(payload, registry=two_source_registry())

    def test_a_section_stripped_of_its_citations_is_refused(self):
        payload = self._payload()
        payload["sections"][0]["citations"] = []
        with pytest.raises(CitationCompletenessError):
            SynthesizedAnswer.from_payload(payload, registry=two_source_registry())

    def test_a_registry_from_another_run_is_refused(self):
        with pytest.raises(SynthesisValidationError, match="does not belong"):
            SynthesizedAnswer.from_payload(
                self._payload(),
                registry=CitationRegistry(
                    run_id=2, entries=two_source_registry().entries
                ),
            )

    def test_an_unreadable_schema_version_is_refused(self):
        payload = self._payload()
        payload["schema_version"] = "research-synthesis-v0"
        with pytest.raises(SynthesisValidationError, match="cannot be read"):
            SynthesizedAnswer.from_payload(payload, registry=two_source_registry())

    def test_a_payload_with_no_sections_is_refused(self):
        payload = self._payload()
        payload["sections"] = []
        with pytest.raises(SynthesisValidationError, match="at least one section"):
            SynthesizedAnswer.from_payload(payload, registry=two_source_registry())

    def test_a_payload_that_is_not_an_object_is_refused(self):
        with pytest.raises(SynthesisValidationError):
            SynthesizedAnswer.from_payload(["not", "an", "object"], registry=two_source_registry())

    def test_a_stored_conflict_naming_an_unknown_claim_shape_is_refused(self):
        payload = self._payload()
        payload["contradictions"] = [{"left_claim_id": 101, "right_claim_id": "x",
                                       "note": "", "citations": []}]
        with pytest.raises(ContradictionReferenceError):
            SynthesizedAnswer.from_payload(payload, registry=two_source_registry())