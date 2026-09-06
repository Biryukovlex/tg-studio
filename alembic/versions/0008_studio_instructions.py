"""Workspace-wide Studio instructions."""
from alembic import op
import sqlalchemy as sa

revision = "0008_studio_instructions"
down_revision = "0007_post_formatting"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("workspaces", sa.Column("studio_system_prompt", sa.Text(), nullable=False, server_default=""))


def downgrade() -> None:
    op.drop_column("workspaces", "studio_system_prompt")
