import json
import re
import uuid
from types import SimpleNamespace

import pytest
from ag_ui.core import RunStartedEvent, RunFinishedEvent, TextMessageStartEvent, TextMessageContentEvent, TextMessageEndEvent
from pydantic_ai import ModelRetry
from pydantic_ai.messages import ModelResponse
from pydantic_ai.ui.ag_ui import AGUIAdapter

from app.config import Settings
from app.studio.agent import _clean_publication_text, _require_publication_text
from app.studio.repository import MemoryStudioRepository
from app.studio.service import StudioService


@pytest.mark.asyncio
async def test_settings_authenticated_csrf_validated_and_workspace_scoped(client, settings, app):
    settings.studio_enabled = settings.studio_test_mode = True
    await client.post("/login", data={"username": settings.admin_username, "password": settings.admin_password})
    home = await client.get("/studio")
    token = re.search(r'<meta name="studio-csrf-token" content="([^"]+)"', home.text).group(1)
    assert (await client.get("/studio/api/settings")).json() == {"system_prompt": ""}
    assert (await client.patch("/studio/api/settings", json={"system_prompt": "x"})).status_code == 403
    headers = {"x-csrf-token": token}
    for invalid in [None, 42, "x" * 12001]:
        assert (await client.patch("/studio/api/settings", headers=headers, json={"system_prompt": invalid})).status_code == 422
    prompt = "Пиши коротко. Без служебных комментариев в постах."
    assert (await client.patch("/studio/api/settings", headers=headers, json={"system_prompt": prompt})).status_code == 200
    assert (await client.get("/studio/api/settings")).json()["system_prompt"] == prompt
    other = MemoryStudioRepository(workspace_id="other-workspace")
    assert await other.get_system_prompt() == ""
    assert (await client.patch("/studio/api/settings", headers=headers, json={"system_prompt": ""})).status_code == 200


@pytest.mark.asyncio
async def test_final_reply_is_persisted_before_finished_and_history_contains_answers(monkeypatch):
    repository = MemoryStudioRepository()
    await repository.set_system_prompt("Use a warm editorial voice.")
    seen = []
    class Adapter:
        build_run_input = staticmethod(AGUIAdapter.build_run_input)
        def __init__(self, **kwargs): pass
        async def run_stream(self, **kwargs):
            seen.append(kwargs)
            yield RunStartedEvent(thread_id=kwargs["conversation_id"], run_id=kwargs["run_id"])
            yield TextMessageStartEvent(message_id="interim")
            yield TextMessageContentEvent(message_id="interim", delta="I am thinking about the old question")
            yield TextMessageEndEvent(message_id="interim")
            await kwargs["on_complete"](SimpleNamespace(output="The final answer", usage=None, response=None))
            yield RunFinishedEvent(thread_id=kwargs["conversation_id"], run_id=kwargs["run_id"])
        def encode_stream(self, stream): return stream
    monkeypatch.setattr("app.studio.service.AGUIAdapter", Adapter)
    service = StudioService(repository, Settings(studio_test_mode=True))
    for request in ["Continue", "Prepare a draft"]:
        conversation = await repository.create_conversation(channel_id=1)
        await repository.append_message(conversation_id=conversation["id"], role="user", content="Old question")
        await repository.append_message(conversation_id=conversation["id"], role="assistant", content="Already answered")
        payload = {"threadId": str(conversation["id"]), "runId": str(uuid.uuid4()), "messages": [{"id": "user", "role": "user", "content": request}], "tools": [], "context": [], "forwardedProps": {}}
        response = await service.stream_request(None, json.dumps(payload).encode())
        events = []
        async for event in response.body_iterator:
            events.append(event)
            if event.type == "RUN_FINISHED":
                assert (await repository.list_messages(conversation["id"]))[-1]["content"] == "The final answer"
        assert [event.delta for event in events if event.type == "TEXT_MESSAGE_CONTENT"] == ["The final answer"]
        assert "Use a warm editorial voice." in seen[-1]["instructions"]
        assert any(isinstance(item, ModelResponse) and item.parts[0].content == "Already answered" for item in seen[-1]["message_history"])


def test_agent_rejects_research_commentary_but_allows_reader_facing_source_links():
    with pytest.raises(ModelRetry):
        _require_publication_text("Основной первоисточник — Computerworld; часть сайтов не открылись у меня.")
    _require_publication_text("Голосовой агент: STT → LLM → TTS. Подробнее: https://example.org/report")
    cleaned, removed = _clean_publication_text(
        "Заголовок\n\nПолезный текст для читателя.\n\nОсновной первоисточник — Example; часть сайтов не открылись у меня."
    )
    assert removed is True
    assert cleaned == "Заголовок\n\nПолезный текст для читателя."


@pytest.mark.asyncio
async def test_existing_artifact_turn_requires_revision_instead_of_duplicate_creation(monkeypatch):
    repository = MemoryStudioRepository()
    conversation = await repository.create_conversation(channel_id=1)
    await repository.create_draft(
        conversation_id=conversation["id"],
        channel_id=1,
        payload={"body": "Existing owner artifact.", "creative": True},
    )
    seen = []

    class Adapter:
        build_run_input = staticmethod(AGUIAdapter.build_run_input)

        def __init__(self, **kwargs):
            pass

        async def run_stream(self, **kwargs):
            seen.append(kwargs["deps"].required_tools)
            yield RunStartedEvent(thread_id=kwargs["conversation_id"], run_id=kwargs["run_id"])
            await kwargs["on_complete"](SimpleNamespace(output="Revision handled.", usage=None, response=None))
            yield RunFinishedEvent(thread_id=kwargs["conversation_id"], run_id=kwargs["run_id"])

        def encode_stream(self, stream):
            return stream

    monkeypatch.setattr("app.studio.service.AGUIAdapter", Adapter)
    service = StudioService(repository, Settings(studio_test_mode=True))
    payload = {
        "threadId": str(conversation["id"]),
        "runId": str(uuid.uuid4()),
        "messages": [{"id": "user", "role": "user", "content": "Сделай драфт поста по первой"}],
        "tools": [],
        "context": [],
        "forwardedProps": {},
    }
    response = await service.stream_request(None, json.dumps(payload).encode())
    async for _ in response.body_iterator:
        pass

    assert seen == [("get_channel_context", "revise_draft")]
