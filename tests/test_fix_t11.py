"""T11 acceptance tests: Studio runtime."""
from __future__ import annotations

import asyncio
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.studio.analytics import _percentiles, analyze_posts, analyze_posts_async
from app.studio.repository import ActiveRunExists, MemoryStudioRepository


def _quadratic_percentiles(values: list[float]) -> list[float]:
    if not values:
        return []
    if len(values) == 1:
        return [1.0]
    ordered = sorted(values)
    result: list[float] = []
    denominator = float(len(values) - 1)
    for value in values:
        lower = sum(1 for candidate in ordered if candidate < value)
        equal = sum(1 for candidate in ordered if candidate == value)
        result.append((lower + (equal - 1) / 2) / denominator)
    return result


def _rows(count: int) -> list[dict]:
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    return [
        {
            "post_id": index,
            "channel_id": 1,
            "message_id": 1000 + index,
            "posted_at": (base + timedelta(days=index)).isoformat(),
            "text": "synthetic performance post body with enough words to be style eligible " + str(index),
            "views": (index * 37) % 500,
            "reactions": index % 11,
            "comments": index % 5,
            "shares": index % 3,
            "snapshot_at": (base + timedelta(days=index, hours=1)).isoformat(),
        }
        for index in range(count)
    ]


def test_percentiles_fast_and_equivalent():
    import random

    random.seed(11)
    fixture = [random.random() * 100 for _ in range(200)]
    assert _percentiles(fixture) == pytest.approx(_quadratic_percentiles(fixture))
    large = [float(index % 997) for index in range(20_000)]
    started = time.perf_counter()
    _percentiles(large)
    assert time.perf_counter() - started < 0.1
    assert _percentiles([]) == []
    assert _percentiles([3.0]) == [1.0]


@pytest.mark.asyncio
async def test_large_analysis_never_blocks_the_loop():
    rows = _rows(10_000)
    gaps: list[float] = []
    stop = False

    async def ticker():
        previous = time.monotonic()
        while not stop:
            await asyncio.sleep(0.01)
            now = time.monotonic()
            gaps.append(now - previous)
            previous = now

    task = asyncio.create_task(ticker())
    try:
        analysis = await analyze_posts_async(rows, 1, identifier="@sample_channel")
    finally:
        stop = True
        await task
    assert analysis.eligible_post_count > 0
    assert max(gaps) < 0.1


@pytest.mark.asyncio
async def test_identical_rows_share_one_analysis_row():
    first = analyze_posts(_rows(10), 1, identifier="@sample_channel", now=datetime(2026, 1, 5, 8, 30, tzinfo=timezone.utc))
    second = analyze_posts(_rows(10), 1, identifier="@sample_channel", now=datetime(2026, 1, 5, 22, 45, tzinfo=timezone.utc))
    assert first.input_hash == second.input_hash
    repository = MemoryStudioRepository()
    await repository.create_analysis(
        {
            "channel_id": 1,
            "eligible_post_count": first.eligible_post_count,
            "input_hash": first.input_hash,
        }
    )
    assert (await repository.get_analysis_by_hash(1, second.input_hash)) is not None


def test_registry_evicts_only_inactive_handles():
    from app.studio.service import RunRegistry

    registry = RunRegistry(max_runs=3)
    first = uuid.uuid4()
    registry.begin(first)
    for _ in range(5):
        registry.begin(uuid.uuid4())
    assert first in registry._runs


@pytest.mark.asyncio
async def test_failed_run_creation_leaves_no_orphan_message():
    repository = MemoryStudioRepository()
    conversation = await repository.create_conversation(channel_id=1)
    conversation_id = conversation["id"]
    first_message, first_run = await repository.append_user_message_and_create_run(
        conversation_id=conversation_id, content="first", requested_model="m", run_id=uuid.uuid4()
    )
    assert first_message["id"] == first_run["user_message_id"]
    with pytest.raises(ActiveRunExists):
        await repository.append_user_message_and_create_run(
            conversation_id=conversation_id, content="second", requested_model="m", run_id=uuid.uuid4()
        )
    messages = await repository.list_messages(conversation_id)
    assert [message["content"] for message in messages] == ["first"]


@pytest.mark.asyncio
async def test_queued_grace_covers_run_timeout():
    from app import limits
    from app.studio.service import StudioService

    seen: dict = {}

    class FakeRepository(MemoryStudioRepository):
        async def mark_stale_runs_interrupted(self, *, queued_grace_seconds: int = 60):
            seen["queued_grace_seconds"] = queued_grace_seconds
            return 0

    service = StudioService.__new__(StudioService)
    service.repository = FakeRepository()
    from app.config import Settings

    service.settings = Settings(_env_file=None)
    assert await service.recover_stale_runs() == 0
    assert seen["queued_grace_seconds"] >= int(limits.RUN_TIMEOUT_SECONDS)


@pytest.mark.asyncio
async def test_queued_run_survives_bootstrap_during_slot_wait():
    repository = MemoryStudioRepository()
    conversation = await repository.create_conversation(channel_id=1)
    run = await repository.create_run(
        conversation_id=conversation["id"], user_message_id=1, requested_model="m", run_id=uuid.uuid4()
    )
    repository.runs[run["id"]]["created_at"] = datetime.now(timezone.utc) - timedelta(seconds=90)
    assert await repository.mark_stale_runs_interrupted(queued_grace_seconds=300) == 0
    assert (await repository.get_run(run["id"]))["status"] == "queued"
    assert await repository.mark_stale_runs_interrupted(queued_grace_seconds=60) == 1


@pytest.mark.asyncio
async def test_concurrent_tool_calls_keep_all_sources():
    from app.studio.provenance import SourceEvidence
    from app.studio.research import ResearchService
    from app.config import Settings

    repository = MemoryStudioRepository()
    service = ResearchService(Settings(_env_file=None, studio_search_enabled=False), repository=None)
    service.repository = None

    def evidence(source_id: str) -> SourceEvidence:
        return SourceEvidence(
            source_id=source_id,
            url=f"https://example.test/{source_id}",
            canonical_url=f"https://example.test/{source_id}",
            title=f"Story {source_id}",
        )

    workspace, conversation = "ws", uuid.uuid4()
    first = [evidence(f"a-{index}") for index in range(6)]
    second = [evidence(f"b-{index}") for index in range(6)]
    await asyncio.gather(
        service._store(workspace_id=workspace, conversation_id=conversation, channel_id=1, query="q", values=first),
        service._store(workspace_id=workspace, conversation_id=conversation, channel_id=1, query="q", values=second),
    )
    state = service._state(workspace, conversation, 1)
    assert len(state.sources) == 12


@pytest.mark.asyncio
async def test_corrupt_persisted_story_does_not_break_reload():
    from app.studio.research import ResearchService
    from app.config import Settings

    class FakeRepository:
        async def get_research_bundle(self, *, conversation_id, channel_id):
            return {
                "query": "q",
                "sources": [],
                "stories": [{"cluster_id": "broken", "score": "not-a-float!!!"}],
                "warnings": [],
                "retrieved_at": None,
                "selected_source_ids": [],
            }

    service = ResearchService(Settings(_env_file=None, studio_search_enabled=False), repository=FakeRepository())
    state = await service._ensure_loaded(workspace_id="ws", conversation_id=uuid.uuid4(), channel_id=1)
    assert state.bundle is not None
    assert state.bundle.stories == ()


@pytest.mark.asyncio
async def test_unchanged_bundle_skips_persist():
    from app.studio.provenance import SourceEvidence
    from app.studio.research import ResearchService
    from app.config import Settings

    calls: list = []

    class FakeRepository:
        async def persist_research_bundle(self, *, conversation_id, channel_id, bundle):
            calls.append(bundle)
            return {"sources": 1, "stories": 0}

    service = ResearchService(Settings(_env_file=None, studio_search_enabled=False), repository=FakeRepository())
    values = [
        SourceEvidence(source_id="s-1", url="https://example.test/s-1", canonical_url="https://example.test/s-1", title="T")
    ]
    conversation_id = uuid.uuid4()
    await service._store(workspace_id="ws", conversation_id=conversation_id, channel_id=1, query="q", values=values)
    assert len(calls) == 1
    # Same scope again: fingerprint matches, persist is skipped.
    await service._store(workspace_id="ws", conversation_id=conversation_id, channel_id=1, query="q", values=values)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_slot_heartbeat_fires_while_queued():
    from app.studio.service import RunCoordinator

    coordinator = RunCoordinator(1)
    beats: list = []

    async def heartbeat():
        beats.append(1)

    await coordinator._slots.acquire()
    waiter = asyncio.create_task(
        _acquire_with_heartbeat(coordinator, heartbeat, interval=0.05)
    )
    await asyncio.sleep(0.2)
    coordinator._slots.release()
    await waiter
    assert beats


async def _acquire_with_heartbeat(coordinator, heartbeat, interval: float = 0.05):
    async with coordinator.slot(heartbeat=heartbeat, interval=interval):
        return True
