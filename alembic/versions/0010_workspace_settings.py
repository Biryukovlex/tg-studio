"""Workspace settings store."""

from alembic import op
import sqlalchemy as sa

revision = "0010_workspace_settings"
down_revision = "0009_profile_text"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workspace_settings",
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("is_secret", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("workspace_id", "key"),
    )
    op.create_index("ix_workspace_settings_workspace_id", "workspace_settings", ["workspace_id"])


def downgrade() -> None:
    op.drop_index("ix_workspace_settings_workspace_id", table_name="workspace_settings")
    op.drop_table("workspace_settings")
