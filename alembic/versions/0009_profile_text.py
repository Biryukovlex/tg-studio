"""Profile text fields."""
from alembic import op
import sqlalchemy as sa

revision = "0009_profile_text"
down_revision = "0008_studio_instructions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("studio_profiles", sa.Column("topics_text", sa.Text(), nullable=False, server_default=""))
    op.add_column("studio_profiles", sa.Column("editorial_text", sa.Text(), nullable=False, server_default=""))
    op.add_column("studio_profiles", sa.Column("style_text", sa.Text(), nullable=False, server_default=""))
    op.add_column("studio_profiles", sa.Column("built_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("studio_profiles", sa.Column("built_from_posts", sa.Integer(), nullable=False, server_default="0"))
    # Migrate existing topics JSON to topics_text for existing rows
    op.execute("""
        UPDATE studio_profiles
        SET topics_text = (
            SELECT string_agg(elem->>'name' || ' — ' || COALESCE(elem->>'scope', ''), E'\n')
            FROM jsonb_array_elements(topics) AS elem
        )
        WHERE topics_text = '' AND topics IS NOT NULL AND jsonb_array_length(topics) > 0
    """)


def downgrade() -> None:
    op.drop_column("studio_profiles", "built_from_posts")
    op.drop_column("studio_profiles", "built_at")
    op.drop_column("studio_profiles", "style_text")
    op.drop_column("studio_profiles", "editorial_text")
    op.drop_column("studio_profiles", "topics_text")
