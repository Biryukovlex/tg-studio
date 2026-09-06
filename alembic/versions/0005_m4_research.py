"""M4 durable source provenance, story clusters, and research activity."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID


revision = "0005_m4_research"
down_revision = "0004_m3_intelligence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "studio_sources",
        sa.Column("workspace_id", UUID(as_uuid=True), nullable=False),
        sa.Column("conversation_id", UUID(as_uuid=True), nullable=False),
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("channel_id", sa.BigInteger(), nullable=False),
        sa.Column("url", sa.Text(), server_default="", nullable=False),
        sa.Column("canonical_url", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), server_default="", nullable=False),
        sa.Column("publisher", sa.Text(), server_default="", nullable=False),
        sa.Column("domain", sa.Text(), server_default="", nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("excerpt", sa.Text(), server_default="", nullable=False),
        sa.Column("content_hash", sa.Text(), server_default="", nullable=False),
        sa.Column("provider", sa.Text(), server_default="", nullable=False),
        sa.Column("query", sa.Text(), server_default="", nullable=False),
        sa.Column("accessible", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("status", sa.Text(), server_default="ok", nullable=False),
        sa.Column("warnings", JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("injection_flags", JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("metadata_json", JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("quality_score", sa.Float(), server_default="0", nullable=False),
        sa.Column("quality_notes", JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id", "conversation_id"],
            ["studio_conversations.workspace_id", "studio_conversations.id"],
            ondelete="CASCADE",
            name="fk_studio_sources_workspace_conversation",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "channel_id"],
            ["channels.workspace_id", "channels.id"],
            ondelete="CASCADE",
            name="fk_studio_sources_workspace_channel",
        ),
        sa.PrimaryKeyConstraint("workspace_id", "conversation_id", "id", name="pk_studio_sources"),
    )
    op.create_index(
        "ix_studio_sources_workspace_conversation_retrieved",
        "studio_sources",
        ["workspace_id", "conversation_id", "retrieved_at"],
    )
    op.create_index("ix_studio_sources_workspace_channel", "studio_sources", ["workspace_id", "channel_id"])

    op.create_table(
        "studio_story_clusters",
        sa.Column("workspace_id", UUID(as_uuid=True), nullable=False),
        sa.Column("conversation_id", UUID(as_uuid=True), nullable=False),
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("channel_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.Text(), server_default="new", nullable=False),
        sa.Column("topic_ids", JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("headline", sa.Text(), server_default="", nullable=False),
        sa.Column("summary", sa.Text(), server_default="", nullable=False),
        sa.Column("source_ids", JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("primary_source_id", sa.Text(), nullable=True),
        sa.Column("supporting_source_ids", JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("score", sa.Float(), server_default="0", nullable=False),
        sa.Column("topic_relevance", sa.Float(), server_default="0", nullable=False),
        sa.Column("freshness", sa.Float(), server_default="0", nullable=False),
        sa.Column("source_quality", sa.Float(), server_default="0", nullable=False),
        sa.Column("novelty", sa.Float(), server_default="0", nullable=False),
        sa.Column("score_breakdown", JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("matched_topics", JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("relevance_features", JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("novelty_features", JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("conflict_flags", JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("warnings", JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("channel_evidence", JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("content_fingerprint", sa.Text(), server_default="", nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id", "conversation_id"],
            ["studio_conversations.workspace_id", "studio_conversations.id"],
            ondelete="CASCADE",
            name="fk_studio_story_clusters_workspace_conversation",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "channel_id"],
            ["channels.workspace_id", "channels.id"],
            ondelete="CASCADE",
            name="fk_studio_story_clusters_workspace_channel",
        ),
        sa.PrimaryKeyConstraint("workspace_id", "conversation_id", "id", name="pk_studio_story_clusters"),
        sa.CheckConstraint("status IN ('new', 'saved', 'dismissed', 'used')", name="ck_studio_story_cluster_status"),
        sa.UniqueConstraint("workspace_id", "conversation_id", "content_fingerprint", name="uq_studio_story_cluster_fingerprint"),
    )
    op.create_index(
        "ix_studio_story_clusters_workspace_conversation_score",
        "studio_story_clusters",
        ["workspace_id", "conversation_id", "score"],
    )
    op.create_index("ix_studio_story_clusters_workspace_channel", "studio_story_clusters", ["workspace_id", "channel_id"])

    op.create_table(
        "studio_research_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("workspace_id", UUID(as_uuid=True), nullable=False),
        sa.Column("conversation_id", UUID(as_uuid=True), nullable=False),
        sa.Column("channel_id", sa.BigInteger(), nullable=False),
        sa.Column("event_type", sa.Text(), server_default="search", nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("query_count", sa.Integer(), server_default="1", nullable=False),
        sa.Column("result_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("cache_hit", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("degraded", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("trace_id", sa.Text(), nullable=False),
        sa.Column("metadata_json", JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id", "conversation_id"],
            ["studio_conversations.workspace_id", "studio_conversations.id"],
            ondelete="CASCADE",
            name="fk_studio_research_events_workspace_conversation",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "channel_id"],
            ["channels.workspace_id", "channels.id"],
            ondelete="CASCADE",
            name="fk_studio_research_events_workspace_channel",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_studio_research_events"),
    )
    op.create_index(
        "ix_studio_research_events_workspace_conversation_created",
        "studio_research_events",
        ["workspace_id", "conversation_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_studio_research_events_workspace_conversation_created", table_name="studio_research_events")
    op.drop_table("studio_research_events")
    op.drop_index("ix_studio_story_clusters_workspace_channel", table_name="studio_story_clusters")
    op.drop_index("ix_studio_story_clusters_workspace_conversation_score", table_name="studio_story_clusters")
    op.drop_table("studio_story_clusters")
    op.drop_index("ix_studio_sources_workspace_channel", table_name="studio_sources")
    op.drop_index("ix_studio_sources_workspace_conversation_retrieved", table_name="studio_sources")
    op.drop_table("studio_sources")
