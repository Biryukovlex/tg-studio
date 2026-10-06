"""Retry uses the failed request and cannot duplicate or cross a conversation."""
import asyncio
import json
import uuid

import pytest
from pydantic_ai import Agent
from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.models.function import FunctionModel

from app.config import Settings
from app.studio.repository import ActiveRunExists, MemoryStudioRepository, StudioRepositoryError
from app.studio.service import StudioService


def payload(conversation, run_id, retry=None):
    return json.dumps({"threadId": str(conversation), "runId": str(run_id),
                       "messages": [{"id": "client", "role": "user", "content": "UNTRUSTED retry text"}],
                       "tools": [], "context": [],
                       "forwardedProps": {"runConfig": {"retryRunId": str(retry)}} if retry else {}}).encode()


async def failed_request(repo, conversation):
    message, run = await repo.append_user_message_and_create_run(
        conversation_id=conversation, content="Summarize this channel", requested_model="test")
    await repo.set_run_status(run["id"], status="failed", stage="failed")
    await repo.append_message(conversation_id=conversation, role="assistant", content="Previous failure", metadata={"run_id": str(run["id"]), "run_failed": True})
    return message, run


@pytest.mark.asyncio
async def test_retry_reuses_stored_user_and_executes_real_stream():
    repo = MemoryStudioRepository()
    conv = await repo.create_conversation(channel_id=1)
    user, failed = await failed_request(repo, conv["id"])
    service = StudioService(repo, Settings(studio_test_mode=True))
    retry_id = uuid.uuid4()
    response = await service.stream_request(None, payload(conv["id"], retry_id, failed["id"]))
    chunks = [chunk async for chunk in response.body_iterator]
    assert '"type":"RUN_FINISHED"' in "".join(chunks)
    messages = await repo.list_messages(conv["id"])
    assert [row["content"] for row in messages if row["role"] == "user"] == [user["content"]]
    retried = await repo.get_run(retry_id)
    assert retried["status"] == "succeeded"
    assert retried["user_message_id"] == user["id"]
    assert len(await repo.get_events(retry_id)) > 1
    stale = await service.stream_request(None, payload(conv["id"], uuid.uuid4(), failed["id"]))
    assert stale.status_code == 409


@pytest.mark.asyncio
async def test_retry_refuses_other_conversation_newer_request_and_concurrent_clicks():
    repo = MemoryStudioRepository()
    conv = await repo.create_conversation(channel_id=1)
    other = await repo.create_conversation(channel_id=1)
    user, failed = await failed_request(repo, conv["id"])
    with pytest.raises(StudioRepositoryError):
        await repo.append_user_message_and_create_run(conversation_id=other["id"], content="x", requested_model="m", retry_run_id=failed["id"])
    results = await asyncio.gather(*[repo.append_user_message_and_create_run(
        conversation_id=conv["id"], content="x", requested_model="m", retry_run_id=failed["id"])
        for _ in range(2)], return_exceptions=True)
    assert sum(isinstance(result, ActiveRunExists) for result in results) == 1
    retry = next(result[1] for result in results if isinstance(result, tuple))
    assert retry["user_message_id"] == user["id"]
    await repo.set_run_status(retry["id"], status="failed")
    await repo.append_message(conversation_id=conv["id"], role="user", content="Newer instruction")
    with pytest.raises(StudioRepositoryError):
        await repo.append_user_message_and_create_run(conversation_id=conv["id"], content="x", requested_model="m", retry_run_id=retry["id"])


@pytest.mark.asyncio
async def test_actual_model_exception_is_classified_before_agui_erases_type():
    async def broken(messages, info):
        raise UnexpectedModelBehavior("Streamed response ended without content or tool calls; secret-value")
        yield ""
    repo = MemoryStudioRepository()
    conv = await repo.create_conversation(channel_id=1)
    service = StudioService(repo, Settings(studio_test_mode=True), agent_factory=lambda settings: Agent(FunctionModel(stream_function=broken)))
    run_id = uuid.uuid4()
    response = await service.stream_request(None, payload(conv["id"], run_id))
    chunks = [chunk async for chunk in response.body_iterator]
    run = await repo.get_run(run_id)
    assert run["status"] == "failed"
    assert run["error_class"] == "UnexpectedModelBehavior"
    assert run["error_code"] == "invalid_agent_output"
    assert "secret-value" not in "".join(chunks)
    assert "secret-value" not in json.dumps(await repo.get_events(run_id), default=str)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_postgres_retry_is_atomic_and_scoped(app, channel_id):
    repo = app.state.studio_repository
    conv = await repo.create_conversation(channel_id=channel_id)
    user, failed = await failed_request(repo, conv["id"])
    results = await asyncio.gather(*[repo.append_user_message_and_create_run(
        conversation_id=conv["id"], content="x", requested_model="m", retry_run_id=failed["id"])
        for _ in range(2)], return_exceptions=True)
    assert sum(isinstance(result, ActiveRunExists) for result in results) == 1
    retry = next(result[1] for result in results if isinstance(result, tuple))
    assert retry["user_message_id"] == user["id"]
    messages = await repo.list_messages(conv["id"])
    assert sum(row["role"] == "user" for row in messages) == 1
    await repo.set_run_status(retry["id"], status="succeeded")
    with pytest.raises(StudioRepositoryError):
        await repo.append_user_message_and_create_run(conversation_id=conv["id"], content="x", requested_model="m", retry_run_id=failed["id"])
