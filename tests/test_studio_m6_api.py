import re
import uuid

import pytest


async def _login(client, settings) -> str:
    response = await client.post(
        "/login",
        data={"username": settings.admin_username, "password": settings.admin_password},
        follow_redirects=False,
    )
    assert response.status_code == 303
    page = await client.get("/studio")
    match = re.search(r'<meta name="studio-csrf-token" content="([^"]+)"', page.text)
    assert match
    return match.group(1)


@pytest.mark.asyncio
async def test_reload_discovers_active_run_and_returns_durable_snapshot(client, app, settings):
    settings.studio_enabled = True
    settings.studio_test_mode = True
    token = await _login(client, settings)
    created = await client.post(
        "/studio/api/conversations",
        json={"channel_id": 1, "title": "Durable work"},
        headers={"x-csrf-token": token},
    )
    conversation_id = uuid.UUID(created.json()["conversation"]["id"])
    repository = app.state.studio_repository
    message = await repository.append_message(
        conversation_id=conversation_id,
        role="user",
        content="Continue this after reload.",
    )
    run = await repository.create_run(
        conversation_id=conversation_id,
        user_message_id=message["id"],
        requested_model="test",
    )
    await repository.claim_run(run["id"], worker_id="worker-a", lease_seconds=120)
    await repository.append_event(run["id"], event_type="RUN_STARTED", safe_payload={})
    await repository.append_event(
        run["id"],
        event_type="TOOL_CALL_START",
        safe_payload={"tool_name": "get_channel_context"},
    )

    discovered = await client.get(f"/studio/api/conversations/{conversation_id}/active-run")
    assert discovered.status_code == 200
    assert discovered.json()["run"]["id"] == str(run["id"])
    assert discovered.json()["run"]["status"] == "running"

    snapshot = await client.get(f"/studio/api/runs/{run['id']}/events")
    assert [event["event_type"] for event in snapshot.json()["events"]] == [
        "RUN_STARTED",
        "TOOL_CALL_START",
    ]
    subsequent = await client.get(f"/studio/api/runs/{run['id']}/events?after=1")
    assert [event["sequence"] for event in subsequent.json()["events"]] == [2]

    bootstrap = await client.get("/studio/api/bootstrap")
    assert bootstrap.status_code == 200
    assert bootstrap.json()["active_run"]["id"] == str(run["id"])
    assert "worker" not in str(bootstrap.json()["active_run"]).lower()


def test_frontend_restores_and_polls_active_run_contract():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    source = (root / "studio-frontend/src/main.tsx").read_text(encoding="utf-8")
    bundle = (root / "app/web/static/studio-dist/assets/studio.js").read_text(encoding="utf-8")
    assert "/active-run" in source
    assert "active-run" in source
    assert "onRunStartedEvent" in source
    assert "?after=" in source
    assert "onRunStartedEvent" in bundle
