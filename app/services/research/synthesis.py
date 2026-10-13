"""Cross-source synthesis service (P3.8 — FR-33 … FR-39).

The application-layer step between a run's persisted verified claims and the run's
persisted, cited answer. It owns the ordering of the operations and nothing else:

```text
run + its verified citations (persisted)
    → CitationRegistry                      (one join, scoped by claims.run_id)
    → plan_synthesis_context                (bounded, deterministic, ranked)
    → SynthesisProvider.synthesize()        → SynthesisProposal   [untrusted]
    → verify_synthesis()                    → SynthesizedAnswer   [trusted]
    → one transaction: synthesis payload + final answer + completed status
```

**Verification happens here, not behind the port.** ``SynthesisProvider`` returns
proposals because :func:`app.domain.research.synthesis.verify_synthesis` is the only
way to produce a ``SynthesizedAnswer``. Putting verification inside an adapter would
mean the ability to accept a citation lived in a replaceable component — and the
adapter is the part of this stage closest to untrusted text, holding the model's raw
output. The service owns the ordering, the domain owns the rule, and no model is ever
asked to check another model (AD-033, § 15.7).

**The registry is read, never computed.** This service does not decide which
citations exist; it asks the database for the ones that belong to this run and hands
the result to the verifier. A fabricated identifier is caught because it is absent
from that read, not because this service compares it against anything.

**Persistence is one transaction, after verification.** The synthesis payload, the
plain-text answer, and the terminal status are written together, so there is no state
in which a run reads as ``completed`` without a validated answer behind it (§ 17). If
verification fails, **nothing is written at all**: the run keeps whatever status it
had, no answer is stored, and the typed error propagates. A fabricated citation must
fail closed, and a rolled-back transaction is how that is made structural rather than
conventional.

**The service holds no session across a model call.** The read transaction closes
before the provider is called, so a slow or failing provider cannot hold a connection
or an open read snapshot.

**What this service does not do.** It does not search, fetch, extract, rank, extract
claims, render, deliver, or schedule. It is handed a run whose evidence already
exists, produces a cited answer, and stops. Orchestration is P3.9; Telegram delivery
and length-bounded citation rendering are later stages (§ 3).

**One provider call per run.** Deliberate, and it is what makes cross-source synthesis
possible at all: P3.7's extractor was given one source per call so it *could not*
compare two, and windowing synthesis across calls would produce several
partial answers with no authority to merge them. What is bounded instead is the
**evidence**: the highest-ranked items are offered first, what does not fit is
counted and reported, and a citation to an item that was not shown is refused rather
than accepted (§ 21, FR-42).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict

from app.core.config import Settings
from app.domain.errors import (
    ResearchRunNotFoundError,
    SynthesisInputError,
)
from app.domain.research.evidence import TokenCounter
from app.domain.research.planning import ResearchPlan
from app.domain.research.ports import SynthesisProvider
from app.domain.research.synthesis import (
    CitationRegistry,
    SynthesisInput,
    SynthesizedAnswer,
    plan_synthesis_context,
    verify_synthesis,
)
from app.services.context import CharRatioTokenEstimator

if TYPE_CHECKING:  # pragma: no cover
    from app.services.research.usage import RunTokenLedger

log = logging.getLogger(__name__)


class SynthesisService:
    """Synthesize a run's verified evidence into a cited, persisted answer."""

    def __init__(
        self,
        *,
        settings: Settings,
        synthesizer: SynthesisProvider,
        uow_factory: Callable[[], Any],
        counter: TokenCounter | None = None,
    ) -> None:
        self._settings = settings
        self._synthesizer = synthesizer
        self._uow_factory = uow_factory
        # Phase 2's estimator, reused rather than reimplemented: the context planner
        # needs an estimate and this project already has one, with the same
        # ``LLM_CHARS_PER_TOKEN`` configuration behind it.
        self._counter = counter or CharRatioTokenEstimator(settings.LLM_CHARS_PER_TOKEN)

    async def synthesize_run(
        self,
        *,
        run_id: int,
        user_id: int,
        terminal_status: str = "completed",
        tokens: RunTokenLedger | None = None,
    ) -> SynthesisOutcome:
        """Produce and persist this run's cited answer (FR-35 … FR-39).

        ``terminal_status`` is the status the terminal write uses, and it is the
        caller's decision, not this stage's (AD-044, § R.17). P3.8 originally wrote
        ``completed`` unconditionally and said so: it "cannot know whether some
        sources failed earlier in the run — it sees citations, not fetch outcomes"
        (§ AD-042). The orchestrator is the only component that knows, so the value
        is injected and defaults to ``completed`` so that no existing caller changes
        behaviour.

        It defaults to ``completed`` rather than being required for exactly that
        reason, and it is **not** a free-form status: ``queued`` and ``running`` are
        refused, because writing them here would move a terminal run back into the
        lifecycle and is precisely what ``ResearchRunLifecycle`` exists to prevent.

        ``tokens`` is the run's accumulated provider-reported usage (FR-9, SR-5),
        written onto the run in the same transaction as the answer and the status.
        It is optional so nothing changes for a caller that has no ledger, and the
        values are **set** rather than incremented, so a repeated terminal write
        converges on the same total instead of accumulating it. Nothing is estimated
        here and no budget is enforced: enforcement stays deferred (AD-047).

        Raises, rather than returning an empty result:

        * :class:`ResearchRunNotFoundError` — the run is not this user's;
        * :class:`SynthesisInputError` — the run has **no persisted citation that
          verifies**, so **no model is called** and nothing is written. FR-41 in
          synthesis form: a run with no usable evidence produces a clear failure,
          never an invented answer;
        * the ``SynthesisError`` family — the model answered with something that is
          not the requested structure, or **cited something that does not resolve**;
        * the ``CitationIntegrityError`` family — a proposed reference failed to
          resolve to evidence of this run. The whole answer is refused (§ 17);
        * the existing ``LLM*Error`` family — provider failure, un-retried here and
          re-typed by nothing, so the task policy keeps reading ``retryable``.

        No partial success: a proposal that cites one fabricated reference produces
        no answer, not an answer with that reference removed. That is the difference
        between this stage and § 15.7's "strip and mark", and the choice is recorded
        in the audit — a fabricated citation is an attack signal, and delivering the
        rest would present a proposal that tried to deceive as though it were
        verified.
        """
        registry, question, objective, language = await self._load(run_id, user_id)

        synthesis_input = self._build_input(
            run_id=run_id,
            registry=registry,
            question=question,
            objective=objective,
            language=language,
        )

        proposal = await self._synthesizer.synthesize(synthesis_input)
        answer = verify_synthesis(proposal, synthesis_input)

        await self._persist(
            run_id,
            user_id,
            answer,
            terminal_status=terminal_status,
            prompt_tokens=tokens.prompt_tokens if tokens is not None else None,
            completion_tokens=tokens.completion_tokens if tokens is not None else None,
        )

        outcome = SynthesisOutcome(
            answer=answer,
            evidence_offered=answer.evidence_offered,
            evidence_available=answer.evidence_available,
            evidence_dropped=synthesis_input.context.dropped,
            source_count=answer.source_count,
            citation_count=answer.citation_count,
            contradiction_count=len(answer.contradictions),
        )
        log.info(
            "synthesis stored: run=%d sections=%d citations=%d sources=%d "
            "contradictions=%d evidence=%d/%d truncated=%s",
            run_id,
            len(answer.sections),
            outcome.citation_count,
            outcome.source_count,
            outcome.contradiction_count,
            outcome.evidence_offered,
            outcome.evidence_available,
            outcome.evidence_dropped > 0,
        )
        return outcome

    # --- internals -----------------------------------------------------------

    async def _load(
        self, run_id: int, user_id: int
    ) -> tuple[CitationRegistry, str, str | None, str | None]:
        """Read the run, its plan, and its citable evidence, in one short transaction.

        The transaction closes before any provider call. The plan is read back
        through ``ResearchPlan.from_payload`` — re-validated, not trusted — so an
        objective that cannot be read is a typed failure rather than text spliced
        into a prompt; the *question* comes from the run row, which is the only copy
        that is definitely the user's own question (FR-2).
        """
        async with self._uow_factory() as uow:
            run = await uow.research_runs.get_for_user(run_id, user_id)
            if run is None:
                raise ResearchRunNotFoundError(
                    f"research run {run_id} does not exist for this user"
                )
            entries = await uow.citations.list_citable_for_run(run_id)

        objective: str | None = None
        language: str | None = None
        if run.plan:
            plan = ResearchPlan.from_payload(run.plan)
            objective = plan.objective
            language = plan.language

        if not entries:
            # No verified evidence ⇒ no answer. Refused before the provider is called
            # so a run that retrieved nothing cannot spend money or invent one
            # (FR-41, SR-12).
            raise SynthesisInputError(
                "this research run has no verified evidence to synthesize; nothing "
                "was retrieved that could be cited"
            )

        registry = CitationRegistry(run_id=run_id, entries=tuple(entries))
        return registry, run.question, objective, language

    def _build_input(
        self,
        *,
        run_id: int,
        registry: CitationRegistry,
        question: str,
        objective: str | None,
        language: str | None,
    ) -> SynthesisInput:
        """Bound what the call may be shown, deterministically (FR-42, § 21).

        The context is the highest-ranked prefix of the registry's canonical order, so
        a run with more evidence than the budget can hold offers its *best* evidence
        rather than an arbitrary subset, and the same run always produces the same
        context.
        """
        context = plan_synthesis_context(
            registry,
            max_tokens=self._settings.SYNTHESIS_MAX_INPUT_TOKENS,
            counter=self._counter,
            max_items=self._settings.SYNTHESIS_MAX_EVIDENCE,
        )
        return SynthesisInput(
            run_id=run_id,
            question=question,
            objective=objective,
            language=language,
            registry=registry,
            context=context,
        )

    async def _persist(
        self,
        run_id: int,
        user_id: int,
        answer: SynthesizedAnswer,
        *,
        terminal_status: str = "completed",
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
    ) -> None:
        """Write the answer and mark the run successful, atomically.

        One unit of work, so the whole result commits or none of it does. The
        synthesis payload is written **before** the status, and both in the same
        transaction, so a reader can never observe a terminal status without a
        validated answer behind it (§ 17).

        The status is set only here, after verification, which is the whole point: a
        run whose citations did not verify keeps whatever status it had and has no
        ``final_answer``, so there is no state in which a run looks finished while its
        citations are missing or invalid.

        ``terminal_status`` is the status the caller decided on and defaults to
        ``completed``. P3.8 originally wrote ``completed`` unconditionally, because
        this service "cannot know whether some sources failed earlier in the run — it
        sees citations, not fetch outcomes", and deciding ``partial`` here would mean
        guessing from a stage that was not executed here (AD-042). The orchestrator is
        the only component that knows, so the decision is injected: it passes
        ``partial`` when it recorded degradation, and ``completed`` when it did not.

        It is rejected for anything other than a terminal status. ``queued`` and
        ``running`` are refused here — and again by
        ``ResearchRunLifecycle.validate`` inside ``set_status`` — because a stage that
        synthesizes a verified answer must not be able to move a run back out of a
        terminal state.
        """
        if terminal_status not in {"completed", "partial"}:
            raise SynthesisInputError(
                "the synthesis stage may only write a terminal status it was given"
            )

        async with self._uow_factory() as uow:
            await uow.research_runs.set_synthesis(run_id, user_id, answer.to_payload())
            await uow.research_runs.set_status(
                run_id,
                user_id,
                terminal_status,
                final_answer=answer.plain_text,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            )


async def load_synthesis(
    run_id: int, user_id: int, uow_factory: Callable[[], Any]
) -> SynthesizedAnswer | None:
    """Read a run's stored answer back, ownership-scoped and **re-verified**.

    Reconstruction goes through ``SynthesizedAnswer.from_payload`` with the run's
    citation registry, so a stored answer is *checked* rather than trusted: a
    hand-edited row, a row from an older build, or a row whose citation no longer
    matches its evidence fails here with a typed error instead of surfacing later as
    a trustworthy answer (SR-9, § 16).

    Returns ``None`` when the run has no synthesis yet — the state a run is in
    between evidence extraction and this stage — which is a normal intermediate state,
    not an error.
    """
    async with uow_factory() as uow:
        run = await uow.research_runs.get_for_user(run_id, user_id)
        if run is None:
            raise ResearchRunNotFoundError(
                f"research run {run_id} does not exist for this user"
            )
        if not run.synthesis:
            return None
        payload = dict(run.synthesis)
        entries = await uow.citations.list_citable_for_run(run_id)

    registry = CitationRegistry(run_id=run_id, entries=tuple(entries))
    return SynthesizedAnswer.from_payload(payload, registry=registry)


class SynthesisOutcome(BaseModel):
    """What one synthesis produced, for the caller that owns the run.

    A **report**, not just the answer. The counts exist so a later stage can state
    coverage honestly (FR-42, SR-12) without inferring it: ``evidence_dropped > 0``
    means the answer was synthesized from a truncated context, and a caller that
    ignores that field can present partial coverage as complete — which is why it is
    a field rather than a log line nobody reads.

    ``answer`` is the trusted, verified result. There is deliberately no field for the
    model's raw proposal: it is not persisted and not returned, because an unvalidated
    citation must not be reachable as research output by any route (§ 16).
    """

    model_config = ConfigDict(frozen=True)

    answer: SynthesizedAnswer

    evidence_offered: int = 0
    """Evidence items the synthesis call was shown."""

    evidence_available: int = 0
    """Citable evidence this run held."""

    evidence_dropped: int = 0
    """Evidence that did not fit the input budget. Counted, never silently dropped."""

    source_count: int = 0
    """Distinct sources that contributed evidence (FR-33)."""

    citation_count: int = 0
    """Distinct citations the answer relies on."""

    contradiction_count: int = 0
    """Reported conflicts, each keeping both sides (FR-34)."""

    @property
    def truncated(self) -> bool:
        """Whether the answer was synthesized from a truncated evidence context."""
        return self.evidence_dropped > 0


__all__ = ["SynthesisOutcome", "SynthesisService", "load_synthesis"]