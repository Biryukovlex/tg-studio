"""T33 collector retirement guards with a fake asynchronous repository."""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.collector import Collector
from app.config import Settings
from tests.test_fix_t09 import FakeDB


class FakeMsg:
    def __init__(self, mid: int, date: datetime):
        self.id = mid
        self.date = date
        self.message = f"post {mid}"
        self.action = None
        self.entities = None
        self.views = mid * 10
        self.replies = SimpleNamespace(replies=0)
        self.reactions = SimpleNamespace(results=[])
        self.forwards = 0


class FakeClient:
    def __init__(self, messages: list[FakeMsg]):
        self._messages = list(messages)

    async def get_entity(self, identifier: str):
        return SimpleNamespace(title="T33", id=123)

    def iter_messages(self, entity, limit=None):
        messages = self._messages if limit in (None, 0) else self._messages[:limit]

        async def gen():
            for message in messages:
                yield message

        return gen()


def _settings(**overrides) -> Settings:
    values = dict(
        _env_file=None, api_id=1, api_hash="hash", session_string="session",
        channels="@t33", admin_password="pw", track_days=0,
        backfill_limit=0, poll_minutes=15,
    )
    values.update(overrides)
    return Settings(**values)


async def _seed(db: FakeDB, now: datetime) -> None:
    for mid in (1, 2, 3):
        await db.upsert_post(1, mid, now, f"post {mid}")


@pytest.mark.asyncio
async def test_collector_retires_missing_post_and_restores_reseen_post():
    db = FakeDB()
    now = datetime.now(timezone.utc)
    await _seed(db, now)
    collector = Collector(FakeClient([FakeMsg(3, now), FakeMsg(1, now)]), db, _settings())
    seen, *_ = await collector.poll_channel({"id": 1, "identifier": "@t33"})
    assert seen == 2
    assert [row["message_id"] for row in db.posts.values() if row["is_deleted"]] == [2]

    collector.client = FakeClient([FakeMsg(3, now), FakeMsg(2, now), FakeMsg(1, now)])
    await collector.poll_channel({"id": 1, "identifier": "@t33"})
    assert all(not row["is_deleted"] for row in db.posts.values())


@pytest.mark.asyncio
async def test_capped_scan_does_not_retire_unreached_old_posts():
    db = FakeDB()
    now = datetime.now(timezone.utc)
    await _seed(db, now)
    collector = Collector(
        FakeClient([FakeMsg(3, now), FakeMsg(2, now), FakeMsg(1, now)]),
        db, _settings(backfill_limit=1),
    )
    seen, *_ = await collector.poll_channel({"id": 1, "identifier": "@t33"})
    assert seen == 1
    assert all(not row["is_deleted"] for row in db.posts.values())


@pytest.mark.asyncio
async def test_aborted_and_empty_scans_skip_retirement():
    db = FakeDB()
    now = datetime.now(timezone.utc)
    await _seed(db, now)
    channel = {"id": 1, "identifier": "@t33"}
    collector = Collector(FakeClient([]), db, _settings())
    seen, *_ = await collector.poll_channel(channel)
    assert seen == 0
    assert all(not row["is_deleted"] for row in db.posts.values())

    class BoomClient(FakeClient):
        def iter_messages(self, entity, limit=None):
            raise RuntimeError("boom")

    collector.client = BoomClient([])
    with pytest.raises(RuntimeError):
        await collector.poll_channel(channel)
    assert all(not row["is_deleted"] for row in db.posts.values())
