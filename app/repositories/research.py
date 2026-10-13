"""Research persistence: runs, sources, chunks, claims, citations.

The concrete SQLAlchemy side of the research ports in
:mod:`app.domain.research.ports`. Ownership of a run is expressed the same
way Phase 2 expresses conversation ownership: :meth:`ResearchRunRepositoryImpl.get_for_user`
takes the owner, so a run can only be read through the user it belongs to
(SR-10, T-23). Child artifacts are reached by their parent's identifiers,
the same way Phase 2 reaches messages by ``conversation_id``.

Uniqueness and referential integrity are enforced by the database (the
constraints on the models), not by application-level checks — the schema is
the authority, the same principle as ``uq_messages_turn_id_role``.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    CitationModel,
    ClaimModel,
    ResearchRunModel,
    SourceChunkModel,
    SourceModel,
)
from app.domain.errors import (
    ClaimEvidenceError,
    DuplicateMessageError,
    DuplicateTargetError,
    SourceRankingError,
)
from app.domain.research.synthesis import CitationEntry
from app.domain.research.types import (
    Citation,
    Claim,
    ResearchRun,
    Source,
    SourceChunk,
    SourceRanking,
)


class ResearchRunRepositoryImpl:
    """SQLAlchemy implementation of the research-run port."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        user_id: int,
        question: str,
        conversation_id: int | None = None,
        *,
        request_key: str | None = None,
    ) -> ResearchRun:
        """Create a queued run, optionally guarded by a request key (B7).

        ``request_key`` is the derived identity of the *request*; when it is
        supplied the insert carries ``ON CONFLICT (user_id, request_key) DO
        NOTHING`` (migration 0005), so the **database** decides whether this
        request already produced a run rather than the application guessing. Two
        concurrent redeliveries therefore converge on one row even though neither
        could have seen the other.

        Why the conflict is carried in the statement rather than parsed from an
        ``IntegrityError`` afterwards. Catching the violation and inspecting it
        means re-typing whatever the driver reports, and the asyncpg adapter wraps
        the driver error in a way that does not expose the constraint name
        reliably — so "which constraint fired?" becomes a string match on localised
        text. Naming the target in the statement instead makes the intent part of
        the SQL: a conflict on the request key is the only one that resolves
        silently, and a violation of a foreign key, of the status CHECK, or of any
        other unique constraint still raises exactly the driver's own error. That
        distinction is the point — a caller that named a missing conversation must
        not be told "this request already has a run", or it would retry a defect.

        A collision raises a **retryable** :class:`DuplicateMessageError` for the
        same reason the user repository raises one on a concurrent insert: the
        database already holds the winning row, but this transaction is spent, so
        the caller rolls back and re-reads with :meth:`get_for_request`.
        """
        if request_key is not None:
            return await self._create_with_request_key(
                user_id, question, conversation_id, request_key
            )

        model = ResearchRunModel(
            user_id=user_id, question=question, conversation_id=conversation_id
        )
        self._session.add(model)
        await self._session.flush()
        return ResearchRun.model_validate(model)

    async def _create_with_request_key(
        self,
        user_id: int,
        question: str,
        conversation_id: int | None,
        request_key: str,
    ) -> ResearchRun:
        """The ``ON CONFLICT`` variant of :meth:`create`, used when a key exists.

        Issued as a core INSERT rather than through the ORM's unit-of-work path,
        because ``ON CONFLICT DO NOTHING`` is a statement-level decision and there
        is no ORM-level equivalent. The row is then re-read through the session, so
        what the caller receives is a managed ``ResearchRunModel`` and the caller's
        later reads and writes behave exactly as they do for the plain path.

        Every other column keeps its model default, so a conflicting request cannot
        cause *any* column of the existing run to be rewritten: this insert is
        either the only writer of its own row or a no-op.
        """
        from typing import Any as _Any

        from sqlalchemy.dialects.postgresql import insert as pg_insert

        statement: _Any = pg_insert(ResearchRunModel).values(
            user_id=user_id,
            question=question,
            conversation_id=conversation_id,
            request_key=request_key,
            status="queued",
        ).on_conflict_do_nothing(
            index_elements=["user_id", "request_key"]
        ).returning(
            ResearchRunModel.id
        )
        new_id = (await self._session.execute(statement)).scalar_one_or_none()
        if new_id is None:
            error = DuplicateMessageError("a research run already exists for this request")
            error.retryable = True
            raise error
        model = await self._session.get(ResearchRunModel, new_id)
        assert model is not None  # the insert just returned this row's id
        return ResearchRun.model_validate(model)

    async def get_for_request(
        self, user_id: int, request_key: str | None
    ) -> ResearchRun | None:
        """The run a request key already produced for this user, if any (B7).

        The read half of the idempotency contract: it is what a retry of a
        duplicated ``/research`` command reads after its insert collided, so the
        caller converges on the existing run instead of creating a second one.

        Ownership-scoped by ``user_id`` for the same reason the unique constraint
        is: the key is derived from a Telegram user id, so a lookup without the
        owner would be able to name a run belonging to somebody else (SR-10).

        Returns ``None`` for a ``None`` key rather than matching some arbitrary
        run: a run with no request identity is not a match for anything.
        """
        if request_key is None:
            return None
        result = await self._session.execute(
            select(ResearchRunModel).where(
                ResearchRunModel.user_id == user_id,
                ResearchRunModel.request_key == request_key,
            )
        )
        model = result.scalar_one_or_none()
        return ResearchRun.model_validate(model) if model else None

    async def get_for_user(self, run_id: int, user_id: int) -> ResearchRun | None:
        result = await self._session.execute(
            select(ResearchRunModel).where(
                ResearchRunModel.id == run_id,
                ResearchRunModel.user_id == user_id,
            )
        )
        model = result.scalar_one_or_none()
        return ResearchRun.model_validate(model) if model else None

    async def list_for_user(self, user_id: int, limit: int = 20) -> Sequence[ResearchRun]:
        result = await self._session.execute(
            select(ResearchRunModel)
            .where(ResearchRunModel.user_id == user_id)
            .order_by(ResearchRunModel.created_at.desc(), ResearchRunModel.id.desc())
            .limit(limit)
        )
        return [ResearchRun.model_validate(m) for m in result.scalars()]

    async def set_synthesis(self, run_id: int, user_id: int, synthesis: dict) -> None:
        """Persist a verified synthesis on the run (FR-35 … FR-39, P3.8).

        Scoped by ``user_id`` as well as ``run_id``, like ``set_plan``: the synthesis
        is the run's authoritative output, so a caller that named someone else's run
        affects zero rows rather than overwriting what that run concluded (SR-10).

        Takes the already-validated payload, not a ``SynthesizedAnswer``. The
        repository is persistence and does not re-derive domain validity; the answer
        was verified once by ``app.domain.research.synthesis.verify_synthesis`` before
        this was called, and only a verified answer can produce a payload.
        """
        await self._session.execute(
            update(ResearchRunModel)
            .where(
                ResearchRunModel.id == run_id,
                ResearchRunModel.user_id == user_id,
            )
            .values(synthesis=synthesis, updated_at=func.now())
        )

    async def set_plan(self, run_id: int, user_id: int, plan: dict) -> None:
        """Persist a validated plan on the run (FR-4).

        Scoped by ``user_id`` as well as ``run_id``: the plan is the largest
        attacker-influenced value the research schema stores, so the write is
        ownership-scoped like every read in this repository (SR-10). A caller that
        named someone else's run would affect zero rows rather than overwrite what
        that run claims it will investigate.
        """
        await self._session.execute(
            update(ResearchRunModel)
            .where(
                ResearchRunModel.id == run_id,
                ResearchRunModel.user_id == user_id,
            )
            .values(plan=plan, updated_at=func.now())
        )

    async def set_summary_note(
        self, run_id: int, user_id: int, failure_summary: str
    ) -> None:
        """Persist a run's terminal summary without touching its status.

        The write an orchestrator needs *after* a terminal status already exists.
        ``set_status`` cannot do it: the lifecycle refuses a same-state transition
        (``SAME_STATE``), and that refusal is exactly what protects a terminal run
        from being reopened — so the answer is not to relax it, but to provide a
        separate, status-free write for the one field the caller can only know after
        the terminal write landed.

        Scoped by ``user_id`` as well as ``run_id``, like every other write on this
        repository (SR-10): the summary is what a run history shows, so a caller that
        named somebody else's run must affect zero rows rather than describe another
        user's run to them.

        Writes the single named column and nothing else, so it can never move a
        status, overwrite an answer, or alter the token ledger.
        """
        await self._session.execute(
            update(ResearchRunModel)
            .where(
                ResearchRunModel.id == run_id,
                ResearchRunModel.user_id == user_id,
            )
            .values(
                failure_summary=failure_summary,
                updated_at=func.now(),
            )
        )

    async def set_status(
        self,
        run_id: int,
        user_id: int,
        status: str,
        *,
        failure_summary: str | None = None,
        final_answer: str | None = None,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
    ) -> None:
        """Transition a run's status, atomically and ownership-scoped (P3.9-B, B4).

        The write is scoped by ``user_id`` as well as ``run_id``, the same ownership
        discipline ``set_plan`` and ``set_synthesis`` already apply (SR-10): a caller
        that names a run belonging to someone else affects zero rows rather than
        mutating another user's run.

        Lifecycle validation is enforced **before** the write: the current status is
        read under a row lock, the requested transition is validated against
        ``ResearchRunLifecycle``, and only an allowed transition is issued. The
        read-then-write runs inside the caller's unit of work, and the ``SELECT
        ... FOR UPDATE`` serialises concurrent transitions: with READ COMMITTED
        alone two readers would both see the old status and both write, so the
        row lock is what stops a concurrent transition from silently reopening a
        terminal status or double-applying ``queued -> running``. This method
        raises ``TransitionError`` (via ``ResearchRunLifecycle.validate``) before
        issuing any ``UPDATE``.

        ``status`` is coerced through ``ResearchRunStatus.coerce`` so that a caller
        cannot write an unrecognised value past the CHECK constraint by accident.

        What may be written alongside the status:

        * ``failure_summary`` — a non-revealing reason for a terminal failure.
        * ``final_answer`` — the run's verified answer (only on the transition into
          ``completed`` or ``partial``, and never on the way out of a terminal status).
        * ``prompt_tokens`` / ``completion_tokens`` — cumulative ledger values.

        The caller owns the commit boundary; this method does not commit.
        """
        from app.domain.errors import ResearchRunNotFoundError
        from app.domain.research.lifecycle import (
            ResearchRunLifecycle,
        )
        from app.domain.research.types import ResearchRunStatus

        new_status = ResearchRunStatus.coerce(status)

        # Read the current status inside the caller's transaction and lock the
        # row. Two concurrent transitions then serialise: the second waits for
        # the first to commit, re-reads the committed status under the lock, and
        # is refused (SAME_STATE / TERMINAL_STATE) instead of overwriting it.
        current = await self._session.scalar(
            select(ResearchRunModel)
            .where(ResearchRunModel.id == run_id)
            .with_for_update()
        )
        if current is None or current.user_id != user_id:
            raise ResearchRunNotFoundError(
                f"research run {run_id} does not belong to user {user_id}"
            )

        current_status = ResearchRunStatus.coerce(current.status)
        outcome = ResearchRunLifecycle.validate(current_status, new_status)
        if not outcome.is_accepted:
            assert outcome.error is not None
            raise outcome.error

        values: dict[str, object] = {
            "status": new_status.value,
            "updated_at": func.now(),
        }
        if failure_summary is not None:
            values["failure_summary"] = failure_summary
        if final_answer is not None:
            values["final_answer"] = final_answer
        if prompt_tokens is not None:
            values["prompt_tokens"] = prompt_tokens
        if completion_tokens is not None:
            values["completion_tokens"] = completion_tokens

        await self._session.execute(
            update(ResearchRunModel)
            .where(
                ResearchRunModel.id == run_id,
                ResearchRunModel.user_id == user_id,
            )
            .values(**values)
        )


class SourceRepositoryImpl:
    """SQLAlchemy implementation of the source port."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(
        self,
        run_id: int,
        url: str,
        domain: str,
        *,
        title: str | None = None,
        snippet: str | None = None,
    ) -> Source:
        model = SourceModel(run_id=run_id, url=url, domain=domain, title=title, snippet=snippet)
        self._session.add(model)
        await self._session.flush()
        return Source.model_validate(model)

    async def get(self, source_id: int) -> Source | None:
        model = await self._session.get(SourceModel, source_id)
        return Source.model_validate(model) if model else None

    async def get_for_url(self, run_id: int, url: str) -> Source | None:
        """The run's source for an exact ``url``, if it already has one (B7).

        The read half of source-level idempotency, and it exists because
        ``uq_sources_run_id_url`` alone is not enough. That constraint says a run
        cannot hold the same URL twice; it says nothing about *which* row a retry
        finds, and a re-executed pipeline must converge on the row the first
        attempt created rather than colliding with it and aborting the run.

        Scoped by ``run_id`` as well as ``url`` for the same reason ``set_summary``
        is: naming another run's source would be a cross-run read of someone else's
        research (SR-10).

        The match is on the **stored** URL, verbatim. ``canonical_url`` is a
        comparison key for P3.5's deduplication policy and is deliberately not a
        second identity (§ 16.1) — the caller that needs canonical matching
        resolves it before it gets here, which is also where the bound on how many
        candidates exist is applied.
        """
        result = await self._session.execute(
            select(SourceModel).where(
                SourceModel.run_id == run_id, SourceModel.url == url
            )
        )
        model = result.scalar_one_or_none()
        return Source.model_validate(model) if model else None

    async def claim_unanalysed(self, run_id: int, source_id: int) -> bool:
        """Claim the right to produce this source's evidence. Returns True once (B7).

        The atomic, database-backed idempotency guard for the evidence stage, and
        the reason a redelivered research task cannot write a second set of claims
        for one source.

        What it does, in the caller's transaction:

        * takes a ``SELECT ... FOR UPDATE`` row lock on the source, so two workers
          reaching the same source serialise — the second waits, re-reads, and sees
          the committed summary;
        * refuses a source that belongs to another run, which is the ownership
          check this repository's unscoped ``get`` does not provide (SR-10);
        * refuses a source whose ``summary`` is already set.

        **Why ``summary`` is the marker.** P3.7 writes the summary and every
        accepted claim and its citation in **one** transaction, so "this source has
        a summary" is exactly "this source's evidence has already been persisted"
        — including the case where the model returned no claim that verified, which
        is why the marker is not a claim count. It is a fact that already lives in
        the schema, so making idempotency durable costs no new column.

        The alternative — checking the flag and then writing — is the defect this
        replaces: under READ COMMITTED two readers both see ``NULL`` and both write.
        The lock is what closes that window, and the assertion that makes it real is
        that this method is called *inside* the same unit of work as the writes.

        Returns ``True`` when the caller may write this source's evidence and
        ``False`` when it already has been written. It never raises for a missing
        or foreign source beyond ``False``; the caller distinguishes "already
        done" from "not mine" by checking ownership before calling.
        """
        current = await self._session.scalar(
            select(SourceModel)
            .where(SourceModel.id == source_id)
            .with_for_update()
        )
        if current is None or current.run_id != run_id:
            return False
        return current.summary is None

    async def list_for_run(self, run_id: int) -> Sequence[Source]:
        result = await self._session.execute(
            select(SourceModel)
            .where(SourceModel.run_id == run_id)
            .order_by(SourceModel.rank.asc().nulls_last(), SourceModel.id.asc())
        )
        return [Source.model_validate(m) for m in result.scalars()]

    async def set_status(
        self, source_id: int, status: str, *, failure_reason: str | None = None
    ) -> None:
        values: dict[str, object] = {"status": status, "updated_at": func.now()}
        if failure_reason is not None:
            values["failure_reason"] = failure_reason
        await self._session.execute(
            update(SourceModel).where(SourceModel.id == source_id).values(**values)
        )

    async def set_summary(self, source_id: int, run_id: int, summary: str) -> None:
        """Persist a source's summary (FR-31).

        Scoped by ``run_id`` as well as ``source_id``, like
        ``ResearchRunRepositoryImpl.set_plan``: the summary is model output derived
        from fetched web content, so the write is scoped to the pair the caller has
        already established. A caller that named a source belonging to another run
        affects zero rows rather than overwriting what that run says its source
        says (SR-10).
        """
        await self._session.execute(
            update(SourceModel)
            .where(SourceModel.id == source_id, SourceModel.run_id == run_id)
            .values(summary=summary, updated_at=func.now())
        )

    async def set_ranking(
        self, source_id: int, run_id: int, ranking: SourceRanking
    ) -> int:
        """Persist one source's ranking metadata; see the port for the contract."""
        return await self.set_rankings(run_id, (ranking,))

    async def set_rankings(
        self, run_id: int, rankings: Sequence[SourceRanking]
    ) -> int:
        """Persist ranking metadata for one run's sources, atomically.

        Every column is written and nothing else is: the ``UPDATE`` names exactly
        the four ranking columns, so a source's ``title``, ``snippet``,
        ``status``, ``failure_reason``, ``summary``, and timestamps are untouched.
        A ranking stage that also changed what a source said about itself would be
        a rewrite of P3.2's or P3.7's data, not a ranking (FR-27 … FR-30).

        Ownership is checked before the first statement rather than discovered
        from a zero-row count. Two checks, both of them things the database
        cannot express:

        * every named ``source_id`` must be a source of ``run_id`` — otherwise a
          caller could silently rank another run's sources;
        * every ``duplicate_of_id`` must be a source of ``run_id`` — the
          self-referential foreign key is unconstrained by run, so without this
          check a source could be attached to a duplicate target in a different
          run (FR-26).

        Both raise :class:`~app.domain.errors.DuplicateTargetError` /
        :class:`~app.domain.errors.SourceRankingError` before anything is
        written, so a refused batch leaves no partial ranking behind. The
        statements themselves share the caller's unit of work, so a failure
        mid-batch rolls the whole batch back (the commit boundary is the caller's
        ``__aexit__``, exactly as for every other write here).

        Returns the number of rows written.
        """
        updates = tuple(rankings)
        if not updates:
            return 0

        source_ids = sorted({update_.source_id for update_ in updates})
        owned = set(
            await self._session.scalars(
                select(SourceModel.id).where(
                    SourceModel.run_id == run_id, SourceModel.id.in_(source_ids)
                )
            )
        )
        foreign = sorted(set(source_ids) - owned)
        if foreign:
            raise SourceRankingError(
                f"{len(foreign)} source(s) are not part of research run {run_id}"
            )

        targets = sorted(
            {u.duplicate_of_id for u in updates if u.duplicate_of_id is not None}
        )
        if targets:
            valid_targets = set(
                await self._session.scalars(
                    select(SourceModel.id).where(
                        SourceModel.run_id == run_id, SourceModel.id.in_(targets)
                    )
                )
            )
            missing = [target for target in targets if target not in valid_targets]
            if missing:
                raise DuplicateTargetError(
                    f"{len(missing)} duplicate target(s) are not sources of "
                    f"research run {run_id}"
                )

        written = 0
        for ranking in updates:
            result = await self._session.execute(
                update(SourceModel)
                .where(SourceModel.id == ranking.source_id, SourceModel.run_id == run_id)
                .values(
                    rank=ranking.rank,
                    quality_score=ranking.quality_score,
                    content_hash=ranking.content_hash,
                    duplicate_of_id=ranking.duplicate_of_id,
                    updated_at=func.now(),
                )
            )
            written += result.rowcount or 0
        return written


class SourceChunkRepositoryImpl:
    """SQLAlchemy implementation of the source-chunk port."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(
        self,
        source_id: int,
        ordinal: int,
        text: str,
        char_start: int,
        char_end: int,
    ) -> SourceChunk:
        model = SourceChunkModel(
            source_id=source_id,
            ordinal=ordinal,
            text=text,
            char_start=char_start,
            char_end=char_end,
        )
        self._session.add(model)
        await self._session.flush()
        return SourceChunk.model_validate(model)

    async def list_for_source(self, source_id: int) -> Sequence[SourceChunk]:
        result = await self._session.execute(
            select(SourceChunkModel)
            .where(SourceChunkModel.source_id == source_id)
            .order_by(SourceChunkModel.ordinal.asc(), SourceChunkModel.id.asc())
        )
        return [SourceChunk.model_validate(m) for m in result.scalars()]

    async def delete_for_source(self, source_id: int) -> int:
        """Delete every chunk of one source. Returns the row count.

        Issued as one ``DELETE`` scoped by ``source_id``, inside the caller's
        unit of work — so the replacement it is half of (delete, then re-insert)
        commits or rolls back as one operation and can never leave a mixture of
        stale and new passages behind.

        The obligation this method cannot discharge itself: ``citations.chunk_id``
        is ``ON DELETE CASCADE`` from ``source_chunks``, so the database removes
        every citation that cites a deleted passage. A caller that deletes
        without checking destroys verified evidence, which is why
        :class:`app.services.research.chunking.SourceChunkService` refuses the
        whole replacement when
        :meth:`CitationRepositoryImpl.count_for_source` is non-zero.
        """
        result = await self._session.execute(
            delete(SourceChunkModel).where(SourceChunkModel.source_id == source_id)
        )
        return result.rowcount or 0


class ClaimRepositoryImpl:
    """SQLAlchemy implementation of the claim port."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(
        self,
        run_id: int,
        text: str,
        *,
        status: str = "unsupported",
        confidence: float | None = None,
    ) -> Claim:
        model = ClaimModel(run_id=run_id, text=text, status=status, confidence=confidence)
        self._session.add(model)
        await self._session.flush()
        return Claim.model_validate(model)

    async def list_for_run(self, run_id: int) -> Sequence[Claim]:
        result = await self._session.execute(
            select(ClaimModel).where(ClaimModel.run_id == run_id).order_by(ClaimModel.id.asc())
        )
        return [Claim.model_validate(m) for m in result.scalars()]

    async def set_status(self, claim_id: int, status: str) -> None:
        await self._session.execute(
            update(ClaimModel).where(ClaimModel.id == claim_id).values(status=status)
        )


class CitationRepositoryImpl:
    """SQLAlchemy implementation of the citation port."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(
        self,
        claim_id: int,
        source_id: int,
        chunk_id: int,
        quote: str,
        *,
        location: str | None = None,
    ) -> Citation:
        """Link a claim to the passage that supports it (FR-32, FR-37, AD-033).

        ``quote`` is required and stored. It must already have been verified
        against ``source_chunks.text`` by
        ``app.domain.research.evidence.verify_claims``; the database can enforce
        that the cited passage exists and belongs to ``source_id`` (the composite
        foreign key added at P3.7) but it cannot compare one row's text against
        another's, so this repository is the last application-level gate before the
        link exists.

        Two non-``None`` checks are deliberate refusals rather than passing ``None``
        through: a citation row without a passage is the failure the trust chain
        exists to prevent, and it should not be creatable through this API.
        """
        if not isinstance(quote, str) or not quote.strip():
            raise ClaimEvidenceError(
                "a citation must carry the passage that supports it"
            )
        model = CitationModel(
            claim_id=claim_id,
            source_id=source_id,
            chunk_id=chunk_id,
            quote=quote,
            location=location,
        )
        self._session.add(model)
        await self._session.flush()
        return Citation.model_validate(model)

    async def list_for_claim(self, claim_id: int) -> Sequence[Citation]:
        result = await self._session.execute(
            select(CitationModel)
            .where(CitationModel.claim_id == claim_id)
            .order_by(CitationModel.id.asc())
        )
        return [Citation.model_validate(m) for m in result.scalars()]

    async def count_for_source(self, source_id: int) -> int:
        """How many citations cite a passage of this source.

        Exact by construction, not an approximation: the composite foreign key
        P3.7 added forces ``chunk.source_id == citation.source_id``, so every
        citation whose ``source_id`` is this source cites a passage that belongs
        to it. One count therefore answers the question the chunk producer asks
        before replacing anything — "is any of this evidence already cited?" —
        and ``0`` is the only value under which replacement is safe.
        """
        result = await self._session.execute(
            select(func.count())
            .select_from(CitationModel)
            .where(CitationModel.source_id == source_id)
        )
        return int(result.scalar_one())

    async def list_citable_for_run(self, run_id: int) -> Sequence[CitationEntry]:
        """The citation registry for one run (P3.8, § 15.7, AD-033).

        One join across ``citations → claims → source_chunks → sources``, scoped by
        ``claims.run_id``. That scope is the structural half of citation ownership:
        a citation belonging to another run is **not returned at all**, so no later
        check has to remember to exclude it (Rule 2).

        The join also re-establishes the evidence chain from single rows, so an entry
        cannot mix one source's metadata with another source's passage: the composite
        foreign key added at P3.7 (``fk_citations_chunk_id_source_id_source_chunks``)
        guarantees ``chunk.source_id == citation.source_id``, and this query reads
        both from the same join rather than reassembling them.

        **A citation whose quote is not inside its passage is not returned.** P3.7
        verified that before writing the row, and the containment rule is not
        expressible in SQL, so a row edited afterwards is filtered here — where the
        evidence becomes something a model may cite — rather than being offered and
        caught later. Excluding it also means a violation is invisible rather than
        loud, which is why the count is reported by the caller and asserted by tests
        against the rows directly.

        Ordered canonically by the same key the domain uses (``sources.rank``,
        ``sources.id``, ``source_chunks.ordinal``, ``source_chunks.id``,
        ``citations.id``) so the read order is already the synthesis context order and
        depends on no database row order, no ``NULLS LAST`` default, and no insertion
        sequence (§ 20).
        """
        from app.domain.research.evidence import normalize_evidence_text

        # Scoped by ``claims.run_id`` — the structural half of citation ownership.
        # A citation of another run is not returned at all, so no downstream check
        # has to remember to exclude it.
        # Every column is labelled explicitly. Two of them would otherwise collide —
        # `claims.text` and `source_chunks.text` both land on the label ``text`` —
        # and a colliding label silently hands back the wrong row's value, which for
        # the passage column would mean checking a quote against the *claim*.
        rows = await self._session.execute(
            select(
                CitationModel.id.label("citation_id"),
                CitationModel.claim_id.label("claim_id"),
                ClaimModel.text.label("claim_text"),
                CitationModel.source_id.label("citation_source_id"),
                CitationModel.chunk_id.label("chunk_id"),
                SourceChunkModel.ordinal.label("ordinal"),
                SourceChunkModel.source_id.label("chunk_source_id"),
                CitationModel.quote.label("quote"),
                SourceChunkModel.text.label("passage_text"),
                CitationModel.location.label("location"),
                SourceModel.url.label("source_url"),
                SourceModel.domain.label("source_domain"),
                SourceModel.title.label("source_title"),
                SourceModel.rank.label("source_rank"),
            )
            .join(ClaimModel, ClaimModel.id == CitationModel.claim_id)
            .join(SourceChunkModel, SourceChunkModel.id == CitationModel.chunk_id)
            .join(SourceModel, SourceModel.id == CitationModel.source_id)
            # The chain is **not** filtered here. ``chunk_source_id`` is selected
            # alongside ``citation_source_id`` and the mismatch is refused by
            # ``CitationRegistry``, so a broken link is a loud failure rather than a
            # silently absent citation — a run whose evidence is inconsistent must say
            # so, not quietly answer with less (SR-12).
            .where(ClaimModel.run_id == run_id)
            .order_by(
                SourceModel.rank.asc().nulls_last(),
                SourceModel.id.asc(),
                SourceChunkModel.ordinal.asc(),
                SourceChunkModel.id.asc(),
                CitationModel.id.asc(),
            )
        )

        entries: list[CitationEntry] = []
        for row in rows:
            quote = normalize_evidence_text(row.quote)
            if not quote or quote not in normalize_evidence_text(row.passage_text):
                # Not returned: a citation whose quote is not in its passage must
                # never reach a model. P3.7 verified this before writing the row, so
                # a row failing here is one that was edited afterwards.
                continue
            entries.append(
                CitationEntry(
                    citation_id=row.citation_id,
                    claim_id=row.claim_id,
                    claim_text=normalize_evidence_text(row.claim_text),
                    source_id=row.citation_source_id,
                    chunk_source_id=row.chunk_source_id,
                    chunk_id=row.chunk_id,
                    ordinal=row.ordinal,
                    quote=quote,
                    passage_text=row.passage_text,
                    location=row.location,
                    source_url=row.source_url,
                    source_domain=row.source_domain,
                    source_title=row.source_title,
                    source_rank=row.source_rank,
                )
            )
        return entries
