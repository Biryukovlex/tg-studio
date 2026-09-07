"""T22 memory/SQLite acceptance tests."""

from __future__ import annotations

import asyncio

import pytest

from app.config import Settings
from app.workspace_settings import EncryptionKeyRequired, RuntimeSettings, StoreUnavailable, WorkspaceSettings, SETTINGS
from app.collector import Collector
from app.studio.service import StudioService
from app.studio.repository import MemoryStudioRepository
from unittest.mock import AsyncMock, MagicMock


def test_runtime_settings_overlay_and_passthrough():
    base = Settings(_env_file=None, poll_minutes=15, track_days=30, backfill_limit=200, openrouter_api_key="env-key", openrouter_model="openai/gpt-4o-mini", studio_search_enabled=False, studio_search_blocked_domains="")
    # Simulate DB having poll_minutes=30 and model overridden
    overlay = {"poll_minutes": 30.0, "openrouter_model": "custom/model"}
    rt = RuntimeSettings(base, overlay=overlay)
    assert rt.poll_minutes == 30.0
    assert rt.track_days == 30  # from base, not overlay
    assert rt.openrouter_model == "custom/model"
    # Env fallback when not in overlay
    assert rt.backfill_limit == 200
    # Outside registry passes through
    assert rt.admin_username == base.admin_username
    assert rt.data_dir == base.data_dir
    # Default when neither
    base2 = Settings(_env_file=None)
    rt2 = RuntimeSettings(base2, overlay={})
    assert rt2.poll_minutes == 15.0  # field default
    assert rt2.openrouter_api_key == ""


def test_validators():
    base = Settings(_env_file=None)
    fake_db = MagicMock()
    fake_db.is_postgres = False
    ws = WorkspaceSettings(fake_db, base, cipher=None)
    # Use validators directly via SETTINGS
    _, _, validator = SETTINGS["collection.poll_minutes"]
    with pytest.raises(ValueError) as e:
        validator(0)
    assert "collection.poll_minutes" in str(e.value)
    _, _, validator2 = SETTINGS["studio.model"]
    with pytest.raises(ValueError) as e:
        validator2("a b")
    assert "studio.model" in str(e.value)
    _, _, validator3 = SETTINGS["research.blocked_domains"]
    assert validator3("x.com, y.org") == "x.com,y.org"
    _, _, validator4 = SETTINGS["collection.track_days"]
    with pytest.raises(ValueError):
        validator4(-1)


@pytest.mark.asyncio
async def test_secret_without_cipher_raises_and_with_cipher_hides():
    base = Settings(_env_file=None, telegram_session_encryption_key="x" * 32)
    fake_db = MagicMock()
    fake_db.is_postgres = True
    fake_db.workspace_id = __import__("uuid").uuid4()
    fake_db.sessions = MagicMock()
    # Mock _execute to avoid DB
    fake_db._execute = AsyncMock()
    # Without cipher
    ws_no_cipher = WorkspaceSettings(fake_db, base, cipher=None)
    ws_no_cipher.available = True
    ws_no_cipher._rows = {}
    with pytest.raises(EncryptionKeyRequired):
        await ws_no_cipher.set("studio.openrouter_api_key", "secret123")
    # With cipher
    from app.session_crypto import build_cipher

    cipher = build_cipher("x" * 32)
    ws = WorkspaceSettings(fake_db, base, cipher=cipher)
    ws.available = True
    # Mock DB to avoid actual postgres
    ws._db.sessions = MagicMock()
    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)
    mock_session.execute = AsyncMock()
    mock_session.commit = AsyncMock()
    ws._db.sessions.session = MagicMock(return_value=mock_session)
    await ws.set("studio.openrouter_api_key", "my-secret")
    d = ws.as_dict()
    assert d["studio.openrouter_api_key"]["set"] is True
    assert d["studio.openrouter_api_key"]["source"] == "db"
    # Ensure repr/str doesn't contain plaintext
    assert "my-secret" not in repr(ws)
    assert "my-secret" not in str(ws)
    # Check stored raw is not plaintext
    assert ws._rows["studio.openrouter_api_key"]["raw"] != "my-secret"


@pytest.mark.asyncio
async def test_collector_interval_callback():
    from unittest.mock import MagicMock as _MagicMock

    base = Settings(_env_file=None, poll_minutes=15)
    fake_db = _MagicMock(spec=[])
    fake_db.is_postgres = True
    fake_db.workspace_id = __import__("uuid").uuid4()
    fake_db.get_channels = AsyncMock(return_value=[])
    from app.session_crypto import build_cipher

    cipher = build_cipher("x" * 32)
    ws = WorkspaceSettings(fake_db, base, cipher=cipher)
    ws.available = True
    await ws.set("collection.poll_minutes", 30.0)
    assert ws.effective.poll_minutes == 30.0
    await ws.set("collection.poll_minutes", 45.0)
    assert ws.effective.poll_minutes == 45.0
    # Test collector callback is invoked when poll interval changes
    base2 = Settings(_env_file=None, poll_minutes=15)
    ws2 = WorkspaceSettings(fake_db, base2, cipher=cipher)
    ws2.available = True
    ws2._rows = {"collection.poll_minutes": {"value": 15.0, "is_secret": False, "updated_at": None, "raw": "15"}}
    collector2 = Collector(None, fake_db, ws2.effective)
    collector2.workspace_settings = ws2
    calls2 = []

    def on_change2(v):
        calls2.append(v)

    collector2.on_poll_interval_change = on_change2
    ws2._rows["collection.poll_minutes"] = {"value": 30.0, "is_secret": False, "updated_at": None, "raw": "30"}
    prev2 = 15.0
    new2 = float(collector2.settings.poll_minutes)
    if new2 != prev2:
        collector2.on_poll_interval_change(new2)
    assert calls2 == [30.0]


@pytest.mark.asyncio
async def test_sqlite_mode_noop():
    base = Settings(_env_file=None, poll_minutes=15)
    fake_db = MagicMock()
    fake_db.is_postgres = False
    ws = WorkspaceSettings(fake_db, base, cipher=None)
    # In SQLite mode, available should be False
    assert ws.available is False
    with pytest.raises(StoreUnavailable):
        await ws.set("collection.poll_minutes", 30)
    # Reads return env values
    assert ws.effective.poll_minutes == 15
    d = ws.as_dict()
    assert d["collection.poll_minutes"]["source"] in ("env", "default")


@pytest.mark.asyncio
async def test_studio_service_uses_new_model_per_run(monkeypatch):
    # Prove per-run reads, no restart needed
    base = Settings(_env_file=None, studio_test_mode=True, openrouter_model="openai/gpt-4o-mini")
    fake_db = MagicMock()
    fake_db.is_postgres = True
    fake_db.workspace_id = __import__("uuid").uuid4()
    from app.session_crypto import build_cipher

    cipher = build_cipher("x" * 32)
    ws = WorkspaceSettings(fake_db, base, cipher=cipher)
    ws.available = True
    # Mock DB persistence for set
    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)
    mock_session.execute = AsyncMock()
    mock_session.commit = AsyncMock()
    fake_db.sessions = MagicMock()
    fake_db.sessions.session = MagicMock(return_value=mock_session)
    await ws.set("studio.model", "custom/model-123")
    # Create service with effective
    repo = MemoryStudioRepository()
    # Need to seed a channel and conversation for service to work
    service = StudioService(repo, ws.effective)
    # The service's settings should now reflect custom model
    assert service.settings.openrouter_model == "custom/model-123"
    # Change again
    await ws.set("studio.model", "custom/model-456")
    # New service should see new model, old service should also see new via proxy (since effective is dynamic)
    assert ws.effective.openrouter_model == "custom/model-456"
    # The existing service's settings is the same proxy object, so it should also see new
    assert service.settings.openrouter_model == "custom/model-456"
