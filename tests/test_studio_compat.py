import json
import re
import uuid

import pytest


async def _login(client):
    response = await client.post(
        "/login",
        data={"username": "test-admin", "password": "test-password"},
        follow_redirects=False,
    )
    assert response.status_code == 303


async def _csrf(client) -> str:
    response = await client.get("/studio")
    assert response.status_code == 200
    match = re.search(r'<meta name="studio-csrf-token" content="([^"]+)"', response.text)
    assert match
    return match.group(1)


def _payload(conversation_id: str, run_id: str) -> dict:
    return {
        "threadId": conversation_id,
        "runId": run_id,
        "messages": [{"id": str(uuid.uuid4()), "role": "user", "content": "Inspect the channel context."}],
        "tools": [],
        "context": [],
        "forwardedProps": {},
    }


def _events(stream: str) -> list[dict]:
    return [
        json.loads(line[6:])
        for chunk in stream.split("\n\n")
        for line in chunk.splitlines()
        if line.startswith("data: ")
    ]


@pytest.mark.asyncio
async def test_studio_spike_alias_is_disabled_by_default(client):
    await _login(client)
    response = await client.get("/studio-spike", follow_redirects=False)
    assert response.status_code == 404
    assert response.json()["detail"] == "Studio is disabled"


@pytest.mark.asyncio
async def test_studio_spike_alias_redirects_to_production_surface(client, settings):
    settings.studio_enabled = True
    settings.studio_test_mode = True
    await _login(client)
    response = await client.get("/studio-spike", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "/studio"


@pytest.mark.asyncio
async def test_studio_spike_alias_uses_durable_m2_stream_and_events(client, settings):
    settings.studio_enabled = True
    settings.studio_test_mode = True
    await _login(client)
    token = await _csrf(client)
    created = await client.post(
        "/studio/api/conversations",
        json={"channel_id": 1},
        headers={"x-csrf-token": token},
    )
    assert created.status_code == 200
    conversation_id = created.json()["conversation"]["id"]
    run_id = str(uuid.uuid4())
    response = await client.post(
        "/studio-spike/api/agent",
        json=_payload(conversation_id, run_id),
        headers={"accept": "text/event-stream", "x-csrf-token": token},
    )
    assert response.status_code == 200
    event_types = [event["type"] for event in _events(response.text)]
    assert event_types[0] == "RUN_STARTED"
    assert "TOOL_CALL_START" in event_types
    assert event_types[-1] == "RUN_FINISHED"

    reloaded = await client.get(f"/studio-spike/api/runs/{run_id}/events")
    assert reloaded.status_code == 200
    assert reloaded.json()["run_id"] == run_id
    assert reloaded.json()["thread_id"] == conversation_id
    assert reloaded.json()["events"][-1]["type"] == "RUN_FINISHED"
    assert all("content" not in event for event in reloaded.json()["events"])


@pytest.mark.asyncio
async def test_studio_spike_alias_preserves_auth_and_csrf_boundaries(client, settings):
    settings.studio_enabled = True
    settings.studio_test_mode = True
    response = await client.post("/studio-spike/api/agent", json={})
    assert response.status_code == 303
    assert response.headers["location"] == "/login"

    await _login(client)
    token = await _csrf(client)
    missing = await client.post(
        "/studio-spike/api/agent",
        json={},
        headers={"accept": "text/event-stream"},
    )
    assert missing.status_code == 403
    assert missing.json() == {"detail": "CSRF validation failed"}

    unknown = await client.get("/studio-spike/api/runs/not-a-uuid/events")
    assert unknown.status_code == 404
    assert unknown.json()["detail"] == "Run not found"

    cancel = await client.post(
        f"/studio-spike/api/runs/{uuid.uuid4()}/cancel",
        headers={"x-csrf-token": token},
    )
    assert cancel.status_code == 404
    assert cancel.json()["detail"] == "Run not found"
