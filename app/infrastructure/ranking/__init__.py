"""Deduplication and ranking infrastructure (Phase 3, P3.5)."""
from app.infrastructure.ranking.deterministic import (
    DeterministicDeduplicator,
    DeterministicRankingPolicy,
    create_deduplicator,
    create_ranking_policy,
    process,
)

__all__ = [
    "DeterministicDeduplicator",
    "DeterministicRankingPolicy",
    "create_deduplicator",
    "create_ranking_policy",
    "process",
]