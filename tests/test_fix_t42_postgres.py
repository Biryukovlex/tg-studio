"""T42 PostgreSQL acceptance: diagnostics persist, reload, and display."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _url() -> str:
    url = os.environ.get("TEST_POSTGRES_URL", "")
    if not url:
        pytest.skip("TEST_POSTGRES_URL is required")
    return url


@pytest.mark.integration
@pytest.mark.asyncio
async def test_diagnostics_persist_reload_and_display(client, settings, app, channel_id):
    settings.studio_test_mode = True
    repository = app.state.studio_repository
    conversation = await repository.create_conversation(channel_id=channel_id, title="Diagnostics")
    message = await repository.append_message(
        conversation_id=conversation["id"], role="user", content="Summarize."
    )
    run = await repository.create_run(
        conversation_id=conversation["id"],
        user_message_id=int(message["id"]),
        requested_model="nex-agi/nex-n2.5-pro:free",
    )
    diagnostics = {
        "phase": "startup",
        "error_code": "provider_auth_failed",
        "exception_class": "ModelHTTPError",
        "status_code": 401,
        "provider_code": 401,
        "requested_model": "nex-agi/nex-n2.5-pro:free",
        "elapsed_ms": 120,
        "requests": 1,
        "tool_calls": 0,
    }
    await repository.set_run_status(
        run["id"],
        status="failed",
        stage="failed",
        error_code="provider_auth_failed",
        error_message="OpenRouter rejected the API key. Check it in Settings → Studio.",
        usage={"requests": 1, "tool_calls": 0},
        diagnostics=diagnostics,
    )
    await repository.append_event(
        run_id=run["id"],
        event_type="RUN_ERROR",
        safe_payload={"code": "provider_auth_failed", "diagnostics": diagnostics},
    )

    # Reload through a fresh repository instance on the same workspace.
    from app.studio.repository import StudioRepository

    fresh = StudioRepository(app.state.db)
    reloaded = await fresh.get_run(run["id"])
    assert reloaded is not None
    assert reloaded["error_phase"] == "startup"
    assert reloaded["error_class"] == "ModelHTTPError"
    assert reloaded["error_status"] == 401
    assert reloaded["error_provider_code"] == 401
    assert "sk-or-" not in json.dumps(reloaded, default=str)

    login = await client.post(
        "/login",
        data={"username": settings.admin_username, "password": settings.admin_password},
        follow_redirects=False,
    )
    assert login.status_code == 303
    details = await client.get(f"/studio/api/runs/{run['id']}/details")
    assert details.status_code == 200
    payload = details.json()
    assert payload["details"]["diagnostics"] == {
        "phase": "startup",
        "exception_class": "ModelHTTPError",
        "status_code": 401,
        "provider_code": 401,
    }
    assert payload["details"]["error"]["code"] == "provider_auth_failed"
    events = await client.get(f"/studio/api/runs/{run['id']}/events")
    assert events.status_code == 200
    run_errors = [event for event in events.json()["events"] if event["event_type"] == "RUN_ERROR"]
    assert run_errors
    assert run_errors[0]["safe_payload"]["diagnostics"]["error_code"] == "provider_auth_failed"
    assert "sk-or-" not in events.text


@pytest.mark.integration
def test_migration_0017_round_trip():
    """Upgrade/downgrade proof for the diagnostics columns (disposable DB)."""

    url = _url()
    env = {**os.environ, "DATABASE_URL": url}

    def _alembic(*args: str) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "alembic", *args],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            env=env,
        )
        assert result.returncode == 0, result.stderr[-2000:]

    _alembic("downgrade", "0015_consent_default")
    _alembic("upgrade", "head")

    async def _columns():
        from sqlalchemy import text as _text
        from sqlalchemy.ext.asyncio import create_async_engine as _engine

        engine = _engine(url)
        try:
            async with engine.connect() as conn:
                rows = (
                    await conn.execute(
                        _text(
                            "SELECT column_name FROM information_schema.columns "
                            "WHERE table_name='studio_agent_runs' AND column_name LIKE 'error\\_%'"
                        )
                    )
                ).scalars().all()
                assert {"error_phase", "error_class", "error_status", "error_provider_code"} <= set(rows), rows
        finally:
            await engine.dispose()

    import asyncio as _asyncio

    _asyncio.run(_columns())
