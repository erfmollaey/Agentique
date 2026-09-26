"""Phase 2 initial schema: users, conversations, messages

Revision ID: 0001_phase2
Revises:
Create Date: 2026-09-26

Three tables for the AI chat MVP: ``users`` -> ``conversations`` -> ``messages``.

Invariants this migration establishes, and why each is a database constraint
rather than an application check:

* ``users.telegram_user_id`` is unique. User resolution is a
  get-or-create on every inbound message, so two workers resolving the same
  identity concurrently must converge on one row (Phase 2 § 8). An
  application-level "does it exist?" check cannot do that.
* ``messages.turn_id`` + ``role`` is unique. A turn pairs one user message with
  one assistant reply. This is the idempotency boundary: a redelivered Telegram
  update and a retried Celery task compute the same ``turn_id`` and collide here
  instead of producing a second reply (Phase 2 § 15, § 16).
* ``messages.(telegram_chat_id, telegram_message_id)`` is unique. The same
  inbound message cannot be persisted twice. Nulls are distinct in PostgreSQL, so
  assistant rows, which carry no Telegram id, are unaffected.
* Foreign keys cascade. Deleting a conversation removes its messages, and
  deleting a user removes their conversations. Resetting a conversation deletes
  only messages, never the user (FR-6).
* ``ck_messages_role_values`` and ``ck_conversations_status_values`` keep the
  closed sets closed for any writer, including a hand-run INSERT.

``conversations.project_id`` is reserved for the Phase 5 project association and
is deliberately not a foreign key: the ``projects`` table does not exist yet, and
a dangling reference now would be implementing Phase 5 (AD-015).

``downgrade`` drops the tables in reverse dependency order, so a rollback leaves
an empty database rather than a broken one (T-18).
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# Do not import app models here. A migration must keep working after a model
# changes; it describes the schema as it was at this revision, not as the code
# currently believes it to be.

revision: str = '0001_phase2'
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('users',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('telegram_user_id', sa.BigInteger(), nullable=False),
    sa.Column('username', sa.String(length=64), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_users'))
    )
    op.create_index(op.f('ix_users_telegram_user_id'), 'users', ['telegram_user_id'], unique=True)
    op.create_table('conversations',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('user_id', sa.BigInteger(), nullable=False),
    sa.Column('title', sa.String(length=200), nullable=True),
    sa.Column('status', sa.String(length=16), server_default='active', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    # Reserved for Phase 5. No foreign key: `projects` does not exist yet.
    sa.Column('project_id', sa.BigInteger(), nullable=True),
    sa.CheckConstraint("status IN ('active', 'archived')", name=op.f('ck_conversations_status_values')),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_conversations_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_conversations'))
    )
    op.create_index(op.f('ix_conversations_project_id'), 'conversations', ['project_id'], unique=False)
    op.create_index(op.f('ix_conversations_user_id'), 'conversations', ['user_id'], unique=False)
    # Retrieval path: "this user's conversations, newest first".
    op.create_index('ix_conversations_user_id_updated_at', 'conversations', ['user_id', 'updated_at'], unique=False)
    op.create_table('messages',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('conversation_id', sa.BigInteger(), nullable=False),
    sa.Column('role', sa.String(length=16), nullable=False),
    sa.Column('content', sa.Text(), nullable=False),
    # Shared by a user message and its assistant reply; the idempotency key.
    sa.Column('turn_id', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('model', sa.String(length=100), nullable=True),
    sa.Column('prompt_tokens', sa.Integer(), server_default='0', nullable=False),
    sa.Column('completion_tokens', sa.Integer(), server_default='0', nullable=False),
    sa.Column('telegram_chat_id', sa.BigInteger(), nullable=True),
    sa.Column('telegram_message_id', sa.BigInteger(), nullable=True),
    sa.CheckConstraint("role IN ('user', 'assistant', 'system')", name=op.f('ck_messages_role_values')),
    sa.ForeignKeyConstraint(['conversation_id'], ['conversations.id'], name=op.f('fk_messages_conversation_id_conversations'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_messages')),
    sa.UniqueConstraint('telegram_chat_id', 'telegram_message_id', name='uq_messages_telegram_message'),
    sa.UniqueConstraint('turn_id', 'role', name='uq_messages_turn_id_role')
    )
    # History retrieval: one conversation, in order. `id` is the tiebreak for two
    # messages committed in the same transaction.
    op.create_index('ix_messages_conversation_id_created_at', 'messages', ['conversation_id', 'created_at', 'id'], unique=False)


def downgrade() -> None:
    # Reverse dependency order, so a rollback leaves an empty schema.
    op.drop_index('ix_messages_conversation_id_created_at', table_name='messages')
    op.drop_table('messages')
    op.drop_index('ix_conversations_user_id_updated_at', table_name='conversations')
    op.drop_index(op.f('ix_conversations_user_id'), table_name='conversations')
    op.drop_index(op.f('ix_conversations_project_id'), table_name='conversations')
    op.drop_table('conversations')
    op.drop_index(op.f('ix_users_telegram_user_id'), table_name='users')
    op.drop_table('users')
