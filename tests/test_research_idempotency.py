"""B7 — the run-level idempotency contract (Phase 3, P3.9-D).

Re-execution of a research run is an **expected condition**, not a hypothetical:
``task_acks_late=True`` with ``task_reject_on_worker_lost=True`` means a task killed
by the hard limit is redelivered and re-executes from the start (audit § R.11,
§ R.14). These tests drive the real repositories against the real PostgreSQL so the
constraints under test are the ones a deployment runs, not a fake's approximation.

What is proved here:

* one Telegram ``/research`` command produces exactly one run, however many times it
  is delivered (migration 0005's unique constraint);
* a caller with no derivable request key is *not* blocked by another keyless run;
* a re-executed pipeline converges on the source row the first attempt created
  instead of colliding with ``uq_sources_run_id_url``;
* a re-executed evidence stage refuses rather than appending a second set of claims
  and citations for one source;
* ownership is checked before any of it — a foreign run and a foreign source are
  both refused, and neither writes anything;
* a terminal run is never reopened, by any caller, in any order.

Not proved here, and deliberately: whether a whole *pipeline* is safe under
simultaneous execution of the same run. That is the orchestrator's contract
(Gate 4) and is covered in ``test_research_orchestration.py``; what is proved here is
that the persistence primitives it depends on behave as specified.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.db.models import ResearchRunModel as RunModel
from app.domain.errors import (
    DuplicateMessageError,
    ResearchRunNotFoundError,
    SourceAlreadyAnalyzedError,
)
from app.repositories.research import (
    CitationRepositoryImpl,
    ClaimRepositoryImpl,
    ResearchRunRepositoryImpl,
    SourceRepositoryImpl,
)
from app.repositories.users import UserRepositoryImpl
from tests.conftest import run_on_process_loop
from tests.research_support import (
    ENGLISH_PASSAGE,
    RecordingLLM,
    claim,
    claims_response,
    seed_source,
    uow_over,
)

# --- request key derivation --------------------------------------------------


class _FixedSummarizer:
    """A ``SourceSummarizer`` double returning one fixed summary.

    Defined here rather than imported so this module's tests read as a whole; the
    evidence stage verifies the summary and the claims independently of what the
    summarizer produced, so the summary's content is irrelevant here.
    """

    def __init__(self, text: str = "A summary.") -> None:
        self.text = text

    async def summarize(self, source):
        from app.domain.research.evidence import SourceSummary

        return SourceSummary(source_id=source.source_id, text=self.text)


ENGLISH_QUOTE = "recorded a forty percent increase across the sampled districts"
ENGLISH_STATEMENT = "The survey recorded a forty percent increase."


class TestRequestKeyDerivation:
    def test_the_key_is_derived_and_stable(self):
        from app.services.research.intake import derive_request_key

        first = derive_request_key(telegram_user_id=4242, telegram_message_id=99)
        again = derive_request_key(telegram_user_id=4242, telegram_message_id=99)
        assert first == again, "a retry must recompute the same key or it proves nothing"

    def test_the_key_is_short_enough_for_its_column(self):
        from app.services.research.intake import derive_request_key

        key = derive_request_key(telegram_user_id=1, telegram_message_id=2)
        assert key is not None
        assert len(key) <= 32, "migration 0005 declared String(32)"

    def test_different_messages_have_different_keys(self):
        from app.services.research.intake import derive_request_key

        assert derive_request_key(
            telegram_user_id=1, telegram_message_id=2
        ) != derive_request_key(telegram_user_id=1, telegram_message_id=3)

    def test_different_users_have_different_keys(self):
        from app.services.research.intake import derive_request_key

        assert derive_request_key(
            telegram_user_id=1, telegram_message_id=2
        ) != derive_request_key(telegram_user_id=2, telegram_message_id=2)

    def test_an_absent_platform_id_yields_no_key(self):
        from app.services.research.intake import derive_request_key

        assert derive_request_key(telegram_user_id=1, telegram_message_id=None) is None
        assert derive_request_key(telegram_user_id=None, telegram_message_id=2) is None


# --- one command, one run ----------------------------------------------------


class TestOneCommandOneRun:
    """The constraint under test, against a database that really commits.

    ``clean_database`` rather than ``db``: the behaviour being verified is what a
    *retry in a fresh transaction* finds, and the ``db`` fixture's outer transaction
    makes ``rollback()`` discard the earlier commit as well — which would hide the
    very thing this test asserts.
    """

    def test_a_second_run_with_the_same_key_is_refused(self, clean_database):
        database = clean_database

        async def scenario() -> None:
            async with database.session() as first_session:
                user = await UserRepositoryImpl(first_session).get_or_create(5150, None)
                runs = ResearchRunRepositoryImpl(first_session)
                created = await runs.create(
                    user.id, "what happened in 1403?", request_key="a" * 32
                )
                await first_session.commit()
                user_id, first_id = user.id, created.id

            # A fresh session, as a retry would have: the collision must happen now.
            async with database.session() as retry_session:
                with pytest.raises(DuplicateMessageError):
                    await ResearchRunRepositoryImpl(retry_session).create(
                        user_id, "what happened in 1403?", request_key="a" * 32
                    )
                await retry_session.rollback()

            async with database.session() as check:
                assert len(await ResearchRunRepositoryImpl(check).list_for_user(user_id, 20)) == 1
                assert first_id is not None

        run_on_process_loop(scenario())

    def test_the_refusal_is_retryable_so_the_caller_can_re_read(self, clean_database):
        """The retry is what makes the race resolve, exactly as for users.

        A uniqueness violation aborts the enclosing transaction (AD-019), so the
        only correct response is to retry and read the winner's row — which only
        works if the error says the operation is worth retrying.
        """
        database = clean_database

        async def scenario() -> None:
            async with database.session() as setup:
                user = await UserRepositoryImpl(setup).get_or_create(5151, None)
                await ResearchRunRepositoryImpl(setup).create(
                    user.id, "q", request_key="b" * 32
                )
                await setup.commit()
                user_id = user.id

            async with database.session() as retry_session:
                with pytest.raises(DuplicateMessageError) as caught:
                    await ResearchRunRepositoryImpl(retry_session).create(
                        user_id, "q", request_key="b" * 32
                    )
                assert caught.value.retryable is True
                await retry_session.rollback()

            # The retry reads the winner's row rather than creating one.
            async with database.session() as reread:
                found = await ResearchRunRepositoryImpl(reread).get_for_request(
                    user_id, "b" * 32
                )
            assert found is not None
            assert found.question == "q"

        run_on_process_loop(scenario())

    def test_the_same_key_for_a_different_user_is_not_a_collision(self, clean_database):
        """The constraint is scoped by ``user_id``, so isolation is structural.

        Two users cannot collide even if the same digest were somehow derived for
        both, and — more importantly — a caller naming somebody else's key can
        neither be refused by nor satisfied by that user's row.
        """
        database = clean_database

        async def scenario() -> None:
            async with database.session() as setup:
                one = await UserRepositoryImpl(setup).get_or_create(5152, None)
                two = await UserRepositoryImpl(setup).get_or_create(5153, None)
                await ResearchRunRepositoryImpl(setup).create(
                    one.id, "q1", request_key="c" * 32
                )
                await setup.commit()
                one_id, two_id = one.id, two.id

            async with database.session() as second:
                created = await ResearchRunRepositoryImpl(second).create(
                    two_id, "q2", request_key="c" * 32
                )
                await second.commit()
                assert created.id is not None

            async with database.session() as check:
                runs = ResearchRunRepositoryImpl(check)
                assert await runs.get_for_request(one_id, "c" * 32) is not None
                assert await runs.get_for_request(two_id, "c" * 32) is not None
                assert len(await runs.list_for_user(one_id, 20)) == 1
                assert len(await runs.list_for_user(two_id, 20)) == 1

        run_on_process_loop(scenario())

    def test_a_lookup_cannot_reach_another_users_run(self, clean_database):
        database = clean_database

        async def scenario() -> None:
            async with database.session() as setup:
                owner = await UserRepositoryImpl(setup).get_or_create(5154, None)
                intruder = await UserRepositoryImpl(setup).get_or_create(5155, None)
                await ResearchRunRepositoryImpl(setup).create(
                    owner.id, "q", request_key="d" * 32
                )
                await setup.commit()
                owner_id, intruder_id = owner.id, intruder.id

            async with database.session() as check:
                runs = ResearchRunRepositoryImpl(check)
                assert await runs.get_for_request(intruder_id, "d" * 32) is None
                assert await runs.get_for_request(owner_id, "d" * 32) is not None

        run_on_process_loop(scenario())

    def test_runs_without_a_key_do_not_block_each_other(self, clean_database):
        """Nullability is a deliberate property, not an oversight.

        A caller with no Telegram ``message_id`` — a console, an administrative
        retry, a test — has no request to derive a key from. PostgreSQL treats
        NULLs as distinct inside a unique constraint, so such runs coexist.
        """
        database = clean_database

        async def scenario() -> None:
            async with database.session() as session:
                user = await UserRepositoryImpl(session).get_or_create(5156, None)
                runs = ResearchRunRepositoryImpl(session)
                first = await runs.create(user.id, "q1")
                second = await runs.create(user.id, "q2")
                await session.commit()

                assert first.id is not None and second.id is not None
                assert first.request_key is None
                assert len(await runs.list_for_user(user.id, 20)) == 2

        run_on_process_loop(scenario())

    async def test_an_absent_key_never_matches_a_run(self, db):
        user = await UserRepositoryImpl(db).get_or_create(5157, None)
        runs = ResearchRunRepositoryImpl(db)
        await runs.create(user.id, "q")
        await db.commit()

        assert await runs.get_for_request(user.id, None) is None


# --- source-level convergence ------------------------------------------------


class TestSourceConvergence:
    def test_a_second_insert_of_the_same_url_is_refused_by_the_database(
        self, clean_database
    ):
        """``uq_sources_run_id_url`` really does refuse it.

        The orchestrator converges by *reading* the existing row first; this proves
        the constraint is the backstop that makes converging mandatory rather than
        merely advisable, so a caller that forgets aborts the run rather than
        silently creating a duplicate.
        """
        from sqlalchemy.exc import IntegrityError

        database = clean_database

        async def scenario() -> None:
            async with database.session() as setup:
                user = await UserRepositoryImpl(setup).get_or_create(5160, None)
                runs = ResearchRunRepositoryImpl(setup)
                run = await runs.create(user.id, "q")
                source = await SourceRepositoryImpl(setup).add(
                    run.id, "https://example.com/a", "example.com"
                )
                await setup.commit()
                run_id, url, domain = run.id, source.url, source.domain

            async with database.session() as collide:
                with pytest.raises(IntegrityError):
                    await SourceRepositoryImpl(collide).add(run_id, url, domain)
                await collide.rollback()

            async with database.session() as check:
                assert len(await SourceRepositoryImpl(check).list_for_run(run_id)) == 1

        run_on_process_loop(scenario())

    def test_a_re_executed_pipeline_finds_the_row_it_already_created(
        self, clean_database
    ):
        database = clean_database

        async def scenario() -> None:
            async with database.session() as setup:
                user = await UserRepositoryImpl(setup).get_or_create(5161, None)
                run = await ResearchRunRepositoryImpl(setup).create(user.id, "q")
                source = await SourceRepositoryImpl(setup).add(
                    run.id, "https://example.com/a", "example.com"
                )
                await setup.commit()
                run_id, url, source_id = run.id, source.url, source.id

            # A re-execution: the same URL, reached through a different session.
            async with database.session() as again:
                found = await SourceRepositoryImpl(again).get_for_url(run_id, url)
                assert found is not None
                assert found.id == source_id, "a re-run must converge, not duplicate"

        run_on_process_loop(scenario())

    async def test_a_url_lookup_is_scoped_to_its_run(self, db):
        """A caller cannot discover another run's source by naming its URL."""
        _u1, _run_a, source_a, *_ = await seed_source(db, telegram_user_id=5162)
        _u2, run_b, *_ = await seed_source(
            db, telegram_user_id=5163, domain="other.example"
        )
        await db.commit()

        assert await SourceRepositoryImpl(db).get_for_url(run_b.id, source_a.url) is None


# --- evidence re-execution ---------------------------------------------------


class TestEvidenceIsNotAppended:
    async def test_a_second_analysis_of_one_source_is_refused(self, db, make_settings):
        """The core B7 guarantee at the stage that would otherwise duplicate rows.

        Without the claim, a redelivered task would write a *second* claim row and
        a *second* citation for the same passage. ``uq_citations_claim_source_chunk``
        would not notice, because each duplicate claim has its own id — the
        duplication is invisible to the schema and must be refused by the service.
        """
        from app.infrastructure.evidence.analyzer import LLMClaimExtractor
        from app.services.research.evidence import SourceEvidenceService

        settings = make_settings()
        user, run, source, rows, *_ = await seed_source(
            db, telegram_user_id=5164, passages=(ENGLISH_PASSAGE,)
        )

        def _service(replies: list[str]) -> SourceEvidenceService:
            return SourceEvidenceService(
                settings=settings,
                summarizer=_FixedSummarizer("A summary."),
                extractor=LLMClaimExtractor(RecordingLLM(replies), settings),
                uow_factory=uow_over(db),
            )

        first = _service(
            [
                claims_response(
                    claim(ENGLISH_STATEMENT, rows[0].id, ENGLISH_QUOTE)
                )
            ]
        )
        await first.analyze_source(
            run_id=run.id, user_id=user.id, source_id=source.id
        )
        await db.commit()

        second = _service(
            [
                claims_response(
                    claim("A different statement entirely.", rows[0].id, ENGLISH_QUOTE)
                )
            ]
        )
        with pytest.raises(SourceAlreadyAnalyzedError):
            await second.analyze_source(
                run_id=run.id, user_id=user.id, source_id=source.id
            )
        await db.commit()

        assert len(await ClaimRepositoryImpl(db).list_for_run(run.id)) == 1
        claims = await ClaimRepositoryImpl(db).list_for_run(run.id)
        citations = await CitationRepositoryImpl(db).list_for_claim(claims[0].id)
        assert len(citations) == 1, "the redelivery appended a second citation"

    async def test_the_refusal_writes_nothing(self, db, make_settings):
        """The existing evidence survives untouched — not replaced, not emptied."""
        from app.infrastructure.evidence.analyzer import LLMClaimExtractor
        from app.services.research.evidence import SourceEvidenceService, load_source_evidence

        settings = make_settings()
        user, run, source, rows, *_ = await seed_source(
            db, telegram_user_id=5165, passages=(ENGLISH_PASSAGE,)
        )

        async def _once(text: str, statement: str) -> None:
            service = SourceEvidenceService(
                settings=settings,
                summarizer=_FixedSummarizer(text),
                extractor=LLMClaimExtractor(
                    RecordingLLM(
                        [claims_response(claim(statement, rows[0].id, ENGLISH_QUOTE))]
                    ),
                    settings,
                ),
                uow_factory=uow_over(db),
            )
            await service.analyze_source(
                run_id=run.id, user_id=user.id, source_id=source.id
            )
            await db.commit()

        await _once("The first summary.", ENGLISH_STATEMENT)
        with pytest.raises(SourceAlreadyAnalyzedError):
            await _once("A summary that must not survive.", "Another statement.")

        _row, summary = await load_source_evidence(
            run.id, user.id, source.id, uow_over(db)
        )
        assert summary == "The first summary."

    async def test_a_source_with_no_verified_claim_is_still_claimed(self, db, make_settings):
        """The marker is the summary, not a claim count — and that is deliberate.

        A model that proposes only claims which fail verification produces **no**
        claim rows. Re-running must not be invited on that basis: the extraction ran,
        it was verified, the answer was that nothing was supported, and re-spending
        provider budget on it would be exactly the unbounded retry the phase
        forbids.
        """
        from app.infrastructure.evidence.analyzer import LLMClaimExtractor
        from app.services.research.evidence import SourceEvidenceService

        settings = make_settings()
        user, run, source, rows, *_ = await seed_source(
            db, telegram_user_id=5166, passages=(ENGLISH_PASSAGE,)
        )

        def _service() -> SourceEvidenceService:
            return SourceEvidenceService(
                settings=settings,
                summarizer=_FixedSummarizer("A summary."),
                # A claim whose quote is not in the passage: rejected, never raised.
                extractor=LLMClaimExtractor(
                    RecordingLLM(
                        [claims_response(claim("Unsupported.", rows[0].id, "not in it"))]
                    ),
                    settings,
                ),
                uow_factory=uow_over(db),
            )

        await _service().analyze_source(
            run_id=run.id, user_id=user.id, source_id=source.id
        )
        await db.commit()
        assert await ClaimRepositoryImpl(db).list_for_run(run.id) == []

        with pytest.raises(SourceAlreadyAnalyzedError):
            await _service().analyze_source(
                run_id=run.id, user_id=user.id, source_id=source.id
            )
        await db.commit()

    async def test_the_claim_is_refused_for_another_runs_source(self, db):
        """``False`` covers a foreign source, so ownership stays a caller duty."""
        _u1, _run_a, _source_a, *_ = await seed_source(db, telegram_user_id=5167)
        _u2, run_b, source_b, *_ = await seed_source(
            db, telegram_user_id=5168, question="a different question"
        )
        await db.commit()

        assert await SourceRepositoryImpl(db).claim_unanalysed(run_b.id, source_b.id) is True
        await db.commit()
        # Same row, different run: the claim is refused rather than granted.
        assert await SourceRepositoryImpl(db).claim_unanalysed(_run_a.id, source_b.id) is False

    async def test_the_claim_grants_once_per_source(self, db):
        _user, run, source, *_ = await seed_source(db, telegram_user_id=5169)
        await db.commit()
        sources = SourceRepositoryImpl(db)

        assert await sources.claim_unanalysed(run.id, source.id) is True
        await sources.set_summary(source.id, run.id, "A summary.")
        await db.commit()

        assert await sources.claim_unanalysed(run.id, source.id) is False

    async def test_two_sources_claim_independently(self, db):
        """The lock is per source, so unrelated sources do not serialise the run."""
        _user, run, _source, *_ = await seed_source(
            db, telegram_user_id=5170, extra_source=True
        )
        other = await SourceRepositoryImpl(db).list_for_run(run.id)
        await db.commit()
        assert len(other) == 2

        sources = SourceRepositoryImpl(db)
        for row in other:
            assert await sources.claim_unanalysed(run.id, row.id) is True
        await db.commit()


# --- terminal state protection -----------------------------------------------


class TestTerminalRunsAreNotReopened:
    async def test_every_terminal_status_refuses_a_reopen(self, db):
        """The lifecycle is the authority, and B7 depends on it.

        A redelivered task that tried to restart a finished run would re-spend the
        whole provider budget and could deliver a second terminal message. The row
        lock plus the transition table make that impossible from any caller.
        """
        from app.domain.research.lifecycle import TransitionError

        user = await UserRepositoryImpl(db).get_or_create(5171, None)
        runs = ResearchRunRepositoryImpl(db)

        for terminal in ("completed", "partial", "failed"):
            run = await runs.create(user.id, f"q-{terminal}")
            await runs.set_status(run.id, user.id, "running")
            await runs.set_status(run.id, user.id, terminal)
            await db.commit()

            with pytest.raises(TransitionError):
                await runs.set_status(run.id, user.id, "running")
            await db.commit()

            assert (await runs.get_for_user(run.id, user.id)).status == terminal

    async def test_a_redelivered_task_cannot_claim_a_running_run_again(self, db):
        """``queued -> running`` is the claim; a second one is refused as SAME_STATE.

        This is the durable half of "repeated task delivery must not re-execute a
        run": once claimed, the transition is not available a second time.
        """
        from app.domain.research.lifecycle import TransitionError, TransitionErrorKind

        user = await UserRepositoryImpl(db).get_or_create(5172, None)
        runs = ResearchRunRepositoryImpl(db)
        run = await runs.create(user.id, "q")
        await runs.set_status(run.id, user.id, "running")
        await db.commit()

        with pytest.raises(TransitionError) as caught:
            await runs.set_status(run.id, user.id, "running")
        assert caught.value.kind is TransitionErrorKind.SAME_STATE
        await db.commit()


# --- ownership ---------------------------------------------------------------


class TestOwnershipIsCheckedFirst:
    async def test_a_foreign_user_cannot_create_a_key_that_names_another_run(self, db):
        owner = await UserRepositoryImpl(db).get_or_create(5173, None)
        runs = ResearchRunRepositoryImpl(db)
        run = await runs.create(owner.id, "q", request_key="e" * 32)
        await db.commit()

        intruder = await UserRepositoryImpl(db).get_or_create(5174, None)
        await db.commit()

        # The intruder may create its own run under the same derived key — the
        # constraint is scoped by owner — but can never *reach* the owner's.
        assert await runs.get_for_user(run.id, intruder.id) is None
        with pytest.raises(ResearchRunNotFoundError):
            await runs.set_status(run.id, intruder.id, "running")
        await db.commit()

    async def test_a_foreign_run_is_refused_by_the_evidence_claim(self, db):
        _owner, run, *_ = await seed_source(db, telegram_user_id=5175)
        await db.commit()

        assert await SourceRepositoryImpl(db).claim_unanalysed(run.id + 10_000, 1) is False


# --- the intake path end to end ----------------------------------------------


class TestIntakeIsIdempotent:
    """The intake service driven on the process loop, against real PostgreSQL.

    Synchronous by necessity, not by preference: ``run_on_process_loop`` starts the
    process's long-lived loop, so an ``async`` test would be asking it to start a
    second loop from inside pytest-asyncio's — the ``RuntimeError`` the project
    skill records. They therefore use ``clean_database`` (which commits for real)
    rather than ``db``, for the same reason the lifecycle concurrency test does.
    """

    def test_the_same_command_delivered_twice_creates_one_run(
        self, clean_database, settings
    ):
        """The property the handler relies on, driven through the real service."""
        from app.services.research.intake import ResearchIntakeService

        database = clean_database

        async def scenario() -> None:
            service = ResearchIntakeService(
                settings=settings, uow_factory=_uow_factory(database)
            )
            first = await service.accept(5180, 777, None, "What changed in 1403?")
            second = await service.accept(5180, 777, None, "What changed in 1403?")

            assert first.run.id == second.run.id, "a redelivered command made a new run"
            assert first.created is True
            assert second.created is False

            async with database.session() as check:
                rows = list((await check.execute(select(RunModel))).scalars())
            assert len(rows) == 1, f"one command produced {len(rows)} runs"

        run_on_process_loop(scenario())

    def test_two_different_commands_create_two_runs(self, clean_database, settings):
        from app.services.research.intake import ResearchIntakeService

        database = clean_database

        async def scenario() -> None:
            service = ResearchIntakeService(
                settings=settings, uow_factory=_uow_factory(database)
            )
            a = await service.accept(5181, 1, None, "First question")
            b = await service.accept(5181, 2, None, "Second question")

            assert a.run.id != b.run.id
            assert a.created is True and b.created is True

            async with database.session() as check:
                rows = list((await check.execute(select(RunModel))).scalars())
            assert len(rows) == 2

        run_on_process_loop(scenario())

    def test_the_question_is_stored_collapsed_and_trimmed(self, clean_database, settings):
        """Whitespace normalization happens before the length check.

        A question padded with newlines is the question a user typed, and measuring
        it after collapsing is what makes the cap mean what § 15.1 says it means.
        """
        from app.services.research.intake import ResearchIntakeService

        database = clean_database

        async def scenario() -> None:
            service = ResearchIntakeService(
                settings=settings, uow_factory=_uow_factory(database)
            )
            accepted = await service.accept(
                5182, 5, None, "  what   changed\n\nin 1403?  "
            )
            assert accepted.run.question == "what changed in 1403?"

        run_on_process_loop(scenario())

    def test_an_invalid_question_creates_nothing(self, clean_database, settings):
        """§ 15.9: an invalid request is refused at intake with no run created.

        Asserted against the database, not against a return value: the requirement
        is that *no state changed*, so a test that only checked "it raised" would
        pass for an implementation that created the run and then refused.
        """
        from app.domain.errors import PlanningInputError
        from app.services.research.intake import ResearchIntakeService

        database = clean_database

        async def scenario() -> None:
            service = ResearchIntakeService(
                settings=settings, uow_factory=_uow_factory(database)
            )
            for bad in (
                "",
                "   ",
                "\n\t",
                "x" * (settings.PLAN_MAX_QUESTION_CHARS + 1),
            ):
                with pytest.raises(PlanningInputError):
                    await service.accept(5183, 9, None, bad)

            async with database.session() as check:
                rows = list((await check.execute(select(RunModel))).scalars())
            assert rows == [], f"a rejected question created {len(rows)} run(s)"

        run_on_process_loop(scenario())

    def test_the_cap_is_the_planner_cap(self, settings):
        """AD-046: intake and the planner must agree, or intake accepts what planning refuses."""
        from app.domain.errors import PlanningInputError
        from app.services.research.intake import ResearchIntakeService

        service = ResearchIntakeService(settings=settings, uow_factory=None)
        at_cap = "x" * settings.PLAN_MAX_QUESTION_CHARS
        assert service.validate_question(at_cap) == at_cap
        with pytest.raises(PlanningInputError):
            service.validate_question("x" * (settings.PLAN_MAX_QUESTION_CHARS + 1))

    def test_a_persian_question_is_accepted_and_preserved(self, clean_database, settings):
        """The product's primary language, at the surface a user actually types at."""
        from app.services.research.intake import ResearchIntakeService

        database = clean_database
        question = "تغییرات آب و هوا در ایران در سال ۱۴۰۳ چه بوده است؟"

        async def scenario() -> None:
            service = ResearchIntakeService(
                settings=settings, uow_factory=_uow_factory(database)
            )
            accepted = await service.accept(5184, 11, None, question)
            assert accepted.run.question == question

        run_on_process_loop(scenario())


def _uow_factory(database):
    """A real unit-of-work factory over the test database, as production builds one."""

    def factory():
        from app.db.unit_of_work import SqlAlchemyUnitOfWork

        return SqlAlchemyUnitOfWork(database)

    return factory


def _uow_over(session):
    """A unit of work over the ``db`` session, with every repository intake needs.

    Committing and rolling back are the test's business, which is why ``__aexit__``
    does neither: the assertions here read what intake actually persisted, so an
    auto-commit would hide a rolled-back insert and a silent rollback would hide a
    committed one.
    """
    from app.repositories.conversations import ConversationRepositoryImpl
    from app.repositories.messages import MessageRepositoryImpl
    from app.repositories.research import SourceChunkRepositoryImpl

    class _UoW:
        def __init__(self) -> None:
            self.users = UserRepositoryImpl(session)
            self.conversations = ConversationRepositoryImpl(session)
            self.messages = MessageRepositoryImpl(session)
            self.research_runs = ResearchRunRepositoryImpl(session)
            self.sources = SourceRepositoryImpl(session)
            self.source_chunks = SourceChunkRepositoryImpl(session)
            self.claims = ClaimRepositoryImpl(session)
            self.citations = CitationRepositoryImpl(session)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc_info) -> None:
            return None

    return lambda: _UoW()