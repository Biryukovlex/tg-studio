"""T10 acceptance tests: session key handling."""
from __future__ import annotations

import io
import os
import subprocess
import sys
import uuid
from contextlib import redirect_stdout

import pytest
from cryptography.fernet import Fernet

from app.session_crypto import SessionCipher, build_cipher


def _fernet_key() -> str:
    return Fernet.generate_key().decode("ascii")


def test_weak_passphrase_rejected_and_fernet_key_accepted():
    with pytest.raises(ValueError, match="generate_session_key"):
        SessionCipher("short-passphrase")
    with pytest.raises(ValueError, match="generate_session_key"):
        SessionCipher("x" * 40)
    cipher = SessionCipher(_fernet_key())
    token = cipher.encrypt("synthetic-session-string")
    assert cipher.decrypt(token) == "synthetic-session-string"


def test_build_cipher_requires_value():
    assert build_cipher("") is None
    assert build_cipher("   ") is None
    assert isinstance(build_cipher(_fernet_key()), SessionCipher)


def test_previous_key_row_is_repersisted_under_current_key():
    from app.postgres_db import PostgresDatabase

    key_a = _fernet_key()
    key_b = _fernet_key()
    cipher_a = SessionCipher(key_a)
    cipher_b = SessionCipher(key_b, previous_key=key_a)

    stored = {
        "api_id": 123,
        "api_hash": None,
        "encrypted_api_hash": cipher_a.encrypt("synthetic-api-hash"),
        "encrypted_session": cipher_a.encrypt("synthetic-session"),
        "session_key_version": cipher_a.key_version,
    }
    persisted: dict = {}

    class FakeResult:
        def mappings(self):
            class Maps:
                def first(self):
                    return dict(stored)

            return Maps()

    async def fake_execute(statement, params=None):
        if statement.lstrip().upper().startswith("SELECT"):
            return FakeResult()
        persisted.update(params or {})
        persisted["statement"] = statement

        class Empty:
            def mappings(self):
                class Maps:
                    def first(self):
                        return None

                return Maps()

        return Empty()

    async def run():
        db = PostgresDatabase.__new__(PostgresDatabase)
        db._execute = fake_execute  # type: ignore[attr-defined]
        connection = await PostgresDatabase.load_telegram_connection(db, label="default", cipher=cipher_b)
        assert connection is not None
        assert connection["api_hash"] == "synthetic-api-hash"
        assert connection["session_string"] == "synthetic-session"
        # Re-persisted under the current key with a new version.
        assert persisted["key_version"] == cipher_b.key_version
        assert cipher_b.decrypt(bytes(persisted["encrypted_session"])) == "synthetic-session"
        assert cipher_b.decrypt(bytes(persisted["encrypted_api_hash"])) == "synthetic-api-hash"

    import asyncio

    asyncio.run(run())


def test_rotate_script_reencrypts_without_leaking_secrets(capsys):
    import scripts.rotate_session_key as rotate

    key_a = _fernet_key()
    key_b = _fernet_key()
    current = SessionCipher(key_b)
    previous = SessionCipher(key_a)
    session_secret = "synthetic-session-AAA"
    hash_secret = "synthetic-hash-BBB"
    rows = [
        {
            "id": "connection-in-workspace-a",
            "label": "default",
            "api_hash": None,
            "encrypted_api_hash": previous.encrypt(hash_secret),
            "encrypted_session": previous.encrypt(session_secret),
            "session_key_version": previous.key_version,
        }
    ]
    updates, report = rotate.rotate_connection_rows(rows, current, previous)
    assert report == {"total": 1, "re_encrypted": 1, "skipped": 0, "unreadable": 0}
    assert len(updates) == 1
    assert updates[0]["id"] == "connection-in-workspace-a"
    assert current.decrypt(bytes(updates[0]["encrypted_session"])) == session_secret
    assert current.decrypt(bytes(updates[0]["encrypted_api_hash"])) == hash_secret
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        print(f"total={report['total']} re_encrypted={report['re_encrypted']}")
    out, _ = capsys.readouterr()
    assert session_secret not in out and hash_secret not in out
    assert key_a not in out and key_b not in out


def test_rotation_keeps_same_label_connections_distinct():
    import scripts.rotate_session_key as rotate

    old = SessionCipher(_fernet_key())
    current = SessionCipher(_fernet_key())
    rows = [
        {"id": "workspace-a", "label": "default", "api_hash": "hash-a", "encrypted_api_hash": None,
         "encrypted_session": old.encrypt("session-a"), "session_key_version": old.key_version},
        {"id": "workspace-b", "label": "default", "api_hash": "hash-b", "encrypted_api_hash": None,
         "encrypted_session": old.encrypt("session-b"), "session_key_version": old.key_version},
    ]
    updates, report = rotate.rotate_connection_rows(rows, current, old)
    assert report["re_encrypted"] == 2
    assert {item["id"] for item in updates} == {"workspace-a", "workspace-b"}
    assert [current.decrypt(item["encrypted_session"]) for item in updates] == ["session-a", "session-b"]


def test_loading_row_after_migration_returns_decrypted_api_hash():
    from app.postgres_db import PostgresDatabase

    key = _fernet_key()
    cipher = SessionCipher(key)
    stored = {
        "api_id": 7,
        "api_hash": None,
        "encrypted_api_hash": cipher.encrypt("synthetic-hash-after-migration"),
        "encrypted_session": cipher.encrypt("synthetic-session-after-migration"),
        "session_key_version": cipher.key_version,
    }

    class FakeResult:
        def mappings(self):
            class Maps:
                def first(self):
                    return dict(stored)

            return Maps()

    async def fake_execute(statement, params=None):
        return FakeResult()

    async def run():
        db = PostgresDatabase.__new__(PostgresDatabase)
        db._execute = fake_execute  # type: ignore[attr-defined]
        connection = await PostgresDatabase.load_telegram_connection(db, label="default", cipher=cipher)
        assert connection is not None
        assert connection["api_hash"] == "synthetic-hash-after-migration"
        assert connection["session_string"] == "synthetic-session-after-migration"

    import asyncio

    asyncio.run(run())


def test_migration_0014_shape():
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "0014_session_crypto.py"
    assert path.is_file()
    content = path.read_text(encoding="utf-8")
    assert "encrypted_api_hash" in content
    assert "DROP NOT NULL" in content


@pytest.mark.integration
def test_existing_connection_is_encrypted_on_upgrade_and_restored_on_downgrade():
    """Run only against an explicitly empty, single-purpose PostgreSQL DB."""
    database_url = os.getenv("T10_MIGRATION_POSTGRES_URL")
    if not database_url:
        pytest.skip("set T10_MIGRATION_POSTGRES_URL to an empty disposable database")
    import asyncio
    import asyncpg

    key = _fernet_key()
    cipher = SessionCipher(key)
    historical_cipher = SessionCipher(_fernet_key())
    sessions = [cipher.encrypt("synthetic-session"),
                historical_cipher.encrypt("historical-session"), None]
    row_ids = [uuid.uuid4() for _ in sessions]
    env = os.environ.copy()
    env["DATABASE_URL"] = database_url
    env["TELEGRAM_SESSION_ENCRYPTION_KEY"] = key

    def migrate(target: str) -> None:
        result = subprocess.run([sys.executable, "-m", "alembic", target.split()[0], target.split()[1]],
                                check=False, env=env, capture_output=True, text=True)
        errors = [line.strip() for line in result.stderr.splitlines() if "Error:" in line]
        assert result.returncode == 0, f"Alembic {target} failed: {(errors or ['see isolated migration log'])[-1][:200]}"

    async def proof() -> None:
        url = database_url.replace("postgresql+asyncpg://", "postgresql://")
        connection = await asyncpg.connect(url)
        try:
            assert await connection.fetchval("SELECT to_regclass('public.alembic_version')") is None
        finally:
            await connection.close()
        migrate("upgrade 0013_collection_integrity")
        connection = await asyncpg.connect(url)
        workspace_id = uuid.uuid4()
        try:
            await connection.execute(
                "INSERT INTO workspaces(id, slug, name) VALUES ($1, $2, 'Synthetic')",
                workspace_id, f"crypto-{workspace_id.hex}",
            )
            for index, (row_id, session) in enumerate(zip(row_ids, sessions)):
                await connection.execute(
                    "INSERT INTO telegram_connections(id, workspace_id, label, api_id, api_hash, encrypted_session) "
                    "VALUES ($1, $2, $3, 42, 'synthetic-api-hash', $4)",
                    row_id, workspace_id, f"synthetic-{index}", session,
                )
        finally:
            await connection.close()
        migrate("upgrade 0014_session_crypto")
        connection = await asyncpg.connect(url)
        try:
            for row_id, session in zip(row_ids, sessions):
                row = await connection.fetchrow(
                    "SELECT api_hash, encrypted_api_hash, encrypted_session, session_key_version "
                    "FROM telegram_connections WHERE id=$1", row_id
                )
                assert row["api_hash"] is None
                assert cipher.decrypt(row["encrypted_api_hash"]) == "synthetic-api-hash"
                assert row["encrypted_session"] == session
                assert row["session_key_version"] == "v1"
        finally:
            await connection.close()
        migrate("downgrade 0013_collection_integrity")
        connection = await asyncpg.connect(url)
        try:
            for row_id, session in zip(row_ids, sessions):
                row = await connection.fetchrow(
                    "SELECT api_hash, encrypted_session, session_key_version "
                    "FROM telegram_connections WHERE id=$1", row_id
                )
                assert row["api_hash"] == "synthetic-api-hash"
                assert row["encrypted_session"] == session
                assert row["session_key_version"] == "v1"
        finally:
            await connection.close()

    asyncio.run(proof())
