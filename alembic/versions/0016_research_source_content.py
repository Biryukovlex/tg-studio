"""Store bounded read text for verified claim evidence."""

from alembic import op
import sqlalchemy as sa


revision = "0016_research_source_content"
down_revision = "0015_consent_default"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(sa.text("SET LOCAL TIME ZONE 'UTC'"))
    op.execute(sa.text("ALTER TABLE studio_sources ADD COLUMN IF NOT EXISTS content TEXT NOT NULL DEFAULT ''"))


def downgrade() -> None:
    op.execute(sa.text("SET LOCAL TIME ZONE 'UTC'"))
    op.execute(sa.text("ALTER TABLE studio_sources DROP COLUMN IF EXISTS content"))
