"""T45 Settings + Agent logs view: tabs/scope, channel cards/actions, JSON
save merge, secrets, and server-backed log filtering.

Synthetic fixtures only (FakeDB + MemoryStudioRepository). No PostgreSQL,
no network, no real channels or credentials.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path

import httpx
import pytest

from app.config import Settings
from app.web.links import normalize_channel_identifier


def _fake_settings_app(tmp_path, role="owner", restart_required=False):
    from cryptography.fernet import Fernet

    from app.collector import Collector
    from app.web.routes import WorkspaceContext, create_app

    key = Fernet.generate_key().decode("ascii")

    class FakeStore:
        def __init__(self):
            self.available = True
            self._rows = {}
            self.telegram_restart_required = restart_required
            self._dict = {
                "collection.poll_minutes": {"value": 15.0, "source": "default"},
                "collection.track_days": {"value": 30, "source": "default"},
                "collection.backfill_limit": {"value": 200, "source": "default"},
                "studio.openrouter_api_key": {"set": False, "source": "default"},
                "studio.model": {"value": "openai/gpt-4o-mini", "source": "default"},
                "research.enabled": {"value": False, "source": "default"},
                "research.blocked_domains": {"value": "", "source": "default"},
            }

        def as_dict(self):
            return dict(self._dict)

        def validate(self, key, value):
            from app.workspace_settings import SETTINGS

            return SETTINGS[key][2](value)

        async def set_many(self, changes, reset_keys=()):
            for key in reset_keys:
                if key not in (
                    "collection.poll_minutes",
                    "collection.track_days",
                    "collection.backfill_limit",
                    "studio.openrouter_api_key",
                    "studio.model",
                    "research.enabled",
                    "research.blocked_domains",
                ):
                    raise ValueError(f"unknown settings key: {key}")
            for key, value in changes.items():
                self.validate(key, value)
            for key in reset_keys:
                self._rows.pop(key, None)
                self._dict[key] = {"value": "", "source": "default"}
            for key, value in changes.items():
                self._rows[key] = value
                if key == "studio.openrouter_api_key":
                    self._dict[key] = {"set": True, "source": "db", "updated_at": "2026-10-02 00:00"}
                else:
                    self._dict[key] = {"value": value, "source": "db", "updated_at": "2026-10-02 00:00"}

        async def reset(self, key):
            await self.set_many({}, reset_keys=(key,))

    class FakeDB:
        def __init__(self):
            self.workspace_id = uuid.uuid4()
            self.user_id = uuid.uuid4()
            self.workspace_slug = "community"
            self._channels = [
                {"id": 11, "identifier": "@live_channel", "title": "Live", "chat_id": -100111, "active": True},
                {"id": 22, "identifier": "@paused_channel", "title": "", "chat_id": None, "active": False},
            ]
            self._next_id = 23

        async def get_channels(self):
            return [dict(row) for row in self._channels]

        async def get_channels_for_settings(self):
            return [dict(row) for row in self._channels]

        async def channel_data_summaries(self):
            return {
                11: {"posts": 7, "comments": 3, "conversations": 2, "drafts": 1},
                22: {"posts": 0, "comments": 0, "conversations": 0, "drafts": 0},
            }

        async def telegram_connection_status(self, label):
            return {"configured": False, "api_id": 12345, "has_session": False, "updated_at": None}

        async def add_channel(self, identifier):
            ident = normalize_channel_identifier(identifier)
            for row in self._channels:
                if row["identifier"] == ident:
                    row["active"] = True
                    return int(row["id"])
            new_id = self._next_id
            self._next_id += 1
            self._channels.append({"id": new_id, "identifier": ident, "title": "", "chat_id": None, "active": True})
            return new_id

        async def deactivate_channel(self, channel_id):
            for row in self._channels:
                if int(row["id"]) == int(channel_id) and row["active"]:
                    row["active"] = False
                    return True
            return False

        async def delete_channel(self, channel_id, confirmation=""):
            for row in list(self._channels):
                if int(row["id"]) == int(channel_id):
                    if confirmation.strip() != row["identifier"]:
                        raise ValueError(f"Type {row['identifier']} to confirm deletion.")
                    self._channels.remove(row)
                    return {"identifier": row["identifier"], "posts": 0, "comments": 0, "conversations": 0, "drafts": 0}
            return None

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
        telegram_session_encryption_key=key,
    )
    db = FakeDB()
    ws = FakeStore()
    collector = Collector(None, db, settings)
    collector.workspace_settings = ws
    app = create_app(collector, settings, workspace_settings=ws)
    app.state.workspace_context = WorkspaceContext(
        user_id=db.user_id, workspace_id=db.workspace_id, workspace_slug="community", role=role
    )
    from app.studio.repository import MemoryStudioRepository

    app.state.studio_repository = MemoryStudioRepository()
    return app, ws, db


async def _login(client, username="admin", password="pw"):
    resp = await client.post("/login", data={"username": username, "password": password}, follow_redirects=False)
    assert resp.status_code == 303


def _csrf_token(page_text: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', page_text)
    assert match, "CSRF token not found in settings page"
    return match.group(1)


def _client_for(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _seed_logs(app):
    repo = app.state.studio_repository
    repo.channels = [
        {"id": 11, "identifier": "@live_channel", "title": "Live", "active": True},
        {"id": 22, "identifier": "@paused_channel", "title": "", "active": True},
    ]
    conv_a = await repo.create_conversation(channel_id=11, title="Alpha research")
    msg_a = await repo.append_message(conversation_id=conv_a["id"], role="user", content="Find alpha")
    run_a = await repo.create_run(conversation_id=conv_a["id"], user_message_id=msg_a["id"], requested_model="model-req-a")
    await repo.set_run_status(run_a["id"], status="succeeded", actual_model="model-act-a")
    await repo.append_event(
        run_a["id"], event_type="TOOL_CALL_RESULT",
        safe_payload={"tool_name": "search_web"}, result_content="alpha complete result",
    )
    conv_b = await repo.create_conversation(channel_id=22, title="Beta outage")
    msg_b = await repo.append_message(conversation_id=conv_b["id"], role="user", content="Find beta")
    run_b = await repo.create_run(conversation_id=conv_b["id"], user_message_id=msg_b["id"], requested_model="model-req-b")
    await repo.set_run_status(run_b["id"], status="failed", actual_model="model-act-b")
    await repo.append_event(
        run_b["id"], event_type="TOOL_CALL_RESULT",
        safe_payload={"tool_name": "read_source"}, result_content="beta complete result",
    )
    conv_c = await repo.create_conversation(channel_id=11, title="Gamma draft")
    msg_c = await repo.append_message(conversation_id=conv_c["id"], role="user", content="Draft gamma")
    run_c = await repo.create_run(conversation_id=conv_c["id"], user_message_id=msg_c["id"], requested_model="model-req-c")
    await repo.set_run_status(run_c["id"], status="cancelled")
    await repo.append_event(
        run_c["id"], event_type="TOOL_CALL_RESULT",
        safe_payload={"tool_name": "search_web"}, result_content="gamma complete result",
    )
    return conv_a, conv_b, conv_c


@pytest.mark.asyncio
async def test_scope_and_view_tabs_share_the_page_header(tmp_path):
    app, _, _ = _fake_settings_app(tmp_path)
    async with _client_for(app) as client:
        await _login(client)
        for path in ("/settings", "/settings/logs"):
            page = await client.get(path)
            assert page.status_code == 200
            scope = page.text.index("data-settings-scope")
            tabs = page.text.index("settings-view-tabs")
            assert scope < tabs < page.text.index('id="main-content"'), f"scope and tabs must share the header on {path}"
            assert "All channels" in page.text and "profiles and prompts stay in Studio" in page.text
            assert "Configuration" in page.text and "Agent logs" in page.text


@pytest.mark.asyncio
async def test_channel_cards_expose_status_counts_actions(tmp_path):
    app, _, _ = _fake_settings_app(tmp_path)
    async with _client_for(app) as client:
        await _login(client)
        page = await client.get("/settings")
        assert page.status_code == 200
        text = page.text
        assert "data-channel-cards" in text
        assert "@live_channel" in text and "@paused_channel" in text
        # Status, counts and distinct actions per channel state.
        assert "Collecting" in text and "Deactivated" in text
        assert "Open Studio" in text
        assert "Reactivate" in text
        assert "Deactivate" in text
        assert "Delete data" in text
        # Typed exact-identifier confirmation and reactivate reuses add-channel.
        assert 'data-delete-dialog-open="delete-channel-22"' in text
        assert 'Type <code>@paused_channel</code> to confirm' in text
        reactivate = re.search(
            r'<form method="post" action="/settings/channels/add" class="inline-form">\s*'
            r'<input type="hidden" name="csrf_token"[^>]*>\s*'
            r'<input type="hidden" name="identifier" value="@paused_channel">',
            text,
        )
        assert reactivate, "deactivated channels reactivate through the add-channel operation"


@pytest.mark.asyncio
async def test_restart_banner_reflects_real_backend_state(tmp_path):
    app, _, _ = _fake_settings_app(tmp_path, restart_required=False)
    async with _client_for(app) as client:
        await _login(client)
        idle = await client.get("/settings")
        assert "data-restart-notice" in idle.text
        assert "data-idle" in idle.text
        assert "collector restarts" in idle.text.lower()
        assert "restart-banner" not in idle.text
    app2, _, _ = _fake_settings_app(tmp_path, restart_required=True)
    async with _client_for(app2) as client:
        await _login(client)
        required = await client.get("/settings")
        assert 'class="banner warn restart-banner" data-restart-notice' in required.text


@pytest.mark.asyncio
async def test_settings_save_json_merges_without_secret_echo(tmp_path):
    app, ws, _ = _fake_settings_app(tmp_path)
    async with _client_for(app) as client:
        await _login(client)
        page = await client.get("/settings")
        token = _csrf_token(page.text)
        # Adapter wiring is present for in-place saves.
        assert 'data-settings-save="collection"' in page.text
        assert 'data-provenance-for="collection.poll_minutes"' in page.text
        assert "data-save-feedback" in page.text
        good = await client.post(
            "/settings/collection",
            data={"poll_minutes": "30", "track_days": "30", "backfill_limit": "200", "csrf_token": token},
            headers={"Accept": "application/json"},
        )
        assert good.status_code == 200
        payload = good.json()
        assert payload["ok"] is True and payload["section"] == "collection"
        assert payload["fields"]["collection.poll_minutes"]["source"] == "db"
        assert "restart_required" in payload and "setup" in payload and "connection" in payload
        # Secrets stay write-only: blank keeps, plaintext never echoes.
        secret = await client.post(
            "/settings/studio",
            data={"openrouter_api_key": "sk-test-secret", "model": "openai/gpt-4o-mini", "csrf_token": token},
            headers={"Accept": "application/json"},
        )
        assert secret.status_code == 200
        assert "sk-test-secret" not in secret.text
        assert secret.json()["fields"]["studio.openrouter_api_key"]["set"] is True
        snapshot = await client.get("/settings", headers={"Accept": "application/json"})
        assert "sk-test-secret" not in snapshot.text


@pytest.mark.asyncio
async def test_settings_validation_applies_nothing(tmp_path):
    app, ws, _ = _fake_settings_app(tmp_path)
    async with _client_for(app) as client:
        await _login(client)
        page = await client.get("/settings")
        token = _csrf_token(page.text)
        bad = await client.post(
            "/settings/collection",
            data={"poll_minutes": "0", "track_days": "30", "backfill_limit": "200", "csrf_token": token},
            headers={"Accept": "application/json"},
        )
        assert bad.status_code == 422
        assert bad.json()["ok"] is False
        assert "poll_minutes" in bad.json()["errors"]
        assert ws._rows == {}, "invalid saves must not partially apply"


@pytest.mark.asyncio
async def test_channel_add_deactivate_and_delete_confirmation(tmp_path):
    app, _, db = _fake_settings_app(tmp_path)
    async with _client_for(app) as client:
        await _login(client)
        page = await client.get("/settings")
        token = _csrf_token(page.text)
        added = await client.post(
            "/settings/channels/add",
            data={"identifier": "@fresh_channel", "csrf_token": token},
            headers={"Accept": "application/json"},
        )
        assert added.status_code == 200
        assert added.json()["ok"] is True
        stopped = await client.post(
            "/settings/channels/11/deactivate",
            data={"csrf_token": token},
            headers={"Accept": "application/json"},
        )
        assert stopped.status_code == 200
        assert stopped.json()["ok"] is True
        # Wrong typed confirmation deletes nothing.
        denied = await client.post(
            "/settings/channels/22/delete",
            data={"confirmation": "@live_channel", "csrf_token": token},
            headers={"Accept": "application/json"},
        )
        assert denied.status_code == 422
        assert any(row["identifier"] == "@paused_channel" for row in db._channels)
        # Exact identifier deletes that synthetic channel only.
        removed = await client.post(
            "/settings/channels/22/delete",
            data={"confirmation": "@paused_channel", "csrf_token": token},
            headers={"Accept": "application/json"},
        )
        assert removed.status_code == 200
        assert removed.json()["ok"] is True
        assert not any(row["identifier"] == "@paused_channel" for row in db._channels)
        assert any(row["identifier"] == "@live_channel" for row in db._channels)


@pytest.mark.asyncio
async def test_logs_filter_on_server_across_all_results(tmp_path):
    app, _, _ = _fake_settings_app(tmp_path)
    await _seed_logs(app)
    async with _client_for(app) as client:
        await _login(client)
        failed = await client.get("/settings/logs?status=failed")
        assert failed.status_code == 200
        assert "Beta outage" in failed.text
        assert "Alpha research" not in failed.text
        assert "Gamma draft" not in failed.text
        query = await client.get("/settings/logs?q=alpha")
        assert "Alpha research" in query.text
        assert "Beta outage" not in query.text
        channel = await client.get("/settings/logs?channel=22")
        assert "Beta outage" in channel.text
        assert "Alpha research" not in channel.text
        # Complete stored results render escaped, never raw.
        assert "alpha complete result" in query.text
        # Pagination preserves the server filters across pages.
        second = await client.get("/settings/logs?page_size=1&page=2")
        assert second.status_code == 200
        assert "Page 2" in second.text
        assert "page=1" in second.text
        filtered_page = await client.get("/settings/logs?q=result&status=&page_size=1&page=1")
        assert "Gamma draft" in filtered_page.text
        assert "Beta outage" not in filtered_page.text
        assert "q=result" in filtered_page.text and "page=2" in filtered_page.text


@pytest.mark.asyncio
async def test_logs_show_models_states_and_safe_details(tmp_path):
    app, _, _ = _fake_settings_app(tmp_path)
    await _seed_logs(app)
    async with _client_for(app) as client:
        await _login(client)
        page = await client.get("/settings/logs")
        assert "Requested model" in page.text and "Actual model" in page.text
        assert "model-req-a" in page.text and "model-act-a" in page.text
        # Failed/cancelled/succeeded pills stay visually distinct.
        assert 'class="pill bad">failed' in page.text
        assert 'class="pill warn">cancelled' in page.text
        assert 'class="pill ok">succeeded' in page.text
        assert "data-logs-filter" in page.text
        assert "data-logs-availability" in page.text


@pytest.mark.asyncio
async def test_logs_json_contract_and_invalid_filters(tmp_path):
    app, _, _ = _fake_settings_app(tmp_path)
    await _seed_logs(app)
    async with _client_for(app) as client:
        await _login(client)
        resp = await client.get("/settings/logs", headers={"Accept": "application/json"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["available"] is True and body["total"] == 3
        first = body["logs"][0]
        for field in ("requested_model", "actual_model", "run_status", "diagnostics", "channel_id", "created_at"):
            assert field in first, f"logs JSON must carry {field}"
        bad_status = await client.get("/settings/logs?status=banana", headers={"Accept": "application/json"})
        assert bad_status.status_code == 422
        assert bad_status.json()["ok"] is False
        bad_channel = await client.get("/settings/logs?channel=abc")
        assert bad_channel.status_code == 422


def test_app_js_consumes_json_contracts():
    app_js = (Path(__file__).resolve().parent.parent / "app" / "web" / "static" / "app.js").read_text(encoding="utf-8")
    assert 'Accept' in app_js and 'application/json' in app_js
    assert "data-settings-save" in app_js
    assert "mergeProvenance" in app_js or "mergeSetupState" in app_js
    assert "DOMParser" not in app_js, "settings saves must merge JSON, not scrape HTML"
    assert "beforeunload" in app_js, "unsaved edits need a navigation guard"
    assert "data-settings-section-nav" in app_js or "ArrowRight" in app_js
    assert "data-logs-availability" in app_js
    assert "confirm(" in app_js


def test_style_css_covers_scope_cards_states_and_toolbar():
    css = (Path(__file__).resolve().parent.parent / "app" / "web" / "static" / "style.css").read_text(encoding="utf-8")
    assert ".settings-scope" in css
    assert ".channel-cards" in css
    assert ".settings-table-wrap" in css
    assert ".pill.bad" in css and ".pill.warn" in css and ".pill.info" in css
    assert ".log-toolbar" in css
    assert ".save-feedback.is-dirty" in css
    assert ".agent-log-diagnostics" in css
