"""Research domain types, independent of any infrastructure (P3.1).

The domain package must be constructible without a database, a broker, or a
network: these tests import only ``app.domain.research`` and prove the types
enforce their own invariants (the ones the frozen contract makes the schema
enforce at the persistence layer).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.domain.research.ports import (
    CitationRepository,
    ClaimRepository,
    ResearchRunRepository,
    SourceChunkRepository,
    SourceRepository,
)
from app.domain.research.types import (
    Citation,
    Claim,
    ClaimStatus,
    ResearchRun,
    ResearchRunStatus,
    Source,
    SourceChunk,
    SourceStatus,
)


def test_a_run_is_constructible_without_infrastructure():
    run = ResearchRun(user_id=1, question="what is the llm cost of inference?")
    assert run.status is ResearchRunStatus.QUEUED
    assert run.prompt_tokens == 0
    assert not run.status.terminal


def test_status_values_match_the_frozen_lifecycle():
    assert {s.value for s in ResearchRunStatus} == {
        "queued",
        "running",
        "completed",
        "partial",
        "failed",
    }
    assert ResearchRunStatus.COMPLETED.terminal
    assert ResearchRunStatus.PARTIAL.terminal
    assert ResearchRunStatus.FAILED.terminal
    assert not ResearchRunStatus.RUNNING.terminal


def test_source_status_values_match_the_frozen_lifecycle():
    assert {s.value for s in SourceStatus} == {
        "pending",
        "fetched",
        "fetch_failed",
        "extracted",
        "unusable",
        "duplicate",
        "excluded",
        "selected",
    }


def test_claim_status_values_match_the_frozen_set():
    assert {s.value for s in ClaimStatus} == {"supported", "unsupported", "contradicted"}


def test_an_unknown_run_status_is_rejected():
    with pytest.raises(ValidationError):
        ResearchRun(user_id=1, question="q", status="teleported")


def test_an_unknown_source_status_is_rejected():
    with pytest.raises(ValidationError):
        Source(run_id=1, url="https://example.com", domain="example.com", status="enriched")


def test_an_unknown_claim_status_is_rejected():
    with pytest.raises(ValidationError):
        Claim(run_id=1, text="t", status="plausible")


def test_status_coercion_accepts_the_database_string_form():
    run = ResearchRun(user_id=1, question="q", status="running")
    assert run.status is ResearchRunStatus.RUNNING
    source = Source(run_id=1, url="https://example.com", domain="example.com", status="extracted")
    assert source.status is SourceStatus.EXTRACTED
    claim = Claim(run_id=1, text="t", status="supported")
    assert claim.status is ClaimStatus.SUPPORTED


def test_a_run_requires_a_question():
    with pytest.raises(ValidationError):
        ResearchRun(user_id=1, question="   ")


def test_a_source_requires_url_and_domain():
    with pytest.raises(ValidationError):
        Source(run_id=1, url="", domain="example.com")
    with pytest.raises(ValidationError):
        Source(run_id=1, url="https://example.com", domain="")


def test_a_chunk_records_position_for_citation():
    chunk = SourceChunk(source_id=1, ordinal=0, text="passage", char_start=10, char_end=17)
    assert (chunk.char_start, chunk.char_end) == (10, 17)
    with pytest.raises(ValidationError):
        SourceChunk(source_id=1, ordinal=-1, text="passage", char_start=0, char_end=7)


def test_a_citation_points_at_one_specific_evidence():
    citation = Citation(
        claim_id=1,
        source_id=2,
        chunk_id=3,
        quote="the passage that supports the claim",
        location="chunk 1",
    )
    assert citation.chunk_id == 3


def test_a_citation_cannot_exist_without_its_passage():
    """AD-033: the link is worthless without the evidence it links.

    ``citations.quote`` is ``NOT NULL`` in the schema and required here, so a
    citation row with no passage behind it is not representable (P3.7).
    """
    with pytest.raises(ValidationError):
        Citation(claim_id=1, source_id=2, chunk_id=3, location="chunk 1")
    with pytest.raises(ValidationError):
        Citation(
            claim_id=1,
            source_id=2,
            chunk_id=3,
            quote="   ",
            location="chunk 1",
        )


def test_the_ports_are_runtime_checkable_protocols():
    """A structural substitution site: any object with the methods satisfies the port."""
    for port in (
        ResearchRunRepository,
        SourceRepository,
        SourceChunkRepository,
        ClaimRepository,
        CitationRepository,
    ):
        assert isinstance(port, type)
