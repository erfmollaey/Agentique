"""Lifecycle tests: ResearchRun status transitions (P3.9-B, B4).

Verifies the lifecycle policy, ownership-scoped set_status, and concurrency
behaviour against the real PostgreSQL test database.

These tests use the ``db`` fixture (real PostgreSQL, per-test rollback) and
the ``clean_database`` fixture (commits allowed).
"""

from __future__ import annotations

import asyncio

import pytest

from app.db.models import ResearchRunModel
from app.domain.errors import ResearchRunNotFoundError
from app.domain.research.lifecycle import (
    ResearchRunLifecycle,
    TransitionError,
    TransitionErrorKind,
)
from app.domain.research.types import ResearchRunStatus
from app.repositories.research import ResearchRunRepositoryImpl
from app.repositories.users import UserRepositoryImpl
from tests.conftest import run_on_process_loop

# --- helpers -------------------------------------------------------------------------------


async def _seed_user_and_run(db, user_id=5101, question="q"):
    """Create a user and a run owned by that user, return the run."""
    user = await UserRepositoryImpl(db).get_or_create(user_id, None)
    repo = ResearchRunRepositoryImpl(db)
    return await repo.create(user.id, question)


# --- lifecycle policy (pure, no database) -------------------------------------------------


class TestResearchRunLifecyclePolicy:
    """Pure-policy tests: allowed and disallowed transitions."""

    def test_queued_to_running_is_allowed(self) -> None:
        result = ResearchRunLifecycle.validate(
            ResearchRunStatus.QUEUED, ResearchRunStatus.RUNNING
        )
        assert result.is_accepted

    def test_queued_to_failed_is_allowed(self) -> None:
        result = ResearchRunLifecycle.validate(
            ResearchRunStatus.QUEUED, ResearchRunStatus.FAILED
        )
        assert result.is_accepted

    def test_running_to_completed_is_allowed(self) -> None:
        result = ResearchRunLifecycle.validate(
            ResearchRunStatus.RUNNING, ResearchRunStatus.COMPLETED
        )
        assert result.is_accepted

    def test_running_to_partial_is_allowed(self) -> None:
        result = ResearchRunLifecycle.validate(
            ResearchRunStatus.RUNNING, ResearchRunStatus.PARTIAL
        )
        assert result.is_accepted

    def test_running_to_failed_is_allowed(self) -> None:
        result = ResearchRunLifecycle.validate(
            ResearchRunStatus.RUNNING, ResearchRunStatus.FAILED
        )
        assert result.is_accepted

    def test_completed_cannot_be_reopened(self) -> None:
        for target in (ResearchRunStatus.RUNNING, ResearchRunStatus.QUEUED):
            result = ResearchRunLifecycle.validate(
                ResearchRunStatus.COMPLETED, target
            )
            assert result.is_rejected
            assert result.error is not None
            assert result.error.kind is TransitionErrorKind.TERMINAL_STATE

    def test_partial_cannot_be_reopened(self) -> None:
        for target in (ResearchRunStatus.RUNNING, ResearchRunStatus.QUEUED):
            result = ResearchRunLifecycle.validate(
                ResearchRunStatus.PARTIAL, target
            )
            assert result.is_rejected
            assert result.error is not None
            assert result.error.kind is TransitionErrorKind.TERMINAL_STATE

    def test_failed_cannot_be_reopened(self) -> None:
        for target in (ResearchRunStatus.RUNNING, ResearchRunStatus.QUEUED):
            result = ResearchRunLifecycle.validate(
                ResearchRunStatus.FAILED, target
            )
            assert result.is_rejected
            assert result.error is not None
            assert result.error.kind is TransitionErrorKind.TERMINAL_STATE

    def test_same_status_is_rejected(self) -> None:
        result = ResearchRunLifecycle.validate(
            ResearchRunStatus.QUEUED, ResearchRunStatus.QUEUED
        )
        assert result.is_rejected
        assert result.error is not None
        assert result.error.kind is TransitionErrorKind.SAME_STATE

    def test_queued_to_completed_is_forbidden(self) -> None:
        """Not in the allowed set: a run must pass through running."""
        result = ResearchRunLifecycle.validate(
            ResearchRunStatus.QUEUED, ResearchRunStatus.COMPLETED
        )
        assert result.is_rejected
        assert result.error is not None
        assert result.error.kind is TransitionErrorKind.INVALID_TRANSITION

    def test_queued_to_partial_is_forbidden(self) -> None:
        """Not in the allowed set."""
        result = ResearchRunLifecycle.validate(
            ResearchRunStatus.QUEUED, ResearchRunStatus.PARTIAL
        )
        assert result.is_rejected
        assert result.error is not None
        assert result.error.kind is TransitionErrorKind.INVALID_TRANSITION

    def test_allowed_from_queued(self) -> None:
        allowed = ResearchRunLifecycle.allowed_from(ResearchRunStatus.QUEUED)
        assert ResearchRunStatus.RUNNING in allowed
        assert ResearchRunStatus.FAILED in allowed
        assert ResearchRunStatus.COMPLETED not in allowed
        assert ResearchRunStatus.PARTIAL not in allowed

    def test_allowed_from_running(self) -> None:
        allowed = ResearchRunLifecycle.allowed_from(ResearchRunStatus.RUNNING)
        assert ResearchRunStatus.COMPLETED in allowed
        assert ResearchRunStatus.PARTIAL in allowed
        assert ResearchRunStatus.FAILED in allowed
        assert ResearchRunStatus.QUEUED not in allowed

    def test_terminal_has_no_allowed_transitions(self) -> None:
        for terminal in (ResearchRunStatus.COMPLETED, ResearchRunStatus.PARTIAL,
                         ResearchRunStatus.FAILED):
            assert ResearchRunLifecycle.allowed_from(terminal) == frozenset()


# --- repository: ownership-scoped set_status (real PostgreSQL) ---------------------------


class TestSetStatusOwnership:
    """set_status is ownership-scoped: one user cannot mutate another's run."""

    async def test_set_status_rejects_foreign_user(
        self, db,
    ) -> None:
        """A user cannot change another user's run status."""
        run = await _seed_user_and_run(db, user_id=5101)

        repo = ResearchRunRepositoryImpl(db)

        with pytest.raises(ResearchRunNotFoundError):
            await repo.set_status(run.id, user_id=9999, status="running")

    async def test_set_status_allows_owner(
        self, db,
    ) -> None:
        """The run's owner can transition the status."""
        run = await _seed_user_and_run(db, user_id=5102)

        repo = ResearchRunRepositoryImpl(db)
        await repo.set_status(run.id, user_id=run.user_id, status="running")

        refreshed = await db.get(ResearchRunModel, run.id)
        assert ResearchRunStatus.coerce(refreshed.status) is ResearchRunStatus.RUNNING


class TestSetStatusLifecycleEnforcement:
    """set_status refuses disallowed transitions."""

    async def test_cannot_reopen_terminal_status(
        self, db,
    ) -> None:
        """A terminal status cannot be reopened."""
        from app.domain.research.lifecycle import TransitionErrorKind

        run = await _seed_user_and_run(db, user_id=5103)
        # Manually set to completed for testing
        await db.execute(
            ResearchRunModel.__table__.update()
            .where(ResearchRunModel.id == run.id)
            .values(status="completed")
        )

        repo = ResearchRunRepositoryImpl(db)

        with pytest.raises(TransitionError) as exc_info:
            await repo.set_status(run.id, user_id=run.user_id, status="running")

        assert exc_info.value.kind is TransitionErrorKind.TERMINAL_STATE

    async def test_cannot_jump_from_queued_to_completed(
        self, db,
    ) -> None:
        """Invalid transition is refused."""
        from app.domain.research.lifecycle import TransitionErrorKind

        run = await _seed_user_and_run(db, user_id=5104)

        repo = ResearchRunRepositoryImpl(db)

        with pytest.raises(TransitionError) as exc_info:
            await repo.set_status(run.id, user_id=run.user_id, status="completed")

        assert exc_info.value.kind is TransitionErrorKind.INVALID_TRANSITION

    async def test_terminal_failure_from_queued(
        self, db,
    ) -> None:
        """queued → failed is allowed (early failure)."""
        run = await _seed_user_and_run(db, user_id=5105)

        repo = ResearchRunRepositoryImpl(db)
        await repo.set_status(
            run.id, user_id=run.user_id, status="failed",
            failure_summary="search unavailable",
        )

        refreshed = await db.get(ResearchRunModel, run.id)
        assert ResearchRunStatus.coerce(refreshed.status) is ResearchRunStatus.FAILED
        assert refreshed.failure_summary == "search unavailable"


class TestSetStatusAtomicity:
    """set_status writes are atomic and scoped."""

    async def test_status_update_with_metadata(
        self, db,
    ) -> None:
        """Status update carries optional metadata."""
        run = await _seed_user_and_run(db, user_id=5106)

        repo = ResearchRunRepositoryImpl(db)
        await repo.set_status(
            run.id, user_id=run.user_id, status="running",
            prompt_tokens=1000,
            completion_tokens=500,
        )

        refreshed = await db.get(ResearchRunModel, run.id)
        assert refreshed.prompt_tokens == 1000
        assert refreshed.completion_tokens == 500


# --- concurrency: two conflicting transitions -----------------------------------------


class TestConcurrentStatusTransitions:
    """Two concurrent callers cannot silently produce an invalid final state.

    The test is synchronous and drives its whole scenario on the process's
    long-lived loop (``run_on_process_loop``), the same loop a Celery worker
    uses. It cannot be an ``async`` test: ``run_on_process_loop`` would then be
    asked to start a second loop from inside the pytest-asyncio loop, which is
    exactly the ``RuntimeError`` this test used to raise.
    """

    def test_concurrent_transition_one_wins(self, clean_database) -> None:
        """When two callers race, only the first transition may be accepted.

        The race is made deterministic rather than left to ``asyncio.gather``
        interleaving (which does not reliably overlap two short repository
        calls): the first caller transitions ``queued -> running`` and **holds
        the uncommitted transaction**, while a second caller attempts the same
        transition concurrently. The repository must lock the row, so the
        second caller waits for the first commit, re-reads ``running``, and is
        refused. Without the row lock the second caller reads the stale
        ``queued`` status and its update is accepted after the first commit —
        both would win.
        """
        from app.db.models import ResearchRunModel, UserModel

        database = clean_database

        async def scenario() -> None:
            # Seed one user and one queued run; keep the *internal* owner id,
            # because set_status is scoped by the users row id, not the Telegram
            # id.
            async with database.session() as setup:
                user = UserModel(telegram_user_id=5110, username=None)
                setup.add(user)
                await setup.flush()
                run = ResearchRunModel(user_id=user.id, question="q")
                setup.add(run)
                await setup.flush()
                run_id, owner_id = run.id, user.id

            first = database.sessionmaker()
            second = database.sessionmaker()
            async with first as first_session, second as second_session:
                # First caller transitions but does not commit yet, so its
                # transaction still holds the row lock.
                await ResearchRunRepositoryImpl(first_session).set_status(
                    run_id, user_id=owner_id, status="running"
                )

                async def contender() -> bool:
                    try:
                        await ResearchRunRepositoryImpl(second_session).set_status(
                            run_id, user_id=owner_id, status="running"
                        )
                        await second_session.commit()
                        return True
                    except TransitionError:
                        await second_session.rollback()
                        return False

                pending = asyncio.ensure_future(contender())
                # Give the contender time to reach the database and block on the
                # row lock. While the first transaction is uncommitted this must
                # not complete; if it does, the row was not locked and two
                # writers could both win.
                try:
                    await asyncio.wait_for(asyncio.shield(pending), timeout=1.0)
                except TimeoutError:
                    pass
                else:
                    raise AssertionError(
                        "the contender did not wait for the first caller's lock"
                    )

                await first_session.commit()
                accepted = await pending

            # Exactly one transition was accepted; the second observed the
            # already-running status and was refused.
            assert accepted is False

            async with database.session() as final:
                run = await final.get(ResearchRunModel, run_id)
                assert (
                    ResearchRunStatus.coerce(run.status) is ResearchRunStatus.RUNNING
                )

        run_on_process_loop(scenario())


# --- nonexistent run --------------------------------------------------------------------


class TestSetStatusNonexistentRun:
    """A nonexistent run follows the error contract."""

    async def test_set_status_nonexistent_run(
        self, db,
    ) -> None:
        """Setting status on a nonexistent run raises ResearchRunNotFoundError."""
        repo = ResearchRunRepositoryImpl(db)

        with pytest.raises(ResearchRunNotFoundError):
            await repo.set_status(run_id=99999, user_id=5111, status="running")
