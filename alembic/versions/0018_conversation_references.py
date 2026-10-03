"""Explicit reference-channel selection and permission audit per conversation."""
from alembic import op
revision = '0018_conversation_references'
down_revision = '0017_run_error_diagnostics'
branch_labels = None
depends_on = None

def upgrade():
    op.execute("ALTER TABLE studio_conversations ADD COLUMN IF NOT EXISTS reference_channels JSONB NOT NULL DEFAULT '[]'::jsonb")

def downgrade():
    op.execute('ALTER TABLE studio_conversations DROP COLUMN IF EXISTS reference_channels')
