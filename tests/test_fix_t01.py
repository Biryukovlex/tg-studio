"""T01 acceptance tests."""
from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from pydantic_ai.ui.ag_ui import AGUIAdapter as RealAGUIAdapter

from app.config import Settings
from app.studio.analytics import analyze_posts
from app.studio.semantic_profile import build_semantic_profile
from app.studio.repository import MemoryStudioRepository
from app.studio.service import StudioService


AS_OF = datetime(2026, 9, 3, 12, tzinfo=timezone.utc)


def _rows_120():
    rows = []
    for idx in range(1, 121):
        # views vary to create diverse traction; reactions/comments/shares proportional
        views = 50 + idx * 10 + (idx % 7) * 30
        reactions = max(1, views // 20 + (idx % 5))
        comments = max(0, views // 50 + (idx % 3))
        shares = max(0, views // 80 + (idx % 2))
        # Make text long enough for style_eligible (>=80 non-whitespace)
        text = (
            f"Post {idx} technology analysis markets climate science detailed editorial content "
            * 4
        ).strip()
        rows.append(
            {
                "post_id": idx,
                "message_id": 1000 + idx,
                "channel_id": 1,
                "posted_at": AS_OF - timedelta(days=idx % 60 + 2, hours=idx % 24),
                "snapshot_at": AS_OF,
                "text": text,
                "views": views,
                "reactions": reactions,
                "comments": comments,
                "shares": shares,
            }
        )
    return rows


@pytest.mark.asyncio
async def test_semantic_profile_with_120_posts_in_test_mode():
    rows = _rows_120()
    analytics = analyze_posts(rows, 1, now=AS_OF, identifier="@sample_channel")
    # Ensure preconditions: at least 13 style-eligible top posts and 8 baseline posts
    assert len([p for p in analytics.top_posts if p.style_eligible]) >= 13
    assert len(analytics.baseline_posts) >= 8
    # analytics.evidence_posts is 20, top_posts ~20
    settings = Settings(
        api_id=1,
        api_hash="test",
        session_string="test",
        channels="@sample",
        data_dir="/tmp",
        admin_username="u",
        admin_password="p",
        session_secret="secret",
        studio_test_mode=True,
        studio_enabled=True,
    )
    profile, analysis = await build_semantic_profile(analytics, rows, settings)
    # Must not raise EvidenceIntegrityError; all evidence IDs must be within trimmed evidence
    # Recompute the trimmed sample as build_semantic_profile does
    sample = {p.post_id: p for p in [*analytics.top_posts[:12], *analytics.baseline_posts[:8]]}
    for post in analytics.evidence_posts:
        if len(sample) >= 20:
            break
        sample.setdefault(post.post_id, post)
    sample_ids = set(sample.keys())
    # Style evidence must be within the trimmed sample; topics are empty in test mode
    assert set(profile.style_profile.evidence_post_ids) <= sample_ids
    assert profile.editorial_rules.get("extraction_version") == "channel.semantic.v1"


# --- heartbeat failure test ---

class _SlowAdapter:
    build_run_input = staticmethod(RealAGUIAdapter.build_run_input)

    def __init__(self, **_kwargs):
        pass

    async def run_stream(self, **_kwargs):
        # Minimal stream that yields a completion
        from app.studio.service import _UpstreamRunError

        class _E:
            def __init__(self, t, delta=None):
                self.type = t
                self.delta = delta

        yield _E("RUN_STARTED")
        await asyncio.sleep(0.02)
        yield _E("TEXT_MESSAGE_START")
        yield _E("TEXT_MESSAGE_CONTENT", "hello")
        yield _E("RUN_FINISHED")

    def encode_stream(self, stream):
        return stream


class _FlakyRepo(MemoryStudioRepository):
    def __init__(self):
        super().__init__()
        self.calls = 0

    async def renew_run_lease(self, run_id, worker_id, lease_seconds=120):
        self.calls += 1
        if self.calls == 2:
            raise RuntimeError("lease storage unavailable")
        return await super().renew_run_lease(run_id, worker_id, lease_seconds)


def _payload(conversation_id: uuid.UUID, run_id: uuid.UUID) -> bytes:
    return json.dumps(
        {
            "threadId": str(conversation_id),
            "runId": str(run_id),
            "messages": [{"id": str(uuid.uuid4()), "role": "user", "content": "test run"}],
            "tools": [],
            "context": [],
            "forwardedProps": {},
        }
    ).encode()


@pytest.mark.asyncio
async def test_watchdog_failure_does_not_hang_stream(monkeypatch):
    from app.studio import service as service_module

    monkeypatch.setattr(service_module, "AGUIAdapter", _SlowAdapter)
    repo = _FlakyRepo()
    conversation = await repo.create_conversation(channel_id=1)
    settings = Settings(
        api_id=1,
        api_hash="test",
        session_string="test",
        channels="@sample",
        data_dir="/tmp",
        admin_username="u",
        admin_password="p",
        session_secret="secret",
        studio_enabled=True,
        studio_test_mode=True,
        studio_run_heartbeat_seconds=0.05,
        studio_run_lease_seconds=60,
    )
    service = StudioService(repo, settings)
    run_id = uuid.uuid4()
    response = await service.stream_request(None, _payload(conversation["id"], run_id))
    # Consume stream with timeout – it must terminate even though renew fails once
    events = []
    try:
        async with asyncio.timeout(2):
            async for ev in response.body_iterator:
                events.append(ev)
                # break after we see completion; stream should close
                if getattr(ev, "type", "") == "RUN_FINISHED":
                    # continue to ensure queue closes
                    pass
    except TimeoutError:
        pytest.fail("event stream hung after watchdog lease failure")
    # Registry must be finished (handle inactive)
    handle = service.registry._runs.get(run_id)
    # After finish, handle should exist but not active, or be evicted but finish called
    if handle is not None:
        assert handle.active is False
    # At least the stream closed – if watchdog raised, execute_run finally must have queued complete
    # No hang means success


@pytest.mark.asyncio
async def test_watchdog_three_failures_cancels_run(monkeypatch):
    from app.studio import service as service_module

    monkeypatch.setattr(service_module, "AGUIAdapter", _SlowAdapter)

    class _AlwaysFailRepo(MemoryStudioRepository):
        async def renew_run_lease(self, *a, **kw):
            raise RuntimeError("always fail")

    repo = _AlwaysFailRepo()
    conversation = await repo.create_conversation(channel_id=1)
    settings = Settings(
        api_id=1,
        api_hash="test",
        session_string="test",
        channels="@sample",
        data_dir="/tmp",
        admin_username="u",
        admin_password="p",
        session_secret="secret",
        studio_enabled=True,
        studio_test_mode=True,
        studio_run_heartbeat_seconds=0.05,
        studio_run_lease_seconds=60,
    )
    service = StudioService(repo, settings)
    run_id = uuid.uuid4()
    response = await service.stream_request(None, _payload(conversation["id"], run_id))
    try:
        async with asyncio.timeout(2):
            async for _ in response.body_iterator:
                pass
    except TimeoutError:
        pytest.fail("stream hung with always-failing lease")
    assert service.registry._runs.get(run_id) is None or service.registry._runs[run_id].active is False


@pytest.mark.asyncio
async def test_search_health_does_not_leak_endpoint(client, settings, app):
    # Configure a distinctive host
    settings.studio_enabled = True
    settings.studio_test_mode = True
    settings.studio_search_enabled = True
    settings.studio_search_base_url = "http://searxng-private-host-xyz:8080"
    # Need login
    await client.post(
        "/login",
        data={"username": settings.admin_username, "password": settings.admin_password},
        follow_redirects=False,
    )
    setup = await client.get("/studio/api/setup")
    assert setup.status_code == 200
    body = setup.text
    assert "searxng-private-host-xyz" not in body
    data = setup.json()
    research = data.get("research", {})
    # Must not contain raw endpoint, must have configured bool and hash
    assert "searxng-private-host-xyz" not in json.dumps(data)
    assert research.get("configured") is True
    # endpoint key should not leak host; either absent or hash
    assert "endpoint_hash" in research
    expected_hash = hashlib.sha256("http://searxng-private-host-xyz:8080".encode()).hexdigest()[:12]
    assert research["endpoint_hash"] == expected_hash

    health = await client.get("/studio/api/research/health")
    assert health.status_code == 200
    assert "searxng-private-host-xyz" not in health.text
    hdata = health.json()
    assert "searxng-private-host-xyz" not in json.dumps(hdata)
    assert hdata.get("configured") is True
    assert hdata.get("endpoint_hash") == expected_hash


@pytest.mark.asyncio
async def test_search_health_hash_empty_when_not_configured(client, settings):
    settings.studio_enabled = True
    settings.studio_test_mode = True
    settings.studio_search_enabled = True
    settings.studio_search_base_url = ""
    await client.post(
        "/login",
        data={"username": settings.admin_username, "password": settings.admin_password},
        follow_redirects=False,
    )
    setup = await client.get("/studio/api/setup")
    data = setup.json()
    research = data["research"]
    assert research["configured"] is False
    assert research["endpoint_hash"] == ""
