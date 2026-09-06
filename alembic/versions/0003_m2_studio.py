"""M2 workspace-scoped Studio conversations, runs, and safe events."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID


revision = "0003_m2_studio"
down_revision = "0002_m1_foundation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "studio_conversations",
        sa.Column("id", UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", UUID(as_uuid=True), nullable=False),
        sa.Column("channel_id", sa.BigInteger(), nullable=False),
        sa.Column("title", sa.Text(), server_default="New conversation", nullable=False),
        sa.Column("summary", sa.Text(), server_default="", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["workspace_id", "channel_id"],
            ["channels.workspace_id", "channels.id"],
            ondelete="CASCADE",
            name="fk_studio_conversations_workspace_channel",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_studio_conversations"),
        sa.UniqueConstraint("workspace_id", "id", name="uq_studio_conversations_workspace_id"),
    )
    op.create_index(
        "ix_studio_conversations_workspace_updated",
        "studio_conversations",
        ["workspace_id", "updated_at"],
    )

    op.create_table(
        "studio_messages",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("workspace_id", UUID(as_uuid=True), nullable=False),
        sa.Column("conversation_id", UUID(as_uuid=True), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("metadata_json", JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id", "conversation_id"],
            ["studio_conversations.workspace_id", "studio_conversations.id"],
            ondelete="CASCADE",
            name="fk_studio_messages_workspace_conversation",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_studio_messages"),
        sa.UniqueConstraint("workspace_id", "id", name="uq_studio_messages_workspace_id"),
    )
    op.create_index(
        "ix_studio_messages_workspace_conversation_time",
        "studio_messages",
        ["workspace_id", "conversation_id", "created_at", "id"],
    )

    op.create_table(
        "studio_agent_runs",
        sa.Column("id", UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", UUID(as_uuid=True), nullable=False),
        sa.Column("conversation_id", UUID(as_uuid=True), nullable=False),
        sa.Column("user_message_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.Text(), server_default="queued", nullable=False),
        sa.Column("stage", sa.Text(), server_default="queued", nullable=False),
        sa.Column("provider", sa.Text(), server_default="openrouter", nullable=False),
        sa.Column("requested_model", sa.Text(), nullable=False),
        sa.Column("actual_model", sa.Text(), nullable=True),
        sa.Column("usage", JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("worker_id", sa.Text(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_requested", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["workspace_id", "conversation_id"],
            ["studio_conversations.workspace_id", "studio_conversations.id"],
            ondelete="CASCADE",
            name="fk_studio_runs_workspace_conversation",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "user_message_id"],
            ["studio_messages.workspace_id", "studio_messages.id"],
            ondelete="CASCADE",
            name="fk_studio_runs_workspace_message",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_studio_agent_runs"),
        sa.UniqueConstraint("workspace_id", "id", name="uq_studio_runs_workspace_id"),
    )
    op.create_index(
        "uq_studio_runs_active_conversation",
        "studio_agent_runs",
        ["workspace_id", "conversation_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )
    op.create_index(
        "ix_studio_runs_workspace_status",
        "studio_agent_runs",
        ["workspace_id", "status", "created_at"],
    )

    op.create_table(
        "studio_run_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("workspace_id", UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", UUID(as_uuid=True), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("safe_payload", JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id", "run_id"],
            ["studio_agent_runs.workspace_id", "studio_agent_runs.id"],
            ondelete="CASCADE",
            name="fk_studio_events_workspace_run",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_studio_run_events"),
        sa.UniqueConstraint("workspace_id", "run_id", "sequence", name="uq_studio_events_run_sequence"),
    )
    op.create_index(
        "ix_studio_events_workspace_run_sequence",
        "studio_run_events",
        ["workspace_id", "run_id", "sequence"],
    )


def downgrade() -> None:
    op.drop_index("ix_studio_events_workspace_run_sequence", table_name="studio_run_events")
    op.drop_table("studio_run_events")
    op.drop_index("ix_studio_runs_workspace_status", table_name="studio_agent_runs")
    op.drop_index("uq_studio_runs_active_conversation", table_name="studio_agent_runs")
    op.drop_table("studio_agent_runs")
    op.drop_index("ix_studio_messages_workspace_conversation_time", table_name="studio_messages")
    op.drop_table("studio_messages")
    op.drop_index("ix_studio_conversations_workspace_updated", table_name="studio_conversations")
    op.drop_table("studio_conversations")

