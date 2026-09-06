from datetime import datetime, timezone

import pytest
from telethon import types

from app.collector import Collector
from app.config import Settings


class _Entity:
    id = 123
    title = "Boundary channel"


class _Message:
    action = None
    date = datetime(2024, 1, 1, tzinfo=timezone.utc)
    id = 9
    message = "a complete post body"
    entities = [types.MessageEntityBold(offset=0, length=1)]
    views = 4
    forwards = 1
    replies = None
    reactions = None


class _Client:
    def __init__(self):
        self.message_kwargs = []

    async def get_entity(self, identifier):
        return _Entity()

    async def iter_messages(self, entity, **kwargs):
        self.message_kwargs.append(kwargs)
        yield _Message()


class _AsyncDB:
    def __init__(self):
        self.channels = [{"id": 1, "identifier": "@boundary"}]
        self.jobs = []
        self.posts = []
        self.post_entities = []

    async def get_channels(self):
        return self.channels

    async def upsert_channel(self, *args, **kwargs):
        return 1

    async def upsert_post(self, channel_id, message_id, posted_at, text, formatting_entities=None):
        self.posts.append(text)
        self.post_entities.append(formatting_entities)
        return 1

    async def add_snapshot_if_changed(self, *args):
        return True

    async def has_comments(self, post_id):
        return False

    async def claim_collection_job(self, channel_id):
        self.jobs.append(("claim", channel_id))
        return "job-1"

    async def finish_collection_job(self, job_id, **kwargs):
        self.jobs.append(("finish", job_id, kwargs))


@pytest.mark.asyncio
async def test_collector_uses_async_repository_and_unbounded_history_mode():
    settings = Settings(
        api_id=1,
        api_hash="hash",
        session_string="session",
        channels="@boundary",
        admin_password="password",
        track_days=0,
        backfill_limit=1,
    )
    client = _Client()
    db = _AsyncDB()
    collector = Collector(client, db, settings)
    summary = await collector.poll_all(reason="test")
    assert summary["posts_seen"] == 1
    assert db.posts == ["a complete post body"]
    assert db.post_entities == [[{"type": "bold", "offset": 0, "length": 1}]]
    assert client.message_kwargs == [{"limit": None}]
    assert db.jobs[0] == ("claim", 1)
    assert db.jobs[1][0:2] == ("finish", "job-1")
    assert db.jobs[1][2]["status"] == "succeeded"
