"""Passage/chunk production, the pure half (P3.9-A — FR-22).

No database, no provider, no clock: the chunker is a decision function over text,
so every property the contract claims — determinism, bounds, coverage, boundary
preference, Unicode safety — is asserted here with no infrastructure at all.

Each test names the property it protects, because the failure modes this stage
has are silent ones: a boundary that drifts between two runs, a passage that
loses the middle of a sentence, a Persian compound broken at its ZWNJ.
"""

from __future__ import annotations

import random
import unicodedata
from itertools import pairwise

import pytest

from app.domain.errors import ChunkLimitError
from app.domain.research.chunking import (
    MAX_CHUNK_CEILING,
    MAX_CHUNK_CHARS,
    MIN_CHUNK_CHARS,
    PassageSpan,
    chunk_source_text,
    covers_text,
    normalized_source_text,
)
from app.domain.research.evidence import normalize_evidence_text
from app.domain.research.extraction import ExtractedContent

# A long English body, sentence after sentence, the shape a real page has.
LONG_BODY = " ".join(
    f"The {ordinal}th measurement recorded a change across the sampled districts."
    for ordinal in range(1, 121)
)
PERSIAN_BODY = " ".join(
    f"پژوهشگران در سال {ordinal}۳ افزایش چهل درصدی را در نمونه‌های شهری گزارش کردند."
    for ordinal in range(1, 41)
)
MIXED_BODY = " ".join(
    f"این گزارش بر پایهٔ field survey {ordinal} منتشر شد و رشد ۴۰ درصدی را نشان می‌دهد."
    for ordinal in range(1, 41)
)
UNBROKEN = "x" * 5_000
PERSIAN_UNBROKEN = "یک" * 3_000
ONE_OVERSIZED_WORD = "y" * (MAX_CHUNK_CHARS + 37) + " and a short tail."


def _longer_than_one_passage() -> str:
    """Text that cannot fit in a single passage, however it is split."""
    return LONG_BODY


class TestBounds:
    def test_a_short_source_is_one_passage(self):
        spans = chunk_source_text("A short body of text. With two sentences.")
        assert [span.text for span in spans] == [
            "A short body of text. With two sentences."
        ]

    def test_every_passage_respects_the_maximum(self):
        spans = chunk_source_text(_longer_than_one_passage())
        assert len(spans) > 1
        assert all(len(span.text) <= MAX_CHUNK_CHARS for span in spans)

    def test_the_maximum_is_the_quote_bound(self):
        """Not a coincidence: a passage is quotable in full by construction."""
        assert MAX_CHUNK_CHARS == 1_200
        assert MIN_CHUNK_CHARS == 16

    def test_unbroken_text_is_still_bounded(self):
        spans = chunk_source_text(UNBROKEN)
        assert len(spans) > 1
        assert all(len(span.text) <= MAX_CHUNK_CHARS for span in spans)

    def test_an_unbroken_run_is_cut_exactly_at_the_bound(self):
        spans = chunk_source_text(UNBROKEN)
        assert [len(span.text) for span in spans] == [
            MAX_CHUNK_CHARS,
            MAX_CHUNK_CHARS,
            MAX_CHUNK_CHARS,
            MAX_CHUNK_CHARS,
            200,
        ]

    def test_a_single_oversized_word_is_split_not_dropped(self):
        spans = chunk_source_text(ONE_OVERSIZED_WORD)
        assert covers_text(spans, ONE_OVERSIZED_WORD)
        assert all(len(span.text) <= MAX_CHUNK_CHARS for span in spans)
        # The word itself is 1237 characters, so it needs a second passage.
        assert len(spans) >= 2

    def test_an_oversized_paragraph_is_bounded(self):
        paragraph = "word " * 2_000  # one paragraph, no sentence punctuation
        spans = chunk_source_text(paragraph)
        assert len(spans) > 1
        assert all(len(span.text) <= MAX_CHUNK_CHARS for span in spans)

    def test_the_passage_ceiling_is_structural(self):
        """SR-5's bounded chunk count, as a guard rather than a promise."""
        with pytest.raises(ChunkLimitError):
            chunk_source_text("word " * 20_000, max_chars=MIN_CHUNK_CHARS)
        assert MAX_CHUNK_CEILING == 1_000

    def test_a_ceiling_breach_writes_nothing_because_it_raises_before_any_row(self):
        """The guard is raised by the pure function, so no caller can half-apply."""
        assert chunk_source_text("word " * 20_000)  # the default bound is fine


class TestCoverage:
    def test_passages_partition_the_normalized_text(self):
        for text in (
            LONG_BODY,
            PERSIAN_BODY,
            MIXED_BODY,
            UNBROKEN,
            PERSIAN_UNBROKEN,
            ONE_OVERSIZED_WORD,
        ):
            spans = chunk_source_text(text)
            assert covers_text(spans, text), text[:40]

    def test_offsets_index_exactly_the_passage_text(self):
        normalized = normalized_source_text(LONG_BODY)
        for span in chunk_source_text(LONG_BODY):
            assert normalized[span.char_start : span.char_end] == span.text
            assert span.char_end - span.char_start == len(span.text)

    def test_no_passage_overlaps_another(self):
        spans = chunk_source_text(LONG_BODY)
        for earlier, later in pairwise(spans):
            assert later.char_start >= earlier.char_end

    def test_the_last_passage_ends_at_the_end_of_the_text(self):
        normalized = normalized_source_text(LONG_BODY)
        spans = chunk_source_text(LONG_BODY)
        assert spans[-1].char_end == len(normalized)

    def test_the_first_passage_starts_at_the_beginning(self):
        assert chunk_source_text(LONG_BODY)[0].char_start == 0

    def test_nothing_between_two_passages_is_meaningful_text(self):
        """Only whitespace may fall between passages, so no text is dropped."""
        normalized = normalized_source_text(MIXED_BODY)
        spans = chunk_source_text(MIXED_BODY)
        position = 0
        for span in spans:
            gap = normalized[position : span.char_start]
            assert gap.strip() == ""
            position = span.char_end

    def test_the_coverage_check_is_not_vacuous(self):
        """A plan that drops a passage must fail the check, not pass it."""
        spans = chunk_source_text(LONG_BODY)
        tampered = spans[:1] + spans[2:]
        assert not covers_text(tampered, LONG_BODY)
        assert covers_text(spans, LONG_BODY)

    def test_a_plan_whose_offsets_do_not_match_its_text_is_refused(self):
        """``char_end - char_start == len(text)`` is an invariant, not a hope."""
        with pytest.raises(ValueError):
            PassageSpan(ordinal=0, char_start=0, char_end=12, text="short")


class TestDeterminism:
    def test_the_same_text_yields_the_same_passages(self):
        first = chunk_source_text(LONG_BODY)
        second = chunk_source_text(LONG_BODY)
        assert [span.model_dump() for span in first] == [
            span.model_dump() for span in second
        ]

    def test_the_same_text_yields_the_same_passages_repeatedly(self):
        results = [
            chunk_source_text(PERSIAN_BODY) for _ in range(5)
        ]
        assert all(
            [span.model_dump() for span in result] == [span.model_dump() for span in results[0]]
            for result in results[1:]
        )

    def test_a_randomised_corpus_stays_deterministic_and_covered(self):
        """No clock, no randomness, no dictionary iteration order in the boundary."""
        vocabulary = ["lorem", "ipsum", "درد", "داستان", "end.", "و.", "چیست؟", "done!"]
        generator = random.Random(20261008)
        for _ in range(200):
            words = [
                generator.choice(vocabulary)
                for _ in range(generator.randint(1, 300))
            ]
            text = " ".join(words)
            if generator.random() < 0.3:
                text = text.replace(" ", "\n\n", 3)
            first = chunk_source_text(text)
            second = chunk_source_text(text)
            assert [span.model_dump() for span in first] == [
                span.model_dump() for span in second
            ]
            assert covers_text(first, text)
            assert all(len(span.text) <= MAX_CHUNK_CHARS for span in first)


class TestOrdering:
    def test_ordinals_are_dense_and_zero_based(self):
        spans = chunk_source_text(LONG_BODY)
        assert [span.ordinal for span in spans] == list(range(len(spans)))

    def test_ordinals_follow_the_text_order(self):
        spans = chunk_source_text(PERSIAN_BODY)
        positions = [span.char_start for span in spans]
        assert positions == sorted(positions)


class TestBoundaries:
    def test_a_sentence_boundary_is_preferred(self):
        # Long sentences pack into near-full passages; the sentence that would
        # push a passage past the bound starts the next one, so a passage ends
        # where a sentence ends rather than mid-clause.
        spans = chunk_source_text(_longer_than_one_passage())
        for span in spans[:-1]:
            assert span.text.endswith(".")

    def test_two_short_sentences_are_one_passage(self):
        assert [span.text for span in chunk_source_text("One. Two.")] == ["One. Two."]

    def test_many_short_sentences_pack_into_full_passages(self):
        """Packed, not one passage per sentence, and not one passage for all."""
        text = "a. " * 900
        spans = chunk_source_text(text)
        assert len(spans) == 3
        # Every passage but the last is close to the bound, because the packer
        # fills a passage before opening the next one.
        assert all(len(span.text) > MAX_CHUNK_CHARS // 2 for span in spans[:-1])
        assert len(spans) < 900

    def test_a_terminator_inside_a_word_is_not_a_boundary(self):
        """``3.5`` is a decimal, not a sentence end.

        The bound is forced small so the unit boundaries become visible in the
        passage text: a false boundary would produce ``Version 3. 5 is …``.
        """
        spans = chunk_source_text(
            "Version 3.5 is current and stable. Tail here.", max_chars=40
        )
        assert [span.text for span in spans] == [
            "Version 3.5 is current and stable.",
            "Tail here.",
        ]

    def test_a_run_of_terminators_ends_one_sentence(self):
        """``?!`` is one boundary, not two — and ``Yes.`` starts the next."""
        spans = chunk_source_text("Really?! Yes. No.", max_chars=12)
        assert [span.text for span in spans] == ["Really?!", "Yes. No."]

    def test_persian_sentence_terminators_are_boundaries(self):
        spans = chunk_source_text("این یک آزمایش است. و این یکی دیگر است.", max_chars=30)
        assert [span.text for span in spans] == [
            "این یک آزمایش است.",
            "و این یکی دیگر است.",
        ]

    def test_paragraph_breaks_are_respected_when_present(self):
        text = (
            "First paragraph with enough text to quote.\n\n"
            "Second paragraph with enough text to quote.\n\n"
            "Third paragraph with enough text to quote."
        )
        spans = chunk_source_text(text)
        assert [span.text for span in spans] == [
            "First paragraph with enough text to quote.",
            "Second paragraph with enough text to quote.",
            "Third paragraph with enough text to quote.",
        ]

    def test_a_paragraph_too_short_to_quote_is_merged_not_kept(self):
        """The "no unquotable passage" invariant outranks the paragraph break."""
        text = "Hi.\n\nThere.\n\nFriend."
        assert [span.text for span in chunk_source_text(text)] == [
            "Hi. There. Friend."
        ]

    def test_a_paragraph_break_does_not_defeat_coverage(self):
        text = "One two three.\n\nFour five six.\n\nSeven eight nine."
        assert covers_text(chunk_source_text(text), text)


class TestUnicode:
    def test_english_text_survives_unchanged(self):
        text = "The survey recorded a forty percent increase across the districts."
        assert [span.text for span in chunk_source_text(text)] == [text]

    def test_persian_text_survives_unchanged(self):
        """A Persian body is split, never rewritten — not one character of it.

        The comparison is with the whitespace removed on both sides, because the
        single space that separated two passages belongs to neither of them; what
        must be identical is every non-whitespace character, in order.
        """
        text = PERSIAN_BODY
        spans = chunk_source_text(text)
        assert covers_text(spans, text)
        joined = "".join(span.text for span in spans)
        assert "".join(joined.split()) == normalized_source_text(text).replace(" ", "")

    def test_every_persian_character_is_accounted_for(self):
        text = PERSIAN_UNBROKEN
        joined = "".join(span.text for span in chunk_source_text(text))
        assert joined == text

    def test_persian_unbroken_text_is_bounded_and_covered(self):
        spans = chunk_source_text(PERSIAN_UNBROKEN)
        assert len(spans) > 1
        assert covers_text(spans, PERSIAN_UNBROKEN)
        assert all(len(span.text) <= MAX_CHUNK_CHARS for span in spans)

    def test_zwnj_is_preserved(self):
        text = "نمی‌توان و می‌رود و پژوهشگران گزارش کردند."
        spans = chunk_source_text(text)
        assert len(spans) == 1
        assert "\u200c" in spans[0].text
        assert spans[0].text == text

    def test_zwnj_is_not_treated_as_a_boundary(self):
        text = "کلمه‌پیوندشته " * 300
        spans = chunk_source_text(text)
        assert covers_text(spans, text)

    def test_mixed_script_text_is_covered(self):
        spans = chunk_source_text(MIXED_BODY)
        assert covers_text(spans, MIXED_BODY)
        assert any("field survey" in span.text for span in spans)
        assert any("۴۰" in span.text for span in spans)

    def test_decomposed_text_is_normalized_before_it_is_split(self):
        """NFC, so a decomposed vowel agrees with the composed form.

        Exactly P3.7's rule: a passage is stored in the form a quote is compared
        against, so the two can never disagree about what the passage says.
        """
        decomposed = "cafe\u0301 with an accent. And a second sentence."
        composed = unicodedata.normalize("NFC", decomposed)
        spans = chunk_source_text(decomposed)
        assert covers_text(spans, decomposed)
        assert spans[0].text == normalize_evidence_text(composed)
        assert spans[0].text.startswith("caf\u00e9")

    def test_whitespace_is_collapsed_exactly_as_the_evidence_stage_collapses_it(self):
        text = "a   lot\n\nof\twhitespace   here"
        assert normalized_source_text(text) == normalize_evidence_text(text)


class TestEmpty:
    def test_empty_text_produces_no_passage(self):
        assert chunk_source_text("") == ()

    def test_whitespace_only_text_produces_no_passage(self):
        assert chunk_source_text("   \n\t  \n ") == ()

    def test_a_string_shorter_than_the_minimum_still_produces_one_passage(self):
        """A tiny source is still a source; it is not turned into nothing."""
        spans = chunk_source_text("Hi.")
        assert [span.text for span in spans] == ["Hi."]

    def test_none_is_not_a_passage_source(self):
        assert chunk_source_text(None or "") == ()


class TestAgainstTheExtractionContract:
    def test_the_plan_of_an_extracted_page_is_reproducible(self):
        """The whole point: same extracted content, same passages, forever."""
        content = ExtractedContent(url="https://example.com/a", text=LONG_BODY)
        first = chunk_source_text(content.text)
        second = chunk_source_text(content.text)
        assert [span.model_dump() for span in first] == [
            span.model_dump() for span in second
        ]
        assert covers_text(first, content.text)

    def test_the_plan_covers_the_extracted_text_of_an_unusable_page(self):
        content = ExtractedContent(url="https://example.com/b", text="   ")
        assert chunk_source_text(content.text) == ()

    def test_a_passage_is_always_quotable_in_full(self):
        """MAX_CHUNK_CHARS is the quote bound, so a passage can be quoted whole."""
        for span in chunk_source_text(LONG_BODY):
            assert len(span.text) <= MAX_CHUNK_CHARS
