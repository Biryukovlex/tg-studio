"""M6 exit-gate proofs for durable failure isolation.

These tests stay entirely local.  The provider seam fails or is cancelled on
purpose while the repository retains the current artifact, and a collector
cycle runs alongside an unrelated Studio failure.  This is the last-mile
proof that operational failures are contained instead of becoming data loss.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import uuid

import pytest
from pydantic_ai.ui.ag_ui import AGUIAdapter as RealAGUIAdapter

from app.collector import Collector
from app.config import Settings
from app.studio.repository import MemoryStudioRepository
from app.studio.service import StudioService


def _payload(conversation_id: uuid.UUID, run_id: uuid.UUID, text: str) -> bytes:
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


@dataclass
class _Event:
    type: str
    delta: str | None = None


class _FailingAdapter:
    build_run_input = staticmethod(RealAGUIAdapter.build_run_input)

    def __init__(self, **_kwargs):
        pass

    async def run_stream(self, **_kwargs):
        if False:  # keep this an async generator for the AG-UI contract
            yield None
        raise TimeoutError("provider timeout fixture")

    def encode_stream(self, stream):
        return stream


class _CancellableAdapter:
    build_run_input = staticmethod(RealAGUIAdapter.build_run_input)

    def __init__(self, **_kwargs):
        pass

    async def run_stream(self, **_kwargs):
        yield _Event("RUN_STARTED")
        await asyncio.sleep(1)
        yield _Event("TEXT_MESSAGE_CONTENT", "should not replace the draft")
        yield _Event("RUN_FINISHED")

    def encode_stream(self, stream):
        return stream


async def _seed_draft(repository: MemoryStudioRepository):
    conversation = await repository.create_conversation(channel_id=1)
    draft = await repository.create_draft(
        conversation_id=conversation["id"],
        channel_id=1,
        payload={"body": "Owner draft that must survive.", "creative": True},
    )
    return conversation, draft


@pytest.mark.asyncio
async def test_provider_failure_keeps_current_draft(monkeypatch):
    from app.studio import service as service_module

    monkeypatch.setattr(service_module, "AGUIAdapter", _FailingAdapter)
    repository = MemoryStudioRepository()
    conversation, draft = await _seed_draft(repository)
    service = StudioService(repository, Settings(studio_enabled=True, studio_test_mode=True))

    response = await service.stream_request(
        None,
        _payload(conversation["id"], uuid.uuid4(), "Try a provider-backed revision."),
    )
    async for _ in response.body_iterator:
        pass

    current = await repository.get_current_draft(conversation_id=conversation["id"], channel_id=1)
    assert current and current["id"] == draft["id"]
    assert current["body"] == "Owner draft that must survive."


@pytest.mark.asyncio
async def test_cancellation_keeps_current_draft(monkeypatch):
    from app.studio import service as service_module

    monkeypatch.setattr(service_module, "AGUIAdapter", _CancellableAdapter)
    repository = MemoryStudioRepository()
    conversation, draft = await _seed_draft(repository)
    settings = Settings(
        studio_enabled=True,
        studio_test_mode=True,
        studio_run_heartbeat_seconds=0.05,
    )
    service = StudioService(repository, settings)
    run_id = uuid.uuid4()
    response = await service.stream_request(
        None,
        _payload(conversation["id"], run_id, "Cancel this revision."),
    )
    first = await anext(response.body_iterator)
    assert first.type == "RUN_STARTED"
    await service.cancel(run_id)
    async for _ in response.body_iterator:
        pass

    run = await repository.get_run(run_id)
    current = await repository.get_current_draft(conversation_id=conversation["id"], channel_id=1)
    assert run and run["status"] == "cancelled"
    assert current and current["id"] == draft["id"]
    assert current["body"] == "Owner draft that must survive."


class _CollectorEntity:
    id = 991
    title = "Isolation fixture"


class _CollectorMessage:
    action = None
    date = datetime(2026, 9, 3, 10, tzinfo=timezone.utc)
    id = 701
    message = "Collector must continue during Studio failure."
    entities = []
    views = 12
    forwards = 3
    replies = None
    reactions = None


class _CollectorClient:
    async def get_entity(self, identifier):
        return _CollectorEntity()

    async def iter_messages(self, entity, **kwargs):
        assert kwargs.get("limit") is None
        yield _CollectorMessage()


class _CollectorDatabase:
    def __init__(self):
        self.channels = [{"id": 1, "identifier": "@isolation", "title": "Isolation", "active": True}]
        self.posts: list[str] = []
        self.jobs: list[tuple] = []

    async def get_channels(self):
        return self.channels

    async def upsert_channel(self, *args, **kwargs):
        return 1

    async def upsert_post(self, channel_id, message_id, posted_at, text, formatting_entities=None):
        self.posts.append(text)
        return 1

    async def add_snapshot_if_changed(self, *args):
        return True

    async def has_comments(self, post_id):
        return False

    async def claim_collection_job(self, channel_id):
        self.jobs.append(("claim", channel_id))
        return "isolation-job"

    async def finish_collection_job(self, job_id, **kwargs):
        self.jobs.append(("finish", job_id, kwargs))


@pytest.mark.asyncio
async def test_collector_cycle_completes_when_studio_provider_fails():
    database = _CollectorDatabase()
    settings = Settings(
        api_id=1,
        api_hash="hash",
        session_string="session",
        channels="@isolation",
        admin_password="password",
        track_days=0,
        backfill_limit=1,
    )
    collector = Collector(_CollectorClient(), database, settings)

    async def failing_studio_call():
        await asyncio.sleep(0)
        raise TimeoutError("search/provider fixture")

    collection, studio_failure = await asyncio.gather(
        collector.poll_all(reason="studio-failure-isolation"),
        failing_studio_call(),
        return_exceptions=True,
    )
    assert isinstance(collection, dict)
    assert collection["posts_seen"] == 1
    assert collection["errors"] == 0
    assert isinstance(studio_failure, TimeoutError)
    assert database.posts == ["Collector must continue during Studio failure."]
    assert database.jobs[-1][2]["status"] == "succeeded"
