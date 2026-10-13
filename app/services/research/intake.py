"""Research intake service (Phase 3, P3.9-D — AD-029, AD-046, B7).

The application-layer step between a user's ``/research <question>`` command and a
queued run. It owns the ordering of the operations and nothing else: validate the
question, resolve the owner and the active conversation, create exactly one run, and
report whether it was the one that created it.

```text
/research <question>
  → validate (non-empty, ≤ PLAN_MAX_QUESTION_CHARS)   → refuse before any state
  → derive request_key = sha256(user_id:message_id)  → B7
  → get_or_create user
  → conversations.get_active(user)                   → AD-016, derived not stored
  → research_runs.create(..., request_key)           → 'queued'
  → existing run for the key?  → that one, created=False
```

**Validation happens before anything exists.** § 15.9 requires an invalid research
request to be *rejected at intake with no run created*, and AD-046 fixed the cap at
``PLAN_MAX_QUESTION_CHARS`` so that the value the planner enforces before any model
call is the value intake enforces before any row. A question the planner would reject
therefore never becomes a run, a queue message, and a worker execution.

**Idempotency is the database's, not this service's** (B7). The key is *derived* from
the platform identifiers, exactly as AD-017 derives ``turn_id`` from
``(chat_id, message_id)``, so a redelivered command recomputes the same value and the
``uq_research_runs_user_id_request_key`` constraint refuses the second insert. The
service does not "check whether a run already exists" and then insert — that race is
exactly what the constraint exists to close. It re-reads by key and returns the
winner's row.

**What this service does not do.** It does not enqueue, plan, search, fetch, extract,
rank, chunk, extract claims, synthesize, or render. Delivery is the Telegram layer's
and execution is the Celery task's; a caller that has an accepted request has a run
with a stable identifier and nothing else.

**Not durable on its own.** Creating the run and enqueueing the task cannot be one
atomic operation without an outbox table this schema does not have (AD-034 freezes
five tables and no message registry). The caller therefore compensates: if the enqueue
raises, it transitions the run ``queued → failed`` and tells the user, rather than
leaving a run that looks active and is never executed.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.core.config import Settings
from app.domain.errors import (
    DuplicateMessageError,
    PlanningInputError,
)
from app.domain.research.types import ResearchRun

log = logging.getLogger(__name__)

#: Width of the derived key. Matches ``messages.turn_id`` and migration 0005's
#: ``String(32)``: 128 bits of a SHA-256 is an identity, not a secret, and widening
#: it would mean a second width convention in one schema.
REQUEST_KEY_LENGTH = 32


def derive_request_key(
    telegram_user_id: int | None, telegram_message_id: int | None
) -> str | None:
    """The idempotency key for one inbound ``/research`` command (B7, AD-017's pattern).

    Derived from the platform identifiers rather than generated, so a redelivered
    Telegram update and a retried enqueue both compute the same value and collide
    in the database. That is what makes the unique constraint an effective
    idempotency boundary instead of a uniqueness constraint on random values
    (Phase 2 § 15, § 16).

    Returns ``None`` when either identifier is absent. That is not a cosmetic
    fallback: without a ``message_id`` there is no *request* to identify, and
    inventing a random key would make the constraint useless while a constant key
    would make every such caller collide with every other. A run with no key is not
    deduplicated — and because PostgreSQL treats NULLs as distinct inside a unique
    constraint, it never blocks another keyless run either.
    """
    if telegram_user_id is None or telegram_message_id is None:
        return None
    digest = hashlib.sha256(f"{telegram_user_id}:{telegram_message_id}".encode())
    return digest.hexdigest()[:REQUEST_KEY_LENGTH]


class AcceptedRequest(BaseModel):
    """One accepted ``/research`` request and the run it belongs to."""

    model_config = ConfigDict(frozen=True)

    run: ResearchRun

    created: bool
    """Whether this call is the one that created the run.

    ``False`` means the command was delivered more than once and the existing run
    was returned. The distinction matters to the caller in exactly one way: a
    redelivery must not be acknowledged as a fresh acceptance and must not enqueue a
    second execution.
    """


class ResearchIntakeService:
    """Validate a research question and create exactly one queued run for it."""

    def __init__(
        self,
        *,
        settings: Settings,
        uow_factory: Callable[[], Any],
    ) -> None:
        self._settings = settings
        self._uow_factory = uow_factory

    def validate_question(self, raw: str | None) -> str:
        """Return the normalized question, or raise :class:`PlanningInputError`.

        Normalization is the one rule every stage already shares: collapse internal
        whitespace to single spaces and strip the ends (``SearchRequest._query_usable``,
        the planner's ``_normalize_text``, ``ExtractedContent.from_fetched``). It is
        applied **before** the length check, so a question padded with whitespace is
        measured as the question it is rather than rejected for padding the user did
        not intend as content.

        Non-empty after stripping and within ``PLAN_MAX_QUESTION_CHARS`` are the two
        rules; both are the frozen contract of § 15.1 and AD-046. Raising
        :class:`PlanningInputError` rather than a bespoke type is deliberate: it is
        exactly what the planner would raise one Celery round-trip later for the same
        input, so intake and planning agree by construction and a caller can handle
        one type.

        **Nothing is written and nothing is enqueued when this raises** — the call
        happens before the first database access, which is what § 15.9's "no run
        created" requires.
        """
        if not isinstance(raw, str):
            raise PlanningInputError("a research question must be text")
        collapsed = " ".join(raw.split())
        if not collapsed:
            raise PlanningInputError("a research question must not be empty")
        limit = self._settings.PLAN_MAX_QUESTION_CHARS
        if len(collapsed) > limit:
            # The length is a configuration value, not user content, so it is safe
            # to state; the question itself is never echoed into a message or a log.
            raise PlanningInputError(
                f"a research question must be at most {limit} characters"
            )
        return collapsed

    async def accept(
        self,
        telegram_user_id: int,
        telegram_message_id: int | None,
        username: str | None,
        question: str,
    ) -> AcceptedRequest:
        """Validate the question and create (or find) its run.

        One transaction. The run is created and committed here, before the caller
        enqueues it, for the same reason Phase 2's chat path commits the user's
        message first: a broker outage must not lose the fact that the user asked.
        The cost is a legitimate intermediate state — a ``queued`` run with no
        message behind it — which is what the caller's compensating transition is
        for.

        Raises :class:`PlanningInputError` before any database access if the question
        is empty or over the cap, so an invalid request produces no run, no user row
        for a stranger, and no queue message.
        """
        collapsed = self.validate_question(question)
        request_key = derive_request_key(telegram_user_id, telegram_message_id)

        try:
            async with self._uow_factory() as uow:
                user = await uow.users.get_or_create(telegram_user_id, username)
                conversation = await uow.conversations.get_active(user.id)
                run = await uow.research_runs.create(
                    user.id,
                    collapsed,
                    conversation.id if conversation is not None else None,
                    request_key=request_key,
                )
        except DuplicateMessageError:
            # The derived key collided: this command was already accepted. Not a
            # failure — the run exists, and re-reading it by key is the whole point
            # of deriving the key rather than generating one. The insert's
            # transaction is spent, so the read must be a fresh one.
            log.info(
                "research command already accepted: telegram_user=%s message=%s",
                telegram_user_id,
                telegram_message_id,
            )
            return await self._read_accepted(telegram_user_id, request_key)

        if run.id is None:  # pragma: no cover - create() flushes and assigns an id
            raise DuplicateMessageError("created research run has no id")

        log.info(
            "research accepted: run=%d user=%d conversation=%s chars=%d",
            run.id,
            run.user_id,
            run.conversation_id,
            len(collapsed),
        )
        return AcceptedRequest(run=run, created=True)

    async def _read_accepted(
        self, telegram_user_id: int, request_key: str | None
    ) -> AcceptedRequest:
        """Resolve the run a duplicated command belongs to.

        Scoped by ``telegram_user_id`` as well as the key, for the reason the unique
        constraint is: the key is derived from a platform identifier, so a lookup
        without the owner would be able to name somebody else's run (SR-10).
        """
        async with self._uow_factory() as uow:
            user = await uow.users.get_or_create(telegram_user_id, None)
            existing = await uow.research_runs.get_for_request(user.id, request_key)
        if existing is None:  # pragma: no cover - the collision proved it exists
            raise DuplicateMessageError(
                "a research run collided with an existing request but could not be read"
            )
        return AcceptedRequest(run=existing, created=False)


__all__ = [
    "REQUEST_KEY_LENGTH",
    "AcceptedRequest",
    "ResearchIntakeService",
    "derive_request_key",
]