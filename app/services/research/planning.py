"""Research planning service (Phase 3, P3.6 — FR-1 … FR-4).

The application-layer step between a user's question and the persisted run. It
owns the ordering of the three operations and nothing else: ask the planner for a
plan, persist it, and read one back. No model knowledge, no validation rules, and
no SQL live here.

```text
question
  -> PlanningProvider.plan()      (infrastructure; the only LLM call)
  -> ResearchPlan                 (validated by the domain, not by this service)
  -> ResearchRunRepository.set_plan()
  -> committed run with a plan
```

**This service does not execute the plan.** There is no search, fetch, extraction,
ranking, claim, or citation step here, and no Celery task is enqueued. Planning is
one bounded call that produces one plan and writes it once. The stages that consume
the plan are P3.9's orchestrator, which does not exist yet, so nothing in this
module has a caller that would execute it — which is why the module's only consumer
today is the test suite and the next stage.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from app.core.config import Settings
from app.domain.errors import ResearchRunNotFoundError
from app.domain.research.planning import ResearchPlan
from app.domain.research.ports import PlanningProvider
from app.domain.research.types import ResearchRun

log = logging.getLogger(__name__)


class PlanningService:
    """Produce a research plan for a question and persist it on the run."""

    def __init__(
        self,
        *,
        settings: Settings,
        planner: PlanningProvider,
        uow_factory: Callable[[], Any],
    ) -> None:
        self._settings = settings
        self._planner = planner
        self._uow_factory = uow_factory

    async def plan_for_run(self, run_id: int, user_id: int) -> ResearchPlan:
        """Plan for an existing run and store the plan on it (FR-1, FR-4).

        The run is read through ``get_for_user``, so ownership is settled before
        anything is written and a caller cannot plan against another user's run
        (SR-10, T-23). The question comes from the run row rather than from a
        parameter: the plan must be derived from *the question that was persisted*
        (FR-2), and accepting a second copy of it would allow the two to diverge.

        The plan is written in its own transaction, after the planner returns. A
        provider failure therefore leaves the run exactly as it was — queued, with
        no plan — rather than half-populated, and the failure surfaces as a typed
        error the caller reports (FR-8, FR-12). Nothing is written on failure: a run
        carrying a partial or invented plan would misrepresent what was investigated
        (FR-4).
        """
        async with self._uow_factory() as uow:
            run = await uow.research_runs.get_for_user(run_id, user_id)
            if run is None:
                raise ResearchRunNotFoundError(
                    f"research run {run_id} does not exist for this user"
                )
            question = run.question

        plan = await self._planner.plan(question)

        async with self._uow_factory() as uow:
            await uow.research_runs.set_plan(run_id, user_id, plan.to_payload())

        log.info(
            "stored research plan: run=%d steps=%d", run_id, len(plan.steps)
        )
        return plan

    async def plan_and_store(
        self, user_id: int, question: str
    ) -> tuple[ResearchRun, ResearchPlan]:
        """Create the run for ``question``, plan it, and return both (FR-1, FR-4).

        The convenience path for a caller that has a question but no run yet.

        The run is created and committed **before** the planner is called, for the
        same reason Phase 2's chat path commits the user's message first: a provider
        outage must not lose what the user actually asked. The cost is a legitimate
        intermediate state — a run with no plan — and it is the state the
        failure model already describes as "planning failed", so it is readable
        rather than corrupt.

        **One planner call per run.** The plan is produced once and then written,
        so the object returned to the caller is the same plan that was persisted
        and a caller can never be handed one that differs from what was stored.

        When planning fails, the run is left in place and the failure propagates as a
        typed error. It is not marked ``failed`` here: whether a run that never
        produced a plan is terminal or retryable is the orchestrator's judgement
        (P3.9, AD-035), not this service's, and this stage does not own the run
        state machine.
        """
        async with self._uow_factory() as uow:
            run = await uow.research_runs.create(user_id, question)

        if run.id is None:  # pragma: no cover - create() flushes and assigns an id
            raise ResearchRunNotFoundError("created research run has no id")

        plan = await self._planner.plan(question)

        async with self._uow_factory() as uow:
            await uow.research_runs.set_plan(run.id, user_id, plan.to_payload())

        log.info(
            "planned and stored run=%d steps=%d provider=%s",
            run.id,
            len(plan.steps),
            self._settings.LLM_PROVIDER,
        )
        return run, plan


async def load_plan(
    run_id: int, user_id: int, uow_factory: Callable[[], Any]
) -> ResearchPlan | None:
    """Read a run's persisted plan back as a validated ``ResearchPlan`` (FR-4).

    Reconstruction goes through :meth:`ResearchPlan.from_payload`, so a plan read
    from storage is **re-validated** rather than trusted: a hand-edited row, a plan
    written by an older build, or a truncated write fails here with a typed error
    instead of surfacing two stages later as a step that references nothing.

    Returns ``None`` when the run has no plan yet — the state a run is in between
    intake and planning — which is a normal intermediate state, not an error.
    """
    async with uow_factory() as uow:
        run = await uow.research_runs.get_for_user(run_id, user_id)
    if run is None:
        raise ResearchRunNotFoundError(
            f"research run {run_id} does not exist for this user"
        )
    if not run.plan:
        return None
    return ResearchPlan.from_payload(run.plan)