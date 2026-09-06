"""M1 workspace-owned PostgreSQL foundation.

The M0 probe already created the four archive tables.  This migration evolves
those tables in place so a clean database and an existing probe database both
reach the same workspace-scoped shape without a destructive data rewrite.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects import postgresql


revision = "0002_m1_foundation"
down_revision = "0001_m0_legacy_probe"
branch_labels = None
depends_on = None

COMMUNITY_WORKSPACE = "4caae167-01d3-5296-949b-ad09ec5eb8df"


def _add_if_missing(table: str, column: sa.Column) -> None:
    # PostgreSQL's IF NOT EXISTS keeps a retried deployment safe.  Alembic
    # normally runs once, but this also makes manually interrupted upgrades
    # recoverable.
    rendered_type = column.type.compile(dialect=postgresql.dialect())
    op.execute(sa.text(f'ALTER TABLE "{table}" ADD COLUMN IF NOT EXISTS "{column.name}" {rendered_type}'))


def upgrade() -> None:
    op.create_table(
        "workspaces",
        sa.Column("id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_workspaces"),
        sa.UniqueConstraint("slug", name="uq_workspaces_slug"),
    )
    op.create_table(
        "users",
        sa.Column("id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("username", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), server_default="", nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
        sa.UniqueConstraint("username", name="uq_users_username"),
    )
    op.create_table(
        "memberships",
        sa.Column("workspace_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("role", sa.Text(), server_default="member", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE", name="fk_memberships_workspace"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE", name="fk_memberships_user"),
        sa.PrimaryKeyConstraint("workspace_id", "user_id", name="pk_memberships"),
    )
    op.create_table(
        "provider_consents",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("workspace_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("allowed", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("granted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE", name="fk_provider_consents_workspace"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE", name="fk_provider_consents_user"),
        sa.PrimaryKeyConstraint("id", name="pk_provider_consents"),
        sa.UniqueConstraint("workspace_id", "user_id", "provider", name="uq_provider_consent"),
    )
    op.create_table(
        "telegram_connections",
        sa.Column("id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("label", sa.Text(), server_default="default", nullable=False),
        sa.Column("api_id", sa.Integer(), nullable=False),
        sa.Column("api_hash", sa.Text(), nullable=False),
        sa.Column("encrypted_session", sa.LargeBinary(), nullable=True),
        sa.Column("session_key_version", sa.Text(), server_default="v1", nullable=False),
        sa.Column("session_fingerprint", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), server_default="active", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE", name="fk_telegram_connections_workspace"),
        sa.PrimaryKeyConstraint("id", name="pk_telegram_connections"),
        sa.UniqueConstraint("workspace_id", "label", name="uq_telegram_connection_label"),
        sa.UniqueConstraint("workspace_id", "id", name="uq_telegram_connections_workspace_id"),
    )

    # The M0 tables are populated only after adding a deterministic community
    # workspace.  Columns are nullable while data is backfilled, then made
    # required so all future repository writes carry tenant context.
    op.execute(
        sa.text(
            f"INSERT INTO workspaces(id, slug, name) VALUES ('{COMMUNITY_WORKSPACE}'::uuid, 'community', 'Community') "
            "ON CONFLICT (id) DO NOTHING"
        )
    )
    for table in ("channels", "posts", "snapshots", "comments"):
        _add_if_missing(table, sa.Column("workspace_id", sa.UUID(as_uuid=True), nullable=True))

    for table in ("channels", "posts", "snapshots", "comments"):
        op.execute(sa.text(f"UPDATE {table} SET workspace_id='{COMMUNITY_WORKSPACE}'::uuid WHERE workspace_id IS NULL"))

    # Complete the archive columns before constraints are tightened.
    _add_if_missing("channels", sa.Column("telegram_connection_id", sa.UUID(as_uuid=True), nullable=True))
    _add_if_missing("channels", sa.Column("created_at", sa.DateTime(timezone=True), nullable=True))
    _add_if_missing("posts", sa.Column("is_deleted", sa.Boolean(), nullable=True))
    _add_if_missing("posts", sa.Column("created_at", sa.DateTime(timezone=True), nullable=True))
    op.execute(sa.text("UPDATE channels SET created_at=now() WHERE created_at IS NULL"))
    op.execute(sa.text("UPDATE posts SET created_at=now(), is_deleted=false WHERE created_at IS NULL OR is_deleted IS NULL"))

    # Convert legacy text/0-1 values to their production PostgreSQL types.
    op.execute(sa.text("ALTER TABLE channels ALTER COLUMN active DROP DEFAULT"))
    op.execute(sa.text("ALTER TABLE channels ALTER COLUMN active TYPE boolean USING active <> 0"))
    op.execute(sa.text("ALTER TABLE channels ALTER COLUMN active SET DEFAULT true"))
    op.execute(sa.text("ALTER TABLE channels ALTER COLUMN created_at SET NOT NULL"))
    op.execute(sa.text("ALTER TABLE posts ALTER COLUMN posted_at TYPE timestamptz USING posted_at::timestamptz"))
    # ``posts.is_deleted`` did not exist in the probe, so ADD COLUMN already
    # created it with the final boolean type.
    op.execute(sa.text("ALTER TABLE posts ALTER COLUMN is_deleted SET DEFAULT false"))
    op.execute(sa.text("ALTER TABLE posts ALTER COLUMN is_deleted SET NOT NULL"))
    op.execute(sa.text("ALTER TABLE posts ALTER COLUMN created_at SET NOT NULL"))
    for table in ("snapshots", "comments"):
        for column in (("taken_at",) if table == "snapshots" else ("posted_at", "edited_at", "first_collected_at", "last_collected_at")):
            op.execute(sa.text(f'ALTER TABLE "{table}" ALTER COLUMN "{column}" TYPE timestamptz USING "{column}"::timestamptz'))
    op.execute(sa.text("ALTER TABLE comments ALTER COLUMN is_deleted DROP DEFAULT"))
    op.execute(sa.text("ALTER TABLE comments ALTER COLUMN is_deleted TYPE boolean USING is_deleted <> 0"))
    op.execute(sa.text("ALTER TABLE comments ALTER COLUMN is_deleted SET DEFAULT false"))

    # Remove probe-only foreign keys before widening the referenced identity
    # columns; PostgreSQL otherwise rejects the type change.
    for name, table in (("fk_m0_comments_post", "comments"), ("fk_m0_posts_channel", "posts"), ("fk_m0_snapshots_post", "snapshots")):
        op.drop_constraint(name, table_name=table, type_="foreignkey")

    # Widen legacy INTEGER identity/foreign-key columns to the production
    # BIGINT shape before adding the composite workspace constraints.
    for table, columns in (
        ("channels", ("id",)),
        ("posts", ("id", "channel_id")),
        ("snapshots", ("id", "post_id")),
        ("comments", ("id", "post_id")),
    ):
        for column in columns:
            op.execute(sa.text(f'ALTER TABLE "{table}" ALTER COLUMN "{column}" TYPE bigint USING "{column}"::bigint'))

    for table in ("channels", "posts", "snapshots", "comments"):
        op.alter_column(table, "workspace_id", nullable=False)

    # Replace probe-global uniqueness with workspace-scoped uniqueness and
    # add composite foreign keys that fail closed on cross-tenant links.
    for name, table in (
        ("uq_m0_channels_identifier", "channels"),
        ("uq_m0_posts_channel_message", "posts"),
        ("uq_m0_comments_post_message", "comments"),
    ):
        op.drop_constraint(name, table_name=table, type_="unique")
    op.create_unique_constraint("uq_channels_workspace_id", "channels", ["workspace_id", "id"])
    op.create_unique_constraint("uq_channels_workspace_identifier", "channels", ["workspace_id", "identifier"])
    op.create_unique_constraint("uq_posts_workspace_id", "posts", ["workspace_id", "id"])
    op.create_unique_constraint("uq_posts_channel_message", "posts", ["workspace_id", "channel_id", "message_id"])
    op.create_unique_constraint("uq_comments_post_message", "comments", ["workspace_id", "post_id", "telegram_message_id"])
    # Remove probe-only indexes/FKs now that the workspace composite versions
    # are authoritative.
    for name, table in (
        ("ix_m0_comments_post_date", "comments"),
        ("ix_m0_comments_sender", "comments"),
        ("ix_m0_posts_channel_date", "posts"),
        ("ix_m0_snapshots_post_time", "snapshots"),
    ):
        op.drop_index(name, table_name=table)
    op.create_foreign_key("fk_channels_workspace", "channels", "workspaces", ["workspace_id"], ["id"], ondelete="CASCADE")
    op.create_foreign_key("fk_channels_workspace_connection", "channels", "telegram_connections", ["workspace_id", "telegram_connection_id"], ["workspace_id", "id"], ondelete="RESTRICT")
    op.create_foreign_key("fk_posts_workspace_channel", "posts", "channels", ["workspace_id", "channel_id"], ["workspace_id", "id"], ondelete="CASCADE")
    op.create_foreign_key("fk_snapshots_workspace_post", "snapshots", "posts", ["workspace_id", "post_id"], ["workspace_id", "id"], ondelete="CASCADE")
    op.create_foreign_key("fk_comments_workspace_post", "comments", "posts", ["workspace_id", "post_id"], ["workspace_id", "id"], ondelete="CASCADE")
    op.create_index("ix_channels_workspace_active", "channels", ["workspace_id", "active"])
    op.create_index("ix_posts_workspace_channel_date", "posts", ["workspace_id", "channel_id", "posted_at"])
    op.create_index("ix_snapshots_workspace_post_time", "snapshots", ["workspace_id", "post_id", "taken_at"])
    op.create_index("ix_comments_workspace_post_date", "comments", ["workspace_id", "post_id", "posted_at"])
    op.create_index("ix_comments_workspace_sender", "comments", ["workspace_id", "sender_id"])

    op.create_table(
        "collection_jobs",
        sa.Column("id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("channel_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.Text(), server_default="running", nullable=False),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default="1", nullable=False),
        sa.Column("metadata_json", JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id", "channel_id"], ["channels.workspace_id", "channels.id"], ondelete="CASCADE", name="fk_collection_jobs_workspace_channel"),
        sa.PrimaryKeyConstraint("id", name="pk_collection_jobs"),
    )
    op.create_index("uq_collection_jobs_active_channel", "collection_jobs", ["workspace_id", "channel_id"], unique=True, postgresql_where=sa.text("status = 'running'"))
    op.create_index("ix_collection_jobs_lease", "collection_jobs", ["status", "lease_until"])


def downgrade() -> None:
    """Return the archive tables to the reversible M0 probe shape.

    M1 adds tenant columns, production-native timestamp/boolean types, and
    composite keys in place.  A downgrade must remove those additions before
    Alembic runs the M0 downgrade (which drops the archive tables and its
    indexes).  Text casts intentionally keep the values readable and avoid a
    lossy epoch conversion; identity columns remain BIGINT because narrowing
    them could overflow a live archive.
    """
    op.drop_index("ix_collection_jobs_lease", table_name="collection_jobs")
    op.drop_index("uq_collection_jobs_active_channel", table_name="collection_jobs")
    op.drop_table("collection_jobs")
    for index, table in (
        ("ix_comments_workspace_sender", "comments"),
        ("ix_comments_workspace_post_date", "comments"),
        ("ix_snapshots_workspace_post_time", "snapshots"),
        ("ix_posts_workspace_channel_date", "posts"),
        ("ix_channels_workspace_active", "channels"),
    ):
        op.drop_index(index, table_name=table)
    for name, table in (
        ("fk_comments_workspace_post", "comments"),
        ("fk_snapshots_workspace_post", "snapshots"),
        ("fk_posts_workspace_channel", "posts"),
        ("fk_channels_workspace_connection", "channels"),
        ("fk_channels_workspace", "channels"),
    ):
        op.drop_constraint(name, table_name=table, type_="foreignkey")
    for name, table in (
        ("uq_comments_post_message", "comments"),
        ("uq_posts_channel_message", "posts"),
        ("uq_posts_workspace_id", "posts"),
        ("uq_channels_workspace_identifier", "channels"),
        ("uq_channels_workspace_id", "channels"),
    ):
        op.drop_constraint(name, table_name=table, type_="unique")

    # Restore the M0 scalar column types before dropping M1-only columns.  The
    # explicit casts preserve nullability and remain safe for a populated
    # archive; BIGINT identity columns are deliberately retained.
    op.execute(sa.text("ALTER TABLE channels ALTER COLUMN active DROP DEFAULT"))
    op.execute(sa.text("ALTER TABLE channels ALTER COLUMN active TYPE integer USING CASE WHEN active THEN 1 ELSE 0 END"))
    op.execute(sa.text("ALTER TABLE channels ALTER COLUMN active SET DEFAULT 1"))
    for table, columns in (
        ("posts", ("posted_at",)),
        ("snapshots", ("taken_at",)),
        ("comments", ("posted_at", "edited_at", "first_collected_at", "last_collected_at")),
    ):
        for column in columns:
            op.execute(sa.text(f'ALTER TABLE "{table}" ALTER COLUMN "{column}" TYPE text USING "{column}"::text'))
    op.execute(sa.text("ALTER TABLE comments ALTER COLUMN is_deleted DROP DEFAULT"))
    op.execute(sa.text("ALTER TABLE comments ALTER COLUMN is_deleted TYPE integer USING CASE WHEN is_deleted THEN 1 ELSE 0 END"))
    op.execute(sa.text("ALTER TABLE comments ALTER COLUMN is_deleted SET DEFAULT 0"))

    # M0 did not know about connection/workspace metadata or post deletion
    # flags.  Remove those columns only after every M1 constraint is gone.
    for table, column in (("comments", "workspace_id"), ("snapshots", "workspace_id"), ("posts", "workspace_id"), ("channels", "workspace_id")):
        op.drop_column(table, column)
    op.drop_column("channels", "telegram_connection_id")
    op.drop_column("channels", "created_at")
    op.drop_column("posts", "is_deleted")
    op.drop_column("posts", "created_at")

    op.drop_table("telegram_connections")
    op.drop_table("provider_consents")
    op.drop_table("memberships")
    op.drop_table("users")
    op.drop_table("workspaces")

    # Recreate the M0 constraints and access paths so a subsequent downgrade
    # to base, or a fresh M1 upgrade, sees exactly the probe contract.
    op.create_unique_constraint("uq_m0_channels_identifier", "channels", ["identifier"])
    op.create_unique_constraint("uq_m0_posts_channel_message", "posts", ["channel_id", "message_id"])
    op.create_unique_constraint("uq_m0_comments_post_message", "comments", ["post_id", "telegram_message_id"])
    op.create_foreign_key("fk_m0_posts_channel", "posts", "channels", ["channel_id"], ["id"], ondelete="CASCADE")
    op.create_foreign_key("fk_m0_snapshots_post", "snapshots", "posts", ["post_id"], ["id"], ondelete="CASCADE")
    op.create_foreign_key("fk_m0_comments_post", "comments", "posts", ["post_id"], ["id"], ondelete="CASCADE")
    op.create_index("ix_m0_posts_channel_date", "posts", ["channel_id", "posted_at"])
    op.create_index("ix_m0_snapshots_post_time", "snapshots", ["post_id", "taken_at"])
    op.create_index("ix_m0_comments_post_date", "comments", ["post_id", "posted_at"])
    op.create_index("ix_m0_comments_sender", "comments", ["sender_id"])
