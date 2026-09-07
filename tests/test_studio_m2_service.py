import json
import uuid

import pytest

from app.config import Settings
from app.studio.repository import ActiveRunExists, MemoryStudioRepository
from app.studio.service import StudioService
from app.studio.service import _safe_event_payload


def _payload(conversation_id: uuid.UUID, run_id: uuid.UUID, text: str = "Inspect the channel") -> bytes:
    return json.dumps(
        {
            "threadId": str(conversation_id),
            "runId": str(run_id),
            "messages": [{"id": str(uuid.uuid4()), "role": "user", "content": text}],
            "tools": [],
            "context": [],
            "forwardedProps": {},
        }
    ).encode()


async def _read_stream(response) -> str:
    chunks: list[str] = []
    async for chunk in response.body_iterator:
        chunks.append(chunk)
    return "".join(chunks)


@pytest.mark.asyncio
async def test_service_persists_messages_and_safe_events_on_completion():
    repository = MemoryStudioRepository()
    conversation = await repository.create_conversation(channel_id=1)
    service = StudioService(repository, Settings(studio_test_mode=True))
    run_id = uuid.uuid4()

    response = await service.stream_request(None, _payload(conversation["id"], run_id))
    stream = await _read_stream(response)

    assert response.status_code == 200
    assert '"type":"RUN_STARTED"' in stream
    messages = await repository.list_messages(conversation["id"])
    assert [message["role"] for message in messages] == ["user", "assistant"]
    assert "selected channel context" in messages[-1]["content"]
    run = await repository.get_run(run_id)
    assert run and run["status"] == "succeeded"
    events = await repository.get_events(run_id)
    assert events[0]["event_type"] == "RUN_STARTED"
    assert events[-1]["event_type"] == "RUN_FINISHED"
    assert all("content" not in event["safe_payload"] for event in events)


@pytest.mark.asyncio
async def test_repository_allows_only_one_active_run_per_conversation():
    repository = MemoryStudioRepository()
    conversation = await repository.create_conversation(channel_id=1)
    user = await repository.append_message(conversation_id=conversation["id"], role="user", content="one")
    await repository.create_run(conversation_id=conversation["id"], user_message_id=user["id"], requested_model="test", run_id=uuid.uuid4())
    with pytest.raises(ActiveRunExists):
        await repository.create_run(conversation_id=conversation["id"], user_message_id=user["id"], requested_model="test", run_id=uuid.uuid4())


def test_safe_event_projection_keeps_research_activity_without_source_payload():
    class Event:
        type = "TOOL_CALL_RESULT"
        tool_call_name = "search_web"
        content = {
            "activity": {
                "provider": "searxng",
                "degraded": False,
                "cache_hit": True,
                "result_count": 4,
            },
            "sources": [{"url": "https://secret.example/article", "excerpt": "private text"}],
            "stories": [{"headline": "private headline"}],
        }

    payload = _safe_event_payload(Event())
    assert payload["provider"] == "searxng"
    assert payload["cache_hit"] is True
    assert payload["result_count"] == 4
    assert payload["source_count"] == 1
    assert payload["story_count"] == 1
    assert "secret.example" not in str(payload)
    assert "private text" not in str(payload)


def test_safe_event_projection_decodes_agui_json_tool_results():
    class Event:
        type = "TOOL_CALL_RESULT"
        tool_call_name = "search_web"
        content = json.dumps(
            {
                "activity": {"provider": "searxng", "result_count": 3},
                "sources": [{"url": "https://private.example/one"}, {"url": "https://private.example/two"}],
                "stories": [{"headline": "private story"}],
            }
        )

    payload = _safe_event_payload(Event())
    assert payload["provider"] == "searxng"
    assert payload["result_count"] == 3
    assert payload["source_count"] == 2
    assert payload["story_count"] == 1
    assert "private.example" not in str(payload)
