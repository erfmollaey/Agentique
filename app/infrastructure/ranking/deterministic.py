"""The deterministic dedup + ranking implementation (Phase 3, P3.5).

One adapter satisfies both ports, mirroring the single-adapter pattern P3.2 and
P3.4 established for search and extraction:

* it holds **no state** and performs **no I/O** — no socket, database, model
  call, or clock. Ranking the same candidates twice cannot differ because nothing
  external was consulted.
* it translates configuration into the frozen domain policy objects and then
  delegates to :mod:`app.domain.research.dedup`. All the logic is domain logic;
  this class exists so a caller depends on a port, and so a *different* strategy
  can be registered without a domain change (FR-29).
* it reads no settings itself. The caller passes them in, which is what keeps it
  constructible in a test with no environment at all.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from app.core.config import Settings
from app.domain.research.dedup import (
    DedupConfig,
    DedupRankingResult,
    RankingConfig,
    SourceCandidate,
    deduplicate,
    process_candidates,
    rank_sources,
)
from app.domain.research.ports import RankingPolicy, SourceDeduplication


class DeterministicDeduplicator:
    """``SourceDeduplication`` over canonical-URL and content-fingerprint identity.

    Satisfies the port with one method. Constructed with an explicit
    :class:`DedupConfig` so the identity policy travels with the object instead of
    being read from ambient settings on every call.
    """

    def __init__(self, config: DedupConfig | None = None) -> None:
        self._config = config or DedupConfig()

    def deduplicate(
        self, candidates: Sequence[SourceCandidate]
    ) -> DedupRankingResult:
        return deduplicate(candidates, self._config)


class DeterministicRankingPolicy:
    """``RankingPolicy`` scoring sources from the signals they actually have.

    ``reference_time`` is a constructor argument rather than a per-call one on
    purpose: freshness needs a single anchor for the whole batch, and fixing it at
    construction is what guarantees every source in one ranking is measured
    against the same instant. Pass ``None`` to score without a freshness signal.
    """

    def __init__(
        self,
        config: RankingConfig | None = None,
        *,
        reference_time: datetime | None = None,
    ) -> None:
        self._config = config or RankingConfig()
        self._reference_time = reference_time

    def rank(self, result: DedupRankingResult, query: str) -> DedupRankingResult:
        """Rank the deduplicated candidates in ``result``.

        Returns the same result with ``ranked_results`` filled in, so the caller
        keeps the candidates and duplicate groups it already had. Scores,
        ordering, per-source features, and exclusion reasons all travel together:
        a stage reporting only an ordering could not satisfy FR-42.
        """
        return rank_sources(
            result,
            query,
            config=self._config,
            reference_time=self._reference_time,
        )


def create_deduplicator(settings: Settings | None = None) -> SourceDeduplication:
    """Build the configured deduplicator (the single selection point for P3.5).

    ``DEDUP_GROUP_BY_CONTENT`` and ``DEDUP_GROUP_BY_URL`` are the only knobs:
    both default to on, and turning either off is a deliberate loss of recall that
    an operator should have to type rather than inherit by accident.
    """
    return DeterministicDeduplicator(_dedup_config(settings) or DedupConfig())


def create_ranking_policy(
    settings: Settings | None = None,
    *,
    reference_time: datetime | None = None,
) -> RankingPolicy:
    """Build the configured ranking policy (FR-29's replacement point).

    ``RANKING_MIN_QUALITY`` is the one ranking value exposed as configuration,
    because it is an operational threshold rather than a scoring claim: an
    operator deciding "below this quality, do not use this source" is making a
    judgement about acceptable evidence, not re-deriving the score.
    """
    return DeterministicRankingPolicy(
        _ranking_config(settings) or RankingConfig(), reference_time=reference_time
    )


def process(
    candidates: Sequence[SourceCandidate],
    query: str,
    settings: Settings | None = None,
    *,
    reference_time: datetime | None = None,
) -> DedupRankingResult:
    """Run the whole stage with the configured policies.

    The one call a later orchestration stage needs. It is a plain function rather
    than an object so the two policies cannot end up configured differently within
    a single run.

    It delegates to :func:`process_candidates` after translating settings into the
    two frozen domain policies, so there is exactly one implementation of the stage
    and settings can never drift away from it.
    """
    return process_candidates(
        candidates,
        query,
        dedup_config=_dedup_config(settings),
        ranking_config=_ranking_config(settings),
        reference_time=reference_time,
    )


def _dedup_config(settings: Settings | None) -> DedupConfig | None:
    """Translate settings into the deduplication policy, or ``None`` for the default."""
    if settings is None:
        return None
    return DedupConfig(
        group_by_url=settings.DEDUP_GROUP_BY_URL,
        group_by_content=settings.DEDUP_GROUP_BY_CONTENT,
    )


def _ranking_config(settings: Settings | None) -> RankingConfig | None:
    """Translate settings into the ranking policy, or ``None`` for the default."""
    if settings is None:
        return None
    return RankingConfig(min_quality_score=settings.RANKING_MIN_QUALITY)


__all__ = [
    "DeterministicDeduplicator",
    "DeterministicRankingPolicy",
    "create_deduplicator",
    "create_ranking_policy",
    "deduplicate",
    "process",
    "process_candidates",
    "rank_sources",
]