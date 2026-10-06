"""Private image assets, draft media versions and durable publishing commands."""
from alembic import op
revision = '0019_calendar_publishing'
down_revision = '0018_conversation_references'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE studio_drafts ADD COLUMN media_ids JSONB NOT NULL DEFAULT '[]'::jsonb")
    op.execute("ALTER TABLE studio_draft_versions ADD COLUMN media_ids JSONB NOT NULL DEFAULT '[]'::jsonb")
    op.execute('''CREATE TABLE studio_media (
        id UUID PRIMARY KEY, workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
        draft_id UUID, filename TEXT NOT NULL, content BYTEA NOT NULL,
        content_type TEXT NOT NULL DEFAULT 'image/jpeg', width INTEGER NOT NULL, height INTEGER NOT NULL,
        sha256 TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE(workspace_id,id),
        FOREIGN KEY(workspace_id,draft_id) REFERENCES studio_drafts(workspace_id,id) ON DELETE SET NULL (draft_id),
        CHECK(octet_length(content) <= 10485760), CHECK(width > 0 AND height > 0)
    )''')
    op.execute('''CREATE TABLE publishing_channels (
        workspace_id UUID NOT NULL, channel_id BIGINT NOT NULL, allowed BOOLEAN NOT NULL DEFAULT false,
        account_id BIGINT, reason TEXT NOT NULL DEFAULT 'Checking publishing permissions', caption_limit INTEGER NOT NULL DEFAULT 1024,
        checked_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY(workspace_id,channel_id),
        FOREIGN KEY(workspace_id,channel_id) REFERENCES channels(workspace_id,id) ON DELETE CASCADE
    )''')
    op.execute('''CREATE TABLE scheduled_posts (
        id UUID PRIMARY KEY, workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
        channel_id BIGINT NOT NULL, telegram_user_id BIGINT NOT NULL, draft_id UUID, conversation_id UUID,
        snapshot JSONB NOT NULL, pending_snapshot JSONB, parts JSONB NOT NULL DEFAULT '[]'::jsonb,
        scheduled_at TIMESTAMPTZ NOT NULL, timezone TEXT NOT NULL, pending_at TIMESTAMPTZ, pending_timezone TEXT,
        status TEXT NOT NULL DEFAULT 'queued', action TEXT, revision INTEGER NOT NULL DEFAULT 1,
        idempotency_key UUID NOT NULL, lease_token UUID, lease_until TIMESTAMPTZ,
        error TEXT NOT NULL DEFAULT '', next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE(workspace_id,id), UNIQUE(workspace_id,idempotency_key),
        FOREIGN KEY(workspace_id,channel_id) REFERENCES channels(workspace_id,id),
        CHECK(status IN ('queued','transferring','scheduled','updating','cancelling','published','cancelled','failed','needs_review')),
        CHECK(action IS NULL OR action IN ('schedule','update','cancel'))
    )''')
    op.execute('CREATE INDEX ix_scheduled_posts_calendar ON scheduled_posts(workspace_id,scheduled_at,id)')
    op.execute('CREATE INDEX ix_scheduled_posts_commands ON scheduled_posts(workspace_id,next_attempt_at) WHERE action IS NOT NULL')
    op.execute('''CREATE TABLE publishing_events (
        id BIGSERIAL PRIMARY KEY, workspace_id UUID NOT NULL, post_id UUID NOT NULL,
        status TEXT NOT NULL, message TEXT NOT NULL DEFAULT '', created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        FOREIGN KEY(workspace_id,post_id) REFERENCES scheduled_posts(workspace_id,id) ON DELETE CASCADE
    )''')


def downgrade():
    for table in ('publishing_events', 'scheduled_posts', 'publishing_channels', 'studio_media'):
        op.execute(f'DROP TABLE {table}')
    op.execute('ALTER TABLE studio_draft_versions DROP COLUMN media_ids')
    op.execute('ALTER TABLE studio_drafts DROP COLUMN media_ids')
