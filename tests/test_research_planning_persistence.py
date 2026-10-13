"""Plan persistence round-trip and the planning service (P3.6, FR-4).

Two halves:

* the **domain** round-trip (plan → payload → plan) against a real PostgreSQL
  JSONB column, so the representation is proven against the database rather than
  against a dict;
* the **service**, driven by an in-memory unit of work, proving the ordering
  properties: the run is settled before planning, exactly one planner call happens,
  and a failure writes nothing.

Ownership (SR-10) is exercised for the plan write, which is the one write path
that takes user content.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.domain.errors import (
    PlanningInputError,
    PlanValidationError,
    ResearchRunNotFoundError,
)
from app.domain.research.planning import (
    PLAN_SCHEMA_VERSION,
    ResearchPlan,
    ResearchStep,
    SourceType,
    StepKind,
)
from app.repositories.research import ResearchRunRepositoryImpl
from app.repositories.users import UserRepositoryImpl
from app.services.research.planning import PlanningService, load_plan

#: One shared literal per Persian string. Written twice by hand, the same Persian
#: word came out spelled two different ways, which is exactly the class of mistake
#: the constants remove.
PERSIAN_QUESTION = "کتابخانه‌های ناهمگام پایتون چیست؟"
PERSIAN_OBJECTIVE = "بررسی کتابخانه‌های ناهمگام پایتون"
PERSIAN_SUB_QUESTION = "کدام کتابخانه‌های ناهمگام وجود دارند؟"
PERSIAN_QUERY = "کتابخانه ناهمگام پایتون"
PERSIAN_RATIONALE = "شناسایی گزینه‌ها"


def sample_plan() -> ResearchPlan:
    return ResearchPlan(
        objective="Assess the current state of Python async HTTP clients",
        language="en",
        steps=(
            ResearchStep(
                id="landscape",
                question="Which async HTTP clients exist?",
                query="python async http client",
                expected_source_types=(SourceType.TECHNICAL, SourceType.OFFICIAL),
                rationale="Establish what exists",
            ),
            ResearchStep(
                id="compare",
                kind=StepKind.ANALYZE,
                question="How do the main options compare?",
                depends_on=("landscape",),
                rationale="Synthesise the landscape",
            ),
        ),
    )


def persian_plan() -> ResearchPlan:
    return ResearchPlan(
        objective=PERSIAN_OBJECTIVE,
        language="fa",
        steps=(
            ResearchStep(
                id="گام۱",
                question=PERSIAN_SUB_QUESTION,
                query=PERSIAN_QUERY,
                expected_source_types=(SourceType.NEWS,),
                rationale=PERSIAN_RATIONALE,
            ),
        ),
    )


# ---------------------------------------------------------------------------
# Database round-trip
# ---------------------------------------------------------------------------

class TestPlanRoundTripThroughTheDatabase:
    async def test_a_plan_is_stored_and_read_back_intact(self, db):
        user = await UserRepositoryImpl(db).get_or_create(6101, None)
        runs = ResearchRunRepositoryImpl(db)
        run = await runs.create(user.id, "Which async HTTP clients exist?")
        original = sample_plan()

        await runs.set_plan(run.id, user.id, original.to_payload())
        await db.commit()

        found = await runs.get_for_user(run.id, user.id)
        restored = ResearchPlan.from_payload(found.plan)
        assert restored == original

    async def test_every_field_survives_the_database(self, db):
        """No semantically meaningful field is silently dropped by the JSONB round-trip."""
        user = await UserRepositoryImpl(db).get_or_create(6102, None)
        runs = ResearchRunRepositoryImpl(db)
        run = await runs.create(user.id, "q")
        original = sample_plan()

        await runs.set_plan(run.id, user.id, original.to_payload())
        await db.commit()
        restored = ResearchPlan.from_payload(
            (await runs.get_for_user(run.id, user.id)).plan
        )

        assert restored.schema_version == PLAN_SCHEMA_VERSION
        assert restored.objective == original.objective
        assert restored.language == original.language
        assert len(restored.steps) == len(original.steps)
        for before, after in zip(original.steps, restored.steps, strict=True):
            assert after.id == before.id
            assert after.kind == before.kind
            assert after.question == before.question
            assert after.query == before.query
            assert after.expected_source_types == before.expected_source_types
            assert after.depends_on == before.depends_on
            assert after.rationale == before.rationale

    async def test_derived_properties_survive_the_database(self, db):
        user = await UserRepositoryImpl(db).get_or_create(6103, None)
        runs = ResearchRunRepositoryImpl(db)
        run = await runs.create(user.id, "q")
        await runs.set_plan(run.id, user.id, sample_plan().to_payload())
        await db.commit()

        restored = ResearchPlan.from_payload(
            (await runs.get_for_user(run.id, user.id)).plan
        )
        assert restored.sub_questions == sample_plan().sub_questions
        assert restored.search_queries == sample_plan().search_queries
        assert restored.dependency_depth == sample_plan().dependency_depth
        assert restored.execution_order() == sample_plan().execution_order()

    async def test_a_persian_plan_survives_the_database_as_persian(self, db):
        """Persian must come back as Persian, not as escaped sequences."""
        user = await UserRepositoryImpl(db).get_or_create(6104, None)
        runs = ResearchRunRepositoryImpl(db)
        run = await runs.create(user.id, "کتابخانه‌های ناهمگام پایتون چیست؟")
        original = persian_plan()

        await runs.set_plan(run.id, user.id, original.to_payload())
        await db.commit()

        found = await runs.get_for_user(run.id, user.id)
        assert "قیمت" not in found.plan["objective"]
        restored = ResearchPlan.from_payload(found.plan)
        assert restored == original
        assert restored.language == "fa"

    async def test_the_stored_plan_is_valid_json_in_the_database(self, db):
        user = await UserRepositoryImpl(db).get_or_create(6105, None)
        runs = ResearchRunRepositoryImpl(db)
        run = await runs.create(user.id, "q")
        await runs.set_plan(run.id, user.id, sample_plan().to_payload())
        await db.commit()

        raw = (
            await db.execute(
                text("SELECT plan FROM research_runs WHERE id = :id"), {"id": run.id}
            )
        ).scalar_one()
        assert raw["schema_version"] == PLAN_SCHEMA_VERSION
        assert isinstance(raw["steps"], list)

    async def test_a_run_with_no_plan_is_read_as_none(self, db):
        """A run between intake and planning is a normal intermediate state."""
        user = await UserRepositoryImpl(db).get_or_create(6106, None)
        runs = ResearchRunRepositoryImpl(db)
        run = await runs.create(user.id, "q")
        await db.commit()
        assert await load_plan(run.id, user.id, _uow(db)) is None

    async def test_a_corrupt_stored_plan_is_rejected_on_read(self, db):
        """A hand-edited or truncated row fails on read, not two stages later."""
        user = await UserRepositoryImpl(db).get_or_create(6107, None)
        runs = ResearchRunRepositoryImpl(db)
        run = await runs.create(user.id, "q")
        await runs.set_plan(run.id, user.id, {"schema_version": PLAN_SCHEMA_VERSION})
        await db.commit()

        with pytest.raises(PlanValidationError):
            await load_plan(run.id, user.id, _uow(db))

    async def test_a_stored_plan_with_a_cycle_is_rejected_on_read(self, db):
        user = await UserRepositoryImpl(db).get_or_create(6108, None)
        runs = ResearchRunRepositoryImpl(db)
        run = await runs.create(user.id, "q")
        payload = sample_plan().to_payload()
        payload["steps"][0]["depends_on"] = ["compare"]
        await runs.set_plan(run.id, user.id, payload)
        await db.commit()

        with pytest.raises(PlanValidationError):
            await load_plan(run.id, user.id, _uow(db))

    async def test_a_plan_cannot_be_written_to_another_users_run(self, db):
        """SR-10: the plan write is ownership-scoped like every read."""
        owner = await UserRepositoryImpl(db).get_or_create(6109, None)
        intruder = await UserRepositoryImpl(db).get_or_create(6110, None)
        runs = ResearchRunRepositoryImpl(db)
        run = await runs.create(owner.id, "q")

        await runs.set_plan(run.id, intruder.id, sample_plan().to_payload())
        await db.commit()

        found = await runs.get_for_user(run.id, owner.id)
        assert found.plan is None

    async def test_planning_a_run_you_do_not_own_is_not_found(self, db):
        owner = await UserRepositoryImpl(db).get_or_create(6111, None)
        intruder = await UserRepositoryImpl(db).get_or_create(6112, None)
        runs = ResearchRunRepositoryImpl(db)
        run = await runs.create(owner.id, "q")
        await db.commit()

        with pytest.raises(ResearchRunNotFoundError):
            await load_plan(run.id, intruder.id, _uow(db))

    async def test_existing_run_behaviour_is_unchanged(self, db):
        """Setting a plan must not disturb the status, question, or timestamps."""
        user = await UserRepositoryImpl(db).get_or_create(6113, None)
        runs = ResearchRunRepositoryImpl(db)
        run = await runs.create(user.id, "the original question")
        await db.commit()
        before = await runs.get_for_user(run.id, user.id)

        await runs.set_plan(run.id, user.id, sample_plan().to_payload())
        await db.commit()
        after = await runs.get_for_user(run.id, user.id)

        assert after.question == before.question
        assert after.status == before.status
        assert after.final_answer == before.final_answer
        assert after.created_at == before.created_at


def _uow(db):
    """A minimal unit-of-work factory over an existing session.

    Commits are the caller's business: these tests assert what was stored, so the
    session is reused and the commit is explicit.
    """

    class _UoW:
        def __init__(self, session) -> None:
            from app.repositories.research import (
                CitationRepositoryImpl,
                ClaimRepositoryImpl,
                SourceChunkRepositoryImpl,
                SourceRepositoryImpl,
            )

            self.research_runs = ResearchRunRepositoryImpl(session)
            self.sources = SourceRepositoryImpl(session)
            self.source_chunks = SourceChunkRepositoryImpl(session)
            self.claims = ClaimRepositoryImpl(session)
            self.citations = CitationRepositoryImpl(session)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc_info) -> None:
            return None

    return lambda: _UoW(db)


# ---------------------------------------------------------------------------
# The service
# ---------------------------------------------------------------------------

class RecordingPlanner:
    """A fake ``PlanningProvider`` that records calls and returns a fixed plan.

    The fixed plan is held in ``plan_to_return`` rather than ``plan`` so it cannot
    shadow the ``plan`` method the service calls.
    """

    def __init__(
        self, plan_to_return: ResearchPlan | None = None, error: Exception | None = None
    ) -> None:
        self.plan_to_return = plan_to_return or sample_plan()
        self.error = error
        self.calls: list[str] = []

    async def plan(self, question: str) -> ResearchPlan:
        self.calls.append(question)
        if self.error is not None:
            raise self.error
        return self.plan_to_return


class TestPlanningService:
    async def test_plan_for_run_stores_the_plan_on_the_run(self, db, make_settings):
        user = await UserRepositoryImpl(db).get_or_create(6201, None)
        runs = ResearchRunRepositoryImpl(db)
        run = await runs.create(user.id, "Which async HTTP clients exist?")
        await db.commit()

        planner = RecordingPlanner()
        service = PlanningService(
            settings=make_settings(), planner=planner, uow_factory=_uow(db)
        )
        produced = await service.plan_for_run(run.id, user.id)
        await db.commit()

        assert produced == sample_plan()
        assert planner.calls == ["Which async HTTP clients exist?"]
        stored = ResearchPlan.from_payload((await runs.get_for_user(run.id, user.id)).plan)
        assert stored == produced

    async def test_the_plan_is_derived_from_the_persisted_question(self, db, make_settings):
        """The plan is derived from *the stored question*, not a second copy of it."""
        user = await UserRepositoryImpl(db).get_or_create(6202, None)
        runs = ResearchRunRepositoryImpl(db)
        run = await runs.create(user.id, PERSIAN_QUESTION)
        await db.commit()

        planner = RecordingPlanner()
        service = PlanningService(
            settings=make_settings(), planner=planner, uow_factory=_uow(db)
        )
        await service.plan_for_run(run.id, user.id)
        assert planner.calls == [PERSIAN_QUESTION]

    async def test_exactly_one_planner_call_per_run(self, db, make_settings):
        user = await UserRepositoryImpl(db).get_or_create(6203, None)
        runs = ResearchRunRepositoryImpl(db)
        run = await runs.create(user.id, "q")
        await db.commit()

        planner = RecordingPlanner()
        service = PlanningService(
            settings=make_settings(), planner=planner, uow_factory=_uow(db)
        )
        await service.plan_for_run(run.id, user.id)
        assert len(planner.calls) == 1

    async def test_a_planner_failure_writes_no_plan(self, db, make_settings):
        """A run must never carry a partial or invented plan."""
        user = await UserRepositoryImpl(db).get_or_create(6204, None)
        runs = ResearchRunRepositoryImpl(db)
        run = await runs.create(user.id, "q")
        await db.commit()

        planner = RecordingPlanner(error=PlanningInputError("empty question"))
        service = PlanningService(
            settings=make_settings(), planner=planner, uow_factory=_uow(db)
        )
        with pytest.raises(PlanningInputError):
            await service.plan_for_run(run.id, user.id)

        assert (await runs.get_for_user(run.id, user.id)).plan is None

    async def test_planning_another_users_run_is_refused_before_the_model_call(self, db, make_settings):
        owner = await UserRepositoryImpl(db).get_or_create(6205, None)
        intruder = await UserRepositoryImpl(db).get_or_create(6206, None)
        runs = ResearchRunRepositoryImpl(db)
        run = await runs.create(owner.id, "q")
        await db.commit()

        planner = RecordingPlanner()
        service = PlanningService(
            settings=make_settings(), planner=planner, uow_factory=_uow(db)
        )
        with pytest.raises(ResearchRunNotFoundError):
            await service.plan_for_run(run.id, intruder.id)
        assert planner.calls == []

    async def test_plan_and_store_creates_the_run_and_plans_it(self, db, make_settings):
        user = await UserRepositoryImpl(db).get_or_create(6207, None)
        planner = RecordingPlanner()
        service = PlanningService(
            settings=make_settings(), planner=planner, uow_factory=_uow(db)
        )
        run, plan = await service.plan_and_store(user.id, "Which clients exist?")
        await db.commit()

        assert run.question == "Which clients exist?"
        assert plan == sample_plan()
        stored = ResearchPlan.from_payload(
            (await ResearchRunRepositoryImpl(db).get_for_user(run.id, user.id)).plan
        )
        assert stored == plan

    async def test_the_returned_plan_is_the_stored_plan(self, db, make_settings):
        """One call, one plan: the caller cannot be handed a different object."""
        user = await UserRepositoryImpl(db).get_or_create(6208, None)
        planner = RecordingPlanner()
        service = PlanningService(
            settings=make_settings(), planner=planner, uow_factory=_uow(db)
        )
        run, returned = await service.plan_and_store(user.id, "q")
        await db.commit()

        stored = ResearchPlan.from_payload(
            (await ResearchRunRepositoryImpl(db).get_for_user(run.id, user.id)).plan
        )
        assert returned == stored
        assert len(planner.calls) == 1

    async def test_the_run_is_created_before_planning_is_attempted(self, db, make_settings):
        """A provider outage must not lose the question the user actually asked."""
        user = await UserRepositoryImpl(db).get_or_create(6209, None)
        planner = RecordingPlanner(error=PlanningInputError("nope"))
        service = PlanningService(
            settings=make_settings(), planner=planner, uow_factory=_uow(db)
        )
        with pytest.raises(PlanningInputError):
            await service.plan_and_store(user.id, "the question that must survive")
        await db.commit()

        runs = await ResearchRunRepositoryImpl(db).list_for_user(user.id)
        assert [r.question for r in runs] == ["the question that must survive"]
        assert runs[0].plan is None