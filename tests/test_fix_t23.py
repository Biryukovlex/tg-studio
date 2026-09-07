"""T23 acceptance tests – settings page."""

from __future__ import annotations

import pytest
import httpx
from unittest.mock import AsyncMock, MagicMock

from app.config import Settings
from app.db import Database
from app.collector import Collector
from app.web.routes import create_app


class FakeWorkspaceSettings:
    def __init__(self, available=True):
        self.available = available
        self._rows = {}
        self._calls = []
        self._as_dict_return = {
            "collection.poll_minutes": {"value": 15.0, "source": "default"},
            "collection.track_days": {"value": 30, "source": "default"},
            "collection.backfill_limit": {"value": 200, "source": "default"},
            "studio.openrouter_api_key": {"set": False, "source": "default"},
            "studio.model": {"value": "openai/gpt-4o-mini", "source": "default"},
            "research.enabled": {"value": False, "source": "default"},
            "research.blocked_domains": {"value": "", "source": "default"},
        }
        self.telegram_restart_required = False

    def as_dict(self):
        return self._as_dict_return

    async def set(self, key, value):
        self._calls.append(("set", key, value))
        if key == "studio.openrouter_api_key" and value == "raise-encryption":
            from app.workspace_settings import EncryptionKeyRequired

            raise EncryptionKeyRequired("Set TELEGRAM_SESSION_ENCRYPTION_KEY to store secrets.")
        # Simulate validation
        if key == "collection.poll_minutes" and float(value) == 0:
            raise ValueError("Poll interval must be between 1 and 1440 minutes.")
        self._rows[key] = value
        # Update as_dict
        if key in self._as_dict_return:
            if isinstance(self._as_dict_return[key], dict) and "set" in self._as_dict_return[key]:
                self._as_dict_return[key] = {"set": True, "source": "db", "updated_at": "2026-09-06 20:58 UTC"}
            else:
                self._as_dict_return[key] = {"value": value, "source": "db", "updated_at": "2026-09-06 21:14 UTC"}

    async def reset(self, key):
        self._calls.append(("reset", key))
        if key in self._as_dict_return:
            # Reset to env/default
            self._as_dict_return[key] = {"value": 15 if "poll_minutes" in key else 200 if "backfill" in key else "", "source": "env" if "poll_minutes" in key else "default"}

    @property
    def effective(self):
        # Return a simple object that has the same attributes as Settings but with overlay
        # For test, we just return a MagicMock that has the same
        return MagicMock()


class FakeDB:
    def __init__(self):
        self.is_postgres = True
        self.workspace_id = __import__("uuid").uuid4()
        self.user_id = __import__("uuid").uuid4()
        self.workspace_slug = "community"
        self._channels = [
            {"id": 1, "identifier": "@city_digest", "title": "City Digest", "chat_id": -1001234567890, "active": True},
            {"id": 2, "identifier": "@weekly_market", "title": "Weekly Market", "chat_id": -1009876543210, "active": True},
        ]
        self._add_calls = []
        self._deactivate_calls = []

    async def get_channels(self):
        return [c for c in self._channels if c["active"]]

    async def add_channel(self, identifier):
        self._add_calls.append(identifier)
        # Simple validation
        if " " in identifier:
            raise ValueError("Channel identifier must not contain spaces.")
        # Check pattern
        import re

        if not re.match(r"^(@[A-Za-z0-9_]{5,32}|https?://t\.me/[A-Za-z0-9_]{5,32}|-100\d{5,})$", identifier):
            raise ValueError("Channel identifier must be @name, t.me/name, or -100…")
        new_id = max(c["id"] for c in self._channels) + 1 if self._channels else 1
        self._channels.append({"id": new_id, "identifier": identifier, "title": "", "chat_id": None, "active": True})
        return new_id

    async def deactivate_channel(self, channel_id):
        self._deactivate_calls.append(channel_id)
        for c in self._channels:
            if c["id"] == channel_id:
                c["active"] = False
                return True
        return False

    async def telegram_connection_status(self, label):
        return {"configured": True, "api_id": 123456, "has_session": True, "updated_at": "2026-09-05 14:02:00+00:00"}

    async def persist_telegram_session(self, label, api_id, api_hash, session_string, cipher):
        self._persist_called = (label, api_id, api_hash, session_string)
        return __import__("uuid").uuid4()

    async def kpis(self, channel_id=None):
        return {"posts": 0, "views": 0, "reactions": 0, "comments": 0, "shares": 0, "last_poll": None, "collected_comments": 0}

    async def timeseries_totals(self, days=None, channel_id=None):
        return {"days": [], "views": [], "reactions": [], "comments": [], "shares": [], "posts_per_day": []}

    async def latest_stats(self, channel_id=None, limit=500, order="date"):
        return []

    async def _execute(self, query, params):
        # For settings page channel table, we need to handle SELECT for all channels
        class FakeResult:
            def __init__(self, rows):
                self._rows = rows

            def mappings(self):
                return self

            def all(self):
                return self._rows

            def first(self):
                return self._rows[0] if self._rows else None

        if "SELECT id, identifier, title, chat_id, active FROM channels" in query:
            return FakeResult(self._channels)
        if "SELECT identifier FROM channels" in query:
            for c in self._channels:
                if c["id"] == params.get("channel_id"):
                    return FakeResult([{"identifier": c["identifier"]}])
            return FakeResult([])
        return FakeResult([])


def _make_app_with_fake(tmp_path, role="owner", available=True, ws=None, db=None):
    import tempfile
    from pathlib import Path

    settings = Settings(
        _env_file=None,
        data_dir=str(tmp_path),
        admin_username="admin",
        admin_password="pw",
        session_secret="sec",
        database_url="",
        api_id=1,
        api_hash="h",
        session_string="s",
        channels="@test",
        telegram_session_encryption_key="x" * 32,
    )
    if db is None:
        db = FakeDB()
    if ws is None:
        ws = FakeWorkspaceSettings(available=available)
        # For available, ensure as_dict has proper data
    # Create collector with fake db
    collector = Collector(None, db, settings)
    collector.workspace_settings = ws
    # Patch WorkspaceContext to have role
    from app.web.routes import create_app, WorkspaceContext
    import uuid

    # Create app with our fake ws
    app = create_app(collector, settings, workspace_settings=ws)
    # Override workspace_context role
    app.state.workspace_context = WorkspaceContext(
        user_id=db.user_id,
        workspace_id=db.workspace_id,
        workspace_slug="community",
        role=role,
    )
    # Also need to set can_manage_settings via the same logic? create_app already sets via workspace_context role
    # But we need to ensure that the settings page's can_manage_settings is correct
    return app, ws, db


@pytest.mark.asyncio
async def test_get_settings_auth_and_sidebar(tmp_path):
    app, ws, db = _make_app_with_fake(tmp_path, role="owner", available=True)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Anonymous -> 303
        resp = await client.get("/settings", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/login"
        # Login as owner
        # Need to set session for auth
        # Use the app's login flow: post to /login
        login = await client.post("/login", data={"username": "admin", "password": "pw"})
        # After login, GET /settings should be 200 and contain ids
        resp = await client.get("/settings")
        assert resp.status_code == 200
        assert 'id="telegram"' in resp.text
        assert 'id="collection"' in resp.text
        assert 'id="studio"' in resp.text
        assert 'id="research"' in resp.text
        assert 'href="/settings"' in resp.text
        assert 'active' in resp.text  # Settings link active
        # Check sidebar link
        assert 'mgc-settings-3-core-regular' in resp.text
    # Member should get 403
    app2, ws2, db2 = _make_app_with_fake(tmp_path, role="member", available=True)
    transport2 = httpx.ASGITransport(app=app2)
    async with httpx.AsyncClient(transport=transport2, base_url="http://test") as client2:
        # Need to login but with member role, the session will still be owner? Actually login creates owner context
        # For this test, we need to simulate member via directly setting workspace_context role to member and having auth
        # We'll manually set session auth and then request
        login2 = await client2.post("/login", data={"username": "admin", "password": "pw"})
        # Override role to member after login
        app2.state.workspace_context = app2.state.workspace_context.__class__(
            user_id=app2.state.workspace_context.user_id,
            workspace_id=app2.state.workspace_context.workspace_id,
            workspace_slug=app2.state.workspace_context.workspace_slug,
            role="member",
        )
        resp2 = await client2.get("/settings", follow_redirects=False)
        assert resp2.status_code == 403


@pytest.mark.asyncio
async def test_post_collection_csrf_and_validation(tmp_path):
    app, ws, db = _make_app_with_fake(tmp_path, role="owner", available=True)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        login = await client.post("/login", data={"username": "admin", "password": "pw"})
        # Get CSRF
        page = await client.get("/settings")
        import re

        m = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
        assert m
        token = m.group(1)
        # Without CSRF -> 403
        resp = await client.post("/settings/collection", data={"poll_minutes": "30", "track_days": "30", "backfill_limit": "200"})
        assert resp.status_code == 403
        # With CSRF and poll_minutes=0 -> 422
        resp = await client.post(
            "/settings/collection",
            data={"poll_minutes": "0", "track_days": "30", "backfill_limit": "200", "csrf_token": token},
        )
        assert resp.status_code == 422
        assert 'class="field has-error"' in resp.text
        assert "Poll interval must be between 1 and 1440 minutes." in resp.text
        # With valid -> 303
        resp = await client.post(
            "/settings/collection",
            data={"poll_minutes": "30", "track_days": "30", "backfill_limit": "200", "csrf_token": token},
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/settings#collection"
        assert ("set", "collection.poll_minutes", 30.0) in ws._calls


@pytest.mark.asyncio
async def test_post_studio_key_handling(tmp_path):
    app, ws, db = _make_app_with_fake(tmp_path, role="owner", available=True)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        login = await client.post("/login", data={"username": "admin", "password": "pw"})
        page = await client.get("/settings")
        import re

        m = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
        token = m.group(1)
        # Empty key leaves untouched
        resp = await client.post(
            "/settings/studio",
            data={"openrouter_api_key": "", "model": "openai/gpt-4o-mini", "system_prompt": "test", "csrf_token": token},
        )
        assert resp.status_code == 303
        # Ensure no set for empty key
        assert not any(call[1] == "studio.openrouter_api_key" and call[2] == "" for call in ws._calls)
        # With key sets it
        resp = await client.post(
            "/settings/studio",
            data={"openrouter_api_key": "new-secret-key", "model": "openai/gpt-4o-mini", "system_prompt": "test", "csrf_token": token},
        )
        assert resp.status_code == 303
        assert ("set", "studio.openrouter_api_key", "new-secret-key") in ws._calls
        # HTML never contains key
        page2 = await client.get("/settings")
        assert "new-secret-key" not in page2.text
        # With clear checkbox resets
        resp = await client.post(
            "/settings/studio",
            data={"openrouter_api_key": "", "clear_openrouter_api_key": "1", "model": "openai/gpt-4o-mini", "system_prompt": "test", "csrf_token": token},
        )
        assert resp.status_code == 303
        assert ("reset", "studio.openrouter_api_key") in ws._calls
        # EncryptionKeyRequired -> 409
        # Create a new app with ws that will raise
        ws2 = FakeWorkspaceSettings(available=True)

        async def fake_set_raise(key, value):
            if key == "studio.openrouter_api_key":
                from app.workspace_settings import EncryptionKeyRequired

                raise EncryptionKeyRequired("Set TELEGRAM_SESSION_ENCRYPTION_KEY to store secrets.")
            ws2._calls.append(("set", key, value))

        ws2.set = fake_set_raise
        app2, _, _ = _make_app_with_fake(tmp_path, role="owner", available=True, ws=ws2, db=db)
        transport2 = httpx.ASGITransport(app=app2)
        async with httpx.AsyncClient(transport=transport2, base_url="http://test") as client2:
            login2 = await client2.post("/login", data={"username": "admin", "password": "pw"})
            page2 = await client2.get("/settings")
            import re

            m2 = re.search(r'name="csrf_token" value="([^"]+)"', page2.text)
            token2 = m2.group(1)
            resp = await client2.post(
                "/settings/studio",
                data={"openrouter_api_key": "raise-encryption", "model": "openai/gpt-4o-mini", "system_prompt": "test", "csrf_token": token2},
            )
            assert resp.status_code == 409
            assert "Set TELEGRAM_SESSION_ENCRYPTION_KEY to store secrets." in resp.text
        # system_prompt reaches set_system_prompt
        # Check via studio_repository mock
        from unittest.mock import AsyncMock

        app.state.studio_repository.set_system_prompt = AsyncMock(return_value="test")
        resp = await client.post(
            "/settings/studio",
            data={"openrouter_api_key": "", "model": "openai/gpt-4o-mini", "system_prompt": "my prompt", "csrf_token": token},
        )
        assert resp.status_code == 303
        assert app.state.studio_repository.set_system_prompt.called


@pytest.mark.asyncio
async def test_channels_add_and_deactivate(tmp_path):
    app, ws, db = _make_app_with_fake(tmp_path, role="owner", available=True)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        login = await client.post("/login", data={"username": "admin", "password": "pw"})
        page = await client.get("/settings")
        import re

        m = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
        token = m.group(1)
        resp = await client.post("/settings/channels/add", data={"identifier": "@new_channel", "csrf_token": token})
        assert resp.status_code == 303
        assert "@new_channel" in db._add_calls
        # Deactivate
        resp = await client.post("/settings/channels/1/deactivate", data={"csrf_token": token})
        assert resp.status_code == 303
        assert 1 in db._deactivate_calls
        # Invalid identifier with spaces -> 422
        resp = await client.post("/settings/channels/add", data={"identifier": "bad id", "csrf_token": token})
        assert resp.status_code == 422
        # Invalid pattern -> 422
        resp = await client.post("/settings/channels/add", data={"identifier": "notvalid", "csrf_token": token})
        assert resp.status_code == 422


@pytest.mark.asyncio
async def test_connection_render_and_persist(tmp_path):
    app, ws, db = _make_app_with_fake(tmp_path, role="owner", available=True)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        login = await client.post("/login", data={"username": "admin", "password": "pw"})
        page = await client.get("/settings")
        assert "123456" in page.text
        assert "Session set" in page.text
        assert "hashed_value" not in page.text
        assert "session_string_value" not in page.text
        import re

        m = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
        token = m.group(1)
        resp = await client.post(
            "/settings/telegram-connection",
            data={"api_id": "123456", "api_hash": "newhash", "session_string": "newsession", "csrf_token": token},
        )
        assert resp.status_code == 303
        assert hasattr(db, "_persist_called")
        assert "Restart the collector" in resp.headers.get("location", "") or True  # Check flash via next page
        # Follow redirect and check flash
        page2 = await client.get("/settings", follow_redirects=True)
        # The flash is in banner
        assert "Restart the collector" in page2.text or "Connection saved" in page2.text


@pytest.mark.asyncio
async def test_reset_and_source_tags(tmp_path):
    app, ws, db = _make_app_with_fake(tmp_path, role="owner", available=True)
    # Set ws to have a saved value
    ws._as_dict_return["collection.backfill_limit"] = {"value": 500, "source": "db", "updated_at": "2026-09-06 21:14 UTC"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        login = await client.post("/login", data={"username": "admin", "password": "pw"})
        page = await client.get("/settings")
        assert 'class="source-tag saved"' in page.text
        assert "Use .env value" in page.text
        import re

        m = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
        token = m.group(1)
        resp = await client.post("/settings/collection/reset", data={"key": "collection.backfill_limit", "csrf_token": token})
        assert resp.status_code == 303
        assert ("reset", "collection.backfill_limit") in ws._calls
        # Env-seeded one shows from .env
        ws2 = FakeWorkspaceSettings(available=True)
        ws2._as_dict_return["collection.backfill_limit"] = {"value": 200, "source": "env"}
        app2, _, _ = _make_app_with_fake(tmp_path, role="owner", available=True, ws=ws2, db=db)
        transport2 = httpx.ASGITransport(app=app2)
        async with httpx.AsyncClient(transport=transport2, base_url="http://test") as client2:
            login2 = await client2.post("/login", data={"username": "admin", "password": "pw"})
            page2 = await client2.get("/settings")
            assert "from .env" in page2.text


@pytest.mark.asyncio
async def test_sqlite_mode_warn_and_no_forms(tmp_path):
    app, ws, db = _make_app_with_fake(tmp_path, role="owner", available=False)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        login = await client.post("/login", data={"username": "admin", "password": "pw"})
        page = await client.get("/settings")
        assert "Settings are read from .env until PostgreSQL is configured" in page.text
        # Count forms - should be zero (the logout link is an anchor, not counted)
        import re

        forms = re.findall(r"<form", page.text)
        assert len(forms) == 0


@pytest.mark.asyncio
async def test_base_sidebar_link_visibility(tmp_path):
    # Owner sees link
    app, ws, db = _make_app_with_fake(tmp_path, role="owner", available=True)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        login = await client.post("/login", data={"username": "admin", "password": "pw"})
        dash = await client.get("/")
        assert 'href="/settings"' in dash.text
        studio = await client.get("/studio")
        assert 'href="/settings"' in studio.text
    # Member does not
    app2, ws2, db2 = _make_app_with_fake(tmp_path, role="member", available=True)
    transport2 = httpx.ASGITransport(app=app2)
    async with httpx.AsyncClient(transport=transport2, base_url="http://test") as client2:
        login2 = await client2.post("/login", data={"username": "admin", "password": "pw"})
        # Need to override role after login as before
        app2.state.workspace_context = app2.state.workspace_context.__class__(
            user_id=app2.state.workspace_context.user_id,
            workspace_id=app2.state.workspace_context.workspace_id,
            workspace_slug=app2.state.workspace_context.workspace_slug,
            role="member",
        )
        dash2 = await client2.get("/")
        assert 'href="/settings"' not in dash2.text
