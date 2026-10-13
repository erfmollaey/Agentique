"""Research pipeline task (Phase 3, P3.9-D — AD-043, AD-045).

Thin by design, the same rule ``process_research`` follows: resolve the process
container, call the orchestrator, deliver exactly one terminal message. No prompt
construction, no SQL, no provider SDK, no transport management (TR-2, § 15.12).

**One task per run, all stages inside it** (AD-043). The alternative — chained stage
tasks passing results through the return value — is unavailable in this repository,
because ``task_ignore_result=True`` and nothing reads results, and it would make
FR-44's "terminate with what it has" a distributed-transaction problem the schema
cannot express (audit § R.11).

**What the message carries and why.** ``run_id`` is the stable identifier of the run
and ``chat_id`` is the *destination*, which is not part of the run's durable record —
the research schema deliberately holds ``conversation_id`` and no chat column, and a
conversation's chat is a Phase 2 fact. Everything else — the question, the owner, the
plan, the sources, the evidence, and the answer — is resolved from storage, so a
redelivery cannot drift from the run it is redelivering. Ownership is *re-checked*
from the run row by the orchestrator (``get_for_user``) before any work, which is what
makes a message that named someone else's run fail before a model call rather than
after one (SR-10).

**Retry classification in one place.** ``retryable`` is read from the exception the
orchestrator raised, exactly as ``process_research`` reads it:

* ``ResearchDeadlineError`` and the synthesis/citation families are non-retryable, so
  a deadline produces one terminal notice instead of a re-execution that spends the
  budget again (AD-045, § S.3.2);
* ``LLMServiceError``/``SearchProviderError``/``FetchConnectionError`` are retryable
  and are retried a bounded number of times, on the retry allowance the Celery
  annotations already provide;
* ``SoftTimeLimitExceeded`` is caught and **not** retried, because § S.2(2) measured
  that the signal arrives in the awaiting frame rather than the coroutine — the
  unit of work is left open, nothing committed, and a re-execution would spend the
  budget again from the start. Retrying it is the failure AD-045 records, so the
  default ``getattr(exc, "retryable", True)`` that assumes retryable is corrected
  here rather than inherited.

**Terminal notification safety.** The message is sent from exactly one code path,
after the orchestrator has already written the terminal status. A second delivery
finds the run terminal and never reaches this line, so a redelivery cannot produce a
second notice (B7, § R.14). Nothing here holds a lock across the send: the run's
status is committed before delivery begins, and a failed delivery leaves the run
terminal and the answer stored, which is the state Phase 2's chat path already treats
as "redelivery re-sends it" (§ 15.9 row: Delivery failure, R).

**A terminal failure leaves a terminal row.** The exhaustion branch of
``_handle_failure`` writes the failed status and its reason *before* it builds the
notice, so the two possible ends of a run — answer and failure — both leave a row
that says so, and neither can be interrupted by the delivery that follows into a
looks-active lie. An intermediate retry deliberately writes nothing: that attempt
is not the end of the run.
"""

from __future__ import annotations

import logging

from celery.exceptions import SoftTimeLimitExceeded

from app.bot import container as container_module
from app.bot.container import AppContainer
from app.core.celery_app import celery_app
from app.domain.errors import ResearchDeadlineError
from app.services.failures import user_message_for
from app.services.research.orchestrator import ResearchOutcome

# Importing the lifecycle module registers the Celery worker signals that give this
# process its long-lived event loop and close the Telegram session on shutdown
# (P0-7, C-3). The import must not be removed.
from app.tasks import lifecycle as _lifecycle  # noqa: F401
from app.tasks.research_task import run_async

log = logging.getLogger(__name__)

#: Used only when Celery's own soft limit fires, which the cooperative deadline
#: (AD-045) is designed to pre-empt. It says the run stopped early, which is the
#: truthful description of what a soft limit means for a multi-stage run.
_STOPPED_EARLY_MESSAGE = (
    "The research took longer than I have, so I stopped early. "
    "Please try again with a more specific question."
)

#: The terminal notice for a run the orchestrator classified without an answer
#: (``NO_EVIDENCE``). Chosen by outcome rather than by exception, so it describes
#: what happened to the user's request rather than what a stage raised (FR-43).
_NO_ANSWER_MESSAGE = (
    "I could not find enough reliable information to answer that. "
    "Try rephrasing the question or asking about something else."
)


@celery_app.task(
    bind=True,
    name="app.tasks.research_task.run_research",
    autoretry_for=(),
)
def run_research(self, run_id: int, user_id: int, chat_id: int) -> int:
    """Execute one research run and deliver exactly one terminal message.

    ``user_id`` and ``chat_id`` are deliberately different values, for the reason
    ``process_research`` keeps them apart: in a group the first owns the run and the
    second is where the reply goes (H-1, SR-10).
    """
    container: AppContainer = container_module.get_container()
    orchestrator = container.research
    if orchestrator is None:  # pragma: no cover - only a misbuilt container
        raise RuntimeError("container has no research orchestrator")

    try:
        outcome = run_async(orchestrator.execute(run_id=run_id, user_id=user_id))
    except ResearchDeadlineError:
        # The cooperative deadline, classified at the boundary where it can still
        # persist. Non-retryable: a second attempt spends the budget again (AD-045).
        log.warning(
            "research hit its cooperative deadline: run=%d user=%d task=%s retries=%s",
            run_id, user_id, self.request.id, self.request.retries,
        )
        return _send(container, chat_id, _STOPPED_EARLY_MESSAGE)
    except SoftTimeLimitExceeded:
        # The signal-based soft limit. Unlike the cooperative deadline this
        # arrives in the *awaiting frame*, not inside the coroutine, so the
        # orchestrator's own terminal write never ran and the run is still
        # ``running``. Marking it failed here is what stops the next redelivery
        # from tripping over that state (see _mark_failed). Not retried: a second
        # attempt spends the budget again from the start.
        log.warning(
            "research soft time limit reached: run=%d user=%d task=%s retries=%s",
            run_id, user_id, self.request.id, self.request.retries,
        )
        _mark_failed(
            container, run_id, user_id, "stopped early: task soft time limit"
        )
        return _send(container, chat_id, _STOPPED_EARLY_MESSAGE)
    except Exception as exc:  # bounded retry, then one terminal notice
        return _handle_failure(self, container, chat_id, run_id, user_id, exc)

    # A stage error never reaches here: ``execute`` raises and the branch above owns
    # the notice, so this line is the *successful* terminal and the only other one.
    return _deliver(container, chat_id, outcome)


def _mark_failed(
    container: AppContainer, run_id: int, user_id: int, failure_summary: str
) -> None:
    """Record a terminal failure for a run the orchestrator could not finish.

    Used where the coroutine was interrupted and its own terminal write never
    ran — the signal-based soft limit, and an exhausted stage failure. Without it
    the row is left at ``running``, which is a lie two readers act on: a
    redelivered task re-claims it (now handled, but only for a run this user
    owns) and every reader counts the run as still active.

    ``failure_summary`` is written by the caller and must be a reason the caller
    itself composed. It is never the exception's text, which can echo a provider
    message or a credential (SR-5); the callers here pass either a fixed phrase
    or the exception's *class name*, which is this codebase's own type name.

    Ownership-scoped, and terminal-state-tolerant: a row already ``failed`` is
    not an error worth raising here, because the purpose is to leave *a* truthful
    terminal state, and the previous attempt may already have written one.
    """
    from app.domain.research.lifecycle import (
        TransitionError,
        TransitionErrorKind,
    )
    from app.infrastructure.asyncio_runtime import run_coroutine

    factory = container.research_uow_factory
    if factory is None:  # pragma: no cover - only a misbuilt container
        return

    async def _transition() -> None:
        try:
            async with factory() as uow:
                await uow.research_runs.set_status(
                    run_id,
                    user_id,
                    "failed",
                    failure_summary=failure_summary,
                )
        except TransitionError as exc:
            # Terminal or same-state: the row already says what this call would
            # have said. That is a success, not a failure to report.
            if exc.kind in (TransitionErrorKind.TERMINAL_STATE, TransitionErrorKind.SAME_STATE):
                log.info(
                    "research run already terminal, not rewritten: run=%d status=%s",
                    run_id,
                    exc.current_status,
                )
                return
            raise
        except Exception:
            # A failed terminal write must not mask the stop the user is about to
            # be told about. Logged, then the notice still goes out.
            log.exception("could not record the failed status for run %d", run_id)

    try:
        run_coroutine(_transition())
    except Exception:
        log.exception("could not mark research run %d failed", run_id)


# --- internals ----------------------------------------------------------------


def _deliver(container: AppContainer, chat_id: int, outcome: ResearchOutcome) -> int:
    """Send the run's terminal message.

    A verified answer when there is one, and an explicit no-evidence notice when
    there is not. Never both, never neither: a run that produced a usable answer
    receives that answer, and a run that produced none is told so rather than being
    delivered a fabricated summary of what failed (§ 15.9, FR-41).

    **One logical message, possibly several Telegram messages.** When the run has a
    verified answer, ``outcome.messages`` is the rendered form — the answer with its
    inline citation markers and the numbered source list, already split so each part
    is within the platform's limit (FR-40). They are sent in order and none is sent
    twice, so a long cited answer arrives whole rather than truncated. The fallback
    to ``outcome.answer`` is for a run whose rendering fell back to plain prose; the
    notice is for a run with no verified answer at all.
    """
    if outcome.messages:
        return _send_messages(container, chat_id, outcome.messages)
    text = outcome.answer if outcome.answer else _NO_ANSWER_MESSAGE
    return _send(container, chat_id, text)


def _handle_failure(
    self, container: AppContainer, chat_id: int, run_id: int, user_id: int, exc: BaseException
) -> int:
    """Retry a retryable failure within its allowance, otherwise report it once.

    The soft-time-limit special case is the reason this method exists rather than the
    inline block ``process_research`` uses: ``getattr(exc, "retryable", True)`` would
    retry a soft limit, and § S.2(2) shows why that is wrong. Everything else follows
    the established policy, including the user not being messaged on an attempt that
    will be retried.

    The two branches write different amounts of state, deliberately: an attempt
    that will be retried leaves the row exactly as it found it, because the run is
    still alive; the final attempt marks the run failed before it sends anything,
    because the run is over either way the send goes.
    """
    retryable = getattr(exc, "retryable", True)

    from celery.exceptions import SoftTimeLimitExceeded

    if isinstance(exc, SoftTimeLimitExceeded):
        # Not retryable, and with a message that describes what a soft limit means
        # for a multi-stage run: the run stopped early. ``user_message_for`` maps the
        # type to the generic notice, which would tell the user nothing they can act
        # on.
        retryable = False

    will_retry = retryable and self.request.retries < self.max_retries
    log.warning(
        "research failed: run=%d user=%d task=%s attempt=%s/%s error=%s "
        "retryable=%s will_retry=%s",
        run_id, user_id, self.request.id, self.request.retries,
        self.max_retries, type(exc).__name__, retryable, will_retry,
    )
    if will_retry:
        from app.tasks.research_task import _backoff

        raise self.retry(
            exc=exc, countdown=_backoff(self.request.retries)
        ) from exc

    # The retry allowance is exhausted (or the failure was never retryable), so
    # this run is over: the row is made terminal *before* the notice is sent.
    # Ordered this way on purpose — a delivery failure then leaves the run
    # ``failed`` rather than ``running``, which is the state Run 18 was left in
    # when its terminal notice was delivered and the row was not.
    #
    # Only the exception's class name reaches storage. ``str(exc)`` can carry a
    # provider's message, a SQL fragment, or a credential echoed back by a
    # service, and ``failure_summary`` is an operator's field, not a user one
    # (SR-5). ``_mark_failed`` never raises, so it cannot mask the failure the
    # user is about to be told about.
    _mark_failed(
        container,
        run_id,
        user_id,
        f"research failed: {type(exc).__name__}",
    )

    notice = (
        _STOPPED_EARLY_MESSAGE
        if _is_soft_limit(exc)
        else user_message_for(exc)
    )
    try:
        return _send(container, chat_id, notice)
    except Exception:  # never mask the original failure
        log.error("could not deliver research failure notice to chat %s", chat_id)
        raise


def _is_soft_limit(exc: BaseException) -> bool:
    """Whether ``exc`` is Celery's signal-based soft limit."""
    from celery.exceptions import SoftTimeLimitExceeded

    return isinstance(exc, SoftTimeLimitExceeded)


def _send(container: AppContainer, chat_id: int, text: str) -> int:
    """Send one already-rendered message through the shared delivery service."""
    sent = container.delivery.send(chat_id, text)
    log.info(
        "research terminal delivered: chat=%s messages=%s", chat_id, sent
    )
    return sent


def _send_messages(
    container: AppContainer, chat_id: int, texts: tuple[str, ...]
) -> int:
    """Send a pre-split terminal message in order, returning how many went out.

    Each element is already within Telegram's limit, so ``DeliveryService`` does not
    re-split it and the citation markers and source entries the renderer placed stay
    exactly where it put them.

    A failure part-way through is **not** swallowed: the exception propagates to
    ``_handle_failure`` like any other delivery failure, so a partially delivered
    answer is an error the run's own policy sees rather than a silent truncation.
    The answer is already persisted on the run, so the existing redelivery path can
    re-send it (§ 15.9, Delivery failure).
    """
    sent = 0
    for text in texts:
        sent += container.delivery.send(chat_id, text)
    log.info(
        "research terminal delivered: chat=%s messages=%s parts=%s",
        chat_id, sent, len(texts),
    )
    return sent
