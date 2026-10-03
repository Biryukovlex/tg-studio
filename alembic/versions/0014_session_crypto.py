"""Encrypt existing Telegram API hashes without leaving plaintext behind."""

from alembic import op
import sqlalchemy as sa

from app.config import Settings
from app.session_crypto import build_cipher


revision = "0014_session_crypto"
down_revision = "0013_collection_integrity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(sa.text("SET LOCAL TIME ZONE 'UTC'"))
    op.execute(sa.text("ALTER TABLE telegram_connections ADD COLUMN IF NOT EXISTS encrypted_api_hash BYTEA"))
    op.execute(sa.text("ALTER TABLE telegram_connections ALTER COLUMN api_hash DROP NOT NULL"))
    connection = op.get_bind()
    rows = connection.execute(
        sa.text("SELECT id, api_hash FROM telegram_connections WHERE api_hash IS NOT NULL")
    ).mappings().all()
    if rows:
        settings = Settings()
        cipher = build_cipher(
            settings.telegram_session_encryption_key,
            settings.telegram_session_encryption_key_previous,
        )
        if cipher is None:
            raise RuntimeError("Set TELEGRAM_SESSION_ENCRYPTION_KEY before migrating stored Telegram connections")
        for row in rows:
            # This migration encrypts API hashes only. Historical sessions may
            # use keys unavailable to this deployment; preserve their ciphertext
            # and key version. The normal connection loader rotates readable
            # sessions when they are next used.
            connection.execute(
                sa.text(
                    """UPDATE telegram_connections
                          SET encrypted_api_hash=:api_hash, api_hash=NULL
                        WHERE id=:id"""
                ),
                {
                    "id": row["id"],
                    "api_hash": cipher.encrypt(row["api_hash"]) if row["api_hash"] else None,
                },
            )


def downgrade() -> None:
    op.execute(sa.text("SET LOCAL TIME ZONE 'UTC'"))
    connection = op.get_bind()
    rows = connection.execute(
        sa.text("SELECT id, encrypted_api_hash FROM telegram_connections WHERE api_hash IS NULL")
    ).mappings().all()
    if rows:
        settings = Settings()
        cipher = build_cipher(
            settings.telegram_session_encryption_key,
            settings.telegram_session_encryption_key_previous,
        )
        if cipher is None and any(row["encrypted_api_hash"] is not None for row in rows):
            raise RuntimeError("Set TELEGRAM_SESSION_ENCRYPTION_KEY before downgrading stored Telegram connections")
        for row in rows:
            try:
                api_hash = cipher.decrypt(bytes(row["encrypted_api_hash"])) if row["encrypted_api_hash"] is not None else ""
            except ValueError as exc:
                raise RuntimeError("Telegram API hash cannot be decrypted; restore the matching key before downgrade") from exc
            connection.execute(
                sa.text("UPDATE telegram_connections SET api_hash=:api_hash WHERE id=:id"),
                {"id": row["id"], "api_hash": api_hash},
            )
    op.execute(sa.text("ALTER TABLE telegram_connections ALTER COLUMN api_hash SET NOT NULL"))
    op.execute(sa.text("ALTER TABLE telegram_connections DROP COLUMN IF EXISTS encrypted_api_hash"))
