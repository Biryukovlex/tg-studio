"""Persist Telegram formatting entities with archived post text."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = "0007_post_formatting"
down_revision = "0006_m5_drafts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "posts",
        sa.Column(
            "formatting_entities",
            JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("posts", "formatting_entities")
