"""History tools use scoped PostgreSQL reads and explicit Telegram refreshes."""

import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text

from app import limits
from app.config import Settings
from app.postgres_db import PostgresDatabase
from scripts import restore_history


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", [restore_history._diagnostic, restore_history._run])
async def test_history_requires_postgres(operation, monkeypatch):
    monkeypatch.setattr(restore_history, "TelegramClient", lambda *a: pytest.fail("Telegram was contacted"))
    with pytest.raises(restore_history.HistoryError, match="DATABASE_URL is required"):
        await operation(Settings(_env_file=None, database_url=""))


@pytest.mark.integration
@pytest.mark.asyncio
async def test_diagnostic_is_read_only_and_workspace_scoped(app, settings, monkeypatch):
    owner = app.state.db
    other = PostgresDatabase(settings.database_url, workspace_slug=f"diagnostic-other-{uuid.uuid4().hex}")
    await other.init_db(admin_username="synthetic-history-admin")
    await other.upsert_channel("@synthetic_other", "Other workspace", 9001)
    try:
        monkeypatch.setattr(limits, "WORKSPACE_SLUG", owner.workspace_slug)
        monkeypatch.setattr(PostgresDatabase, "init_db", AsyncMock(side_effect=AssertionError("Diagnostic wrote a workspace")))
        monkeypatch.setattr(restore_history, "TelegramClient", lambda *a: pytest.fail("Diagnostic contacted Telegram"))
        result = await restore_history._diagnostic(settings)
        assert result["storage"] == "postgresql" and result["read_only"] is True
        assert result["channels"] == 1 and result["total_posts"] == 1
        assert result["oldest_post"] == result["newest_post"]
        assert "text" not in result
        monkeypatch.setattr(limits, "WORKSPACE_SLUG", f"uninitialized-{uuid.uuid4().hex}")
        with pytest.raises(restore_history.HistoryError, match="not initialized"):
            await restore_history._diagnostic(settings)
        async with owner.sessions.session() as session:
            assert (await session.execute(text("SELECT COUNT(*) FROM workspaces WHERE slug=:slug"),
                                          {"slug": limits.WORKSPACE_SLUG})).scalar_one() == 0
    finally:
        async with other.sessions.session() as session:
            await session.execute(text("DELETE FROM workspaces WHERE id=:id"), {"id": other.workspace_id})
            await session.commit()
        await other.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, "initialize", "connect", "unauthorized", "collect", "disconnect"])
async def test_refresh_uses_saved_connection_and_closes_resources(failure, monkeypatch):
    db = SimpleNamespace(
        init_db=AsyncMock(), close=AsyncMock(),
        load_telegram_connection=AsyncMock(return_value={
            "api_id": 9876, "api_hash": "synthetic-saved-hash", "session_string": "synthetic-saved-session",
        }),
    )
    client = SimpleNamespace(connect=AsyncMock(), disconnect=AsyncMock(), is_user_authorized=AsyncMock(return_value=True))
    collector = SimpleNamespace(sync_channels=AsyncMock(return_value=[]), poll_all=AsyncMock(return_value={"posts_seen": 2}))
    saved_settings = Settings(_env_file=None, channels="@saved_fixture", track_days=7, backfill_limit=5)
    store = SimpleNamespace(load=AsyncMock(), effective=saved_settings)
    monkeypatch.setattr(restore_history, "_database", lambda settings: db)
    monkeypatch.setattr(restore_history, "WorkspaceSettings", lambda *args: store)
    monkeypatch.setattr(restore_history, "StringSession", lambda value: value)

    def make_client(session, api_id, api_hash):
        assert (session, api_id, api_hash) == ("synthetic-saved-session", 9876, "synthetic-saved-hash")
        return client

    def make_collector(telegram, database, effective):
        assert telegram is client and database is db
        assert effective.channel_list == ["@saved_fixture"]
        assert effective.track_days == effective.backfill_limit == 0
        return collector

    monkeypatch.setattr(restore_history, "TelegramClient", make_client)
    monkeypatch.setattr(restore_history, "Collector", make_collector)
    failing = {
        "initialize": db.init_db, "connect": client.connect,
        "collect": collector.poll_all, "disconnect": client.disconnect,
    }.get(failure)
    if failing is not None:
        failing.side_effect = RuntimeError("synthetic failure")
    if failure == "unauthorized":
        client.is_user_authorized.return_value = False
    settings = Settings(_env_file=None, channels="@env_fixture", api_id=1, api_hash="synthetic-env-hash")
    if failure:
        with pytest.raises(RuntimeError):
            await restore_history._run(settings)
    else:
        result = await restore_history._run(settings)
        assert result == {"posts_seen": 2, "failed_channels": [], "history_mode": "all"}
        db.load_telegram_connection.assert_awaited_once_with(label=limits.TELEGRAM_CONNECTION_LABEL, cipher=None)
    db.close.assert_awaited_once()
    if failure != "initialize":
        client.disconnect.assert_awaited_once()


def test_cli_does_not_expose_database_exception_payload(monkeypatch, capsys):
    async def fail(settings):
        raise RuntimeError("synthetic-private-connection-detail")

    monkeypatch.setattr(restore_history, "load_settings", lambda: Settings(_env_file=None))
    monkeypatch.setattr(restore_history, "_diagnostic", fail)
    monkeypatch.setattr("sys.argv", ["restore_history.py"])
    with pytest.raises(SystemExit) as exited:
        restore_history.main()
    assert exited.value.code == 1
    output = json.loads(capsys.readouterr().err)
    assert output["status"] == "error"
    assert "synthetic-private-connection-detail" not in output["detail"]
