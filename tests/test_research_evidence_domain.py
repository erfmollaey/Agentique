"""Per-source evidence domain contracts (P3.7, FR-31, FR-32, AD-033).

The trust decision this stage makes lives in pure code, so it is tested here with
no infrastructure at all: no database, no model, no network, no clock.

Four groups, in the order they matter:

1. **the value types** — what a passage, a source's content, a summary, and a
   candidate may be, and what they may never be;
2. **verification** — which candidates become claims with evidence, which are
   rejected, and with what reason (§ 13's four steps);
3. **the window planner** — the deterministic budget policy that keeps a long
   source bounded without silently losing evidence (§ 16);
4. **structure and determinism** — frozen values, no I/O, no randomness, no clock,
   and an inspection of the module's own import list.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.domain.errors import (
    ClaimExtractionSchemaError,
    ClaimStructureError,
    EvidenceError,
    SourceContentEmptyError,
    SummaryInputError,
    SummarySchemaError,
    SummaryValidationError,
)
from app.domain.research.evidence import (
    CLAIM_EXTRACTION_SCHEMA_VERSION,
    MAX_CLAIMS_CEILING,
    MAX_QUOTE_CHARS,
    MAX_STATEMENT_CHARS,
    MAX_SUMMARY_CHARS,
    MIN_QUOTE_CHARS,
    SUMMARY_SCHEMA_VERSION,
    ChunkWindow,
    ClaimExtraction,
    ClaimVerification,
    EvidenceChunk,
    EvidenceReference,
    EvidenceRejection,
    RejectedClaim,
    SourceContent,
    SourceEvidenceResult,
    SourceSummary,
    VerifiedClaim,
    normalize_evidence_text,
    plan_chunk_windows,
    verify_claims,
)

# --- fixtures ---------------------------------------------------------------

#: One shared literal per Persian string. Written twice by hand, the same Persian
#: phrase comes out spelled two different ways — the class of mistake constants
#: remove.
PERSIAN_PASSAGE = (
    "پژوهشگران در سال ۱۴۰۳ افزایش چهل درصدی را در نمونه‌های شهری گزارش کردند."
)
PERSIAN_QUOTE = "افزایش چهل درصدی را در نمونه‌های شهری"
PERSIAN_STATEMENT = "پژوهشگران افزایش چهل درصدی را گزارش کردند."
PERSIAN_TITLE = "گزارش پژوهش دربارهٔ رشد شهری"

ENGLISH_PASSAGE = (
    "The 2024 field survey recorded a forty percent increase across the sampled "
    "districts, according to the published methodology section."
)
ENGLISH_QUOTE = "recorded a forty percent increase across the sampled districts"
MIXED_PASSAGE = (
    "این گزارش بر پایهٔ field survey منتشر شده و رشد ۴۰ درصدی را نشان می‌دهد."
)
MIXED_QUOTE = "field survey منتشر شده و رشد ۴۰ درصدی"


class FixedCounter:
    """A token estimate with no configuration behind it: one token per 4 chars."""

    def count(self, text: str) -> int:
        return max(1, -(-len(text) // 4)) if text else 0


def chunk(chunk_id: int = 10, ordinal: int = 0, text: str = ENGLISH_PASSAGE) -> EvidenceChunk:
    return EvidenceChunk(chunk_id=chunk_id, ordinal=ordinal, text=text)


def content(*chunks: EvidenceChunk, source_id: int = 1, domain: str = "example.com", title: str | None = "A study") -> SourceContent:
    return SourceContent(
        source_id=source_id, domain=domain, title=title, chunks=chunks or (chunk(),)
    )


def extraction(*claims: dict, source_id: int = 1) -> ClaimExtraction:
    return ClaimExtraction.from_payload({"claims": list(claims)}, source_id=source_id)


def good_claim(chunk_id: int = 10, quote: str = ENGLISH_QUOTE, statement: str = "The survey recorded a forty percent increase.") -> dict:
    return {"statement": statement, "source_chunk_id": chunk_id, "quote": quote}


# --- EvidenceChunk ----------------------------------------------------------


class TestEvidenceChunk:
    def test_a_passage_is_a_positive_id_an_ordinal_and_text(self):
        c = chunk(chunk_id=7, ordinal=3, text="some stored passage text")
        assert (c.chunk_id, c.ordinal) == (7, 3)

    @pytest.mark.parametrize("bad_id", [0, -1, -1000])
    def test_a_passage_id_must_be_positive(self, bad_id):
        with pytest.raises(EvidenceError):
            chunk(chunk_id=bad_id)

    def test_an_ordinal_must_not_be_negative(self):
        with pytest.raises(EvidenceError):
            chunk(ordinal=-1)

    @pytest.mark.parametrize("bad", ["", "   ", "\n\t ", None, 42, ["text"]])
    def test_a_passage_must_carry_readable_text(self, bad):
        with pytest.raises(EvidenceError):
            EvidenceChunk(chunk_id=1, ordinal=0, text=bad)

    @pytest.mark.parametrize(
        "control", ["a\x00b", "a\x07b", "a\x1bb", "a\x7fb", "a\x85b"]
    )
    def test_a_passage_refuses_control_characters(self, control):
        """A control character can disguise one passage boundary as another."""
        with pytest.raises(EvidenceError):
            EvidenceChunk(chunk_id=1, ordinal=0, text=control)

    @pytest.mark.parametrize("allowed", ["a\tb", "a\nb", "a\rb"])
    def test_whitespace_controls_are_tolerated_because_the_normalizer_handles_them(self, allowed):
        assert EvidenceChunk(chunk_id=1, ordinal=0, text=allowed).text == allowed

    def test_persian_and_zwnj_survive_a_passage_intact(self):
        c = chunk(text=PERSIAN_PASSAGE)
        assert "نمونه‌های" in c.text, "ZWNJ must not be folded away"
        assert "۱۴۰۳" in c.text, "Arabic-Indic digits must survive"

    def test_the_stored_text_is_not_normalized_at_construction(self):
        """The stored text stays verbatim; normalization is a separate, visible view.

        The quote comparison must run against what is *stored*. Normalizing during
        construction would make it impossible to see whether a match came from the
        text or from the normalizer.
        """
        c = EvidenceChunk(chunk_id=1, ordinal=0, text="  a\n  b  ")
        assert c.text == "  a\n  b  "
        assert c.normalized_text == "a b"

    def test_contains_compares_against_the_normalized_text(self):
        c = EvidenceChunk(chunk_id=1, ordinal=0, text="a  wrapped\npassage")
        assert c.contains("a wrapped passage")
        assert not c.contains("a wrapped\npassage")
        assert not c.contains("")

    def test_a_passage_is_frozen(self):
        c = chunk()
        with pytest.raises(ValidationError):
            c.text = "different"  # type: ignore[misc]


# --- SourceContent ----------------------------------------------------------


class TestSourceContent:
    def test_a_source_needs_at_least_one_passage(self):
        with pytest.raises(SourceContentEmptyError):
            SourceContent(source_id=1, domain="example.com", chunks=())

    @pytest.mark.parametrize("bad", ["", "   ", None, 7, ["example.com"]])
    def test_the_domain_must_be_a_non_empty_string(self, bad):
        with pytest.raises(SummaryInputError):
            SourceContent(source_id=1, domain=bad, chunks=(chunk(),))

    def test_the_domain_refuses_control_characters_and_echoes_nothing(self):
        for control in ("exa\x00mple.com", "exa\x07mple.com", "exa\x1bmple.com"):
            with pytest.raises(SummaryInputError) as excinfo:
                SourceContent(source_id=1, domain=control, chunks=(chunk(),))
            assert "mple" not in str(excinfo.value), "attacker text must not be echoed"

    def test_whitespace_in_the_domain_is_collapsed_so_no_line_break_survives(self):
        built = SourceContent(source_id=1, domain="exa\nmple.com", chunks=(chunk(),))
        assert built.domain == "exa mple.com"

    def test_an_over_long_domain_is_refused(self):
        with pytest.raises(SummaryInputError):
            SourceContent(source_id=1, domain="d" * 300, chunks=(chunk(),))

    @pytest.mark.parametrize("source_id", [0, -1])
    def test_the_source_id_must_be_positive(self, source_id):
        with pytest.raises(SummaryInputError):
            SourceContent(source_id=source_id, domain="example.com", chunks=(chunk(),))

    def test_a_missing_title_is_normal_state_not_an_error(self):
        assert content(title=None).title is None

    @pytest.mark.parametrize("bad", ["", "   ", "\n\n", "\u2028"])
    def test_an_unusable_title_is_dropped_rather_than_failing_the_source(self, bad):
        """A page with a blank title still has perfectly good passages."""
        assert content(title=bad).title is None

    def test_a_control_character_in_a_title_is_refused_not_dropped(self):
        with pytest.raises(SummaryInputError):
            content(title="a\x00b")

    def test_an_over_long_title_is_refused(self):
        with pytest.raises(SummaryInputError):
            content(title="t" * 400)

    def test_a_persian_title_is_preserved_with_its_zwnj(self):
        assert content(title=PERSIAN_TITLE).title == PERSIAN_TITLE

    def test_passages_are_canonically_ordered_by_ordinal_then_id(self):
        third = chunk(chunk_id=30, ordinal=2, text="third passage text")
        first = chunk(chunk_id=10, ordinal=0, text="first passage text")
        second = chunk(chunk_id=20, ordinal=1, text="second passage text")
        built = content(second, third, first)
        assert [c.chunk_id for c in built.chunks] == [10, 20, 30]

    def test_a_duplicate_passage_id_is_refused(self):
        with pytest.raises(EvidenceError):
            content(chunk(chunk_id=10), chunk(chunk_id=10, ordinal=1, text="other"))

    def test_a_duplicate_ordinal_is_refused(self):
        with pytest.raises(EvidenceError):
            content(chunk(chunk_id=10, ordinal=0), chunk(chunk_id=11, ordinal=0))

    def test_passages_may_not_be_a_string_or_a_mapping(self):
        with pytest.raises(EvidenceError):
            SourceContent(source_id=1, domain="example.com", chunks="a passage")
        with pytest.raises(EvidenceError):
            SourceContent(source_id=1, domain="example.com", chunks={"a": 1})

    def test_passages_may_be_given_as_mappings(self):
        built = SourceContent(
            source_id=1,
            domain="example.com",
            chunks=({"chunk_id": 5, "ordinal": 1, "text": "text of a passage"},),
        )
        assert built.chunk_ids == frozenset({5})

    def test_the_derived_views_agree_with_the_passages(self):
        built = content(chunk(10, 0, "one"), chunk(11, 1, "two"))
        assert built.chunk_ids == frozenset({10, 11})
        assert built.passage_count == 2
        assert built.char_count == len("one") + len("two")

    def test_a_window_restricts_the_content_without_changing_its_source(self):
        built = content(chunk(10, 0, "one"), chunk(11, 1, "two"))
        narrowed = built.window((built.chunks[1],))
        assert narrowed.source_id == built.source_id
        assert narrowed.chunk_ids == frozenset({11})
        assert built.chunk_ids == frozenset({10, 11}), "the original is unchanged"


# --- SourceSummary ----------------------------------------------------------


class TestSourceSummary:
    def test_a_valid_summary_is_accepted(self):
        summary = SourceSummary(source_id=3, text="This page describes one field survey.")
        assert summary.source_id == 3
        assert summary.schema_version == SUMMARY_SCHEMA_VERSION

    @pytest.mark.parametrize("bad", ["", "   ", None, 5, ["a summary"]])
    def test_an_empty_or_non_text_summary_is_refused(self, bad):
        with pytest.raises(SummaryValidationError):
            SourceSummary(source_id=3, text=bad)

    def test_an_over_long_summary_is_refused(self):
        with pytest.raises(SummaryValidationError):
            SourceSummary(source_id=3, text="x" * (MAX_SUMMARY_CHARS + 1))

    @pytest.mark.parametrize("bad_source_id", [0, -1])
    def test_the_source_id_must_be_positive(self, bad_source_id):
        with pytest.raises(SummaryValidationError):
            SourceSummary(source_id=bad_source_id, text="a summary")

    def test_a_persian_summary_is_preserved(self):
        text = "این صفحه یک گزارش میدانی دربارهٔ رشد شهری است."
        assert SourceSummary(source_id=1, text=text).text == text

    def test_whitespace_is_collapsed_but_nothing_else_is_rewritten(self):
        summary = SourceSummary(source_id=1, text="a  wrapped\nsummary with ZWNJ می‌شود")
        assert summary.text == "a wrapped summary with ZWNJ می‌شود"

    @pytest.mark.parametrize("tag", ["en", "fa", "en-GB", "FA", "fa-IR"])
    def test_a_language_tag_is_accepted(self, tag):
        assert SourceSummary(source_id=1, text="s", language=tag).language == tag.lower()

    @pytest.mark.parametrize("tag", ["english!", "e n", "x" * 20, 5, "en/GB"])
    def test_an_unusable_language_tag_is_refused(self, tag):
        with pytest.raises(SummaryValidationError):
            SourceSummary(source_id=1, text="s", language=tag)

    def test_an_absent_language_is_none(self):
        assert SourceSummary(source_id=1, text="s", language=None).language is None

    def test_from_payload_takes_the_source_id_from_the_caller_not_the_payload(self):
        """Source identity is pipeline-assigned; a page cannot declare it (§ 15.6)."""
        summary = SourceSummary.from_payload(
            {"summary": "the page describes a survey", "source_id": 999, "language": "en"},
            source_id=7,
        )
        assert summary.source_id == 7

    def test_from_payload_requires_the_summary_field(self):
        with pytest.raises(SummarySchemaError):
            SourceSummary.from_payload({"language": "en"}, source_id=1)

    @pytest.mark.parametrize("payload", ["a string", 5, ["a", "list"], None])
    def test_from_payload_requires_an_object(self, payload):
        with pytest.raises(SummaryValidationError):
            SourceSummary.from_payload(payload, source_id=1)

    def test_from_payload_refuses_a_schema_version_from_another_build(self):
        with pytest.raises(SummaryValidationError):
            SourceSummary.from_payload({"summary": "s", "schema_version": "other-v9"}, source_id=1)

    def test_the_payload_round_trips_through_json_semantics(self):
        original = SourceSummary(source_id=2, text="a summary", language="fa")
        payload = original.to_payload()
        assert list(payload) == ["schema_version", "source_id", "summary", "language"]
        assert SourceSummary.from_payload(payload, source_id=2) == original

    def test_a_summary_is_frozen(self):
        summary = SourceSummary(source_id=1, text="s")
        with pytest.raises(ValidationError):
            summary.text = "other"  # type: ignore[misc]


# --- ClaimExtraction --------------------------------------------------------


class TestClaimExtraction:
    def test_a_well_formed_response_becomes_candidates(self):
        result = extraction(good_claim())
        assert result.source_id == 1
        assert result.schema_version == CLAIM_EXTRACTION_SCHEMA_VERSION
        assert [c.index for c in result.candidates] == [0]
        assert result.candidates[0].statement.startswith("The survey")

    def test_an_empty_claim_list_is_a_valid_answer_not_a_failure(self):
        """A page can support nothing checkable; that must not discard its summary."""
        assert extraction().is_empty

    @pytest.mark.parametrize("payload", ["a string", 5, ["claims"], None])
    def test_the_envelope_must_be_an_object(self, payload):
        with pytest.raises(ClaimExtractionSchemaError):
            ClaimExtraction.from_payload(payload, source_id=1)

    def test_a_missing_claims_field_is_refused(self):
        with pytest.raises(ClaimExtractionSchemaError):
            ClaimExtraction.from_payload({"summary": "s"}, source_id=1)

    @pytest.mark.parametrize("claims", ["a string", 5, {"a": 1}])
    def test_claims_must_be_a_list(self, claims):
        with pytest.raises(ClaimExtractionSchemaError):
            ClaimExtraction.from_payload({"claims": claims}, source_id=1)

    @pytest.mark.parametrize("entry", ["a string", 5, ["statement"], None])
    def test_a_claim_entry_must_be_an_object(self, entry):
        with pytest.raises(ClaimStructureError):
            ClaimExtraction.from_payload({"claims": [entry]}, source_id=1)

    @pytest.mark.parametrize("extra", ["verified", "confidence", "citation", "source_id", "why"])
    def test_an_unsupported_field_is_refused_rather_than_ignored(self, extra):
        """A key nobody reads is still content this stage would store.

        This is also where self-certification is structurally impossible: a model
        cannot add ``"verified": true`` and have it mean anything, because the
        contract has no such field.
        """
        with pytest.raises(ClaimStructureError) as excinfo:
            ClaimExtraction.from_payload({"claims": [{**good_claim(), extra: True}]}, source_id=1)
        assert extra in str(excinfo.value)

    def test_a_response_with_too_many_claims_is_refused(self):
        claims = [good_claim() for _ in range(MAX_CLAIMS_CEILING + 1)]
        with pytest.raises(ClaimStructureError):
            extraction(*claims)

    def test_exactly_the_ceiling_is_accepted(self):
        claims = [good_claim() for _ in range(MAX_CLAIMS_CEILING)]
        assert len(extraction(*claims).candidates) == MAX_CLAIMS_CEILING

    def test_a_string_chunk_id_is_not_coerced_into_an_id(self):
        """``"10"`` is not an id, and guessing is how a citation points at the wrong
        passage. The claim is rejected, not repaired."""
        result = extraction(good_claim() | {"source_chunk_id": "10"})
        assert result.candidates[0].source_chunk_id is None
        assert not result.candidates[0].is_usable

    @pytest.mark.parametrize("bad", [True, False, 1.5, "10", None, ["10"]])
    def test_a_non_integer_chunk_reference_is_absent(self, bad):
        assert extraction(good_claim() | {"source_chunk_id": bad}).candidates[0].source_chunk_id is None

    @pytest.mark.parametrize("value", [None, 5, ["x"], True, {"a": 1}])
    def test_a_non_text_statement_reads_as_empty(self, value):
        assert extraction(good_claim() | {"statement": value}).candidates[0].statement == ""

    @pytest.mark.parametrize("value", [None, 5, ["x"], {"a": 1}])
    def test_a_non_text_quote_reads_as_empty(self, value):
        assert extraction(good_claim() | {"quote": value}).candidates[0].quote == ""

    def test_a_missing_field_becomes_an_unusable_candidate_rather_than_an_exception(self):
        for field in ("statement", "quote", "source_chunk_id"):
            candidate = extraction(good_claim() | {field: None}).candidates[0]
            assert not candidate.is_usable, f"{field} must leave the candidate unusable"

    def test_statement_and_quote_are_normalized_but_not_translated(self):
        candidate = extraction(
            good_claim(statement="  a  statement\nabout می‌شود  ", quote=f"  {PERSIAN_QUOTE}  ")
        ).candidates[0]
        assert candidate.statement == "a statement about می‌شود"
        assert candidate.quote == PERSIAN_QUOTE

    def test_a_candidate_keeps_its_position_for_reporting(self):
        result = extraction(good_claim(), good_claim(statement="Another claim here."))
        assert [c.index for c in result.candidates] == [0, 1]

    def test_an_extraction_cannot_be_attributed_to_another_source(self):
        """The source id comes from the caller's content, never from the response."""
        assert extraction(good_claim(), source_id=42).source_id == 42

    def test_a_candidate_is_frozen(self):
        candidate = extraction(good_claim()).candidates[0]
        with pytest.raises(ValidationError):
            candidate.statement = "other"  # type: ignore[misc]

    def test_the_payload_round_trips(self):
        original = extraction(good_claim(), good_claim(chunk_id=11, statement="Second claim text."))
        restored = ClaimExtraction.from_payload(original.to_payload(), source_id=1)
        assert restored == original


# --- verification -----------------------------------------------------------


class TestVerifyClaims:
    def test_a_claim_whose_quote_is_in_the_passage_is_accepted(self):
        result = verify_claims(extraction(good_claim()), content())
        assert result.accepted_count == 1
        claim = result.accepted[0]
        assert claim.evidence.chunk_id == 10
        assert claim.evidence.source_id == 1
        assert claim.evidence.quote == ENGLISH_QUOTE
        assert result.rejected_count == 0

    def test_an_accepted_claim_carries_a_usable_locator(self):
        result = verify_claims(extraction(good_claim()), content(chunk(10, 3)))
        assert result.accepted[0].evidence.location == "chunk 3"

    def test_several_claims_are_all_accepted(self):
        result = verify_claims(
            extraction(
                good_claim(statement="The survey recorded a forty percent increase."),
                good_claim(statement="The methodology section was published.", quote="published methodology section"),
            ),
            content(),
        )
        assert result.accepted_count == 2
        assert result.rejected_count == 0

    def test_claims_may_point_at_different_passages_of_the_same_source(self):
        built = content(
            chunk(10, 0, ENGLISH_PASSAGE),
            chunk(11, 1, PERSIAN_PASSAGE),
        )
        result = verify_claims(
            extraction(
                good_claim(10, ENGLISH_QUOTE, "English claim."),
                good_claim(11, PERSIAN_QUOTE, PERSIAN_STATEMENT),
            ),
            built,
        )
        assert [c.evidence.chunk_id for c in result.accepted] == [10, 11]
        assert result.rejected_count == 0

    def test_an_unknown_passage_id_is_rejected(self):
        result = verify_claims(extraction(good_claim(chunk_id=999)), content())
        assert result.rejected[0].reason is EvidenceRejection.UNKNOWN_CHUNK
        assert result.accepted_count == 0

    def test_a_passage_id_from_another_source_is_rejected_identically(self):
        """A reference is never resolved against the database.

        Passage 11 exists — it belongs to a different source — and it is refused
        exactly as a nonexistent id would be. Resolving it "because it exists" is
        the behaviour that lets a claim cite evidence from outside its own source.
        """
        other = content(chunk(11, 0, PERSIAN_PASSAGE), source_id=2, domain="other.example")
        assert other.chunk_ids == frozenset({11})
        result = verify_claims(extraction(good_claim(chunk_id=11)), content())
        assert result.rejected[0].reason is EvidenceRejection.UNKNOWN_CHUNK

    def test_a_quote_that_is_not_in_the_passage_is_rejected(self):
        result = verify_claims(
            extraction(good_claim(quote="the article basically says something like this")),
            content(),
        )
        assert result.rejected[0].reason is EvidenceRejection.QUOTE_NOT_FOUND

    def test_a_paraphrased_quote_is_rejected(self):
        result = verify_claims(
            extraction(good_claim(quote="a forty percent rise was seen in the districts")),
            content(),
        )
        assert result.rejected[0].reason is EvidenceRejection.QUOTE_NOT_FOUND

    def test_a_quote_joined_with_an_ellipsis_is_rejected(self):
        """Two fragments stitched together are not a passage."""
        stitched = f"{ENGLISH_QUOTE} ... the published methodology section"
        result = verify_claims(extraction(good_claim(quote=stitched)), content())
        assert result.rejected[0].reason is EvidenceRejection.QUOTE_NOT_FOUND

    def test_a_quote_that_skips_the_words_between_two_fragments_is_rejected(self):
        stitched = f"{ENGLISH_QUOTE} the published methodology section"
        result = verify_claims(extraction(good_claim(quote=stitched)), content())
        assert result.rejected[0].reason is EvidenceRejection.QUOTE_NOT_FOUND

    def test_a_quote_from_another_passage_of_the_same_source_is_rejected(self):
        built = content(chunk(10, 0, ENGLISH_PASSAGE), chunk(11, 1, PERSIAN_PASSAGE))
        result = verify_claims(extraction(good_claim(chunk_id=10, quote=PERSIAN_QUOTE)), built)
        assert result.rejected[0].reason is EvidenceRejection.QUOTE_NOT_FOUND

    @pytest.mark.parametrize("quote", ["", "   "])
    def test_an_empty_quote_is_rejected(self, quote):
        result = verify_claims(extraction(good_claim(quote=quote)), content())
        assert result.rejected[0].reason is EvidenceRejection.EMPTY_QUOTE

    def test_an_empty_statement_is_rejected(self):
        result = verify_claims(extraction(good_claim(statement="  ")), content())
        assert result.rejected[0].reason is EvidenceRejection.EMPTY_STATEMENT

    def test_an_over_long_statement_is_rejected(self):
        result = verify_claims(extraction(good_claim(statement="s" * (MAX_STATEMENT_CHARS + 1))), content())
        assert result.rejected[0].reason is EvidenceRejection.OVERLONG_STATEMENT

    def test_an_over_long_quote_is_rejected(self):
        result = verify_claims(extraction(good_claim(quote="x" * (MAX_QUOTE_CHARS + 1))), content())
        assert result.rejected[0].reason is EvidenceRejection.OVERLONG_QUOTE

    def test_a_trivially_short_quote_is_rejected(self):
        """"the" appears in the passage and supports nothing."""
        result = verify_claims(extraction(good_claim(quote="the")), content())
        assert result.rejected[0].reason is EvidenceRejection.SHORT_QUOTE

    def test_the_minimum_quote_length_is_script_neutral(self):
        """A Persian passage of the same length is accepted too."""
        built = content(chunk(11, 0, PERSIAN_PASSAGE))
        assert len(PERSIAN_QUOTE) >= MIN_QUOTE_CHARS
        result = verify_claims(
            extraction(good_claim(chunk_id=11, quote=PERSIAN_QUOTE, statement=PERSIAN_STATEMENT)),
            built,
        )
        assert result.accepted_count == 1

    def test_an_identical_claim_twice_is_accepted_once(self):
        result = verify_claims(extraction(good_claim(), good_claim()), content())
        assert result.accepted_count == 1
        assert result.rejected[0].reason is EvidenceRejection.DUPLICATE_CLAIM

    def test_the_same_statement_against_two_passages_is_two_claims(self):
        built = content(chunk(10, 0, ENGLISH_PASSAGE), chunk(11, 1, PERSIAN_PASSAGE))
        result = verify_claims(
            extraction(
                good_claim(10, ENGLISH_QUOTE, "Same statement."),
                good_claim(11, PERSIAN_QUOTE, "Same statement."),
            ),
            built,
        )
        assert result.accepted_count == 2

    def test_one_bad_candidate_does_not_discard_the_good_ones(self):
        result = verify_claims(
            extraction(
                good_claim(),
                good_claim(chunk_id=999),
                good_claim(quote="not present in this passage at all"),
                good_claim(statement="", quote=ENGLISH_QUOTE),
                good_claim(statement="Another verified claim.", quote="published methodology section"),
            ),
            content(),
        )
        assert result.accepted_count == 2
        assert result.rejected_count == 3
        assert result.rejections_by_reason() == {
            EvidenceRejection.UNKNOWN_CHUNK: 1,
            EvidenceRejection.QUOTE_NOT_FOUND: 1,
            EvidenceRejection.EMPTY_STATEMENT: 1,
        }

    def test_rejections_keep_their_position_and_reason(self):
        result = verify_claims(extraction(good_claim(chunk_id=1), good_claim(chunk_id=2)), content())
        assert [(r.index, r.reason) for r in result.rejected] == [
            (0, EvidenceRejection.UNKNOWN_CHUNK),
            (1, EvidenceRejection.UNKNOWN_CHUNK),
        ]

    def test_the_accepted_order_follows_the_candidate_order(self):
        first = good_claim(11, PERSIAN_QUOTE, PERSIAN_STATEMENT)
        second = good_claim(10, ENGLISH_QUOTE, "An English claim.")
        result = verify_claims(
            extraction(first, second),
            content(chunk(10, 0, ENGLISH_PASSAGE), chunk(11, 1, PERSIAN_PASSAGE)),
        )
        assert [c.evidence.chunk_id for c in result.accepted] == [11, 10]

    def test_an_extraction_for_another_source_is_refused_rather_than_verified(self):
        """Two sources in one call is a wiring defect, not a model outcome."""
        with pytest.raises(EvidenceError):
            verify_claims(extraction(good_claim(), source_id=2), content(source_id=1))

    def test_verification_is_deterministic(self):
        produced = extraction(good_claim(), good_claim(chunk_id=999), good_claim(quote="nope"))
        built = content()
        assert verify_claims(produced, built) == verify_claims(produced, built)

    def test_verification_reads_no_clock_and_no_network(self):
        """Determinism is a property of this function, asserted structurally."""
        import ast

        import app.domain.research.evidence as evidence_module

        assert evidence_module.__file__ is not None
        with open(evidence_module.__file__, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())

        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert node.func.attr not in {"now", "utcnow", "today", "random", "shuffle"}


# --- normalization policy ---------------------------------------------------


class TestNormalizationPolicy:
    def test_whitespace_runs_collapse_to_one_space(self):
        assert normalize_evidence_text("a \n\t b   c ") == "a b c"

    def test_a_quote_matching_across_a_line_break_is_accepted(self):
        """Wrapping is formatting; a quote should survive it."""
        built = content(chunk(10, 0, "the passage was\nwrapped across   lines here"))
        result = verify_claims(
            extraction(good_claim(quote="the passage was wrapped across lines here")),
            built,
        )
        assert result.accepted_count == 1

    def test_case_is_never_folded(self):
        built = content(chunk(10, 0, "RAG Reduces Hallucination in Retrieval Pipelines"))
        upper = verify_claims(
            extraction(good_claim(quote="RAG Reduces Hallucination in")), built
        )
        lower = verify_claims(
            extraction(good_claim(quote="rag reduces hallucination in")), built
        )
        assert upper.accepted_count == 1
        assert lower.rejected[0].reason is EvidenceRejection.QUOTE_NOT_FOUND

    def test_nfc_composition_is_applied_to_both_sides(self):
        composed = "café accounted for the change"
        decomposed = "café accounted for the change"
        built = content(chunk(10, 0, decomposed))
        assert verify_claims(extraction(good_claim(quote=composed)), built).accepted_count == 1

    def test_persian_digits_and_punctuation_are_never_rewritten(self):
        built = content(chunk(11, 0, PERSIAN_PASSAGE))
        quote = "در سال ۱۴۰۳ افزایش چهل درصدی"
        result = verify_claims(
            extraction(good_claim(chunk_id=11, quote=quote, statement=PERSIAN_STATEMENT)),
            built,
        )
        assert result.accepted_count == 1
        assert "۱۴۰۳" in result.accepted[0].evidence.quote

    def test_zwnj_survives_the_comparison(self):
        assert normalize_evidence_text("نمونه‌های") == "نمونه‌های"

    def test_diacritics_are_not_stripped(self):
        built = content(chunk(10, 0, "the cat sat quietly on the mat"))
        assert verify_claims(extraction(good_claim(quote="sat quietly on the mat")), built).accepted_count == 1

    def test_a_non_string_normalizes_to_empty_rather_than_raising(self):
        assert normalize_evidence_text(None) == ""  # type: ignore[arg-type]

    def test_a_mixed_language_quote_survives_whole(self):
        built = content(chunk(11, 0, MIXED_PASSAGE))
        result = verify_claims(
            extraction(good_claim(chunk_id=11, quote=MIXED_QUOTE, statement="گزارش میدانی است.")),
            built,
        )
        assert result.accepted_count == 1
        assert result.accepted[0].evidence.quote == MIXED_QUOTE


# --- window planner ---------------------------------------------------------


class TestWindowPlanner:
    def short_chunks(self, count: int, size: int = 200) -> list[EvidenceChunk]:
        return [
            EvidenceChunk(chunk_id=100 + i, ordinal=i, text="w" * size) for i in range(count)
        ]

    def test_passages_that_fit_are_all_in_one_window(self):
        windows = plan_chunk_windows(
            self.short_chunks(3, 100), max_tokens=4000, counter=FixedCounter()
        )
        assert len(windows) == 1
        assert windows[0].dropped == 0
        assert not windows[0].truncated

    def test_a_long_source_is_split_into_several_windows_in_order(self):
        windows = plan_chunk_windows(
            self.short_chunks(6, 500), max_tokens=1000, counter=FixedCounter(), max_windows=4
        )
        assert len(windows) > 1
        assert [c.chunk_id for w in windows for c in w.chunks] == [
            100 + i for i in range(6)
        ]
        assert [w.index for w in windows] == list(range(len(windows)))
        assert windows[-1].dropped == 0

    def test_the_window_ceiling_is_respected_and_the_remainder_is_counted(self):
        windows = plan_chunk_windows(
            self.short_chunks(8, 500), max_tokens=900, counter=FixedCounter(), max_windows=2
        )
        assert len(windows) == 2
        shown = sum(len(w.chunks) for w in windows)
        assert windows[-1].dropped == 8 - shown
        assert windows[-1].truncated
        assert all(w.dropped == 0 for w in windows[:-1])

    def test_one_window_is_the_default_and_the_rest_is_counted_not_shown(self):
        windows = plan_chunk_windows(
            self.short_chunks(6, 500), max_tokens=1000, counter=FixedCounter()
        )
        assert len(windows) == 1
        assert windows[0].dropped == 6 - len(windows[0].chunks)
        assert windows[0].truncated

    def test_every_shown_window_reports_the_total_window_count(self):
        windows = plan_chunk_windows(
            self.short_chunks(6, 500), max_tokens=1000, counter=FixedCounter(), max_windows=4
        )
        assert {w.window_count for w in windows} == {len(windows)}

    def test_a_passage_larger_than_the_budget_still_gets_a_window(self):
        """Dropping it would remove evidence quietly; showing it oversized is honest."""
        huge = EvidenceChunk(chunk_id=1, ordinal=0, text="x" * 100_000)
        windows = plan_chunk_windows([huge], max_tokens=700, counter=FixedCounter())
        assert len(windows) == 1
        assert windows[0].dropped == 0
        assert windows[0].chunks[0].chunk_id == 1

    def test_no_passage_is_ever_split(self):
        windows = plan_chunk_windows(
            self.short_chunks(5, 400), max_tokens=800, counter=FixedCounter(), max_windows=3
        )
        seen = {c.chunk_id for w in windows for c in w.chunks}
        # Every shown passage appears whole; what did not fit is counted, not halved.
        assert seen == {100, 101, 102}
        assert windows[-1].dropped == 2
        for window in windows:
            for passage in window.chunks:
                assert len(passage.text) == 400

    def test_the_planner_is_deterministic(self):
        chunks = self.short_chunks(7, 300)
        first = plan_chunk_windows(chunks, max_tokens=1000, counter=FixedCounter())
        second = plan_chunk_windows(chunks, max_tokens=1000, counter=FixedCounter())
        assert first == second

    def test_input_order_does_not_change_the_result(self):
        chunks = self.short_chunks(6, 300)
        forward = plan_chunk_windows(
            chunks, max_tokens=1000, counter=FixedCounter(), max_windows=4
        )
        backward = plan_chunk_windows(
            list(reversed(chunks)), max_tokens=1000, counter=FixedCounter(), max_windows=4
        )
        assert forward == backward

    @pytest.mark.parametrize("bad", [0, -1])
    def test_the_budget_and_ceiling_must_be_positive(self, bad):
        with pytest.raises(EvidenceError):
            plan_chunk_windows(self.short_chunks(1), max_tokens=bad, counter=FixedCounter())
        with pytest.raises(EvidenceError):
            plan_chunk_windows(
                self.short_chunks(1), max_tokens=4000, counter=FixedCounter(), max_windows=bad
            )

    def test_no_passages_at_all_is_the_empty_content_error(self):
        with pytest.raises(SourceContentEmptyError):
            plan_chunk_windows([], max_tokens=4000, counter=FixedCounter())

    def test_a_window_must_not_be_empty(self):
        with pytest.raises(SummaryInputError):
            ChunkWindow(chunks=())


# --- result + evidence reference -------------------------------------------


class TestEvidenceObjects:
    def test_the_result_reports_its_own_coverage_and_rejections(self):
        verified = VerifiedClaim(
            statement="A claim.",
            evidence=EvidenceReference(source_id=1, chunk_id=10, ordinal=0, quote=ENGLISH_QUOTE),
        )
        result = SourceEvidenceResult(
            source_id=1,
            summary=SourceSummary(source_id=1, text="a summary"),
            claims=(verified,),
            rejected=(RejectedClaim(index=1, reason=EvidenceRejection.QUOTE_NOT_FOUND),),
            windows=2,
            summary_passages=1,
            summary_truncated=True,
        )
        assert (result.claim_count, result.rejected_count) == (1, 1)
        assert result.rejections_by_reason() == {EvidenceRejection.QUOTE_NOT_FOUND: 1}
        assert result.summary_truncated and result.windows == 2

    def test_the_result_is_frozen(self):
        result = SourceEvidenceResult(
            source_id=1, summary=SourceSummary(source_id=1, text="s")
        )
        with pytest.raises(ValidationError):
            result.source_id = 2  # type: ignore[misc]

    def test_an_evidence_reference_verifies_against_its_own_passage(self):
        reference = EvidenceReference(
            source_id=1, chunk_id=10, ordinal=0, quote=ENGLISH_QUOTE
        )
        assert reference.verify_against(chunk(10, 0))

    def test_an_evidence_reference_fails_against_another_passage(self):
        reference = EvidenceReference(
            source_id=1, chunk_id=10, ordinal=0, quote=ENGLISH_QUOTE
        )
        assert not reference.verify_against(chunk(11, 0, PERSIAN_PASSAGE))
        assert not reference.verify_against(chunk(10, 1))

    def test_a_verified_claim_refuses_an_empty_statement(self):
        with pytest.raises(EvidenceError):
            VerifiedClaim(
                statement="  ",
                evidence=EvidenceReference(
                    source_id=1, chunk_id=10, ordinal=0, quote=ENGLISH_QUOTE
                ),
            )

    def test_a_verification_can_be_empty_without_being_wrong(self):
        verification = ClaimVerification(source_id=1)
        assert verification.accepted == () and verification.rejected == ()
        assert not verification.has_rejections


# --- structural properties --------------------------------------------------


class TestDomainModuleStructure:
    def test_the_domain_imports_no_io_dependency(self):
        import ast

        import app.domain.research.evidence as evidence_module

        assert evidence_module.__file__ is not None
        with open(evidence_module.__file__, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())

        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                imported.add(node.module.split(".")[0])

        assert imported.isdisjoint(
            {"httpx", "aiohttp", "requests", "socket", "asyncio", "sqlalchemy", "celery", "redis", "openai"}
        )

    def test_the_domain_does_not_duplicate_the_persisted_claim_or_source_types(self):
        """``Claim``, ``Source``, and ``SourceChunk`` already exist (P3.1).

        This module must not grow a parallel version of any of them, which is why
        the only reference to those names is the ORM-facing conversion in the
        service, not a second model here.
        """
        import app.domain.research.evidence as evidence_module

        for duplicate in ("Claim", "Source", "SourceChunk", "Citation"):
            assert not hasattr(evidence_module, duplicate)

    def test_the_stage_has_no_cross_source_concept(self):
        """FR-33 is P3.8's. Nothing here can hold two sources."""
        import app.domain.research.evidence as evidence_module

        assert not hasattr(evidence_module, "CrossSourceComparison")
        assert not hasattr(evidence_module, "Synthesis")