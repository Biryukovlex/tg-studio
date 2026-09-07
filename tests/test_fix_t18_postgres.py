"""PostgreSQL proof for the Channel Profile save path.

The in-memory repository has no ``id`` column, so it could not catch the
NotNullViolation that broke every real save. Run with a disposable database:

    TEST_POSTGRES_URL=postgresql+asyncpg://user:pw@127.0.0.1:55432/tg_studio_test \
        .venv/bin/python -m pytest -q tests/test_fix_t18_postgres.py
"""
import os
import re
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.config import Settings
from app.postgres_db import PostgresDatabase
from app.studio.drafts import DraftConflictError
from app.studio.repository import StudioRepository
from app.web.routes import create_app


def _url() -> str:
    url = os.environ.get("TEST_POSTGRES_URL") or os.environ.get("M6_POSTGRES_URL")
    if not url:
        pytest.skip("set TEST_POSTGRES_URL to run the profile save PostgreSQL proof")
    return url


class FakeCollector:
    def __init__(self, db) -> None:
        self.db = db

    async def poll_all(self, reason: str = "test") -> dict:
        return {"reason": reason}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_upsert_profile_text_creates_then_updates_and_detects_conflicts():
    db = PostgresDatabase(_url(), workspace_slug=f"t18-{uuid.uuid4().hex[:8]}")
    try:
        await db.init_db(admin_username="t18-admin")
        channel_id = await db.upsert_channel(f"@t18_{uuid.uuid4().hex[:6]}", "T18", 7001)
        repo = StudioRepository(db)
        assert await repo.get_profile(channel_id) is None

        first = await repo.upsert_profile_text({
            "channel_id": channel_id, "expected_version": 0,
            "topics_text": "Budget — votes", "editorial_text": "Cite sources.", "style_text": "**Bold** title",
            "built_from_posts": 12,
        })
        assert first["version"] == 1 and first["built_from_posts"] == 12 and first["built_at"] is not None
        built_at = first["built_at"]

        # A manual edit keeps the build provenance and bumps the version.
        second = await repo.upsert_profile_text({
            "channel_id": channel_id, "expected_version": 1,
            "topics_text": "Budget — votes\nProcurement — tenders", "editorial_text": "Cite sources.", "style_text": "**Bold** title",
        })
        assert second["version"] == 2 and second["id"] == first["id"]
        assert second["built_from_posts"] == 12 and second["built_at"] == built_at
        assert (await repo.get_profile(channel_id))["topics_text"] == "Budget — votes\nProcurement — tenders"

        with pytest.raises(DraftConflictError):
            await repo.upsert_profile_text({"channel_id": channel_id, "expected_version": 1, "topics_text": "stale"})
        assert (await repo.get_profile(channel_id))["version"] == 2
    finally:
        await db.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_profile_dialog_flow_saves_through_the_http_api(tmp_path):
    """The exact user flow: open dialog, Build from posts, Save, reopen."""
    db = PostgresDatabase(_url(), workspace_slug=f"t18-{uuid.uuid4().hex[:8]}")
    try:
        await db.init_db(admin_username="t18-admin")
        identifier = f"@t18_{uuid.uuid4().hex[:6]}"
        channel_id = await db.upsert_channel(identifier, "T18", 7002)
        now = datetime.now(timezone.utc)
        for index in range(1, 8):
            post_id = await db.upsert_post(channel_id, index, now - timedelta(days=index), f"**Title {index}**\n" + "Budget vote coverage. " * 6)
            await db.add_snapshot_if_changed(post_id, index * 50, index, index, 0)
        settings = Settings(
            api_id=1, api_hash="h", session_string="s", channels=identifier, data_dir=str(tmp_path),
            admin_username="t18-admin", admin_password="t18-password", session_secret="t18-secret",
            studio_test_mode=True, database_url=_url(),
            telegram_session_encryption_key="x" * 40,
        )
        app = create_app(FakeCollector(db), settings)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            login = await client.post("/login", data={"username": "t18-admin", "password": "t18-password"}, follow_redirects=False)
            assert login.status_code == 303
            home = await client.get("/studio")
            token = re.search(r'<meta name="studio-csrf-token" content="([^"]+)"', home.text).group(1)
            headers = {"x-csrf-token": token, "content-type": "application/json"}

            opened = await client.get(f"/studio/api/profile?channel_id={channel_id}")
            assert opened.status_code == 200 and opened.json()["profile"] is None
            assert opened.json()["can_build"] is True, opened.json()

            built = await client.post("/studio/api/profile/build", json={"channel_id": channel_id}, headers=headers)
            assert built.status_code == 200, built.text
            draft = built.json()["draft"]
            assert draft["topics_text"] and draft["style_text"]

            saved = await client.put("/studio/api/profile", json={
                "channel_id": channel_id, "expected_version": 0,
                "topics_text": draft["topics_text"], "editorial_text": draft["editorial_text"], "style_text": draft["style_text"],
            }, headers=headers)
            assert saved.status_code == 200, saved.text
            assert saved.json()["profile"]["version"] == 1

            reopened = await client.get(f"/studio/api/profile?channel_id={channel_id}")
            profile = reopened.json()["profile"]
            assert profile["version"] == 1 and profile["topics_text"] == draft["topics_text"]

            edited = await client.put("/studio/api/profile", json={
                "channel_id": channel_id, "expected_version": 1,
                "topics_text": profile["topics_text"] + "\nOwner topic — added by hand",
                "editorial_text": profile["editorial_text"], "style_text": profile["style_text"],
            }, headers=headers)
            assert edited.status_code == 200 and edited.json()["profile"]["version"] == 2

            stale = await client.put("/studio/api/profile", json={
                "channel_id": channel_id, "expected_version": 1,
                "topics_text": "stale", "editorial_text": "", "style_text": "",
            }, headers=headers)
            assert stale.status_code == 409 and stale.json()["server_profile"]["version"] == 2

            bootstrap = await client.get("/studio/api/bootstrap")
            assert bootstrap.json()["profile_status"] == "ready"
    finally:
        await db.close()
