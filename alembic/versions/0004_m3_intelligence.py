"""M3 channel intelligence, profiles, and provider configuration consent."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID


revision = "0004_m3_intelligence"
down_revision = "0003_m2_studio"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing M1 consent rows are retained. A configuration fingerprint makes
    # consent revocable when the provider/model endpoint changes without ever
    # storing an API key.
    op.add_column(
        "provider_consents",
        sa.Column("configuration_fingerprint", sa.Text(), nullable=True),
    )
    op.execute(
        sa.text(
            "UPDATE provider_consents SET configuration_fingerprint='legacy:' || user_id::text "
            "WHERE configuration_fingerprint IS NULL"
        )
    )
    op.alter_column("provider_consents", "configuration_fingerprint", nullable=False)
    op.create_unique_constraint(
        "uq_provider_consent_configuration",
        "provider_consents",
        ["workspace_id", "provider", "configuration_fingerprint"],
    )

    op.create_table(
        "studio_analyses",
        sa.Column("id", UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", UUID(as_uuid=True), nullable=False),
        sa.Column("channel_id", sa.BigInteger(), nullable=False),
        sa.Column("analysis_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("analysis_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("eligible_post_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("style_eligible_post_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("evidence_post_ids", JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("scoring_version", sa.Text(), nullable=False),
        sa.Column("scoring_weights", JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("topic_insights", JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("style_insights", JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("limitations", JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("confidence", sa.Text(), server_default="low", nullable=False),
        sa.Column("confidence_score", sa.Float(), server_default="0.25", nullable=False),
        sa.Column("input_hash", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), server_default="local", nullable=False),
        sa.Column("model", sa.Text(), server_default="", nullable=False),
        sa.Column("prompt_version", sa.Text(), server_default="m3.profile.v1", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id", "channel_id"],
            ["channels.workspace_id", "channels.id"],
            ondelete="CASCADE",
            name="fk_studio_analyses_workspace_channel",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_studio_analyses"),
        sa.UniqueConstraint("workspace_id", "id", name="uq_studio_analyses_workspace_id"),
        sa.UniqueConstraint("workspace_id", "channel_id", "input_hash", name="uq_studio_analyses_input"),
    )
    op.create_index(
        "ix_studio_analyses_workspace_channel_created",
        "studio_analyses",
        ["workspace_id", "channel_id", "created_at"],
    )

    op.create_table(
        "studio_profiles",
        sa.Column("id", UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", UUID(as_uuid=True), nullable=False),
        sa.Column("channel_id", sa.BigInteger(), nullable=False),
        sa.Column("topics", JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("style_profile", JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("editorial_rules", JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("confidence", sa.Text(), server_default="low", nullable=False),
        sa.Column("current_analysis_id", UUID(as_uuid=True), nullable=True),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id", "channel_id"],
            ["channels.workspace_id", "channels.id"],
            ondelete="CASCADE",
            name="fk_studio_profiles_workspace_channel",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "current_analysis_id"],
            ["studio_analyses.workspace_id", "studio_analyses.id"],
            ondelete="SET NULL",
            name="fk_studio_profiles_workspace_analysis",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_studio_profiles"),
        sa.UniqueConstraint("workspace_id", "id", name="uq_studio_profiles_workspace_id"),
        sa.UniqueConstraint("workspace_id", "channel_id", name="uq_studio_profiles_channel"),
    )
    op.create_index("ix_studio_profiles_workspace_channel", "studio_profiles", ["workspace_id", "channel_id"])

    op.create_table(
        "studio_profile_changes",
        sa.Column("id", UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", UUID(as_uuid=True), nullable=False),
        sa.Column("channel_id", sa.BigInteger(), nullable=False),
        sa.Column("base_profile_version", sa.Integer(), nullable=False),
        sa.Column("proposed_topics", JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("style_diff", JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("editorial_rules", JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("reason", sa.Text(), server_default="", nullable=False),
        sa.Column("status", sa.Text(), server_default="proposed", nullable=False),
        sa.Column("requested_by", UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["workspace_id", "channel_id"],
            ["channels.workspace_id", "channels.id"],
            ondelete="CASCADE",
            name="fk_studio_profile_changes_workspace_channel",
        ),
        sa.ForeignKeyConstraint(["requested_by"], ["users.id"], ondelete="SET NULL", name="fk_studio_profile_changes_user"),
        sa.PrimaryKeyConstraint("id", name="pk_studio_profile_changes"),
        sa.UniqueConstraint("workspace_id", "id", name="uq_studio_profile_changes_workspace_id"),
        sa.CheckConstraint("status IN ('proposed', 'confirmed', 'applied', 'rejected')", name="ck_studio_profile_change_status"),
    )
    op.create_index(
        "ix_studio_profile_changes_workspace_channel_status",
        "studio_profile_changes",
        ["workspace_id", "channel_id", "status", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_studio_profile_changes_workspace_channel_status", table_name="studio_profile_changes")
    op.drop_table("studio_profile_changes")
    op.drop_index("ix_studio_profiles_workspace_channel", table_name="studio_profiles")
    op.drop_table("studio_profiles")
    op.drop_index("ix_studio_analyses_workspace_channel_created", table_name="studio_analyses")
    op.drop_table("studio_analyses")
    op.drop_constraint("uq_provider_consent_configuration", "provider_consents", type_="unique")
    op.drop_column("provider_consents", "configuration_fingerprint")
