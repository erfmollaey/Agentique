"""Phase 3 request key: one logical research run per inbound Telegram command

Revision ID: 0005_research_request_key
Revises: 0004_research_synthesis
Create Date: 2026-10-09

**B7 — the run idempotency key.** The P3.9 audit (§ R.14) recorded that research
has no analogue of AD-017's ``turn_id``, and that with ``task_acks_late=True`` +
``task_reject_on_worker_lost=True`` a hard-killed task is redelivered and
re-executes, so "one run per user request" is a condition the database has to
refuse rather than something the code is trusted to arrange.

**No table is added.** The five-table model of AD-034 and § 15.8 is unchanged,
and no workspace, registry, or future-stage table appears here. This is one
nullable column plus one unique constraint on the run table that already exists.

1. ``research_runs.request_key`` — the identity of the *request*, not of the row.

   Why a column rather than only a task-level check. AD-017's whole lesson is that
   a uniqueness constraint over a **derived** key is what makes redelivery a
   non-event: a redelivered Telegram update recomputes the same value, and the
   second insert collides in the database instead of needing coordination between
   workers. Checking "does a run with this question already exist?" before
   inserting cannot work — two users may legitimately ask the same question, and
   so may one user twice — so the key has to be derived from the platform
   identifiers of the *command*, exactly as ``turn_id`` is derived from
   ``(chat_id, message_id)``.

   Why ``String(32)``. It carries the first 32 hex characters of a SHA-256 digest,
   the same width and derivation as ``messages.turn_id``. The column is not a
   security boundary — it is an identity, and it is compared for equality by the
   database rather than being guessed — so 128 bits of a SHA-256 is more than
   enough and matches the existing convention rather than introducing a second one.

   **Nullable, with no backfill, and deliberately so.** Two independent reasons:

   * every existing run predates the ``/research`` command, so there is no row
     whose key could be filled in, and inventing one would fabricate a claim that
     two historical requests were the same request;
   * a run created by a caller that has no Telegram ``message_id`` (a future
     console, an administrative retry, a test) has no request to derive a key
     from, and forcing one would make every such caller collide with every other.

   PostgreSQL treats NULLs as distinct inside a unique constraint, so the
   uniqueness rule below is enforced for every run that *has* a key while a
   keyless run is never blocked by another keyless run. That is the intended
   reading: the key records a property of the request that only the Telegram
   command surface can supply.

2. ``uq_research_runs_user_id_request_key`` — ``UNIQUE (user_id, request_key)``.

   Scoped by ``user_id`` for the same reason ``set_plan`` and ``set_synthesis``
   are ownership-scoped (SR-10): the key is derived from a Telegram user id, so
   scoping it by the owning user means the constraint is a statement about *one
   user's* requests rather than about the whole table. Two different users
   therefore cannot collide even if the same key were derived for both, and no
   index on ``request_key`` alone is ever required.

   Without this constraint nothing stops a duplicated ``/research`` update from
   creating a second logical run, a second enqueued task, and a second terminal
   message for one user request. That is precisely the invariant § R.14 states and
   the one this migration makes unarguable.

**What the database does not enforce here.** The constraint says two rows cannot
share a request identity; it says nothing about whether the *work* those rows
performed is idempotent. Source rows, passage replacement, claim persistence, and
the single terminal notification are idempotency concerns of the pipeline, and
they are handled by the orchestrator against ``uq_sources_run_id_url``,
``uq_source_chunks_source_id_ordinal``, the guarded passage replacement, and the
lifecycle's refusal to reopen a terminal run. This migration closes one hole and
is recorded as doing only that.

``downgrade`` drops the constraint and the one added column, leaving the
P3.1/P3.2/P3.7/P3.8 schema byte-identical.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# Do not import app models here. A migration must keep working after a model
# changes; it describes the schema as it was at this revision, not as the code
# currently believes it to be.

revision: str = "0005_research_request_key"
down_revision: str | None = "0004_research_synthesis"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "research_runs",
        sa.Column("request_key", sa.String(length=32), nullable=True),
    )
    op.create_unique_constraint(
        "uq_research_runs_user_id_request_key",
        "research_runs",
        ["user_id", "request_key"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_research_runs_user_id_request_key",
        "research_runs",
        type_="unique",
    )
    op.drop_column("research_runs", "request_key")