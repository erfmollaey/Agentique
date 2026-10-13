"""Phase 3 evidence columns: the summary, the quote, and chunk/source ownership

Revision ID: 0003_research_evidence
Revises: 0002_research
Create Date: 2026-10-08

P3.7 (FR-31, FR-32) needs three things the frozen schema does not yet have, and
this migration is the smallest change that supplies them. **No table is added**:
the five-table model of AD-034 and § 15.8 is unchanged, and no workspace,
citation-registry, or future-stage table appears here.

1. ``sources.summary`` — a per-source summary has to persist somewhere. FR-31
   requires each retained source to be summarized, and the summary is a property
   of the source, not of a run or a claim. Nullable: a source is persisted by
   P3.2 and ranked by P3.5 long before any model is involved, so a source without
   a summary is an ordinary state, not a defect.

2. ``citations.quote`` — the verified passage. § 15.7 requires claim-to-passage
   support to be verifiable against stored content, and the frozen schema had
   nowhere to keep the passage: ``citations.location`` is a 255-character
   human-renderable locator, which is a different thing. The column is
   ``NOT NULL``, which is the point of adding it: a citation row that must carry
   the passage it cites cannot be written without one (AD-033, SR-9).

   The table is empty at this revision. No code has ever written a citation —
   P3.8, which builds citations for an answer, has not run — so the column can be
   added ``NOT NULL`` without a backfill. A backfill would have had to invent a
   passage, which is precisely the failure this stage prevents.

3. ``uq_source_chunks_id_source_id`` + ``fk_citations_chunk_id_source_id_source_chunks``
   — chunk/source ownership as a database constraint. Before this revision
   ``citations.chunk_id`` and ``citations.source_id`` were two independent
   foreign keys, so the database would have accepted a citation whose ``source_id``
   named one source and whose ``chunk_id`` named a passage of a *different* one —
   precisely the cross-source evidence link § 14 forbids. The composite foreign
   key on ``(chunk_id, source_id)`` makes that insert fail. PostgreSQL can only
   reference a unique column set, hence the extra unique constraint; it is
   trivially satisfied because ``id`` is already the primary key.

**Not enforced here, and stated rather than pretended:** the textual containment
rule ``quote ∈ source_chunks.text``. No SQL constraint can compare one row's text
against another's, so it is enforced in the application layer immediately before
the insert (``app.domain.research.evidence.verify_claims``) and re-checked against
the loaded passage, with tests. Everything else about a citation is a constraint.

``downgrade`` reverses all three, in reverse dependency order: the composite
foreign key is dropped before the unique constraint it depends on, and the added
columns are dropped last. A rollback leaves the P3.1/P3.2 schema byte-identical.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# Do not import app models here. A migration must keep working after a model
# changes; it describes the schema as it was at this revision, not as the code
# currently believes it to be.

revision: str = "0003_research_evidence"
down_revision: str | None = "0002_research"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. The per-source summary (FR-31).
    op.add_column(
        "sources",
        sa.Column("summary", sa.Text(), nullable=True),
    )

    # 2. The verified passage on every citation (AD-033, SR-9).
    op.add_column(
        "citations",
        sa.Column("quote", sa.Text(), nullable=False),
    )

    # 3. Chunk/source ownership, as a constraint rather than an application check.
    op.create_unique_constraint(
        "uq_source_chunks_id_source_id", "source_chunks", ["id", "source_id"]
    )
    op.create_foreign_key(
        "fk_citations_chunk_id_source_id_source_chunks",
        "citations",
        "source_chunks",
        ["chunk_id", "source_id"],
        ["id", "source_id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_citations_chunk_id_source_id_source_chunks", "citations", type_="foreignkey"
    )
    op.drop_constraint("uq_source_chunks_id_source_id", "source_chunks", type_="unique")
    op.drop_column("citations", "quote")
    op.drop_column("sources", "summary")