"""Store private full Studio tool results for the owner log viewer."""

from alembic import op
import sqlalchemy as sa

revision = "0011_tool_result_logs"
down_revision = "0010_workspace_settings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("studio_run_events", sa.Column("result_content", sa.Text(), nullable=True))
    op.create_index(
        "ix_studio_events_workspace_tool_results",
        "studio_run_events",
        ["workspace_id", "created_at"],
        postgresql_where=sa.text("event_type = 'TOOL_CALL_RESULT' AND result_content IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_studio_events_workspace_tool_results", table_name="studio_run_events")
    op.drop_column("studio_run_events", "result_content")
