"""Encrypt the stored Telegram API hash and allow nullable plaintext."""

from alembic import op
import sqlalchemy as sa


revision = "0014_session_crypto"
down_revision = "0012_channel_system_prompts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(sa.text("SET LOCAL TIME ZONE 'UTC'"))
    op.execute(sa.text("ALTER TABLE telegram_connections ADD COLUMN IF NOT EXISTS encrypted_api_hash BYTEA"))
    # Existing rows keep their plaintext hash until the next persist or
    # rotation rewrites them encrypted; new writes store NULL plaintext.
    op.execute(sa.text("ALTER TABLE telegram_connections ALTER COLUMN api_hash DROP NOT NULL"))


def downgrade() -> None:
    op.execute(sa.text("SET LOCAL TIME ZONE 'UTC'"))
    # Best effort: rows already re-encrypted have no recoverable plaintext and
    # fall back to an empty hash that the operator must re-enter on the
    # settings page.
    op.execute(sa.text("UPDATE telegram_connections SET api_hash='' WHERE api_hash IS NULL"))
    op.execute(sa.text("ALTER TABLE telegram_connections ALTER COLUMN api_hash SET NOT NULL"))
    op.execute(sa.text("ALTER TABLE telegram_connections DROP COLUMN IF EXISTS encrypted_api_hash"))
