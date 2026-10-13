"""Phase 3 research schema: five research tables

Revision ID: 0002_research
Revises: 0001_phase2
Create Date: 2026-10-07

The frozen Phase 3 persistence model (§ 15.8, AD-034): ``research_runs`` ->
``sources`` -> ``source_chunks``; ``research_runs`` -> ``claims`` ->
``citations``; ``citations`` also reference ``sources`` and ``source_chunks``.

Invariants this migration establishes, and why each is a database constraint
rather than an application check:

* ``research_runs.user_id`` cascades from ``users``. Deleting a user removes
  their entire research subtree (Phase 2 SR-9 extended; without it SR-10 and
  deletion completeness break silently). ``conversation_id`` is ``SET NULL``
  instead: deleting or archiving a conversation must not delete research, and
  deleting the user still cascades through the run row itself.
* All child FKs cascade (``sources``/``claims`` from the run, ``source_chunks``
  from the source, ``citations`` from claim/source/chunk), so one delete of the
  user row removes everything atomically — the same single-statement guarantee
  as the Phase 2 cascade (AD-034).
* ``sources.(run_id, url)`` is unique: a run cannot fetch the same normalized
  URL twice. ``source_chunks.(source_id, ordinal)`` is unique: chunk order is
  unambiguous. ``citations.(claim_id, source_id, chunk_id)`` is unique: no
  duplicate citation of the same passage.
* The FK registry makes a citation to never-persisted evidence uninsertable —
  the structural half of citation integrity (AD-033, SR-9).
* ``ck_research_runs_run_status_values``, ``ck_sources_source_status_values``,
  and ``ck_claims_claim_status_values`` keep the frozen lifecycle sets closed
  for any writer, including a hand-run UPDATE.
* Indexes cover the real retrieval paths: per-user run history
  ``(user_id, created_at)``, ranked sources ``(run_id, rank)``, chunk order
  ``(source_id, ordinal)`` via its unique key, claims ``(run_id)``, citations
  ``(claim_id)`` / ``(source_id)``, and the dedup lookup on
  ``sources.content_hash``.

``downgrade`` drops the tables in reverse dependency order, so a rollback
leaves the Phase 2 schema intact (T-18).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# Do not import app models here. A migration must keep working after a model
# changes; it describes the schema as it was at this revision, not as the code
# currently believes it to be.

revision: str = "0002_research"
down_revision: str | None = "0001_phase2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "research_runs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("conversation_id", sa.BigInteger(), nullable=True),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("plan", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("provider", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=16), server_default="queued", nullable=False),
        sa.Column("failure_summary", sa.Text(), nullable=True),
        sa.Column("final_answer", sa.Text(), nullable=True),
        sa.Column("prompt_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("completion_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'completed', 'partial', 'failed')",
            name=op.f("ck_research_runs_run_status_values"),
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_research_runs_conversation_id_conversations"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_research_runs_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_research_runs")),
    )
    op.create_index(
        op.f("ix_research_runs_conversation_id"), "research_runs", ["conversation_id"], unique=False
    )
    op.create_index(
        "ix_research_runs_user_id_created_at",
        "research_runs",
        ["user_id", "created_at"],
        unique=False,
    )
    op.create_index(op.f("ix_research_runs_user_id"), "research_runs", ["user_id"], unique=False)
    op.create_table(
        "sources",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("domain", sa.String(length=255), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("snippet", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=True),
        sa.Column("rank", sa.Integer(), nullable=True),
        sa.Column("quality_score", sa.Float(), nullable=True),
        sa.Column("duplicate_of_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "retrieved_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'fetched', 'fetch_failed', 'extracted', 'unusable', 'duplicate', 'excluded', 'selected')",
            name=op.f("ck_sources_source_status_values"),
        ),
        sa.ForeignKeyConstraint(
            ["duplicate_of_id"],
            ["sources.id"],
            name=op.f("fk_sources_duplicate_of_id_sources"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["research_runs.id"],
            name=op.f("fk_sources_run_id_research_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sources")),
        sa.UniqueConstraint("run_id", "url", name="uq_sources_run_id_url"),
    )
    op.create_index("ix_sources_content_hash", "sources", ["content_hash"], unique=False)
    op.create_index("ix_sources_run_id_rank", "sources", ["run_id", "rank"], unique=False)
    op.create_table(
        "source_chunks",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("source_id", sa.BigInteger(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("char_start", sa.Integer(), nullable=False),
        sa.Column("char_end", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["sources.id"],
            name=op.f("fk_source_chunks_source_id_sources"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_source_chunks")),
        sa.UniqueConstraint("source_id", "ordinal", name="uq_source_chunks_source_id_ordinal"),
    )
    op.create_table(
        "claims",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="unsupported", nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('supported', 'unsupported', 'contradicted')",
            name=op.f("ck_claims_claim_status_values"),
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["research_runs.id"],
            name=op.f("fk_claims_run_id_research_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_claims")),
    )
    op.create_index("ix_claims_run_id", "claims", ["run_id"], unique=False)
    op.create_table(
        "citations",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("claim_id", sa.BigInteger(), nullable=False),
        sa.Column("source_id", sa.BigInteger(), nullable=False),
        sa.Column("chunk_id", sa.BigInteger(), nullable=False),
        sa.Column("location", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["chunk_id"],
            ["source_chunks.id"],
            name=op.f("fk_citations_chunk_id_source_chunks"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["claim_id"],
            ["claims.id"],
            name=op.f("fk_citations_claim_id_claims"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["sources.id"],
            name=op.f("fk_citations_source_id_sources"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_citations")),
        sa.UniqueConstraint(
            "claim_id", "source_id", "chunk_id", name="uq_citations_claim_source_chunk"
        ),
    )
    op.create_index("ix_citations_claim_id", "citations", ["claim_id"], unique=False)
    op.create_index("ix_citations_source_id", "citations", ["source_id"], unique=False)


def downgrade() -> None:
    # Reverse dependency order, so a rollback leaves the Phase 2 schema.
    op.drop_index("ix_citations_source_id", table_name="citations")
    op.drop_index("ix_citations_claim_id", table_name="citations")
    op.drop_table("citations")
    op.drop_index("ix_claims_run_id", table_name="claims")
    op.drop_table("claims")
    op.drop_table("source_chunks")
    op.drop_index("ix_sources_run_id_rank", table_name="sources")
    op.drop_index("ix_sources_content_hash", table_name="sources")
    op.drop_table("sources")
    op.drop_index(op.f("ix_research_runs_user_id"), table_name="research_runs")
    op.drop_index("ix_research_runs_user_id_created_at", table_name="research_runs")
    op.drop_index(op.f("ix_research_runs_conversation_id"), table_name="research_runs")
    op.drop_table("research_runs")
