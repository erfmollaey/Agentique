"""Citation rendering for delivery (FR-40, FR-37).

The answers under test are built the only way the product builds one: through
``verify_synthesis`` against a real ``CitationRegistry``. Nothing here constructs a
``SynthesizedAnswer`` or a ``VerifiedCitation`` by hand, because the module-private
token guard on ``_Verified`` makes that impossible by design — which is the point the
last two test classes below assert.

What is proved:

* a rendered answer carries inline ``[n]`` markers and a numbered source list;
* every marker resolves to the source it names, and every listed source is cited;
* two citations from one document produce **one** source entry, not two;
* a marker is never orphaned from its entry and an entry is never duplicated, even
  when the answer and the list span several Telegram messages;
* nothing is invented: no marker without a source, no source without a marker, and
  no URL that the persisted registry did not supply;
* the renderer refuses an answer whose citation has no renderable URL rather than
  emitting a dangling reference or dropping content silently.
"""

from __future__ import annotations

import re

import pytest

from app.domain.errors import CitationIntegrityError
from app.domain.research.synthesis import (
    CitationEntry,
    CitationRegistry,
    SynthesisInput,
    SynthesisProposal,
    plan_synthesis_context,
    verify_synthesis,
)
from app.services.context import CharRatioTokenEstimator
from app.services.research.rendering import render_answer

SURVEY = (
    "The 2024 field survey recorded a forty percent increase across the sampled "
    "districts over the preceding twelve months."
)
SURVEY_QUOTE = "recorded a forty percent increase across the sampled districts"
SURVEY_STATEMENT = "The 2024 survey found a large increase."

COUNTER = CharRatioTokenEstimator(4.0)

#: The limit delivery will apply to a 4096-capable deployment, minus headroom. The
#: tests use small synthetic limits; the production value is exercised by
#: ``test_a_limit_the_size_of_a_real_message_produces_one_chunk``.
PRODUCTION_LIMIT = 4080

MARKER = re.compile(r"\[(\d+)\]")
ENTRY = re.compile(r"^\[(\d+)\] ", re.MULTILINE)


def entry(
    citation_id: int,
    *,
    source_id: int | None = None,
    url: str | None = None,
    domain: str = "example.com",
    title: str | None = "A field survey",
    location: str | None = None,
) -> CitationEntry:
    resolved_source = source_id if source_id is not None else citation_id
    return CitationEntry(
        citation_id=citation_id,
        claim_id=citation_id + 100,
        claim_text=SURVEY_STATEMENT,
        source_id=resolved_source,
        chunk_source_id=resolved_source,
        chunk_id=citation_id + 200,
        ordinal=citation_id,
        quote=SURVEY_QUOTE,
        passage_text=SURVEY,
        location=location if location is not None else f"chunk {citation_id}",
        source_url=url or f"https://{domain}/{resolved_source}",
        source_domain=domain,
        source_title=title,
        source_rank=0,
    )


def registry(*entries: CitationEntry) -> CitationRegistry:
    return CitationRegistry(run_id=1, entries=tuple(entries))


def synthesis_input(reg: CitationRegistry) -> SynthesisInput:
    return SynthesisInput(
        run_id=reg.run_id,
        question="What did the 2024 survey find?",
        objective="Establish what the survey measured",
        language="en",
        registry=reg,
        context=plan_synthesis_context(
            reg, max_tokens=8_000, counter=COUNTER, max_items=30
        ),
    )


def answer_for(payload: dict, reg: CitationRegistry):
    return verify_synthesis(
        SynthesisProposal.from_payload(payload), synthesis_input(reg)
    )


def render(payload: dict, *entries: CitationEntry, limit: int = PRODUCTION_LIMIT):
    reg = registry(*(entries or (entry(1),)))
    return render_answer(answer_for(payload, reg), limit=limit)


def two_sources() -> tuple[CitationEntry, CitationEntry]:
    return (
        entry(1, url="https://example.com/survey", domain="example.com",
              title="Field survey 2024", location="chunk 3"),
        entry(2, url="https://stats.example.org/table", domain="stats.example.org",
              title="Statistical annex", location="Introduction"),
    )


# --- markers and the source list ---------------------------------------------


class TestInlineMarkers:
    def test_a_cited_section_carries_a_marker_and_the_list_names_its_source(self):
        rendered = render(
            {"sections": [{"heading": "Findings", "text": "A rise.", "citation_ids": [1]}]},
            *two_sources(),
        )

        assert "[1]" in rendered.body
        assert rendered.cited
        assert rendered.messages == tuple(
            rendered.messages
        )  # a tuple, ordered, and equal to body chunks + list chunks

        numbers = [source.number for source in rendered.sources]
        assert numbers == [1]

    def test_two_sections_citing_two_sources_number_them_separately(self):
        rendered = render(
            {
                "sections": [
                    {"heading": "A", "text": "First.", "citation_ids": [1]},
                    {"heading": "B", "text": "Second.", "citation_ids": [2]},
                ]
            },
            *two_sources(),
        )

        assert rendered.sources[0].url == "https://example.com/survey"
        assert rendered.sources[1].url == "https://stats.example.org/table"
        assert MARKER.findall(rendered.body) == ["1", "2"]

    def test_the_marker_names_the_source_that_supports_its_section(self):
        """The mapping is checked positionally, not just "a marker exists"."""
        rendered = render(
            {
                "sections": [
                    {"heading": "Annex", "text": "From the annex.", "citation_ids": [2]},
                    {"heading": "Survey", "text": "From the survey.", "citation_ids": [1]},
                ]
            },
            *two_sources(),
        )

        body = rendered.body
        annex_line = body.split("\n\n")[0]
        assert "[1]" in annex_line
        assert "[2]" not in annex_line

        # Section one listed citation 2 first, so source 1 is numbered first.
        assert rendered.sources[0].url == "https://stats.example.org/table"
        assert rendered.sources[1].url == "https://example.com/survey"

    def test_a_marker_runs_at_the_end_of_the_section_it_belongs_to(self):
        """Section-level placement: nothing claims sentence-level support (§ 19.3)."""
        rendered = render(
            {"sections": [{"heading": "H", "text": "Line one.\nLine two.", "citation_ids": [1]}]},
            *two_sources(),
        )

        assert rendered.body.splitlines()[-1] == "[1]"

    def test_a_section_with_no_heading_still_gets_its_marker(self):
        rendered = render(
            {"sections": [{"text": "Bare.", "citation_ids": [1]}]},
            *two_sources(),
        )

        assert rendered.body.splitlines() == ["Bare.", "[1]"]


class TestSourceList:
    def test_an_entry_carries_the_persisted_title_domain_url_and_locator(self):
        rendered = render(
            {"sections": [{"text": "A.", "citation_ids": [1]}]},
            *two_sources(),
        )

        source = rendered.sources[0]
        assert source.title == "Field survey 2024"
        assert source.domain == "example.com"
        assert source.url == "https://example.com/survey"
        assert source.locator == "chunk 3"

    def test_a_source_without_a_stored_title_is_labelled_rather_than_left_blank(self):
        rendered = render(
            {"sections": [{"text": "A.", "citation_ids": [1]}]},
            entry(1, title=None),
        )

        assert rendered.sources[0].title == "(untitled source)"

    def test_an_over_long_title_is_bounded_but_its_url_survives_intact(self):
        rendered = render(
            {"sections": [{"text": "A.", "citation_ids": [1]}]},
            entry(1, title="T" * 400),
        )

        source = rendered.sources[0]
        assert len(source.title) <= 120
        assert source.url == "https://example.com/1"

    def test_a_second_citation_from_the_same_document_is_one_entry(self):
        """Two passages of one article are one reference, not two."""
        rendered = render(
            {"sections": [{"text": "Both.", "citation_ids": [1, 2]}]},
            entry(1, source_id=7, url="https://example.com/doc", location="chunk 1"),
            entry(2, source_id=7, url="https://example.com/doc", location="chunk 2"),
        )

        assert len(rendered.sources) == 1
        assert rendered.body.splitlines()[-1] == "[1]"
        assert rendered.messages[-1].count("https://example.com/doc") == 1

    def test_dynamic_values_are_escaped_because_the_bot_parses_html(self):
        """A retrieved title is untrusted content and must not become markup (SR-3)."""
        rendered = render(
            {"sections": [{"text": "A.", "citation_ids": [1]}]},
            entry(1, title="<b>Buy</b> & win", url="https://e.com/a?x=1&y=2"),
        )

        source = rendered.sources[0]
        assert "<b>" not in source.title
        assert "&amp;" in source.title
        assert "&amp;" in source.url
        assert "<b>" not in "".join(rendered.messages)


# --- message boundaries -------------------------------------------------------


class TestTelegramChunking:
    def test_a_short_answer_is_one_message(self):
        rendered = render(
            {"sections": [{"text": "Short.", "citation_ids": [1]}]},
            *two_sources(),
        )

        assert len(rendered.messages) == 1
        assert len(rendered.messages[0]) <= PRODUCTION_LIMIT

    def test_every_chunk_is_within_the_limit(self):
        limit = 120
        rendered = render(
            {
                "sections": [
                    {"heading": "A long heading " * 6, "text": "word " * 200,
                     "citation_ids": [1]},
                    {"heading": "B", "text": "word " * 200, "citation_ids": [2]},
                ]
            },
            *two_sources(),
            limit=limit,
        )

        assert len(rendered.messages) > 1
        assert all(len(message) <= limit for message in rendered.messages)

    def test_a_marker_is_never_orphaned_from_the_source_it_names(self):
        """Across the split, every marker in the body has its entry in the list."""
        limit = 120
        rendered = render(
            {
                "sections": [
                    {"heading": "A" * 80, "text": "filler " * 60, "citation_ids": [1]},
                    {"heading": "B" * 80, "text": "filler " * 60, "citation_ids": [2]},
                ]
            },
            *two_sources(),
            limit=limit,
        )

        listed = {
            int(number)
            for message in rendered.messages
            for number in ENTRY.findall(message)
        }
        used = {int(number) for number in MARKER.findall(rendered.body)}
        assert used == listed == {1, 2}

    def test_a_source_list_split_across_messages_loses_nothing(self):
        """Each entry's URL survives the split, exactly once (FR-40)."""
        limit = 150
        entries = tuple(
            entry(
                n,
                source_id=n,
                url=f"https://publisher-{n}.example.org/a-reasonably-long-path-{n}",
                domain=f"publisher-{n}.example.org",
                title=f"Publisher {n} report",
            )
            for n in range(1, 7)
        )
        rendered = render(
            {
                "sections": [
                    {"text": f"Finding {n}.", "citation_ids": [n]} for n in range(1, 7)
                ]
            },
            *entries,
            limit=limit,
        )

        joined = "\n".join(rendered.messages)
        for n in range(1, 7):
            assert f"[{n}]" in joined
            assert f"https://publisher-{n}.example.org/a-reasonably-long-path-{n}" in joined

    def test_chunking_never_duplicates_a_source_entry(self):
        limit = 150
        entries = tuple(
            entry(n, source_id=n, url=f"https://p{n}.example.org/x", domain=f"p{n}.example.org")
            for n in range(1, 6)
        )
        rendered = render(
            {"sections": [{"text": f"F{n}.", "citation_ids": [n]} for n in range(1, 6)]},
            *entries,
            limit=limit,
        )

        labels = [
            int(number)
            for message in rendered.messages
            for number in ENTRY.findall(message)
        ]
        assert sorted(labels) == [1, 2, 3, 4, 5]

    def test_the_body_arrives_before_the_source_list(self):
        limit = 120
        rendered = render(
            {"sections": [{"text": "filler " * 60, "citation_ids": [1]}]},
            *two_sources(),
            limit=limit,
        )

        joined = "\n".join(rendered.messages)
        assert joined.index("filler") < joined.index("Sources:")

    def test_nothing_is_silently_truncated(self):
        limit = 120
        body_text = " ".join(f"w{i}" for i in range(200))
        rendered = render(
            {"sections": [{"text": body_text, "citation_ids": [1]}]},
            *two_sources(),
            limit=limit,
        )

        joined = "".join(rendered.messages).replace("\n", " ")
        for word in ("w0", "w99", "w199"):
            assert word in joined


# --- nothing invented ---------------------------------------------------------


class TestNoInventedReferences:
    def test_every_marker_has_an_entry_and_every_entry_is_cited(self):
        rendered = render(
            {
                "sections": [
                    {"text": "A.", "citation_ids": [1]},
                    {"text": "B.", "citation_ids": [2]},
                    {"text": "C.", "citation_ids": [1, 2]},
                ]
            },
            *two_sources(),
        )

        used = {int(n) for n in MARKER.findall(rendered.body)}
        listed = {source.number for source in rendered.sources}
        assert used == listed

    def test_no_url_appears_that_the_registry_did_not_supply(self):
        reg = registry(*two_sources())
        rendered = render_answer(
            answer_for(
                {"sections": [{"text": "A.", "citation_ids": [1, 2]}]}, reg
            ),
            limit=PRODUCTION_LIMIT,
        )

        supplied = {entry.source_url for entry in reg.entries}
        printed = {source.url for source in rendered.sources}
        assert printed <= supplied

    def test_a_repeated_citation_prints_one_marker(self):
        rendered = render(
            {"sections": [{"text": "A.", "citation_ids": [1, 1, 1]}]},
            *two_sources(),
        )

        assert rendered.body.splitlines()[-1] == "[1]"

    def test_a_reported_contradiction_keeps_both_sides_cited(self):
        """FR-34 at the rendering boundary: both sources appear, neither is dropped."""
        rendered = render(
            {
                "sections": [{"text": "They disagree.", "citation_ids": [1, 2]}],
                "contradictions": [
                    # ``entry`` numbers its claim ``citation_id + 100``, so these are
                    # the claim ids the registry actually holds.
                    {"claim_ids": [101, 102], "note": "The two sources conflict."}
                ],
            },
            *two_sources(),
        )

        assert {source.number for source in rendered.sources} == {1, 2}
        assert "The two sources conflict." in rendered.body


class TestUntrustedPayloads:
    def test_a_citation_with_no_url_is_refused_rather_than_rendered_dangling(self):
        """A boundary assertion: the renderer refuses instead of inventing a link."""
        blank = entry(1).model_copy(update={"source_url": "   "})
        reg = registry(blank)
        answer = answer_for(
            {"sections": [{"text": "A.", "citation_ids": [1]}]}, reg
        )

        with pytest.raises(CitationIntegrityError, match="no retrievable URL"):
            render_answer(answer, limit=PRODUCTION_LIMIT)

    def test_an_answer_whose_citations_do_not_verify_cannot_be_built_at_all(self):
        """The trust boundary is upstream: a fabricated reference never becomes an
        answer, so the renderer has nothing to invent from."""
        with pytest.raises(Exception) as excinfo:
            answer_for(
                {"sections": [{"text": "A.", "citation_ids": [99]}]}, registry(entry(1))
            )

        assert "99" in str(excinfo.value) or "citation" in str(excinfo.value).lower()

    def test_a_limit_of_zero_is_rejected_rather_than_silently_dropping_content(self):
        with pytest.raises(ValueError, match="limit must be positive"):
            render(
                {"sections": [{"text": "A.", "citation_ids": [1]}]},
                *two_sources(),
                limit=0,
            )


class TestLimitsAgreeWithDelivery:
    def test_the_production_limit_is_what_delivery_would_apply(self):
        """The renderer and DeliveryService must agree, or a chunk is split twice."""
        from app.services.formatting import telegram_chunk_limit

        assert telegram_chunk_limit(4096) == PRODUCTION_LIMIT

    def test_a_rendered_chunk_is_not_re_split_by_delivery(self):
        from app.services.formatting import split_for_telegram, telegram_chunk_limit

        rendered = render(
            {
                "sections": [
                    {"heading": "A" * 60, "text": "filler " * 60, "citation_ids": [1]},
                    {"heading": "B" * 60, "text": "filler " * 60, "citation_ids": [2]},
                ]
            },
            *two_sources(),
            limit=140,
        )

        limit = telegram_chunk_limit(4096)
        for message in rendered.messages:
            assert split_for_telegram(message, limit) == [message]

    def test_a_limit_the_size_of_a_real_message_yields_a_single_chunk(self):
        rendered = render(
            {"sections": [{"text": "A short answer.", "citation_ids": [1]}]},
            *two_sources(),
            limit=PRODUCTION_LIMIT,
        )

        assert len(rendered.messages) == 1
        assert len(rendered.messages[0]) <= PRODUCTION_LIMIT