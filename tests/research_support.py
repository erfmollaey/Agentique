"""Shared scaffolding for the Phase 3 evidence-stage tests (P3.7).

Three P3.7 test modules need the same three things, so they live here rather than
being copied into each file where they would drift:

* :func:`uow_over` — a unit-of-work factory over an existing session, so a
  service test can use the **real** research repositories against the test
  database instead of a second set of in-memory doubles. The evidence decision is
  about persistence boundaries as much as about validation, so testing it against
  a fake store would test the wrong thing.
* :func:`seed_source` — a persisted run, source, and passages to analyze.
* :class:`RecordingLLM` and the two response builders — a fake Phase 2 provider
  that records every call, so a test can assert *what was sent* and *how many
  times* without a model.

Not a mock framework: the fake implements the real ``LLMProvider`` signature, and
running out of replies is an error rather than a silent repeat, which is how "an
unexpected extra provider call" becomes a test failure instead of a passing test.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import update

from app.db.models import SourceModel
from app.domain.chat import LLMReply
from app.repositories.research import (
    CitationRepositoryImpl,
    ClaimRepositoryImpl,
    ResearchRunRepositoryImpl,
    SourceChunkRepositoryImpl,
    SourceRepositoryImpl,
)
from app.repositories.users import UserRepositoryImpl

#: Telemetry ids per test module, so two tests in one module cannot collide on the
#: same user even though each rolls back its own transaction.
PERSIAN_PASSAGE = "پژوهشگران در سال ۱۴۰۳ افزایش چهل درصدی را در نمونه‌های شهری گزارش کردند."

#: A second passage of the *first* source, so a test can seed a source with more than
#: one passage and prove the canonical ordering is by ordinal rather than by id.
SECOND_PASSAGE = (
    "The same methodology section notes that the sample excluded coastal districts "
    "from the second reporting period."
)
ENGLISH_PASSAGE = (
    "The 2024 field survey recorded a forty percent increase across the sampled "
    "districts, according to the published methodology section."
)
#: Deliberately *different* text from :data:`ENGLISH_PASSAGE`, so a test that
#: asserts "the other source's text was not sent" is testing the boundary rather
#: than matching identical strings twice.
OTHER_PASSAGE = (
    "This second page covers an unrelated regional budget report and lists its "
    "own methodology notes."
)
MIXED_PASSAGE = (
    "این گزارش بر پایهٔ field survey منتشر شده و رشد ۴۰ درصدی را نشان می‌دهد."
)


class RecordingLLM:
    """A fake ``LLMProvider`` that hands back queued replies and records every call.

    ``replies`` are consumed in order. Asking for one more reply than the test
    queued fails loudly, which is how "the stage called the provider twice when it
    should have called once" is caught — a retry here would silently look correct.
    """

    def __init__(self, replies: list[str] | None = None, *, error: Exception | None = None) -> None:
        self._replies = list(replies or [])
        self._error = error
        self.calls: list[dict[str, Any]] = []

    @property
    def call_count(self) -> int:
        return len(self.calls)

    @property
    def system_messages(self) -> list[str]:
        return [call["messages"][0].content for call in self.calls]

    @property
    def user_messages(self) -> list[str]:
        return [call["messages"][1].content for call in self.calls]

    def complete(self, messages, *, model, temperature, max_tokens) -> LLMReply:
        self.calls.append(
            {
                "messages": list(messages),
                "model": model,
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
        )
        if self._error is not None:
            raise self._error
        if not self._replies:
            raise AssertionError(
                "the evidence stage called the provider more times than the test "
                f"queued replies for (calls so far: {len(self.calls)})"
            )
        return LLMReply(
            text=self._replies.pop(0),
            model=model,
            prompt_tokens=11,
            completion_tokens=7,
            finish_reason="stop",
        )


def summary_response(text: str, language: str | None = "en", **extra: Any) -> str:
    """A model response in the summary contract."""
    payload: dict[str, Any] = {"summary": text, "language": language}
    payload.update(extra)
    return json.dumps(payload, ensure_ascii=False)


def claims_response(*claims: dict[str, Any], **extra: Any) -> str:
    """A model response in the claim-extraction contract.

    Each claim is ``{"statement": ..., "source_chunk_id": ..., "quote": ...}``.
    ``source_chunk_id`` may be any JSON value, including a wrong type, because the
    point of some tests is what the contract does with one.
    """
    payload: dict[str, Any] = {"claims": list(claims)}
    payload.update(extra)
    return json.dumps(payload, ensure_ascii=False)


def claim(statement: str, source_chunk_id: Any, quote: str, **extra: Any) -> dict[str, Any]:
    """One claim entry."""
    entry: dict[str, Any] = {
        "statement": statement,
        "source_chunk_id": source_chunk_id,
        "quote": quote,
    }
    entry.update(extra)
    return entry


def synthesis_response(*sections: dict[str, Any], contradictions: Any = None, **extra: Any) -> str:
    """A model response in the synthesis contract.

    Each section is ``{"heading": ..., "text": ..., "citation_ids": [...]}``.
    ``citation_ids`` may hold any JSON value, including a wrong type, because the
    point of some tests is what the contract does with one.
    """
    payload: dict[str, Any] = {"sections": list(sections)}
    if contradictions is not None:
        payload["contradictions"] = contradictions
    payload.update(extra)
    return json.dumps(payload, ensure_ascii=False)


def section(
    text: str,
    citation_ids: Any = (),
    heading: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """One section entry."""
    entry: dict[str, Any] = {"text": text}
    if heading is not None:
        entry["heading"] = heading
    if citation_ids != ():
        entry["citation_ids"] = list(citation_ids) if isinstance(citation_ids, list) else citation_ids
    entry.update(extra)
    return entry


def contradiction(claim_ids: Any, note: str | None = None, **extra: Any) -> dict[str, Any]:
    """One reported conflict."""
    entry: dict[str, Any] = {"claim_ids": claim_ids}
    if note is not None:
        entry["note"] = note
    entry.update(extra)
    return entry


async def seed_verified_run(
    session,
    telegram_user_id: int,
    *,
    passages: tuple[str, ...] = (ENGLISH_PASSAGE,),
    quotes: tuple[str, ...] = ("recorded a forty percent increase across the sampled districts",),
    statements: tuple[str, ...] = ("The survey recorded a forty percent increase.",),
    question: str = "What did the 2024 survey find?",
    second_source: tuple[str, ...] | None = None,
    second_quotes: tuple[str, ...] = ("lists its own methodology notes",),
    second_statements: tuple[str, ...] = ("The regional budget page lists its own methodology.",),
    set_summaries: bool = False,
):
    """Create a run whose sources, passages, claims, and citations already exist.

    P3.8 consumes what P3.7 produced, so the fixture writes the **result** of the
    previous stage directly — sources, passages, ``claims`` rows with
    ``status='supported'``, and ``citations`` rows carrying a verified quote. That
    keeps these tests about synthesis rather than about re-running the evidence stage,
    and it is the honest dependency: synthesis has no answer to build without
    persisted verified evidence.

    Returns a dict with ``user``, ``run``, ``sources`` (in creation order),
    ``chunks`` (per source), ``claims`` (per source), and ``citations`` (per source) —
    all id-addressable, because a citation *is* its id in this stage.
    """
    user = await UserRepositoryImpl(session).get_or_create(telegram_user_id, None)
    runs = ResearchRunRepositoryImpl(session)
    sources = SourceRepositoryImpl(session)
    chunks_repo = SourceChunkRepositoryImpl(session)
    claims_repo = ClaimRepositoryImpl(session)
    citations_repo = CitationRepositoryImpl(session)

    run = await runs.create(user.id, question)
    # Set to running state - in a real pipeline the orchestrator does this before
    # calling synthesis. Tests bypass the orchestrator so we set it here.
    await runs.set_status(run.id, user.id, "running")

    async def _build(
        url: str,
        domain: str,
        title: str,
        texts: tuple[str, ...],
        quote_list: tuple[str, ...],
        statement_list: tuple[str, ...],
        rank: int | None,
    ):
        source = await sources.add(run.id, url, domain, title=title)
        if rank is not None:
            # `sources.rank` is P3.5's field and no stage has written it yet, so the
            # fixture sets it directly: the canonical ordering of a synthesis context
            # is built on it, and a test that cannot control rank cannot test that
            # ordering.
            await session.execute(
                update(SourceModel)
                .where(SourceModel.id == source.id)
                .values(rank=rank)
            )
        made_chunks = [
            await chunks_repo.add(source.id, ordinal, text, ordinal * 500, ordinal * 500 + len(text))
            for ordinal, text in enumerate(texts)
        ]
        made_claims, made_citations = [], []
        for chunk, quote, statement in zip(
            made_chunks, quote_list, statement_list, strict=True
        ):
            claim_row = await claims_repo.add(run.id, statement, status="supported")
            citation = await citations_repo.add(
                claim_row.id,
                source.id,
                chunk.id,
                quote,
                location=f"chunk {chunk.ordinal}",
            )
            made_claims.append(claim_row)
            made_citations.append(citation)
        if set_summaries:
            await sources.set_summary(source.id, run.id, "A seeded summary.")
        return source, made_chunks, made_claims, made_citations

    primary = await _build(
        "https://example.com/survey",
        "example.com",
        "A field survey",
        passages,
        quotes,
        statements,
        0,
    )
    sources_built = [primary]
    if second_source is not None:
        sources_built.append(
            await _build(
                "https://second.example/other",
                "second.example",
                "Another page",
                second_source,
                second_quotes,
                second_statements,
                1,
            )
        )

    await session.commit()
    return {
        "user": user,
        "run": run,
        "sources": [item[0] for item in sources_built],
        "chunks": [item[1] for item in sources_built],
        "claims": [item[2] for item in sources_built],
        "citations": [item[3] for item in sources_built],
    }


class FixedSynthesizer:
    """A ``SynthesisProvider`` double that returns one canned proposal.

    Implements the real port signature and hands back a response in the synthesis
    contract, so a service test drives the **real** parsing and the **real**
    verification without a model. A service that resolved a citation without calling
    :func:`app.domain.research.synthesis.verify_synthesis` would still fail here,
    because the answer only exists if verification built it.
    """

    def __init__(
        self, response: str | None = None, *, error: Exception | None = None
    ) -> None:
        self._response = response
        self._error = error
        self.inputs: list[Any] = []

    async def synthesize(self, synthesis_input):
        self.inputs.append(synthesis_input)
        if self._error is not None:
            raise self._error
        if self._response is None:
            raise AssertionError(
                "the synthesis stage called the provider but the test queued no response"
            )
        from app.domain.research.synthesis import SynthesisProposal
        from app.infrastructure.synthesis.synthesizer import _parse_object

        return SynthesisProposal.from_payload(_parse_object(self._response))


def uow_over(session):
    """A unit-of-work factory over an existing ``AsyncSession``.

    Commits are the caller's business, exactly as in the P3.6 persistence tests:
    these tests assert what was stored, so the session is reused and the commit is
    explicit.
    """

    class _UoW:
        def __init__(self) -> None:
            self.research_runs = ResearchRunRepositoryImpl(session)
            self.sources = SourceRepositoryImpl(session)
            self.source_chunks = SourceChunkRepositoryImpl(session)
            self.claims = ClaimRepositoryImpl(session)
            self.citations = CitationRepositoryImpl(session)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc_info) -> None:
            return None

    return lambda: _UoW()


async def seed_source(
    session,
    telegram_user_id: int,
    *,
    passages: tuple[str, ...] = (ENGLISH_PASSAGE,),
    other_passages: tuple[str, ...] = (OTHER_PASSAGE,),
    title: str | None = "A field survey",
    domain: str = "example.com",
    question: str = "What did the 2024 survey find?",
    extra_source: bool = False,
):
    """Create ``(user, run, source, chunks, other_source, other_chunks)``.

    ``other_source`` is a second source in the same run with its own passages. It
    exists so a test can prove the summarizer and the verifier never see it — the
    per-source boundary is the property that matters, and it needs a second source
    to be tested *against*.
    """
    user = await UserRepositoryImpl(session).get_or_create(telegram_user_id, None)
    runs = ResearchRunRepositoryImpl(session)
    sources = SourceRepositoryImpl(session)
    chunks = SourceChunkRepositoryImpl(session)

    run = await runs.create(user.id, question)
    source = await sources.add(run.id, f"https://{domain}/survey", domain, title=title)
    rows = [
        await chunks.add(source.id, ordinal, text, ordinal * 200, ordinal * 200 + len(text))
        for ordinal, text in enumerate(passages)
    ]

    other_source = other_chunks = None
    if extra_source:
        other_source = await sources.add(
            run.id, "https://second.example/other", "second.example", title="Another page"
        )
        other_chunks = [
            await chunks.add(
                other_source.id,
                ordinal,
                text,
                ordinal * 200,
                ordinal * 200 + len(text),
            )
            for ordinal, text in enumerate(other_passages)
        ]

    await session.commit()
    return user, run, source, rows, other_source, other_chunks