"""M5 persistent draft artifacts and append-only versions."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID


revision = "0006_m5_drafts"
down_revision = "0005_m4_research"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "studio_drafts",
        sa.Column("id", UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", UUID(as_uuid=True), nullable=False),
        sa.Column("conversation_id", UUID(as_uuid=True), nullable=False),
        sa.Column("channel_id", sa.BigInteger(), nullable=False),
        sa.Column("story_cluster_id", sa.Text(), nullable=True),
        sa.Column("analysis_id", UUID(as_uuid=True), nullable=True),
        sa.Column("working_title", sa.Text(), server_default="Untitled draft", nullable=False),
        sa.Column("body", sa.Text(), server_default="", nullable=False),
        sa.Column("status", sa.Text(), server_default="working", nullable=False),
        sa.Column("source_ids", JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("claim_support", JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("assumptions", JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("warnings", JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("channel_evidence", JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("web_evidence", JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("confidence", sa.Text(), server_default="low", nullable=False),
        sa.Column("creative", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("provider", sa.Text(), server_default="openrouter", nullable=False),
        sa.Column("model", sa.Text(), server_default="", nullable=False),
        sa.Column("prompt_version", sa.Text(), server_default="m5.draft.v1", nullable=False),
        sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
        sa.Column("current_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("current_version_origin", sa.Text(), server_default="generated", nullable=False),
        sa.Column("copied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id", "conversation_id"],
            ["studio_conversations.workspace_id", "studio_conversations.id"],
            ondelete="CASCADE",
            name="fk_studio_drafts_workspace_conversation",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "channel_id"],
            ["channels.workspace_id", "channels.id"],
            ondelete="CASCADE",
            name="fk_studio_drafts_workspace_channel",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "analysis_id"],
            ["studio_analyses.workspace_id", "studio_analyses.id"],
            ondelete="SET NULL",
            name="fk_studio_drafts_workspace_analysis",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_studio_drafts"),
        sa.UniqueConstraint("workspace_id", "id", name="uq_studio_drafts_workspace_id"),
        sa.CheckConstraint("status IN ('working', 'ready', 'archived')", name="ck_studio_draft_status"),
        sa.CheckConstraint("confidence IN ('high', 'medium', 'low')", name="ck_studio_draft_confidence"),
        sa.CheckConstraint("current_version_origin IN ('generated', 'regenerated', 'user_edit')", name="ck_studio_draft_current_origin"),
    )
    op.create_index(
        "ix_studio_drafts_workspace_conversation_updated",
        "studio_drafts",
        ["workspace_id", "conversation_id", "updated_at"],
    )
    op.create_index("ix_studio_drafts_workspace_channel", "studio_drafts", ["workspace_id", "channel_id"])

    op.create_table(
        "studio_draft_versions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("workspace_id", UUID(as_uuid=True), nullable=False),
        sa.Column("draft_id", UUID(as_uuid=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("body", sa.Text(), server_default="", nullable=False),
        sa.Column("origin", sa.Text(), nullable=False),
        sa.Column("instruction", sa.Text(), server_default="", nullable=False),
        sa.Column("character_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id", "draft_id"],
            ["studio_drafts.workspace_id", "studio_drafts.id"],
            ondelete="CASCADE",
            name="fk_studio_draft_versions_workspace_draft",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_studio_draft_versions"),
        sa.CheckConstraint("origin IN ('generated', 'regenerated', 'user_edit')", name="ck_studio_draft_version_origin"),
        sa.CheckConstraint("character_count >= 0", name="ck_studio_draft_version_char_count"),
        sa.UniqueConstraint("workspace_id", "draft_id", "version", name="uq_studio_draft_versions_version"),
    )
    op.create_index(
        "ix_studio_draft_versions_workspace_draft_created",
        "studio_draft_versions",
        ["workspace_id", "draft_id", "created_at", "id"],
    )

    op.add_column(
        "studio_conversations",
        sa.Column("active_draft_id", UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_studio_conversations_workspace_active_draft",
        "studio_conversations",
        "studio_drafts",
        ["active_draft_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_studio_conversations_workspace_active_draft", "studio_conversations", type_="foreignkey")
    op.drop_column("studio_conversations", "active_draft_id")
    op.drop_index("ix_studio_draft_versions_workspace_draft_created", table_name="studio_draft_versions")
    op.drop_table("studio_draft_versions")
    op.drop_index("ix_studio_drafts_workspace_channel", table_name="studio_drafts")
    op.drop_index("ix_studio_drafts_workspace_conversation_updated", table_name="studio_drafts")
    op.drop_table("studio_drafts")
