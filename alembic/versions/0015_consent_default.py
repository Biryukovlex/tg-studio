"""Align the provider-consent fingerprint default with the model."""

from alembic import op
import sqlalchemy as sa


revision = "0015_consent_default"
down_revision = "0014_session_crypto"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(sa.text("SET LOCAL TIME ZONE 'UTC'"))
    # The model declares server_default="legacy" but migration 0004 created
    # the column without a database default. Raw-SQL inserts would fail where
    # SQLAlchemy inserts succeed; make the database match the model.
    op.execute(sa.text("ALTER TABLE provider_consents ALTER COLUMN configuration_fingerprint SET DEFAULT 'legacy'"))


def downgrade() -> None:
    op.execute(sa.text("SET LOCAL TIME ZONE 'UTC'"))
    op.execute(sa.text("ALTER TABLE provider_consents ALTER COLUMN configuration_fingerprint DROP DEFAULT"))
