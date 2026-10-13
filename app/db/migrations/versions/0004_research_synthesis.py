"""Phase 3 synthesis column: the verified answer on the run

Revision ID: 0004_research_synthesis
Revises: 0003_research_evidence
Create Date: 2026-10-08

P3.8 (FR-35 … FR-39) needs one thing the frozen schema does not have, and this
migration is the smallest change that supplies it. **No table is added**: the
five-table model of AD-034 and § 15.8 is unchanged, and no workspace, registry, or
future-stage table appears here.

1. ``research_runs.synthesis`` — the run's verified, cited answer as JSONB.

   Why a column rather than reuse. The run already carries ``final_answer`` (TEXT),
   and it is not enough for what FR-36 … FR-39 require to be *retrievable*: a text
   blob cannot answer "which citation backs this section", "what is its quote", or
   "what is the source URL", which is what § 15.7 requires to be verifiable against
   persisted content. A structured answer needs a structured home.

   Why JSONB and not a sixth table. A ``research_results`` table would duplicate the
   run's identity, its ownership, its status, and its lifecycle — every one of which
   already exists — while adding a join to reach the row AD-034 was written to avoid.
   § 15.8 explicitly allows transient research concepts to live on the run (the plan
   is JSONB there for exactly this reason), and P3.6 set that precedent. One
   nullable column on the run is the change the frozen contract requires.

   Nullable, with no backfill: every existing run predates this stage, so there is no
   row whose synthesis could be filled in. Inventing one would be precisely the
   failure this column exists to prevent.

**What the database enforces here, and what it does not.** It enforces nothing about
the *content* of the payload: no SQL constraint can compare a stored citation's quote
against a stored passage's text, and it cannot check that a URL matches a source row.
Those are enforced in the application layer — at write time by
``app.domain.research.synthesis.verify_synthesis``, and at read time by
``SynthesizedAnswer.from_payload``, which re-verifies a stored answer against the run's
citation registry. That is stated here rather than implied: the guarantee lives in
code, and the tests read the stored rows back and check it.

``downgrade`` drops the one added column, leaving the P3.1/P3.2/P3.7 schema
byte-identical.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# Do not import app models here. A migration must keep working after a model
# changes; it describes the schema as it was at this revision, not as the code
# currently believes it to be.

revision: str = "0004_research_synthesis"
down_revision: str | None = "0003_research_evidence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The verified, cited answer (FR-35 … FR-39). Nullable: a run exists long before
    # it is synthesized, and nothing about this column's absence is a defect.
    op.add_column(
        "research_runs",
        sa.Column("synthesis", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("research_runs", "synthesis")