"""Store Studio system prompts per Telegram channel."""

from alembic import op
import sqlalchemy as sa


revision = "0012_channel_system_prompts"
down_revision = "0011_tool_result_logs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "channels",
        sa.Column("studio_system_prompt", sa.Text(), nullable=False, server_default=""),
    )
    # Preserve the owner's existing workspace prompt as the initial prompt for
    # every current channel. Subsequent edits are strictly channel-local.
    op.execute(
        """
        UPDATE channels AS channel
           SET studio_system_prompt = workspace.studio_system_prompt
          FROM workspaces AS workspace
         WHERE channel.workspace_id = workspace.id
           AND workspace.studio_system_prompt <> ''
        """
    )


def downgrade() -> None:
    op.drop_column("channels", "studio_system_prompt")
