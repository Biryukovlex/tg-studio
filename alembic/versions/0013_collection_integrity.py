"""Collection data-integrity: retired posts, snapshot index, job pruning."""

from alembic import op
import sqlalchemy as sa


revision = "0013_collection_integrity"
down_revision = "0012_channel_system_prompts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(sa.text("SET LOCAL TIME ZONE 'UTC'"))
    op.execute(sa.text('ALTER TABLE posts ADD COLUMN IF NOT EXISTS deleted_at timestamptz'))
    # Cover the latest-snapshot DISTINCT ON (workspace_id, post_id) … ORDER BY id DESC
    # pattern used three times per dashboard request.
    op.execute(
        sa.text(
            'CREATE INDEX IF NOT EXISTS ix_snapshots_workspace_post_id '
            'ON snapshots (workspace_id, post_id, id DESC)'
        )
    )


def downgrade() -> None:
    op.execute(sa.text("SET LOCAL TIME ZONE 'UTC'"))
    op.execute(sa.text('DROP INDEX IF EXISTS ix_snapshots_workspace_post_id'))
    op.execute(sa.text('ALTER TABLE posts DROP COLUMN IF EXISTS deleted_at'))
