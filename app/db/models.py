"""ORM models for the AI chat MVP.

Three tables, matching the conceptual model in Phase 2 § 4:

    User ──< Conversation ──< Message

Design decisions that affect later phases are recorded as AD-015 … AD-019 in
``docs/PROJECT_PLAN.md``. The two that most constrain Phase 3–5:

* **No ``users.current_conversation_id`` pointer.** The active conversation is
  derived by query. A stored pointer would have to be rewritten when Phase 5
  groups conversations into projects (AD-016).
* **Messages are keyed to a conversation only.** A Phase 5 project association
  is therefore one additive column on ``conversations``, not a rewrite of every
  descendant table (AD-015).

Nothing here stores a secret, and no raw provider payload is retained: only the
model name and the token counts the provider reported (FR-30).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

# Roles are a closed set. Enforced in the database as well as in the domain, so
# a bad write cannot be made by any path, including a hand-run INSERT.
_ROLE_VALUES = "role IN ('user', 'assistant', 'system')"
_STATUS_VALUES = "status IN ('active', 'archived')"
_RUN_STATUS_VALUES = "status IN ('queued', 'running', 'completed', 'partial', 'failed')"
_SOURCE_STATUS_VALUES = (
    "status IN ('pending', 'fetched', 'fetch_failed', 'extracted', "
    "'unusable', 'duplicate', 'excluded', 'selected')"
)
_CLAIM_STATUS_VALUES = "status IN ('supported', 'unsupported', 'contradicted')"


def new_turn_id() -> str:
    """A turn groups one user message with the assistant reply to it.

    A UUID rather than a database sequence because a turn is created before
    either of its rows exists, and because it must be identical across a Celery
    retry of the same logical turn.
    """
    return uuid.uuid4().hex


class UserModel(Base):
    """A Telegram user.

    Only the platform identity and an optional display name are stored. The
    Telegram id is the natural key and is unique, which is what makes user
    resolution safe under concurrency (Phase 2 § 8): two racing inserts produce
    one row and one unique violation, not two users.
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    telegram_user_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, unique=True, index=True
    )
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    conversations: Mapped[list[ConversationModel]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    research_runs: Mapped[list[ResearchRunModel]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class ConversationModel(Base):
    """A conversation: an ordered group of messages owned by exactly one user."""

    __tablename__ = "conversations"
    __table_args__ = (
        CheckConstraint(_STATUS_VALUES, name="status_values"),
        # Retrieval path: "this user's conversations, newest first". Without
        # this, listing conversations is a sequential scan per user.
        Index("ix_conversations_user_id_updated_at", "user_id", "updated_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title: Mapped[str | None] = mapped_column(String(200), nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="active", server_default="active"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    # Reserved for Phase 5 project/collection association. Intentionally not a
    # foreign key: the `projects` table does not exist yet and inventing a
    # dangling reference now would be implementing Phase 5 (AD-015).
    project_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)

    user: Mapped[UserModel] = relationship(back_populates="conversations")
    messages: Mapped[list[MessageModel]] = relationship(
        back_populates="conversation", cascade="all, delete-orphan"
    )


class MessageModel(Base):
    """One message in a conversation.

    Ordering is ``(created_at, id)``. ``id`` is the tiebreak because two messages
    committed together can share a timestamp, and history reconstruction must be
    stable (Phase 2 § 10).

    ``turn_id`` carries the idempotency boundary. A user message and its
    assistant reply share one turn id, and ``(turn_id, role)`` is unique, so a
    redelivered Telegram update or a retried Celery task cannot create a second
    assistant message for a turn that already has one (Phase 2 § 15, § 16).
    """

    __tablename__ = "messages"
    __table_args__ = (
        CheckConstraint(_ROLE_VALUES, name="role_values"),
        # The primary access pattern: history for one conversation in order.
        Index("ix_messages_conversation_id_created_at", "conversation_id", "created_at", "id"),
        # Makes "does this turn already have an assistant reply?" a lookup
        # rather than a scan, which is the idempotency check on retry.
        UniqueConstraint("turn_id", "role", name="uq_messages_turn_id_role"),
        # Telegram redelivery. Unique per chat, so the same update cannot be
        # persisted twice. Nullable: assistant messages have no Telegram id.
        UniqueConstraint(
            "telegram_chat_id", "telegram_message_id", name="uq_messages_telegram_message"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    conversation_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    turn_id: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    # Response metadata, for cost visibility (FR-30). Null on user messages.
    model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    prompt_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    completion_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    # Telegram provenance, used only for idempotency. No message payload beyond
    # `content` is retained from the platform.
    telegram_chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    conversation: Mapped[ConversationModel] = relationship(back_populates="messages")


class ResearchRunModel(Base):
    """One research execution, owned by exactly one user (SR-10, T-23).

    ``plan`` is JSONB: the frozen-question decomposition the run executed
    (FR-4). It is nullable at creation because the plan is produced by the
    planning stage, not the intake. ``conversation_id`` keeps the Phase 2
    scoping affordance, but the FK is ``SET NULL``: archiving or deleting a
    conversation must not delete the research it produced, while deleting a
    *user* cascades through runs to every research artifact.

    The status set is a database CHECK, mirroring ``messages.role``: a bad
    write cannot be made by any path, including a hand-run UPDATE.
    """

    __tablename__ = "research_runs"
    __table_args__ = (
        CheckConstraint(_RUN_STATUS_VALUES, name="run_status_values"),
        # Per-user run history, newest first (Phase 5 will build on this).
        Index("ix_research_runs_user_id_created_at", "user_id", "created_at"),
        # B7: the run idempotency boundary. Two rows may not be the same request
        # for the same user, which is what makes a redelivered `/research` update
        # or a retried task converge on one logical run instead of two (AD-017's
        # `turn_id` pattern, applied to research). Nullable: a caller with no
        # Telegram message id has no request to derive a key from, and
        # PostgreSQL treats NULLs as distinct, so keyless runs never block one
        # another. See migration 0005.
        UniqueConstraint("user_id", "request_key", name="uq_research_runs_user_id_request_key"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    conversation_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("conversations.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    question: Mapped[str] = mapped_column(Text, nullable=False)
    # The identity of the *request* that created this run (B7, AD-017's pattern).
    # Derived from the Telegram command's own identifiers, so a redelivered
    # update recomputes it and the unique constraint refuses a second run.
    # Nullable by design — see migration 0005 for why a run may have no key.
    request_key: Mapped[str | None] = mapped_column(String(32), nullable=True)
    plan: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # The verified synthesis (P3.8). Nullable because a run exists long before it is
    # synthesized: P3.2 creates runs, P3.6 plans them, P3.7 extracts their claims,
    # and none of those produces an answer. A run with no synthesis is an ordinary
    # intermediate state, not a defect.
    #
    # Only a **verified** answer may be written here. `SynthesizedAnswer.to_payload()`
    # is the only producer of this shape and is reachable only from an object
    # `verify_synthesis` built, so there is no path by which a raw model proposal or
    # an unvalidated citation reaches this column (§ 16).
    #
    # The text of the answer is also mirrored into `final_answer`, because delivery
    # and run history read that column; the JSONB payload is the authority, since it
    # carries the citations, the quote, and the source URL for each section.
    synthesis: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="queued", server_default="queued"
    )
    failure_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    final_answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    prompt_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    completion_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped[UserModel] = relationship(back_populates="research_runs")
    sources: Mapped[list[SourceModel]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )
    claims: Mapped[list[ClaimModel]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )


class SourceModel(Base):
    """A normalized source for one run (FR-10, FR-11).

    A search result *is* a source at ``pending`` status and a fetch outcome is
    a source state transition — ``SearchResult``/``FetchResult`` are not
    separate tables (AD-034). ``duplicate_of_id`` retains the dedup
    relationship (FR-26); ``content_hash`` backs dedup now and Phase 5's
    cross-run dedup later. ``(run_id, url)`` is unique so a run cannot fetch
    the same normalized URL twice.
    """

    __tablename__ = "sources"
    __table_args__ = (
        CheckConstraint(_SOURCE_STATUS_VALUES, name="source_status_values"),
        UniqueConstraint("run_id", "url", name="uq_sources_run_id_url"),
        # Spec § 6: ranked source listing within a run.
        Index("ix_sources_run_id_rank", "run_id", "rank"),
        # Dedup lookup path (spec § 6).
        Index("ix_sources_content_hash", "content_hash"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("research_runs.id", ondelete="CASCADE"), nullable=False
    )
    url: Mapped[str] = mapped_column(Text, nullable=False)
    domain: Mapped[str] = mapped_column(String(255), nullable=False)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    snippet: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending"
    )
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    quality_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    duplicate_of_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("sources.id", ondelete="SET NULL"), nullable=True
    )
    # The source's own summary (FR-31, P3.7). Nullable because a source exists
    # long before it is summarized: P3.2 creates sources from search results and
    # P3.5 ranks them, neither of which calls a model. A summary is NOT evidence —
    # no citation may point at this column — and the column is TEXT, not a
    # relation, precisely so it cannot be cited by accident.
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    retrieved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    run: Mapped[ResearchRunModel] = relationship(back_populates="sources")
    chunks: Mapped[list[SourceChunkModel]] = relationship(
        back_populates="source", cascade="all, delete-orphan"
    )


class SourceChunkModel(Base):
    """A persisted evidence passage from a source (FR-22).

    ``(source_id, ordinal)`` is the chunk order of one source; ``char_start``
    / ``char_end`` locate the passage in the extracted text, which is what
    lets a citation name a specific location (FR-37).

    ``uq_source_chunks_id_source_id`` exists for the composite foreign key on
    ``citations`` added at P3.7: PostgreSQL can only reference a *unique* column
    set, so ``(id, source_id)`` must be declared unique for the ownership
    constraint to be expressible at all. It is trivially true — ``id`` is the
    primary key — and it exists only to carry that constraint.
    """

    __tablename__ = "source_chunks"
    __table_args__ = (
        UniqueConstraint("source_id", "ordinal", name="uq_source_chunks_source_id_ordinal"),
        UniqueConstraint("id", "source_id", name="uq_source_chunks_id_source_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("sources.id", ondelete="CASCADE"), nullable=False
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    char_start: Mapped[int] = mapped_column(Integer, nullable=False)
    char_end: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    source: Mapped[SourceModel] = relationship(back_populates="chunks")


class ClaimModel(Base):
    """A claim extracted for a run, with its support verdict (FR-32, FR-39).

    The default verdict is ``unsupported``: a claim is never assumed to be
    backed until a citation verifies it.
    """

    __tablename__ = "claims"
    __table_args__ = (
        CheckConstraint(_CLAIM_STATUS_VALUES, name="claim_status_values"),
        Index("ix_claims_run_id", "run_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("research_runs.id", ondelete="CASCADE"), nullable=False
    )
    text: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="unsupported", server_default="unsupported"
    )
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    run: Mapped[ResearchRunModel] = relationship(back_populates="claims")
    citations: Mapped[list[CitationModel]] = relationship(
        back_populates="claim", cascade="all, delete-orphan"
    )


class CitationModel(Base):
    """The programmatic claim → evidence relationship (FR-36, FR-37, SR-9).

    FKs into ``claims``, ``sources``, and ``source_chunks`` make a citation
    to content never persisted uninsertable — the FK registry is the
    structural half of citation integrity (AD-033). The unique key prevents
    duplicate citations of the same passage for one claim.

    Two constraints added at P3.7, both of them expressions of the same rule
    "a citation names evidence, and the evidence must be that source's passage":

    * ``quote`` is ``NOT NULL``. A citation with no passage is a link to
      nothing, which is the failure AD-033 exists to prevent. The *containment*
      rule — that the quote is really inside ``source_chunks.text`` — is not
      expressible in SQL (it compares one row's text against another's), so it
      is enforced in the application layer immediately before the insert and
      re-checked against the loaded passage; see
      ``app.domain.research.evidence.verify_claims``.
    * ``fk_citations_chunk_id_source_id_source_chunks`` is a composite foreign
      key on ``(chunk_id, source_id)``, so the database itself refuses a
      citation whose chunk belongs to a different source. Without it the two
      independent foreign keys would happily accept ``source_id`` of one source
      with ``chunk_id`` of another, which is exactly the cross-source evidence
      link § 14 forbids.
    """

    __tablename__ = "citations"
    __table_args__ = (
        UniqueConstraint(
            "claim_id", "source_id", "chunk_id", name="uq_citations_claim_source_chunk"
        ),
        ForeignKeyConstraint(
            ["chunk_id", "source_id"],
            ["source_chunks.id", "source_chunks.source_id"],
            name="fk_citations_chunk_id_source_id_source_chunks",
            ondelete="CASCADE",
        ),
        Index("ix_citations_claim_id", "claim_id"),
        Index("ix_citations_source_id", "source_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    claim_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("claims.id", ondelete="CASCADE"), nullable=False
    )
    source_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("sources.id", ondelete="CASCADE"), nullable=False
    )
    chunk_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("source_chunks.id", ondelete="CASCADE"), nullable=False
    )
    quote: Mapped[str] = mapped_column(Text, nullable=False)
    location: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    claim: Mapped[ClaimModel] = relationship(back_populates="citations")
