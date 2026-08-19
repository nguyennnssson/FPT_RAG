"""Create durable chat history and long-term memory tables.

Revision ID: 20260727_0001
Revises:
Create Date: 2026-07-27
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260727_0001"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "conversations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("user_id", sa.String(256), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("pinned", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.Column("purge_after", sa.DateTime(timezone=True)),
    )
    op.create_index(
        "ix_conversations_owner_updated", "conversations", ["tenant_id", "user_id", "updated_at"]
    )
    op.create_index("ix_conversations_purge_after", "conversations", ["purge_after"])

    op.create_table(
        "messages",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "conversation_id", sa.String(36),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("trace_id", sa.String(128)),
        sa.Column("model", sa.String(256)),
        sa.Column("abstained", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("abstention_reason", sa.Text()),
        sa.Column("feedback", sa.String(8)),
        sa.Column("extra", sa.JSON(), nullable=False),
    )
    op.create_index(
        "ix_messages_conversation_created", "messages", ["conversation_id", "created_at"]
    )

    op.create_table(
        "message_sources",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "message_id", sa.String(36),
            sa.ForeignKey("messages.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(32), nullable=False),
        sa.Column("chunk_id", sa.String(256), nullable=False),
        sa.Column("doc_id", sa.String(256), nullable=False),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("source_uri", sa.Text(), nullable=False),
        sa.Column("section_path", sa.JSON(), nullable=False),
        sa.Column("page_number", sa.Integer()),
        sa.UniqueConstraint("message_id", "ordinal", name="uq_message_source_ordinal"),
    )
    op.create_index("ix_message_sources_message", "message_sources", ["message_id"])

    op.create_table(
        "user_memories",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("user_id", sa.String(256), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("normalized_key", sa.String(64), nullable=False),
        sa.Column("is_explicit", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column(
            "source_conversation_id", sa.String(36),
            sa.ForeignKey("conversations.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "source_message_id", sa.String(36),
            sa.ForeignKey("messages.id", ondelete="SET NULL"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("tenant_id", "user_id", "normalized_key", name="uq_user_memory_key"),
    )
    op.create_index(
        "ix_user_memories_owner_updated", "user_memories", ["tenant_id", "user_id", "updated_at"]
    )


def downgrade() -> None:
    op.drop_table("user_memories")
    op.drop_table("message_sources")
    op.drop_table("messages")
    op.drop_table("conversations")
