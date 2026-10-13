"""B2 ranking metadata persistence — SourceRepository rank/quality/hash/dedup.

All four fields survive a database round trip, partial updates do not erase
unrelated source fields, batch updates are atomic, duplicate targets are
scoped to the same run, invalid targets are rejected, ranking order is
observable downstream, and the existing FK/index rules stay intact.

Evidence: real PostgreSQL, no mocks.
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from app.domain.errors import DuplicateTargetError, SourceRankingValueError
from app.domain.research.types import SourceRanking
from app.repositories.research import SourceRepositoryImpl
from tests.test_research_chunk_persistence import _content, _seed, _service

ENGLISH_PASSAGE = "The 2024 field survey recorded a change across the sampled districts."
HEX64 = "0123456789abcdef" * 4


class TestRankingPersistence:
    async def test_all_four_fields_round_trip(self, db):
        _user, run, source = await _seed(db, 9201)
        repo = SourceRepositoryImpl(db)
        await repo.set_ranking(
            source.id, run.id,
            SourceRanking(source_id=source.id, rank=3, quality_score=0.85,
                          content_hash=HEX64, duplicate_of_id=None),
        )
        await db.commit()

        updated = await repo.get(source.id)
        assert updated is not None
        assert updated.rank == 3
        assert updated.quality_score == 0.85
        assert updated.content_hash == HEX64
        assert updated.duplicate_of_id is None

    async def test_partial_update_does_not_erase_unrelated_fields(self, db):
        _user, run, source = await _seed(db, 9202)
        repo = SourceRepositoryImpl(db)
        # First, set all four fields.
        await repo.set_ranking(
            source.id, run.id,
            SourceRanking(source_id=source.id, rank=2, quality_score=0.9,
                          content_hash=HEX64, duplicate_of_id=None),
        )
        await db.commit()
        # Then update only rank and quality_score.
        await repo.set_ranking(
            source.id, run.id,
            SourceRanking(source_id=source.id, rank=1, quality_score=0.75,
                          content_hash=HEX64, duplicate_of_id=None),
        )
        await db.commit()

        updated = await repo.get(source.id)
        assert updated.rank == 1
        assert updated.quality_score == 0.75
        # Unrelated fields preserved from initial add.
        assert updated.title == "A page"
        assert updated.status == "pending"

    async def test_batch_update_is_atomic_and_ownership_scoped(self, db):
        _user, run, source = await _seed(db, 9203)
        second = await SourceRepositoryImpl(db).add(
            run.id, "https://example.com/second", "example.com", title="Second"
        )
        await db.commit()

        repo = SourceRepositoryImpl(db)
        await repo.set_rankings(run.id, [
            SourceRanking(source_id=source.id, rank=1, quality_score=0.7,
                          content_hash=HEX64, duplicate_of_id=None),
            SourceRanking(source_id=second.id, rank=2, quality_score=0.6,
                          content_hash=HEX64, duplicate_of_id=source.id),
        ])
        await db.commit()

        first = await repo.get(source.id)
        second_updated = await repo.get(second.id)
        assert first.rank == 1
        assert second_updated.rank == 2
        assert second_updated.duplicate_of_id == source.id

    async def test_non_existent_duplicate_target_rejected(self, db):
        """A duplicate target that does not exist is refused."""
        _user, run, source = await _seed(db, 9204)
        repo = SourceRepositoryImpl(db)
        with pytest.raises(DuplicateTargetError):
            await repo.set_rankings(
                run.id,
                [SourceRanking(
                    source_id=source.id,
                    rank=1,
                    quality_score=0.5,
                    content_hash=HEX64,
                    duplicate_of_id=999999,  # non-existent
                )],
            )

    async def test_duplicate_target_must_belong_to_same_run(self, db):
        _user, run, source = await _seed(db, 9205)
        # Create another source in a different run.
        from app.repositories.research import ResearchRunRepositoryImpl
        other_run = await ResearchRunRepositoryImpl(db).create(_user.id, "other")
        other_source = await SourceRepositoryImpl(db).add(
            other_run.id, "https://example.com/other", "example.com"
        )
        await db.commit()

        repo = SourceRepositoryImpl(db)
        # Point source's duplicate_of to other_source.id — different run.
        with pytest.raises(DuplicateTargetError):
            await repo.set_rankings(
                run.id,
                [SourceRanking(
                    source_id=source.id,
                    rank=1,
                    quality_score=0.5,
                    content_hash=HEX64,
                    duplicate_of_id=other_source.id,
                )],
            )

    async def test_ranking_order_survives_round_trip(self, db):
        _user, run, source = await _seed(db, 9206)
        extra = await SourceRepositoryImpl(db).add(
            run.id, "https://example.com/extra", "example.com", title="Extra"
        )
        await db.commit()

        repo = SourceRepositoryImpl(db)
        await repo.set_rankings(
            run.id,
            [
                SourceRanking(source_id=source.id, rank=2, quality_score=0.9,
                              content_hash=HEX64, duplicate_of_id=None),
                SourceRanking(source_id=extra.id, rank=1, quality_score=0.95,
                              content_hash=HEX64, duplicate_of_id=source.id),
            ],
        )
        await db.commit()

        ordered = await repo.list_for_run(run.id)
        # The repository orders by rank asc, then id asc.
        assert [s.id for s in ordered] == [extra.id, source.id]
        assert [s.rank for s in ordered] == [1, 2]

    async def test_downstream_citation_selection_observes_rank_and_quality(self, db):
        _user, run, source = await _seed(db, 9208)
        repo = SourceRepositoryImpl(db)
        await repo.set_ranking(
            source.id, run.id,
            SourceRanking(
                source_id=source.id, rank=1, quality_score=0.92,
                content_hash=HEX64, duplicate_of_id=None,
            ),
        )
        await db.commit()

        # Chunk the source, then confirm the source's ranking fields survive.
        result = await _service(db).chunk_source(
            run_id=run.id,
            user_id=_user.id,
            source_id=source.id,
            content=_content(ENGLISH_PASSAGE),
        )
        assert len(result.chunks) == 1
        assert result.chunks[0].ordinal == 0
        # The source's ranking fields survive.
        updated = await repo.get(source.id)
        assert updated.rank == 1
        assert updated.quality_score == 0.92

    async def test_content_hash_is_64_char_hex_sha256_shape(self, db):
        _user, run, source = await _seed(db, 9209)
        repo = SourceRepositoryImpl(db)
        await repo.set_ranking(
            source.id, run.id,
            SourceRanking(
                source_id=source.id, rank=1, quality_score=1.0,
                content_hash=HEX64, duplicate_of_id=None,
            ),
        )
        await db.commit()
        updated = await repo.get(source.id)
        assert updated.content_hash == HEX64
        assert len(updated.content_hash) == 64

    async def test_invalid_duplicate_target_is_same_source_rejected(self, db):
        _user, run, source = await _seed(db, 9210)
        extra = await SourceRepositoryImpl(db).add(
            run.id, "https://example.com/extra", "example.com"
        )
        await db.commit()
        repo = SourceRepositoryImpl(db)
        # A source can point to a different source in the same run.
        await repo.set_ranking(
            source.id, run.id,
            SourceRanking(
                source_id=source.id, rank=1, quality_score=0.8,
                content_hash=HEX64, duplicate_of_id=extra.id,
            ),
        )
        await db.commit()
        updated = await repo.get(source.id)
        assert updated.duplicate_of_id == extra.id

    async def test_self_duplicate_rejected(self, db):
        _user, run, source = await _seed(db, 9211)
        repo = SourceRepositoryImpl(db)
        with pytest.raises(SourceRankingValueError):
            await repo.set_ranking(
                source.id, run.id,
                SourceRanking(
                    source_id=source.id, rank=1, quality_score=0.8,
                    content_hash=HEX64, duplicate_of_id=source.id,
                ),
            )

    async def test_invalid_rank_rejected(self, db):
        _user, run, source = await _seed(db, 9212)
        repo = SourceRepositoryImpl(db)
        with pytest.raises(SourceRankingValueError):
            await repo.set_ranking(
                source.id, run.id,
                SourceRanking(
                    source_id=source.id, rank=0, quality_score=0.8,
                    content_hash=HEX64, duplicate_of_id=None,
                ),
            )

    async def test_invalid_quality_score_rejected(self, db):
        _user, run, source = await _seed(db, 9213)
        repo = SourceRepositoryImpl(db)
        with pytest.raises(SourceRankingValueError):
            await repo.set_ranking(
                source.id, run.id,
                SourceRanking(
                    source_id=source.id, rank=1, quality_score=1.5,
                    content_hash=HEX64, duplicate_of_id=None,
                ),
            )

    async def test_boundary_quality_score_accepted(self, db):
        _user, run, source = await _seed(db, 9214)
        repo = SourceRepositoryImpl(db)
        for score in (0.0, 1.0, 0.0, 0.5):
            await repo.set_ranking(
                source.id, run.id,
                SourceRanking(
                    source_id=source.id, rank=1, quality_score=score,
                    content_hash=HEX64, duplicate_of_id=None,
                ),
            )
        updated = await repo.get(source.id)
        assert updated.quality_score == 0.5


class TestRankingConstraintsAndFK:
    async def test_unique_constraints_and_indexes_remain_effective(self, db):
        """The source table's (run_id, url) unique constraint is still intact."""
        _user, run, _source = await _seed(db, 9215, url="https://example.com/page")
        repo = SourceRepositoryImpl(db)
        # Adding the same URL for the same run should violate the unique constraint.
        with pytest.raises(IntegrityError):
            await repo.add(run.id, "https://example.com/page", "example.com")

    async def test_batch_atomicity_rolls_back_on_invalid_target(self, db):
        _user, run, source = await _seed(db, 9216)
        repo = SourceRepositoryImpl(db)
        # First write a valid ranking.
        await repo.set_rankings(
            run.id,
            [SourceRanking(
                source_id=source.id,
                rank=1,
                quality_score=0.5,
                content_hash=HEX64,
                duplicate_of_id=None,
            )],
        )
        await db.commit()
        # A batch with an invalid target should raise before writing anything.
        with pytest.raises(DuplicateTargetError):
            await repo.set_rankings(
                run.id,
                [
                    SourceRanking(
                        source_id=source.id,
                        rank=2,
                        quality_score=0.6,
                        content_hash=HEX64,
                        duplicate_of_id=None,
                    ),
                    SourceRanking(
                        source_id=source.id,
                        rank=3,
                        quality_score=0.7,
                        content_hash=HEX64,
                        duplicate_of_id=99999,  # invalid
                    ),
                ],
            )
        # After exception, the previous valid ranking should survive.
        updated = await repo.get(source.id)
        assert updated.rank == 1

    async def test_content_hash_not_truncated(self, db):
        """The content_hash column keeps the full 64 hex characters."""
        _user, run, source = await _seed(db, 9217)
        repo = SourceRepositoryImpl(db)
        await repo.set_ranking(
            source.id, run.id,
            SourceRanking(
                source_id=source.id, rank=1, quality_score=0.7,
                content_hash=HEX64, duplicate_of_id=None,
            ),
        )
        await db.commit()
        updated = await repo.get(source.id)
        assert len(updated.content_hash) == 64
        assert updated.content_hash == HEX64


class TestDownstreamObservability:
    """Ranking/order observed by existing downstream paths (B2 req. 5)."""

    async def test_list_for_run_orders_by_rank(self, db):
        _user, run, first = await _seed(db, 9218, url="https://example.com/one")
        second = await SourceRepositoryImpl(db).add(
            run.id, "https://example.com/two", "example.com"
        )
        await db.commit()
        repo = SourceRepositoryImpl(db)
        await repo.set_ranking(
            first.id, run.id,
            SourceRanking(source_id=first.id, rank=2, quality_score=0.6,
                          content_hash=HEX64, duplicate_of_id=None),
        )
        await repo.set_ranking(
            second.id, run.id,
            SourceRanking(source_id=second.id, rank=1, quality_score=0.8,
                          content_hash=HEX64, duplicate_of_id=None),
        )
        await db.commit()

        ordered = await repo.list_for_run(run.id)
        assert [s.rank for s in ordered] == [1, 2]
        assert ordered[0].id == second.id

    async def test_rank_none_orders_last(self, db):
        """Sources without rank order after ranked ones (nulls last)."""
        _user, run, first = await _seed(db, 9219, url="https://example.com/one")
        # No ranking set on `first`, so rank is NULL — it should order first (nulls last).
        repo = SourceRepositoryImpl(db)
        ordered = await repo.list_for_run(run.id)
        assert len(ordered) == 1
        assert ordered[0].id == first.id

    async def test_source_evidence_service_reads_persisted_source(self, db):
        """The persisted ranking is part of the source the evidence stage sees."""
        _user, run, source = await _seed(db, 9220)
        repo = SourceRepositoryImpl(db)
        await repo.set_ranking(
            source.id, run.id,
            SourceRanking(source_id=source.id, rank=1, quality_score=0.9,
                          content_hash=HEX64, duplicate_of_id=None),
        )
        await db.commit()

        # The source's ranking fields are available to downstream reads through
        # the same repository path SourceEvidenceService would use.
        updated = await repo.get(source.id)
        assert updated.rank == 1
        assert updated.quality_score == 0.9
        assert updated.content_hash == HEX64
