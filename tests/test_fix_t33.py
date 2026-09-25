"""T33 collector-deleted-posts acceptance tests (SQLite + fake Telethon client)."""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest

from app.collector import Collector
from app.config import Settings
from app.db import Database
from app.web.routes import create_app


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
    """Yield messages newest-first and honour the collector's message cap."""

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
    base = dict(
        _env_file=None,
        api_id=1,
        api_hash="hash",
        session_string="session",
        channels="@t33",
        admin_username="admin",
        admin_password="pw",
        session_secret="secret",
        track_days=0,
        backfill_limit=0,
        poll_minutes=15,
    )
    base.update(overrides)
    return Settings(**base)


def _seed(db: Database, channel_id: int, now: datetime) -> dict[int, int]:
    ids = {}
    for mid in (1, 2, 3):
        post_id = db.upsert_post(channel_id, mid, now, f"post {mid}")
        db.add_snapshot_if_changed(post_id, views=mid * 10, comments=0, reactions=0, shares=0)
        ids[mid] = post_id
    return ids


@pytest.mark.asyncio
async def test_deleted_post_retired_hidden_and_badged(tmp_path):
    db = Database(tmp_path / "t33.sqlite")
    db.init_db()
    channel_id = db.upsert_channel("@t33", "T33")
    now = datetime.now(timezone.utc)
    ids = _seed(db, channel_id, now)

    collector = Collector(FakeClient([FakeMsg(1, now), FakeMsg(3, now)]), db, _settings())
    seen, *_ = await collector.poll_channel({"id": channel_id, "identifier": "@t33"})
    assert seen == 2

    with db.conn() as conn:
        flag = conn.execute("SELECT is_deleted FROM posts WHERE id=?", (ids[2],)).fetchone()
        assert flag["is_deleted"] == 1

    assert db.kpis()["posts"] == 2
    assert db.kpis()["views"] == 40
    assert sorted(r["message_id"] for r in db.latest_stats()) == [1, 3]
    assert len(db.all_comments()) == 0
    series = db.timeseries_totals(days=None)
    assert series["views"][-1] == 40
    assert series["posts_per_day"][-1] == 2

    settings = _settings(data_dir=str(tmp_path))
    app = create_app(SimpleNamespace(db=db, client=object()), settings)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        login = await client.post("/login", data={"username": "admin", "password": "pw"})
        assert login.status_code == 303
        deleted = await client.get(f"/post/{ids[2]}")
        assert deleted.status_code == 200
        assert "Deleted in Telegram" in deleted.text
        live = await client.get(f"/post/{ids[1]}")
        assert live.status_code == 200
        assert "Deleted in Telegram" not in live.text


@pytest.mark.asyncio
async def test_reseen_post_clears_deleted_flag(tmp_path):
    db = Database(tmp_path / "t33.sqlite")
    db.init_db()
    channel_id = db.upsert_channel("@t33", "T33")
    now = datetime.now(timezone.utc)
    _seed(db, channel_id, now)

    collector = Collector(FakeClient([FakeMsg(1, now), FakeMsg(3, now)]), db, _settings())
    await collector.poll_channel({"id": channel_id, "identifier": "@t33"})
    assert db.kpis()["posts"] == 2

    collector.client = FakeClient([FakeMsg(1, now), FakeMsg(2, now), FakeMsg(3, now)])
    await collector.poll_channel({"id": channel_id, "identifier": "@t33"})
    with db.conn() as conn:
        flags = [r["is_deleted"] for r in conn.execute("SELECT is_deleted FROM posts").fetchall()]
        assert flags == [0, 0, 0]
    assert db.kpis()["posts"] == 3


@pytest.mark.asyncio
async def test_capped_scan_does_not_retire_unreached_old_post(tmp_path):
    db = Database(tmp_path / "t33.sqlite")
    db.init_db()
    channel_id = db.upsert_channel("@t33", "T33")
    now = datetime.now(timezone.utc)
    _seed(db, channel_id, now)

    settings = _settings(backfill_limit=1)
    collector = Collector(
        FakeClient([FakeMsg(3, now), FakeMsg(2, now), FakeMsg(1, now)]),
        db,
        settings,
    )
    seen, *_ = await collector.poll_channel({"id": channel_id, "identifier": "@t33"})
    assert seen == 1
    with db.conn() as conn:
        flags = [r["is_deleted"] for r in conn.execute("SELECT is_deleted FROM posts ORDER BY message_id").fetchall()]
        assert flags == [0, 0, 0]
    assert db.kpis()["posts"] == 3


@pytest.mark.asyncio
async def test_aborted_and_empty_scans_skip_retirement(tmp_path):
    db = Database(tmp_path / "t33.sqlite")
    db.init_db()
    channel_id = db.upsert_channel("@t33", "T33")
    now = datetime.now(timezone.utc)
    _seed(db, channel_id, now)

    collector = Collector(FakeClient([]), db, _settings())
    seen, *_ = await collector.poll_channel({"id": channel_id, "identifier": "@t33"})
    assert seen == 0
    with db.conn() as conn:
        flags = [r["is_deleted"] for r in conn.execute("SELECT is_deleted FROM posts").fetchall()]
        assert flags == [0, 0, 0]

    class _BoomClient(FakeClient):
        def iter_messages(self, entity, limit=None):
            raise RuntimeError("boom")

    collector = Collector(_BoomClient([]), db, _settings())
    with pytest.raises(RuntimeError):
        await collector.poll_channel({"id": channel_id, "identifier": "@t33"})
    with db.conn() as conn:
        flags = [r["is_deleted"] for r in conn.execute("SELECT is_deleted FROM posts").fetchall()]
        assert flags == [0, 0, 0]
