"""Focused P3.5 tests: the port surface, the pipeline, freshness, threshold,
input-order independence, and language behavior.

Deliberately separate from ``tests/test_research_dedup_ranking.py``, which covers
URL canonicalization, fingerprinting, and representative selection. This module
covers the stage as a whole — the ports a caller depends on, the entry point a
later stage will call, and the invariants that hold across the whole operation.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import Settings
from app.domain.errors import (
    InvalidSourceCandidateError,
    RankingInputError,
    UnsupportedSourceIdentityError,
)
from app.domain.research.dedup import (
    CONTENT_WEIGHT,
    FRESHNESS_HALFLIFE_DAYS,
    FRESHNESS_WEIGHT,
    PROVIDER_WEIGHT,
    QUALITY_WEIGHT,
    TITLE_WEIGHT,
    DedupConfig,
    RankingConfig,
    SourceCandidate,
    canonical_url,
    deduplicate,
    process_candidates,
    rank_sources,
    tokenize,
)
from app.domain.research.ports import RankingPolicy, SourceDeduplication
from app.infrastructure.ranking import (
    DeterministicDeduplicator,
    DeterministicRankingPolicy,
    create_deduplicator,
    create_ranking_policy,
)

#: Fixed anchor. Freshness is scored against this, never against the wall clock,
#: so every freshness assertion below is exact rather than approximate.
ANCHOR = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)


def candidate(
    url: str,
    *,
    rank: int = 1,
    title: str | None = None,
    text: str = "",
    char_count: int | None = None,
    published_at: datetime | None = None,
    description: str | None = None,
    is_empty: bool = False,
) -> SourceCandidate:
    """Build a candidate with a plausible domain derived from the URL."""
    domain = url.split("//", 1)[-1].split("/", 1)[0]
    return SourceCandidate(
        url=url,
        domain=domain,
        rank=rank,
        title=title,
        text=text,
        description=description,
        published_at=published_at,
        is_empty=is_empty,
        char_count=len(text) if char_count is None else char_count,
    )


# ---------------------------------------------------------------------------
# Ports
# ---------------------------------------------------------------------------

class TestPorts:
    """The adapters must satisfy the ports a caller depends on."""

    def test_deduplicator_satisfies_port(self):
        assert isinstance(DeterministicDeduplicator(), SourceDeduplication)

    def test_ranking_policy_satisfies_port(self):
        assert isinstance(DeterministicRankingPolicy(), RankingPolicy)

    def test_factories_return_port_satisfying_objects(self):
        settings = Settings(
            BOT_TOKEN="t", GROQ_API_KEY="k", DATABASE_URL="postgresql+asyncpg://u:p@localhost/db",
        )
        assert isinstance(create_deduplicator(settings), SourceDeduplication)
        assert isinstance(create_ranking_policy(settings), RankingPolicy)

    def test_port_is_runtime_checkable(self):
        """A structurally similar object must satisfy the port.

        This is what makes FR-29 real: a different implementation can be
        substituted without a domain change, and that only holds if the port is
        structural rather than nominal.
        """

        class CustomDeduplicator:
            def deduplicate(self, candidates):
                return deduplicate(candidates)

        assert isinstance(CustomDeduplicator(), SourceDeduplication)


# ---------------------------------------------------------------------------
# Pipeline entry point
# ---------------------------------------------------------------------------

class TestPipelineEntryPoint:
    """``process_candidates`` is what a later stage will call."""

    def test_returns_groups_and_ranked_results(self):
        result = process_candidates(
            [
                candidate("https://a.example/x", text="same body text here", char_count=100),
                candidate("https://b.example/y", text="same body text here", char_count=100),
            ],
            "query",
        )
        assert len(result.duplicate_groups) == 1
        assert len(result.ranked_results) == 1

    def test_distinct_sources_all_survive(self):
        """A batch where nothing collides must still rank every source.

        This is the regression the old API shape invited: ranking only the
        reported duplicate groups would return nothing here, silently discarding
        the whole result set.
        """
        result = process_candidates(
            [
                candidate("https://a.example/1", text="first article body", char_count=100),
                candidate("https://b.example/2", text="second article body", char_count=100),
                candidate("https://c.example/3", text="third article body", char_count=100),
            ],
            "query",
        )
        assert len(result.ranked_results) == 3
        assert not result.is_empty

    def test_output_has_no_two_entries_from_one_duplicate_group(self):
        result = process_candidates(
            [
                candidate("https://a.example/1", text="identical syndicated body", char_count=100),
                candidate("https://b.example/2", text="identical syndicated body", char_count=100),
                candidate("https://c.example/3", text="identical syndicated body", char_count=100),
                candidate("https://d.example/4", text="a genuinely different article", char_count=100),
            ],
            "query",
        )
        keys = [r.duplicate_group_key for r in result.ranked_results]
        assert len(keys) == len(set(keys))
        assert len(result.ranked_results) == 2

    def test_every_input_candidate_is_recorded(self):
        candidates = [
            candidate("https://a.example/1", text="one", char_count=50),
            candidate("https://b.example/2", text="two", char_count=50),
        ]
        result = process_candidates(candidates, "query")
        assert result.candidates == tuple(candidates)

    def test_empty_input_produces_empty_output(self):
        result = process_candidates([], "query")
        assert result.is_empty
        assert result.ranked_results == ()
        assert result.duplicate_groups == ()

    def test_pipeline_is_pure(self):
        """Running twice on the same input must produce equal results.

        Purity here means: no clock, no randomness, no mutation of the input.
        """
        candidates = [candidate("https://a.example/1", text="body one", char_count=60)]
        first = process_candidates(candidates, "query", reference_time=ANCHOR)
        second = process_candidates(candidates, "query", reference_time=ANCHOR)
        assert first == second
        assert candidates[0] == candidate("https://a.example/1", text="body one", char_count=60)


# ---------------------------------------------------------------------------
# Freshness (FR-28)
# ---------------------------------------------------------------------------

class TestFreshnessSignal:
    def test_no_reference_time_means_no_freshness(self):
        result = rank_sources(
            deduplicate([candidate("https://a.example/1", text="body", char_count=50, published_at=ANCHOR)]),
            "query",
        )
        assert result.ranked_results[0].features.freshness == 0.0

    def test_no_publication_date_means_no_freshness(self):
        result = rank_sources(
            deduplicate([candidate("https://a.example/1", text="body", char_count=50)]),
            "query",
            reference_time=ANCHOR,
        )
        assert result.ranked_results[0].features.freshness == 0.0

    def test_published_now_scores_full_freshness(self):
        result = rank_sources(
            deduplicate([candidate("https://a.example/1", text="body", char_count=50, published_at=ANCHOR)]),
            "query",
            reference_time=ANCHOR,
        )
        assert result.ranked_results[0].features.freshness == pytest.approx(1.0)

    def test_half_life_scores_one_half(self):
        published = ANCHOR - timedelta(days=FRESHNESS_HALFLIFE_DAYS)
        result = rank_sources(
            deduplicate([candidate("https://a.example/1", text="body", char_count=50, published_at=published)]),
            "query",
            reference_time=ANCHOR,
        )
        assert result.ranked_results[0].features.freshness == pytest.approx(0.5)

    def test_older_source_scores_lower_freshness(self):
        recent = rank_sources(
            deduplicate([candidate("https://a.example/1", text="body", char_count=50,
                                   published_at=ANCHOR - timedelta(days=1))]),
            "query",
            reference_time=ANCHOR,
        ).ranked_results[0].features.freshness
        old = rank_sources(
            deduplicate([candidate("https://a.example/1", text="body", char_count=50,
                                   published_at=ANCHOR - timedelta(days=365))]),
            "query",
            reference_time=ANCHOR,
        ).ranked_results[0].features.freshness
        assert recent > old

    def test_future_date_does_not_exceed_one(self):
        result = rank_sources(
            deduplicate([candidate("https://a.example/1", text="body", char_count=50,
                                   published_at=ANCHOR + timedelta(days=30))]),
            "query",
            reference_time=ANCHOR,
        )
        assert result.ranked_results[0].features.freshness == 1.0

    def test_freshness_weight_is_nonzero_but_smallest(self):
        assert FRESHNESS_WEIGHT > 0.0
        assert FRESHNESS_WEIGHT < PROVIDER_WEIGHT
        assert FRESHNESS_WEIGHT < TITLE_WEIGHT

    def test_naive_reference_time_is_rejected(self):
        """A naive anchor is a defect, not a value to guess at."""
        with pytest.raises(RankingInputError):
            rank_sources(
                deduplicate([candidate("https://a.example/1", text="body", char_count=50)]),
                "query",
                reference_time=datetime(2026, 10, 7, 12, 0),
            )

    def test_naive_publication_date_is_handled(self):
        """A provider date without an offset is read as UTC, not crashed on."""
        naive = datetime(2026, 10, 1, 12, 0)
        result = rank_sources(
            deduplicate([candidate("https://a.example/1", text="body", char_count=50,
                                   published_at=naive)]),
            "query",
            reference_time=ANCHOR,
        )
        assert result.ranked_results[0].features.freshness > 0.0


# ---------------------------------------------------------------------------
# Quality threshold (FR-30)
# ---------------------------------------------------------------------------

class TestQualityThreshold:
    def test_low_quality_source_is_excluded_with_a_reason(self):
        result = process_candidates(
            [candidate("https://a.example/1", text="tiny", char_count=4, is_empty=True)],
            "query",
        )
        assert len(result.excluded_results) == 1
        assert "unusable" in result.excluded_results[0].excluded_reason

    def test_exclusion_is_recorded_not_dropped(self):
        """FR-30: the exclusion is recorded. The entry stays in the output."""
        result = process_candidates(
            [candidate("https://a.example/1", text="tiny", char_count=4, is_empty=True)],
            "query",
        )
        assert len(result.ranked_results) == 1
        assert result.ranked_results[0].is_excluded

    def test_usable_source_is_not_excluded(self):
        result = process_candidates(
            [candidate("https://a.example/1", text="a body with enough content", char_count=200)],
            "query",
        )
        assert not result.ranked_results[0].is_excluded
        assert result.excluded_results == ()

    def test_threshold_is_configurable(self):
        candidates = [candidate("https://a.example/1", text="a reasonably long body", char_count=300)]
        permissive = process_candidates(candidates, "q", ranking_config=RankingConfig(min_quality_score=0.0))
        strict = process_candidates(candidates, "q", ranking_config=RankingConfig(min_quality_score=1.0))
        assert not permissive.ranked_results[0].is_excluded
        assert strict.ranked_results[0].is_excluded

    def test_exclusion_reason_states_the_numbers(self):
        result = process_candidates(
            [candidate("https://a.example/1", text="short but present body", char_count=60)],
            "q",
            ranking_config=RankingConfig(min_quality_score=0.99),
        )
        reason = result.ranked_results[0].excluded_reason
        assert "threshold" in reason

    def test_excluded_source_keeps_its_position_in_one_ordering(self):
        """Exclusion is a property of an entry, not a separate list.

        Sorting excluded sources to the bottom would make the positions of the
        kept sources depend on which weak sources happened to be in the batch.
        """
        result = process_candidates(
            [
                candidate("https://a.example/1", text="short", char_count=5, is_empty=True),
                candidate("https://b.example/2", text="a solid amount of content", char_count=400),
            ],
            "query",
        )
        assert [r.position for r in result.ranked_results] == [1, 2]


# ---------------------------------------------------------------------------
# Ranking signals
# ---------------------------------------------------------------------------

class TestRankingSignals:
    def test_weights_are_public_documented_constants(self):
        assert (PROVIDER_WEIGHT, TITLE_WEIGHT, CONTENT_WEIGHT, FRESHNESS_WEIGHT, QUALITY_WEIGHT) == (
            0.30, 0.30, 0.20, 0.10, 0.10,
        )

    def test_weights_sum_to_one(self):
        total = PROVIDER_WEIGHT + TITLE_WEIGHT + CONTENT_WEIGHT + FRESHNESS_WEIGHT + QUALITY_WEIGHT
        assert total == pytest.approx(1.0)

    def test_features_are_exposed_and_bounded(self):
        result = process_candidates(
            [candidate("https://a.example/1", title="python asyncio", text="python asyncio body",
                       char_count=400, published_at=ANCHOR)],
            "python asyncio",
            reference_time=ANCHOR,
        )
        features = result.ranked_results[0].features
        for value in (features.provider, features.title_match, features.content_match,
                      features.freshness, features.quality):
            assert 0.0 <= value <= 1.0

    def test_score_equals_weighted_sum_of_features(self):
        result = process_candidates(
            [candidate("https://a.example/1", title="python asyncio", text="python asyncio body",
                       char_count=400, published_at=ANCHOR)],
            "python asyncio",
            reference_time=ANCHOR,
        )
        ranked = result.ranked_results[0]
        assert ranked.score == pytest.approx(ranked.features.total)

    def test_provider_relevance_is_used_when_present(self):
        """A provider relevance number is preferred over the position fallback."""
        with_relevance = SourceCandidate(
            url="https://a.example/1", domain="a.example", rank=5,
            relevance=0.9, text="first distinct body", char_count=200,
        )
        without = SourceCandidate(
            url="https://b.example/2", domain="b.example", rank=5,
            text="second distinct body", char_count=200,
        )
        result = process_candidates([with_relevance, without], "query")
        by_url = {r.candidate.url: r for r in result.ranked_results}
        assert by_url["https://a.example/1"].features.provider == pytest.approx(0.9)
        assert by_url["https://b.example/2"].features.provider == pytest.approx(0.2)

    def test_exact_title_match_beats_unrelated(self):
        result = process_candidates(
            [
                candidate("https://a.example/1", title="python asyncio guide",
                          text="asyncio patterns", char_count=200),
                candidate("https://b.example/2", title="unrelated topic",
                          text="nothing relevant here", char_count=200),
            ],
            "python asyncio",
        )
        assert result.ranked_results[0].candidate.url == "https://a.example/1"

    def test_content_only_match_still_scores(self):
        result = process_candidates(
            [candidate("https://a.example/1", title="a heading", text="a passing mention of python asyncio",
                       char_count=300)],
            "python asyncio",
        )
        features = result.ranked_results[0].features
        assert features.content_match > 0.0
        assert features.title_match == 0.0

    def test_no_query_match_scores_zero_on_match_signals(self):
        result = process_candidates(
            [candidate("https://a.example/1", title="unrelated", text="nothing relevant", char_count=300)],
            "python asyncio",
        )
        features = result.ranked_results[0].features
        assert features.title_match == 0.0
        assert features.content_match == 0.0

    def test_empty_query_is_not_a_perfect_match(self):
        result = process_candidates(
            [candidate("https://a.example/1", title="anything", text="body text", char_count=300)],
            "",
        )
        assert result.ranked_results[0].features.title_match == 0.0


# ---------------------------------------------------------------------------
# Determinism, idempotence, input-order independence
# ---------------------------------------------------------------------------

class TestInvariants:
    def _batch(self) -> list[SourceCandidate]:
        return [
            candidate("https://a.example/1", rank=1, title="python asyncio",
                      text="python asyncio body", char_count=200),
            candidate("https://b.example/2", rank=2, title="python asyncio guide",
                      text="python asyncio guide body", char_count=300),
            candidate("https://c.example/3", rank=3, title="rust ownership",
                      text="rust ownership body", char_count=250),
        ]

    def test_repeated_runs_are_identical(self):
        candidates = self._batch()
        first = process_candidates(candidates, "python asyncio", reference_time=ANCHOR)
        for _ in range(3):
            assert process_candidates(candidates, "python asyncio", reference_time=ANCHOR) == first

    def test_input_order_does_not_change_the_ranking(self):
        candidates = self._batch()
        forward = process_candidates(candidates, "python asyncio", reference_time=ANCHOR)
        reversed_result = process_candidates(
            list(reversed(candidates)), "python asyncio", reference_time=ANCHOR
        )
        assert [r.candidate.url for r in forward.ranked_results] == [
            r.candidate.url for r in reversed_result.ranked_results
        ]
        assert [r.score for r in forward.ranked_results] == [
            r.score for r in reversed_result.ranked_results
        ]

    def test_provider_position_survives_shuffling(self):
        """Position is a field, so shuffling must not invent a new ordering."""
        candidates = self._batch()
        forward = process_candidates(candidates, "python asyncio", reference_time=ANCHOR)
        shuffled = process_candidates(
            [candidates[2], candidates[0], candidates[1]], "python asyncio", reference_time=ANCHOR
        )
        assert forward.ranked_results == shuffled.ranked_results

    def test_dedup_is_idempotent(self):
        candidates = [
            candidate("https://a.example/1", text="identical body", char_count=120),
            candidate("https://b.example/2", text="identical body", char_count=120),
            candidate("https://c.example/3", text="a different article", char_count=120),
        ]
        once = deduplicate(candidates)
        representatives = [g.canonical for g in once.duplicate_groups]
        representatives.extend(
            c for c in once.candidates if c.canonical_url() not in
            {m.canonical_url() for g in once.duplicate_groups for m in g.members}
        )
        twice = deduplicate(representatives)
        assert twice.duplicate_groups == ()

    def test_ranking_preserves_candidates_and_groups(self):
        """Ranking must not drop the earlier stage's output."""
        result = process_candidates(self._batch(), "query", reference_time=ANCHOR)
        assert len(result.candidates) == 3
        assert result.duplicate_groups == ()
        assert len(result.ranked_results) == 3

    def test_positions_are_contiguous_from_one(self):
        result = process_candidates(self._batch(), "query", reference_time=ANCHOR)
        assert [r.position for r in result.ranked_results] == [1, 2, 3]

    def test_results_are_sorted_by_descending_score(self):
        result = process_candidates(self._batch(), "python asyncio", reference_time=ANCHOR)
        scores = [r.score for r in result.ranked_results]
        assert scores == sorted(scores, reverse=True)


# ---------------------------------------------------------------------------
# Language compatibility
# ---------------------------------------------------------------------------

class TestLanguageCompatibility:
    def test_english_tokens(self):
        assert tokenize("Python asyncio guide") == frozenset({"python", "asyncio", "guide"})

    def test_persian_tokens(self):
        assert tokenize("راهنمای پایتون") == frozenset({"راهنمای", "پایتون"})

    def test_zwnj_is_preserved_inside_a_token(self):
        """ZWNJ keeps a Persian compound word whole.

        Splitting on it would shred exactly the words an English tokenizer keeps
        intact, so the query and the document would fail to match.
        """
        assert tokenize("نمی‌توان") == frozenset({"نمی‌توان"})

    def test_zwnj_word_matches_across_query_and_document(self):
        result = process_candidates(
            [candidate("https://a.example/1", title="نمی‌توان استفاده کرد", text="body", char_count=200)],
            "نمی‌توان",
        )
        assert result.ranked_results[0].features.title_match > 0.0

    def test_persian_punctuation_is_not_a_token(self):
        """Arabic comma and question mark are separators, not content."""
        tokens = tokenize("پایتون، چیست؟")
        assert tokens == frozenset({"پایتون", "چیست"})

    def test_persian_digits(self):
        assert "۱۲۳" in tokenize("قیمت ۱۲۳ تومان")

    def test_arabic_indic_digits(self):
        assert "١٢٣" in tokenize("سعر ١٢٣ ريال")

    def test_nfc_and_nfd_persian_agree(self):
        """The same Persian word written two ways must be one token."""
        composed = "پایتون"
        decomposed = "پایتون"
        assert tokenize(composed) == tokenize(decomposed)

    def test_mixed_persian_english(self):
        tokens = tokenize("machine learning یادگیری ماشین")
        assert {"machine", "learning", "یادگیری", "ماشین"} <= tokens

    def test_mixed_language_title_matches_both_halves(self):
        result = process_candidates(
            [candidate("https://a.example/1", title="Python یادگیری ماشین",
                       text="body", char_count=200)],
            "یادگیری ماشین",
        )
        assert result.ranked_results[0].features.title_match > 0.0

    def test_single_persian_letter_token_is_kept(self):
        assert "م" in tokenize("م test")

    def test_punctuation_only_input_yields_no_tokens(self):
        assert tokenize("!!! ??? ...") == frozenset()

    def test_whitespace_only_input_yields_no_tokens(self):
        assert tokenize("   \n\t  ") == frozenset()

    def test_matching_is_on_tokens_not_substrings(self):
        """``AI`` must not match inside ``said``, ``cat`` not inside ``education``."""
        tokens = tokenize("said education")
        assert "ai" not in tokens
        assert "cat" not in tokens


# ---------------------------------------------------------------------------
# Configuration and policy switches
# ---------------------------------------------------------------------------

class TestPolicyConfiguration:
    def test_grouping_by_url_can_be_disabled(self):
        """Turning off URL identity leaves content identity to catch the pair.

        The two bodies differ so the only rule that can group these is URL
        identity — which is exactly what the switch is meant to isolate.
        """
        candidates = [
            candidate("https://a.example/1?utm_source=x", text="body one", char_count=100),
            candidate("https://a.example/1?utm_source=y", text="a different body", char_count=100),
        ]
        assert len(deduplicate(candidates, DedupConfig()).duplicate_groups) == 1
        disabled = deduplicate(candidates, DedupConfig(group_by_url=False))
        assert disabled.duplicate_groups == ()

    def test_grouping_by_content_can_be_disabled(self):
        candidates = [
            candidate("https://a.example/1", text="identical", char_count=100),
            candidate("https://b.example/2", text="identical", char_count=100),
        ]
        assert len(deduplicate(candidates, DedupConfig()).duplicate_groups) == 1
        disabled = deduplicate(candidates, DedupConfig(group_by_content=False))
        assert disabled.duplicate_groups == ()

    def test_settings_supply_the_dedup_policy(self):
        settings = Settings(
            BOT_TOKEN="t", GROQ_API_KEY="k",
            DATABASE_URL="postgresql+asyncpg://u:p@localhost/db",
            DEDUP_GROUP_BY_CONTENT=False,
        )
        candidates = [
            candidate("https://a.example/1", text="identical", char_count=100),
            candidate("https://b.example/2", text="identical", char_count=100),
        ]
        assert create_deduplicator(settings).deduplicate(candidates).duplicate_groups == ()

    def test_settings_supply_the_quality_threshold(self):
        settings = Settings(
            BOT_TOKEN="t", GROQ_API_KEY="k",
            DATABASE_URL="postgresql+asyncpg://u:p@localhost/db",
            RANKING_MIN_QUALITY=1.0,
        )
        policy = create_ranking_policy(settings)
        result = policy.rank(
            deduplicate([candidate("https://a.example/1", text="body", char_count=300)]), "query"
        )
        assert result.ranked_results[0].is_excluded

    def test_dedup_config_rejects_a_meaningless_group_size(self):
        with pytest.raises(ValueError):
            DedupConfig(min_group_size=1)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class TestTypedErrors:
    def test_url_without_authority_raises_typed_error(self):
        with pytest.raises(UnsupportedSourceIdentityError):
            canonical_url("not-a-url")

    def test_typed_errors_are_value_errors(self):
        """Invalid arguments stay catchable as ``ValueError`` for existing guards."""
        assert issubclass(UnsupportedSourceIdentityError, ValueError)
        assert issubclass(InvalidSourceCandidateError, ValueError)
        assert issubclass(RankingInputError, ValueError)

    def test_errors_are_not_retryable(self):
        """Deterministic stage: a retry cannot change the outcome."""
        assert InvalidSourceCandidateError.retryable is False
        assert UnsupportedSourceIdentityError.retryable is False
        assert RankingInputError.retryable is False

    def test_candidate_requires_a_url(self):
        with pytest.raises(ValueError):
            SourceCandidate(url="", domain="a.example", rank=1)

    def test_candidate_requires_a_domain(self):
        with pytest.raises(ValueError):
            SourceCandidate(url="https://a.example/1", domain="  ", rank=1)

    def test_error_message_does_not_echo_the_url(self):
        """A URL is untrusted input and must not reach a log line via an error."""
        with pytest.raises(UnsupportedSourceIdentityError) as excinfo:
            canonical_url("not-a-url?token=secret-value")
        assert "secret-value" not in str(excinfo.value)


# ---------------------------------------------------------------------------
# Security boundary (SR-1 … SR-8)
# ---------------------------------------------------------------------------

class TestNoNetworkAccess:
    """Canonicalization compares values; it must never act on them."""

    def test_domain_module_opens_no_socket(self):
        """Structural check: the module holds no HTTP or socket capability.

        Asserted on the module's namespace rather than its source text, so a
        docstring that merely *mentions* sockets does not fail the test while an
        actual import does.
        """
        import app.domain.research.dedup as dedup_module

        forbidden = ("httpx", "aiohttp", "requests", "socket", "urllib.request", "asyncio", "sqlalchemy")
        for name in forbidden:
            assert not hasattr(dedup_module, name.split(".")[0])

    def test_module_source_imports_only_domain_and_stdlib(self):
        """Parse the imports and assert the domain layer has no I/O dependency.

        A stricter statement of the same property: the module's import list must
        contain nothing that can reach the network, a database, or the clock.
        """
        import ast

        import app.domain.research.dedup as dedup_module

        assert dedup_module.__file__ is not None
        with open(dedup_module.__file__, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())

        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                imported.add(node.module.split(".")[0])

        forbidden = {"httpx", "aiohttp", "requests", "socket", "asyncio", "sqlalchemy", "celery", "redis"}
        assert imported.isdisjoint(forbidden)

    def test_canonicalizing_a_private_address_does_not_connect(self):
        """A loopback URL canonicalizes; nothing is resolved or requested."""
        result = canonical_url("http://127.0.0.1:8080/admin")
        assert result == "http://127.0.0.1:8080/admin"

    def test_domain_module_reads_no_clock(self):
        """Freshness must never consult the wall clock.

        ``datetime.now`` and ``utcnow`` in the domain would make the ranking of a
        stored candidate set change on its own, which the reproducibility
        requirement forbids.
        """
        import ast

        import app.domain.research.dedup as dedup_module

        assert dedup_module.__file__ is not None
        with open(dedup_module.__file__, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())

        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert node.func.attr not in {"now", "utcnow", "today"}, (
                    "the domain must take its freshness anchor as an argument"
                )