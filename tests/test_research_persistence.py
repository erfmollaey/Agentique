"""Research persistence against a real PostgreSQL (P3.1).

Same contract as ``test_database_layer.py``: migrations create the schema,
constraints are asserted against the database rather than assumed, and
ownership scoping (SR-10) is exercised through the repository semantics.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.db.models import (
    CitationModel,
    ClaimModel,
    ResearchRunModel,
    SourceChunkModel,
    SourceModel,
)
from app.domain.errors import ClaimEvidenceError
from app.repositories.research import (
    CitationRepositoryImpl,
    ClaimRepositoryImpl,
    ResearchRunRepositoryImpl,
    SourceChunkRepositoryImpl,
    SourceRepositoryImpl,
)
from app.repositories.users import UserRepositoryImpl


@pytest.fixture
def runs(db):
    return ResearchRunRepositoryImpl(db)


@pytest.fixture
def sources(db):
    return SourceRepositoryImpl(db)


@pytest.fixture
def chunks(db):
    return SourceChunkRepositoryImpl(db)


@pytest.fixture
def claims(db):
    return ClaimRepositoryImpl(db)


@pytest.fixture
def citations(db):
    return CitationRepositoryImpl(db)


async def _user(db, telegram_user_id: int):
    return await UserRepositoryImpl(db).get_or_create(telegram_user_id, None)


async def _seeded_run(db, runs, telegram_user_id=5101):
    user = await _user(db, telegram_user_id)
    return await runs.create(user.id, "What is retrieval-augmented generation?")


# --- ownership (SR-10, T-23) ------------------------------------------------


async def test_a_run_belongs_to_exactly_one_user(db, runs):
    user = await _user(db, 5102)
    run = await runs.create(user.id, "q")
    assert run.user_id == user.id
    found = await runs.get_for_user(run.id, user.id)
    assert found is not None and found.id == run.id


async def test_another_users_run_is_not_returned(db, runs):
    owner = await _user(db, 5103)
    other = await _user(db, 5104)
    run = await runs.create(owner.id, "q")
    assert await runs.get_for_user(run.id, other.id) is None


async def test_runs_listed_for_a_user_never_include_anothers(db, runs):
    alice = await _user(db, 5105)
    bob = await _user(db, 5106)
    await runs.create(alice.id, "a1")
    await runs.create(alice.id, "a2")
    await runs.create(bob.id, "b1")
    assert len(await runs.list_for_user(alice.id, 20)) == 2
    assert len(await runs.list_for_user(bob.id, 20)) == 1


# --- the source -> chunk -> claim -> citation chain -------------------------


async def test_sources_belong_to_their_run(db, runs, sources):
    run = await _seeded_run(db, runs)
    source = await sources.add(run.id, "https://example.com/a", "example.com", title="A")
    assert [s.id for s in await sources.list_for_run(run.id)] == [source.id]


async def test_chunks_belong_to_their_source_in_order(db, runs, sources, chunks):
    run = await _seeded_run(db, runs)
    source = await sources.add(run.id, "https://example.com/a", "example.com")
    first = await chunks.add(source.id, 0, "passage one", 0, 11)
    second = await chunks.add(source.id, 1, "passage two", 12, 23)
    listed = await chunks.list_for_source(source.id)
    assert [c.id for c in listed] == [first.id, second.id]


async def test_claims_belong_to_their_run(db, runs, claims):
    run = await _seeded_run(db, runs)
    claim = await claims.add(run.id, "RAG reduces hallucination")
    assert [c.id for c in await claims.list_for_run(run.id)] == [claim.id]
    assert claim.status.value == "unsupported", "claims default to unsupported (FR-39)"


async def test_a_citation_references_valid_evidence(db, runs, sources, chunks, claims, citations):
    run = await _seeded_run(db, runs)
    source = await sources.add(run.id, "https://example.com/a", "example.com")
    chunk = await chunks.add(source.id, 0, "passage", 0, 7)
    claim = await claims.add(run.id, "claim text")
    citation = await citations.add(claim.id, source.id, chunk.id, "passage", location="¶1")
    assert citation.claim_id == claim.id and citation.source_id == source.id
    assert citation.quote == "passage"
    listed = await citations.list_for_claim(claim.id)
    assert [c.chunk_id for c in listed] == [chunk.id]


async def test_a_citation_cannot_be_written_without_its_passage(db, runs, sources, chunks, claims, citations):
    """AD-033: the passage is part of the link, not an optional annotation.

    Refused by the repository before any SQL runs, and the column is ``NOT NULL``
    behind it — so there is no path, including a hand-run INSERT, that creates a
    citation with nothing behind it.
    """
    run = await _seeded_run(db, runs)
    source = await sources.add(run.id, "https://example.com/a", "example.com")
    chunk = await chunks.add(source.id, 0, "passage", 0, 7)
    claim = await claims.add(run.id, "claim text")

    with pytest.raises(ClaimEvidenceError):
        await citations.add(claim.id, source.id, chunk.id, "")

    with pytest.raises(IntegrityError):
        async with db.begin_nested():
            await db.execute(
                text(
                    "INSERT INTO citations (claim_id, source_id, chunk_id, location) "
                    "VALUES (:c, :s, :k, NULL)"
                ),
                {"c": claim.id, "s": source.id, "k": chunk.id},
            )


async def test_a_citation_cannot_name_another_sources_passage(
    db, runs, sources, chunks, claims, citations
):
    """``source_chunk.source_id == citation.source_id``, enforced by the database.

    Before P3.7 the two foreign keys were independent, so this insert would have
    succeeded — a citation attributing one source's claim to a different source's
    passage, which is exactly the cross-source evidence link § 14 forbids.
    """
    run = await _seeded_run(db, runs)
    mine = await sources.add(run.id, "https://example.com/a", "example.com")
    theirs = await sources.add(run.id, "https://example.com/b", "example.com")
    their_chunk = await chunks.add(theirs.id, 0, "their passage", 0, 13)
    claim = await claims.add(run.id, "claim text")

    with pytest.raises(IntegrityError):
        async with db.begin_nested():
            await citations.add(claim.id, mine.id, their_chunk.id, "their passage")


async def test_a_citation_to_a_missing_chunk_is_rejected(db, runs, sources, claims, citations):
    run = await _seeded_run(db, runs)
    source = await sources.add(run.id, "https://example.com/a", "example.com")
    claim = await claims.add(run.id, "claim text")
    with pytest.raises(IntegrityError):
        async with db.begin_nested():
            await citations.add(claim.id, source.id, 999999, "a quote", location=None)


async def test_a_citation_to_a_missing_source_is_rejected(db, runs, claims, citations):
    run = await _seeded_run(db, runs)
    claim = await claims.add(run.id, "claim text")
    with pytest.raises(IntegrityError):
        async with db.begin_nested():
            await citations.add(claim.id, 999999, 999999, "a quote", location=None)


async def test_a_source_cannot_belong_to_a_missing_run(db, sources):
    with pytest.raises(IntegrityError):
        async with db.begin_nested():
            await sources.add(999999, "https://example.com", "example.com")


async def test_a_chunk_cannot_belong_to_a_missing_source(db, chunks):
    with pytest.raises(IntegrityError):
        async with db.begin_nested():
            await chunks.add(999999, 0, "t", 0, 1)


# --- uniqueness --------------------------------------------------------------


async def test_a_run_cannot_record_the_same_url_twice(db, runs, sources):
    run = await _seeded_run(db, runs)
    await sources.add(run.id, "https://example.com/a", "example.com")
    await db.commit()
    with pytest.raises(IntegrityError):
        async with db.begin_nested():
            await sources.add(run.id, "https://example.com/a", "example.com")


async def test_chunk_ordinals_are_unique_per_source(db, runs, sources, chunks):
    run = await _seeded_run(db, runs)
    source = await sources.add(run.id, "https://example.com/a", "example.com")
    await chunks.add(source.id, 0, "one", 0, 3)
    await db.commit()
    with pytest.raises(IntegrityError):
        async with db.begin_nested():
            await chunks.add(source.id, 0, "duplicate ordinal", 4, 21)


async def test_the_same_passage_cannot_be_cited_twice_for_one_claim(
    db, runs, sources, chunks, claims, citations
):
    run = await _seeded_run(db, runs)
    source = await sources.add(run.id, "https://example.com/a", "example.com")
    chunk = await chunks.add(source.id, 0, "passage", 0, 7)
    claim = await claims.add(run.id, "claim text")
    await citations.add(claim.id, source.id, chunk.id, "passage")
    await db.commit()
    with pytest.raises(IntegrityError):
        async with db.begin_nested():
            await citations.add(claim.id, source.id, chunk.id, "passage")


# --- closed lifecycle sets ---------------------------------------------------


async def test_run_status_is_constrained_to_the_frozen_set(db, runs):
    run = await _seeded_run(db, runs)
    await db.commit()
    with pytest.raises(IntegrityError):
        await db.execute(
            text("UPDATE research_runs SET status = 'paused' WHERE id = :id"), {"id": run.id}
        )
        await db.flush()


async def test_source_status_is_constrained(db, runs, sources):
    run = await _seeded_run(db, runs)
    source = await sources.add(run.id, "https://example.com/a", "example.com")
    await db.commit()
    with pytest.raises(IntegrityError):
        await db.execute(
            text("UPDATE sources SET status = 'enriched' WHERE id = :id"), {"id": source.id}
        )
        await db.flush()


async def test_claim_status_is_constrained(db, runs, claims):
    run = await _seeded_run(db, runs)
    claim = await claims.add(run.id, "claim text")
    await db.commit()
    with pytest.raises(IntegrityError):
        await db.execute(
            text("UPDATE claims SET status = 'plausible' WHERE id = :id"), {"id": claim.id}
        )
        await db.flush()


# --- plan persistence (FR-4) and token ledger --------------------------------


async def test_the_run_plan_and_tokens_persist(db, runs):
    from sqlalchemy import update as sa_update

    run = await _seeded_run(db, runs)
    await db.execute(
        sa_update(ResearchRunModel)
        .where(ResearchRunModel.id == run.id)
        .values(plan={"sub_questions": ["a", "b"]}, prompt_tokens=10, completion_tokens=5)
    )
    await db.commit()
    found = await runs.get_for_user(run.id, run.user_id)
    assert found.plan == {"sub_questions": ["a", "b"]}
    assert found.prompt_tokens == 10 and found.completion_tokens == 5


# --- conversation relationship -----------------------------------------------


async def test_a_run_may_reference_a_conversation_but_not_a_missing_one(db, runs):
    from app.repositories.conversations import ConversationRepositoryImpl

    user = await _user(db, 5107)
    conversation = await ConversationRepositoryImpl(db).create(user.id, "t")
    run = await runs.create(user.id, "q", conversation_id=conversation.id)
    assert run.conversation_id == conversation.id
    with pytest.raises(IntegrityError):
        async with db.begin_nested():
            await runs.create(user.id, "q2", conversation_id=999999)


async def test_model_layer_cascade_removes_the_research_tree(db):
    """ORM-level cascade matches the schema-level cascade."""
    from sqlalchemy import select

    from app.db.models import ResearchRunModel

    user = await _user(db, 5108)
    run_model = ResearchRunModel(user_id=user.id, question="q")
    db.add(run_model)
    await db.flush()
    source = SourceModel(run_id=run_model.id, url="https://example.com", domain="example.com")
    db.add(source)
    await db.flush()
    chunk = SourceChunkModel(source_id=source.id, ordinal=0, text="t", char_start=0, char_end=1)
    claim = ClaimModel(run_id=run_model.id, text="c")
    db.add_all([chunk, claim])
    await db.flush()
    db.add(
        CitationModel(claim_id=claim.id, source_id=source.id, chunk_id=chunk.id, quote="t")
    )
    await db.commit()

    await db.delete(run_model)
    await db.commit()

    for model in (SourceModel, SourceChunkModel, ClaimModel, CitationModel):
        remaining = (await db.execute(select(model))).scalars().all()
        assert remaining == [], f"{model.__tablename__} rows survived the cascade"
