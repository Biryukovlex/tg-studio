import json
import re
import uuid

import pytest


async def _login(client, settings):
    response = await client.post(
        "/login",
        data={"username": settings.admin_username, "password": settings.admin_password},
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
        "messages": [{"id": str(uuid.uuid4()), "role": "user", "content": "Inspect the selected channel."}],
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
async def test_studio_bootstrap_create_stream_and_reload(client, settings):
    settings.studio_test_mode = True
    await _login(client, settings)
    token = await _csrf(client)

    bootstrap = await client.get("/studio/api/bootstrap")
    assert bootstrap.status_code == 200
    body = bootstrap.json()
    assert body["setup"]["ready"] is True
    assert body["provider"]["configured"] is True
    assert body["selected_channel_id"] == 1
    assert "api_key" not in json.dumps(body)

    created = await client.post(
        "/studio/api/conversations",
        json={"channel_id": 1, "title": "A focused thread"},
        headers={"x-csrf-token": token},
    )
    assert created.status_code == 200
    conversation = created.json()["conversation"]
    # assistant-ui emits an opaque short AG-UI run ID, not necessarily a UUID.
    run_id = "Lad6pwW"
    streamed = await client.post(
        "/studio/api/agent",
        json=_payload(conversation["id"], run_id),
        headers={"accept": "text/event-stream", "x-csrf-token": token},
    )
    assert streamed.status_code == 200
    event_types = [event["type"] for event in _events(streamed.text)]
    assert event_types[0] == "RUN_STARTED"
    assert "TOOL_CALL_START" in event_types
    assert event_types[-1] == "RUN_FINISHED"

    messages = await client.get(f"/studio/api/conversations/{conversation['id']}/messages")
    assert messages.status_code == 200
    assert [message["role"] for message in messages.json()["messages"]] == ["user", "assistant"]
    assert "selected channel context" in messages.json()["messages"][-1]["content"]

    events = await client.get(f"/studio/api/runs/{run_id}/events")
    assert events.status_code == 200
    assert events.json()["run"]["status"] == "succeeded"
    assert uuid.UUID(events.json()["run"]["id"])
    assert events.json()["events"][-1]["event_type"] == "RUN_FINISHED"
    assert all("content" not in event["safe_payload"] for event in events.json()["events"])


@pytest.mark.asyncio
async def test_studio_rejects_cross_workspace_or_missing_csrf_ids(client, settings):
    settings.studio_test_mode = True
    await _login(client, settings)
    token = await _csrf(client)
    missing_csrf = await client.post("/studio/api/conversations", json={"channel_id": 1})
    assert missing_csrf.status_code == 403

    unknown = str(uuid.uuid4())
    response = await client.get(f"/studio/api/conversations/{unknown}/messages")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "conversation_not_found"

    unknown_run = await client.post(
        f"/studio/api/runs/{uuid.uuid4()}/cancel",
        headers={"x-csrf-token": token},
    )
    assert unknown_run.status_code == 404
    assert unknown_run.json()["error"]["code"] == "run_not_found"


@pytest.mark.asyncio
async def test_studio_api_contract_rejects_invalid_requests_and_paginates_events(client, settings):
    settings.studio_test_mode = True
    await _login(client, settings)
    token = await _csrf(client)

    malformed = await client.post(
        "/studio/api/agent",
        content=json.dumps({"threadId": "not-a-run"}),
        headers={
            "content-type": "application/json",
            "accept": "text/event-stream",
            "x-csrf-token": token,
        },
    )
    assert malformed.status_code == 422
    assert malformed.json()["error"]["code"] == "invalid_agent_request"

    invalid_ids = await client.post(
        "/studio/api/agent",
        json={
            "threadId": "thread-not-a-uuid",
            "runId": "run-not-a-uuid",
            "messages": [{"id": "m1", "role": "user", "content": "hello"}],
            "tools": [],
            "context": [],
            "forwardedProps": {},
        },
        headers={"accept": "text/event-stream", "x-csrf-token": token},
    )
    assert invalid_ids.status_code == 422
    assert invalid_ids.json()["error"]["code"] == "invalid_identifier"

    missing_message = await client.post(
        "/studio/api/agent",
        json={
            "threadId": str(uuid.uuid4()),
            "runId": str(uuid.uuid4()),
            "messages": [],
            "tools": [],
            "context": [],
            "forwardedProps": {},
        },
        headers={"accept": "text/event-stream", "x-csrf-token": token},
    )
    assert missing_message.status_code == 404
    assert missing_message.json()["error"]["code"] == "conversation_not_found"

    created = await client.post(
        "/studio/api/conversations",
        json={"channel_id": 1},
        headers={"x-csrf-token": token},
    )
    assert created.status_code == 200
    conversation_id = created.json()["conversation"]["id"]
    run_id = str(uuid.uuid4())
    streamed = await client.post(
        "/studio/api/agent",
        json=_payload(conversation_id, run_id),
        headers={"accept": "text/event-stream", "x-csrf-token": token},
    )
    assert streamed.status_code == 200
    snapshot = await client.get(f"/studio/api/runs/{run_id}/events")
    assert snapshot.status_code == 200
    events = snapshot.json()["events"]
    assert len(events) >= 2
    after_first = await client.get(
        f"/studio/api/runs/{run_id}/events?after={events[0]['sequence']}"
    )
    assert after_first.status_code == 200
    assert all(event["sequence"] > events[0]["sequence"] for event in after_first.json()["events"])


@pytest.mark.asyncio
async def test_studio_mutations_all_require_csrf(client, settings):
    settings.studio_test_mode = True
    await _login(client, settings)

    setup_validation = await client.post("/studio/api/setup/validate")
    conversation = await client.post("/studio/api/conversations", json={"channel_id": 1})
    agent = await client.post(
        "/studio/api/agent",
        json=_payload(str(uuid.uuid4()), str(uuid.uuid4())),
        headers={"accept": "text/event-stream"},
    )
    cancellation = await client.post(f"/studio/api/runs/{uuid.uuid4()}/cancel")

    assert setup_validation.status_code == 403
    assert conversation.status_code == 403
    assert agent.status_code == 403
    assert cancellation.status_code == 403


@pytest.mark.asyncio
async def test_agent_uses_persisted_conversation_and_authorized_channel_context(client, app, settings):
    settings.studio_test_mode = True
    await _login(client, settings)
    token = await _csrf(client)
    created = await client.post(
        "/studio/api/conversations",
        json={"channel_id": 1, "title": "Authorized context"},
        headers={"x-csrf-token": token},
    )
    assert created.status_code == 200
    conversation_id = created.json()["conversation"]["id"]
    run_id = str(uuid.uuid4())
    payload = _payload(conversation_id, run_id)
    payload["messages"] = [
        {"id": "client-old", "role": "user", "content": "Client history must be ignored."},
        {"id": "client-tool", "role": "assistant", "content": "Client tool output must be ignored."},
        {"id": "client-latest", "role": "user", "content": "Use the persisted channel only."},
    ]
    payload["tools"] = [{"name": "read_other_workspace", "description": "Read another workspace", "parameters": {}}]
    payload["context"] = [{"description": "untrusted client context", "value": "workspace=other channel=999 secret=never forward"}]
    calls: list[int] = []
    repository = app.state.studio_repository
    original_context = repository.channel_context

    async def record_context(channel_id: int):
        calls.append(channel_id)
        return await original_context(channel_id)

    repository.channel_context = record_context
    streamed = await client.post(
        "/studio/api/agent",
        json=payload,
        headers={"accept": "text/event-stream", "x-csrf-token": token},
    )
    assert streamed.status_code == 200
    assert calls == [1]

    messages = await client.get(f"/studio/api/conversations/{conversation_id}/messages")
    assert messages.status_code == 200
    contents = [message["content"] for message in messages.json()["messages"]]
    assert contents == ["Use the persisted channel only.", contents[-1]]
    assert all("Client history" not in content and "other workspace" not in content for content in contents)

    # Replacing the service object simulates an application reload while the
    # repository remains authoritative for the persisted conversation.
    from app.studio.service import StudioService

    app.state.studio_service = StudioService(repository, settings)
    reloaded = await client.get(f"/studio/api/conversations/{conversation_id}/messages")
    assert [message["content"] for message in reloaded.json()["messages"]] == contents

    await repository.archive_conversation(uuid.UUID(conversation_id))
    archived_messages = await client.get(f"/studio/api/conversations/{conversation_id}/messages")
    assert archived_messages.status_code == 404
    assert archived_messages.json()["error"]["code"] == "conversation_not_found"
