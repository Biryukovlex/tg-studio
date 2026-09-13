from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.studio.repository import MemoryStudioRepository
from app.studio.service import _tool_result_content


async def _login(client, settings) -> None:
    response = await client.post(
        "/login",
        data={"username": settings.admin_username, "password": settings.admin_password},
        follow_redirects=False,
    )
    assert response.status_code == 303


@pytest.mark.asyncio
async def test_complete_tool_result_is_private_and_visible_in_owner_logs(client, app, settings):
    repository = app.state.studio_repository
    conversation = await repository.create_conversation(channel_id=1, title="Private research")
    message = await repository.append_message(
        conversation_id=conversation["id"],
        role="user",
        content="Find a story",
    )
    run = await repository.create_run(
        conversation_id=conversation["id"],
        user_message_id=message["id"],
        requested_model="nex-agi/nex-n2.5-pro:free",
    )
    raw_result = '<script>alert("private")</script>\n{"source":"https://example.test","body":"complete"}'
    await repository.append_event(
        run["id"],
        event_type="TOOL_CALL_RESULT",
        safe_payload={"tool_name": "search_web", "result_length": len(raw_result)},
        result_content=raw_result,
    )

    await _login(client, settings)
    event_response = await client.get(f"/studio/api/runs/{run['id']}/events")
    assert event_response.status_code == 200
    assert raw_result not in event_response.text
    assert "result_content" not in event_response.text

    details_response = await client.get(f"/studio/api/runs/{run['id']}/details")
    assert details_response.status_code == 200
    assert raw_result not in details_response.text
    assert "result_content" not in details_response.text

    logs_response = await client.get("/settings/logs")
    assert logs_response.status_code == 200
    assert "Agent logs" in logs_response.text
    assert "search_web" in logs_response.text
    assert "Private research" in logs_response.text
    assert "&lt;script&gt;alert" in logs_response.text
    assert "<script>alert" not in logs_response.text
    assert "https://example.test" in logs_response.text


@pytest.mark.asyncio
async def test_agent_logs_require_authentication(client):
    response = await client.get("/settings/logs", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_tool_result_serialization_keeps_complete_content():
    raw = "line one\nline two\n" + ("x" * 25_000)
    event = SimpleNamespace(type="TOOL_CALL_RESULT", content=raw)
    assert _tool_result_content(event) == raw

    structured = SimpleNamespace(type="TOOL_CALL_RESULT", content={"items": ["one", "два"]})
    serialized = _tool_result_content(structured)
    assert serialized is not None
    assert '"items"' in serialized
    assert "два" in serialized

    non_result = SimpleNamespace(type="TOOL_CALL_START", content="private")
    assert _tool_result_content(non_result) is None


@pytest.mark.asyncio
async def test_parallel_tool_results_keep_their_tool_names():
    repository = MemoryStudioRepository()
    conversation = await repository.create_conversation(channel_id=1, title="Parallel tools")
    message = await repository.append_message(conversation_id=conversation["id"], role="user", content="Research")
    run = await repository.create_run(
        conversation_id=conversation["id"],
        user_message_id=message["id"],
        requested_model="test",
    )
    await repository.append_event(
        run["id"], event_type="TOOL_CALL_START", safe_payload={"tool_name": "search_web", "tool_call_id": "a"}
    )
    await repository.append_event(
        run["id"], event_type="TOOL_CALL_START", safe_payload={"tool_name": "read_source", "tool_call_id": "b"}
    )
    await repository.append_event(
        run["id"], event_type="TOOL_CALL_RESULT", safe_payload={"tool_call_id": "a"}, result_content="search result"
    )
    await repository.append_event(
        run["id"], event_type="TOOL_CALL_RESULT", safe_payload={"tool_call_id": "b"}, result_content="source result"
    )

    logs = await repository.list_tool_result_logs()
    names_by_result = {row["result_content"]: row["tool_name"] for row in logs}
    assert names_by_result == {"search result": "search_web", "source result": "read_source"}


def test_default_model_is_tool_capable_free_openrouter_model():
    from app.config import Settings

    settings = Settings(_env_file=None)
    assert settings.openrouter_model == "nex-agi/nex-n2.5-pro:free"
