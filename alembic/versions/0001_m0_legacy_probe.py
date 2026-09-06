"""M0 PostgreSQL probe schema mirroring the legacy SQLite archive.

This is intentionally a small compatibility proof, not the final application
schema. M1 will replace/evolve it with workspace-owned SQLAlchemy models.
"""

from alembic import op
import sqlalchemy as sa


revision = "0001_m0_legacy_probe"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "channels",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("identifier", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), server_default="", nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=True),
        sa.Column("active", sa.Integer(), server_default="1", nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_m0_channels"),
        sa.UniqueConstraint("identifier", name="uq_m0_channels_identifier"),
    )
    op.create_table(
        "posts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("channel_id", sa.Integer(), nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=False),
        sa.Column("posted_at", sa.Text(), nullable=False),
        sa.Column("text", sa.Text(), server_default="", nullable=False),
        sa.ForeignKeyConstraint(
            ["channel_id"], ["channels.id"], ondelete="CASCADE", name="fk_m0_posts_channel"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_m0_posts"),
        sa.UniqueConstraint(
            "channel_id", "message_id", name="uq_m0_posts_channel_message"
        ),
    )
    op.create_index("ix_m0_posts_channel_date", "posts", ["channel_id", "posted_at"])
    op.create_table(
        "snapshots",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("post_id", sa.Integer(), nullable=False),
        sa.Column("taken_at", sa.Text(), nullable=False),
        sa.Column("views", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("comments", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("reactions", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("shares", sa.BigInteger(), server_default="0", nullable=False),
        sa.ForeignKeyConstraint(
            ["post_id"], ["posts.id"], ondelete="CASCADE", name="fk_m0_snapshots_post"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_m0_snapshots"),
    )
    op.create_index("ix_m0_snapshots_post_time", "snapshots", ["post_id", "taken_at"])
    op.create_table(
        "comments",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("post_id", sa.Integer(), nullable=False),
        sa.Column("telegram_message_id", sa.BigInteger(), nullable=False),
        sa.Column("discussion_chat_id", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("discussion_username", sa.Text(), server_default="", nullable=False),
        sa.Column("sender_id", sa.BigInteger(), nullable=True),
        sa.Column("sender_name", sa.Text(), server_default="", nullable=False),
        sa.Column("sender_username", sa.Text(), server_default="", nullable=False),
        sa.Column("posted_at", sa.Text(), nullable=False),
        sa.Column("edited_at", sa.Text(), nullable=True),
        sa.Column("text", sa.Text(), server_default="", nullable=False),
        sa.Column("media_type", sa.Text(), server_default="", nullable=False),
        sa.Column("reactions", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("reply_to_message_id", sa.BigInteger(), nullable=True),
        sa.Column("is_deleted", sa.Integer(), server_default="0", nullable=False),
        sa.Column("first_collected_at", sa.Text(), nullable=False),
        sa.Column("last_collected_at", sa.Text(), nullable=False),
        sa.Column("last_seen_sync", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["post_id"], ["posts.id"], ondelete="CASCADE", name="fk_m0_comments_post"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_m0_comments"),
        sa.UniqueConstraint(
            "post_id", "telegram_message_id", name="uq_m0_comments_post_message"
        ),
    )
    op.create_index("ix_m0_comments_post_date", "comments", ["post_id", "posted_at"])
    op.create_index("ix_m0_comments_sender", "comments", ["sender_id"])


def downgrade() -> None:
    op.drop_index("ix_m0_comments_sender", table_name="comments")
    op.drop_index("ix_m0_comments_post_date", table_name="comments")
    op.drop_table("comments")
    op.drop_index("ix_m0_snapshots_post_time", table_name="snapshots")
    op.drop_table("snapshots")
    op.drop_index("ix_m0_posts_channel_date", table_name="posts")
    op.drop_table("posts")
    op.drop_table("channels")
