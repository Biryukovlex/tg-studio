import asyncio
import json
import uuid
from dataclasses import dataclass
from datetime import timedelta

import pytest
from pydantic_ai.ui.ag_ui import AGUIAdapter as RealAGUIAdapter

from app.config import Settings
from app.studio.repository import MemoryStudioRepository, RunClaimLost, utcnow
from app.studio.service import RunCoordinator, StudioService


def _payload(conversation_id: uuid.UUID, run_id: uuid.UUID) -> bytes:
    return json.dumps(
        {
            "threadId": str(conversation_id),
            "runId": str(run_id),
            "messages": [
                {"id": str(uuid.uuid4()), "role": "user", "content": "Keep working after disconnect."}
            ],
            "tools": [],
            "context": [],
            "forwardedProps": {},
        }
    ).encode()


@dataclass
class _Event:
    type: str
    delta: str | None = None


class _SlowAdapter:
    build_run_input = staticmethod(RealAGUIAdapter.build_run_input)

    def __init__(self, **_kwargs):
        pass

    async def run_stream(self, **_kwargs):
        yield _Event("RUN_STARTED")
        await asyncio.sleep(0.12)
        yield _Event("TEXT_MESSAGE_CONTENT", "durable result")
        yield _Event("RUN_FINISHED")

    def encode_stream(self, stream):
        return stream


@pytest.mark.asyncio
async def test_atomic_run_claim_lease_and_worker_owned_finish():
    repository = MemoryStudioRepository()
    conversation = await repository.create_conversation(channel_id=1)
    message = await repository.append_message(
        conversation_id=conversation["id"], role="user", content="claim me"
    )
    run = await repository.create_run(
        conversation_id=conversation["id"],
        user_message_id=message["id"],
        requested_model="test",
    )

    claimed = await repository.claim_run(run["id"], worker_id="worker-a", lease_seconds=30)
    assert claimed and claimed["status"] == "running"
    assert await repository.claim_run(run["id"], worker_id="worker-b") is None
    assert await repository.renew_run_lease(run["id"], worker_id="worker-b") is None
    assert await repository.renew_run_lease(run["id"], worker_id="worker-a") is not None
    with pytest.raises(RunClaimLost):
        await repository.set_run_status(run["id"], status="succeeded", worker_id="worker-b")


@pytest.mark.asyncio
async def test_stale_run_recovery_is_durable_and_releases_conversation():
    repository = MemoryStudioRepository()
    conversation = await repository.create_conversation(channel_id=1)
    message = await repository.append_message(
        conversation_id=conversation["id"], role="user", content="recover me"
    )
    run = await repository.create_run(
        conversation_id=conversation["id"],
        user_message_id=message["id"],
        requested_model="test",
    )
    await repository.claim_run(run["id"], worker_id="dead-worker", lease_seconds=30)
    repository.runs[run["id"]]["lease_expires_at"] = utcnow() - timedelta(seconds=1)

    assert await repository.mark_stale_runs_interrupted() == 1
    recovered = await repository.get_run(run["id"])
    assert recovered and recovered["status"] == "interrupted"
    assert (await repository.get_events(run["id"]))[-1]["event_type"] == "RUN_INTERRUPTED"

    next_message = await repository.append_message(
        conversation_id=conversation["id"], role="user", content="try again"
    )
    next_run = await repository.create_run(
        conversation_id=conversation["id"],
        user_message_id=next_message["id"],
        requested_model="test",
    )
    assert next_run["status"] == "queued"


@pytest.mark.asyncio
async def test_provider_execution_continues_after_http_stream_disconnect(monkeypatch):
    from app.studio import service as service_module

    monkeypatch.setattr(service_module, "AGUIAdapter", _SlowAdapter)
    repository = MemoryStudioRepository()
    conversation = await repository.create_conversation(channel_id=1)
    service = StudioService(
        repository,
        Settings(
            studio_enabled=True,
            studio_test_mode=True,
            studio_run_heartbeat_seconds=0.05,
        ),
    )
    run_id = uuid.uuid4()

    response = await service.stream_request(None, _payload(conversation["id"], run_id))
    first = await anext(response.body_iterator)
    assert first.type == "RUN_STARTED"
    await response.body_iterator.aclose()

    for _ in range(30):
        run = await repository.get_run(run_id)
        if run and run["status"] == "succeeded":
            break
        await asyncio.sleep(0.02)
    assert run and run["status"] == "succeeded"
    messages = await repository.list_messages(conversation["id"])
    assert messages[-1]["content"] == "durable result"


@pytest.mark.asyncio
async def test_durable_cancel_is_seen_without_local_registry(monkeypatch):
    from app.studio import service as service_module

    monkeypatch.setattr(service_module, "AGUIAdapter", _SlowAdapter)
    repository = MemoryStudioRepository()
    conversation = await repository.create_conversation(channel_id=1)
    settings = Settings(
        studio_enabled=True,
        studio_test_mode=True,
        studio_run_heartbeat_seconds=0.05,
    )
    worker = StudioService(repository, settings)
    other_process = StudioService(repository, settings)
    run_id = uuid.uuid4()
    response = await worker.stream_request(None, _payload(conversation["id"], run_id))

    first = await anext(response.body_iterator)
    assert first.type == "RUN_STARTED"
    await other_process.cancel(run_id)
    async for _ in response.body_iterator:
        pass

    run = await repository.get_run(run_id)
    assert run and run["status"] == "cancelled"
    assert (await repository.get_events(run_id))[-1]["event_type"] == "RUN_CANCELLED"


@pytest.mark.asyncio
async def test_run_coordinator_enforces_local_provider_limit():
    coordinator = RunCoordinator(max_concurrency=2)
    observed = 0

    async def work():
        nonlocal observed
        async with coordinator.slot():
            observed = max(observed, coordinator.active)
            await asyncio.sleep(0.02)

    await asyncio.gather(*(work() for _ in range(8)))
    assert observed == 2
    assert coordinator.active == 0
