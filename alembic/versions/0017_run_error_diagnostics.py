"""Persisted, non-secret run failure diagnostics."""

from alembic import op
import sqlalchemy as sa


revision = "0017_run_error_diagnostics"
down_revision = "0016_research_source_content"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(sa.text("SET LOCAL TIME ZONE 'UTC'"))
    op.execute(sa.text("ALTER TABLE studio_agent_runs ADD COLUMN IF NOT EXISTS error_phase TEXT"))
    op.execute(sa.text("ALTER TABLE studio_agent_runs ADD COLUMN IF NOT EXISTS error_class TEXT"))
    op.execute(sa.text("ALTER TABLE studio_agent_runs ADD COLUMN IF NOT EXISTS error_status INTEGER"))
    op.execute(sa.text("ALTER TABLE studio_agent_runs ADD COLUMN IF NOT EXISTS error_provider_code INTEGER"))


def downgrade() -> None:
    op.execute(sa.text("SET LOCAL TIME ZONE 'UTC'"))
    op.execute(sa.text("ALTER TABLE studio_agent_runs DROP COLUMN IF EXISTS error_provider_code"))
    op.execute(sa.text("ALTER TABLE studio_agent_runs DROP COLUMN IF EXISTS error_status"))
    op.execute(sa.text("ALTER TABLE studio_agent_runs DROP COLUMN IF EXISTS error_class"))
    op.execute(sa.text("ALTER TABLE studio_agent_runs DROP COLUMN IF EXISTS error_phase"))
