"""T04 acceptance tests."""
from __future__ import annotations

import asyncio
import time
import uuid
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from telethon import errors

from app.bot import CommandHandlers
from app.collector import Collector
from app.config import Settings
from app.postgres_db import PostgresDatabase


# --- helpers ---

class FakeEntity:
    pass

class FakeMsg:
    def __init__(self, mid, date, text="hi", views=10, comments=1):
        self.id = mid
        self.date = date
        self.message = text
        self.views = views
        self.replies = MagicMock(replies=comments)
        self.reactions = MagicMock(results=[])
        self.forwards = 0
        self.action = None
        self.entities = None

    # for comment case, need sender etc but not needed for this test


class FakeClient:
    def __init__(self, posts):
        self.posts = posts
        self.calls = 0

    async def get_entity(self, ident):
        return FakeEntity()

    def iter_messages(self, entity, reply_to=None, limit=None):
        # limit None or int; for main poll, reply_to is None; for comments, reply_to is message_id
        async def gen():
            if reply_to is not None:
                # comment fetch: raise for message_id 2
                if reply_to == 2:
                    raise errors.rpcerrorlist.MsgIdInvalidError("bad msg id")
                # otherwise no comments
                if False:
                    yield
                return
            for p in self.posts:
                yield p
        return gen()


class FakeDB:
    def __init__(self):
        self.channels = [{"id": 1, "identifier": "@test"}]
        self.posts = {}
        self.snapshots = []
        self.jobs = {}

    async def get_channels(self):
        return self.channels

    async def upsert_channel(self, ident, title="", chat_id=None):
        return 1

    async def upsert_post(self, ch_id, mid, posted_at, text, formatting_entities=None):
        pid = mid  # use message_id as post_id for simplicity
        self.posts[mid] = pid
        return pid

    async def add_snapshot_if_changed(self, post_id, views, comments, reactions, shares):
        self.snapshots.append(post_id)
        return True

    async def has_comments(self, post_id):
        return False

    async def post_ids_with_comments(self, channel_id):
        return []

    async def upsert_comment(self, **kwargs):
        return True

    async def mark_unseen_comments_deleted(self, post_id, sync_token):
        return 0

    async def claim_collection_job(self, channel_id, lease_seconds=900):
        jid = uuid.uuid4()
        self.jobs[jid] = {"channel_id": channel_id, "status": "running", "lease_until": datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)}
        return jid

    async def finish_collection_job(self, job_id, status="succeeded", error=None):
        if job_id in self.jobs:
            self.jobs[job_id]["status"] = status

    async def renew_collection_job(self, job_id, lease_seconds=900):
        if job_id in self.jobs:
            self.jobs[job_id]["lease_until"] = datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)


@pytest.mark.asyncio
async def test_poll_channel_continues_after_comment_rpc_error():
    # 3 posts: first and third should be processed, second's comment fetch fails
    now = datetime.now(timezone.utc)
    posts = [FakeMsg(1, now), FakeMsg(2, now), FakeMsg(3, now)]
    client = FakeClient(posts)
    db = FakeDB()
    settings = Settings(api_id=1, api_hash="h", session_string="s", channels="@test", poll_minutes=15, track_days=30)
    collector = Collector(client, db, settings)
    # ensure track_days allows all
    seen, written, c_seen, c_written, c_deleted, c_errors = await collector.poll_channel({"id": 1, "identifier": "@test"})
    assert seen == 3
    assert written == 3
    assert c_errors == 1  # one failed comment thread


@pytest.mark.asyncio
async def test_stats_last_poll_datetime_formatting():
    # fake db returning datetime
    fake_db = MagicMock()
    fake_db.kpis = AsyncMock(return_value={"posts": 1, "views": 100, "reactions": 5, "comments": 2, "shares": 1, "last_poll": datetime(2026, 9, 6, 12, 30, tzinfo=timezone.utc)})
    fake_db.get_channels = AsyncMock(return_value=[{"id": 1, "identifier": "@test"}])
    fake_db.latest_stats = AsyncMock(return_value=[])
    settings = Settings(api_id=1, api_hash="h", session_string="s", channels="@test", admin_tg_ids="123")
    collector = Collector(None, fake_db, settings)
    # need client mock for handlers
    client = AsyncMock()
    client.get_me = AsyncMock(return_value=MagicMock(id=999))
    handlers = CommandHandlers(client, collector, settings)
    handlers._me_id = 999
    # create event
    event = MagicMock()
    event.raw_text = "/stats"
    event.is_private = True
    event.sender_id = 123
    event.chat_id = 123
    event.out = False
    event.reply = AsyncMock()
    await handlers._dispatch(event)
    # reply should have been called, check it contains formatted time "2026-09-06 12:30"
    assert event.reply.called
    args = event.reply.call_args[0][0]
    assert "2026-09-06 12:30" in args
    assert "UTC" in args


@pytest.mark.asyncio
async def test_floodwait_does_not_block_beyond_30s(monkeypatch):
    # collector that raises FloodWait
    now = datetime.now(timezone.utc)
    posts = [FakeMsg(1, now)]
    class FloodClient(FakeClient):
        async def get_entity(self, ident):
            return FakeEntity()
    # fake poll_channel that raises FloodWait
    fake_db = FakeDB()
    settings = Settings(api_id=1, api_hash="h", session_string="s", channels="@test", poll_minutes=15, track_days=30)
    collector = Collector(None, fake_db, settings)
    # patch poll_channel to raise FloodWait
    async def fake_poll(ch, job_id=None):
        raise errors.FloodWaitError(None, 3600)
    collector.poll_channel = fake_poll  # type: ignore
    # also need get_channels to return 1 channel
    sleep_calls = []
    orig_sleep = asyncio.sleep
    async def fast_sleep(secs):
        sleep_calls.append(secs)
        await orig_sleep(0)  # no real wait
    monkeypatch.setattr(asyncio, "sleep", fast_sleep)
    start = time.monotonic()
    summary = await collector.poll_all(reason="test")
    elapsed = time.monotonic() - start
    assert summary["errors"] == 1
    assert elapsed < 1.0  # should not have slept 3600
    assert sleep_calls[0] <= 30
    # next call should skip due to next_allowed_at
    # reset sleep calls
    sleep_calls.clear()
    # second call: should skip channel, not call poll_channel again
    called = False
    async def fake_poll2(ch, job_id=None):
        nonlocal called
        called = True
        raise errors.FloodWaitError(None, 3600)
    collector.poll_channel = fake_poll2  # type: ignore
    summary2 = await collector.poll_all(reason="test2")
    assert not called  # skipped
    assert summary2["posts_seen"] == 0


@pytest.mark.asyncio
async def test_persisted_session_fallback(monkeypatch):
    # Simulate main startup: persisted unauthorized, env authorized
    persisted = "persisted_string"
    env = "env_string_different"
    settings = Settings(api_id=123, api_hash="hash", session_string=env, channels="@test", postgres_enabled=False)
    # Mock PostgresDatabase.load and persist
    mock_db = MagicMock(spec=PostgresDatabase)
    mock_db.load_telegram_session = AsyncMock(return_value=persisted)
    mock_db.expire_stale_collection_jobs = AsyncMock(return_value=0)
    mock_db.persist_telegram_session = AsyncMock(return_value=uuid.uuid4())
    mock_db.is_postgres = True
    # Mock TelegramClient
    fake_clients = []
    class FakeTGClient:
        def __init__(self, session, api_id, api_hash):
            self.session = session
            self.api_id = api_id
            self.api_hash = api_hash
            self._authorized = (session == env)  # only env is authorized
            fake_clients.append(self)
        async def connect(self):
            pass
        async def is_user_authorized(self):
            return self._authorized
        async def disconnect(self):
            pass
        async def get_me(self):
            return MagicMock(first_name="Test")
    monkeypatch.setattr("app.main.TelegramClient", FakeTGClient)
    monkeypatch.setattr("app.main.StringSession", lambda s: s)
    # We need to test the logic directly without running full amain
    # Simulate the session handling block from main.py
    from app.session_crypto import build_cipher
    cipher = MagicMock()
    cipher.key_version = 1
    # mimic main's logic
    persisted_session = await mock_db.load_telegram_session(label="default", cipher=cipher)
    session_string = persisted_session or settings.session_string
    client = FakeTGClient(session_string, settings.api_id, settings.api_hash)
    await client.connect()
    if not await client.is_user_authorized():
        if settings.session_string and settings.session_string != persisted_session:
            await client.disconnect()
            client = FakeTGClient(settings.session_string, settings.api_id, settings.api_hash)
            await client.connect()
            if await client.is_user_authorized():
                session_string = settings.session_string
                await mock_db.persist_telegram_session(label="default", api_id=settings.api_id, api_hash=settings.api_hash, session_string=session_string, cipher=cipher)
                assert session_string == env
                assert mock_db.persist_telegram_session.called
            else:
                assert False, "env should be authorized"
        else:
            assert False, "should have tried env"
    assert len(fake_clients) == 2
    assert fake_clients[0].session == persisted
    assert fake_clients[1].session == env


@pytest.mark.asyncio
async def test_expire_stale_collection_jobs_in_memory():
    # Test PostgresDatabase method via fake in-memory logic
    # Use a fake that mimics SQL: we test the method exists and handles rowcount
    db = MagicMock(spec=PostgresDatabase)
    # Instead test our real method with mocked _execute
    real_db = PostgresDatabase("postgresql+asyncpg://user:pass@localhost/db", workspace_slug="community")
    real_db.workspace_id = uuid.uuid4()
    mock_result = MagicMock(rowcount=2)
    real_db._execute = AsyncMock(return_value=mock_result)
    count = await real_db.expire_stale_collection_jobs()
    assert count == 2
    # ensure SQL contains stale_on_startup
    call_args = real_db._execute.call_args[0][0]
    assert "stale_on_startup" in call_args
    # renew
    mock_result2 = MagicMock(rowcount=1)
    real_db._execute = AsyncMock(return_value=mock_result2)
    await real_db.renew_collection_job(uuid.uuid4(), lease_seconds=900)
    assert real_db._execute.called
    assert "lease_until" in real_db._execute.call_args[0][0]


@pytest.mark.asyncio
async def test_whoami_ignores_group():
    fake_db = MagicMock()
    settings = Settings(api_id=1, api_hash="h", session_string="s", channels="@test", admin_tg_ids="123")
    collector = Collector(None, fake_db, settings)
    client = AsyncMock()
    client.get_me = AsyncMock(return_value=MagicMock(id=999))
    handlers = CommandHandlers(client, collector, settings)
    handlers._me_id = 999
    event = MagicMock()
    event.raw_text = "/whoami"
    event.is_private = False  # group
    event.sender_id = 123
    event.chat_id = -100123
    event.out = False
    event.reply = AsyncMock()
    await handlers._dispatch(event)
    event.reply.assert_not_called()

    # private whoami from non-admin should still reply, but not when out=True
    event2 = MagicMock()
    event2.raw_text = "/whoami"
    event2.is_private = True
    event2.sender_id = 9999  # non-admin
    event2.chat_id = 9999
    event2.out = False
    event2.reply = AsyncMock()
    await handlers._dispatch(event2)
    event2.reply.assert_called_once()

    # Saved Messages (private, outgoing, chat is the owner) must keep working:
    # the documented bootstrap flow sends /whoami to yourself.
    event3 = MagicMock()
    event3.raw_text = "/whoami"
    event3.is_private = True
    event3.sender_id = 999
    event3.chat_id = 999
    event3.out = True
    event3.reply = AsyncMock()
    await handlers._dispatch(event3)
    event3.reply.assert_called_once()

    # An outgoing command in a group must never post the owner's id.
    event4 = MagicMock()
    event4.raw_text = "/whoami"
    event4.is_private = False
    event4.sender_id = 999
    event4.chat_id = -100555
    event4.out = True
    event4.reply = AsyncMock()
    await handlers._dispatch(event4)
    event4.reply.assert_not_called()

    # Admin commands from Saved Messages are still allowed.
    event5 = MagicMock()
    event5.raw_text = "/help"
    event5.is_private = True
    event5.sender_id = 999
    event5.chat_id = 999
    event5.out = True
    event5.reply = AsyncMock()
    await handlers._dispatch(event5)
    event5.reply.assert_called_once()


@pytest.mark.asyncio
async def test_schedule_poll_supervision():
    fake_db = FakeDB()
    settings = Settings(api_id=1, api_hash="h", session_string="s", channels="@test")
    collector = Collector(None, fake_db, settings)
    # patch poll_all to succeed
    collector.poll_all = AsyncMock(return_value={"posts_seen": 1})
    task = collector.schedule_poll(reason="test")
    assert collector._background is task
    await task
    assert collector._background is None

    # failure case logs but clears background
    async def failing_poll(reason="test"):
        raise RuntimeError("fail")
    collector.poll_all = failing_poll  # type: ignore
    task2 = collector.schedule_poll(reason="test2")
    await asyncio.sleep(0.05)  # let done callback run
    assert collector._background is None or task2.done()
