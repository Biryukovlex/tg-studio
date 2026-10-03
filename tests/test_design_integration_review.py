"""Regressions found while reviewing the combined design migration."""
import uuid
import pytest
from app.studio.repository import MemoryStudioRepository, SystemPromptConflict
from app.studio.routes import _profile


@pytest.mark.asyncio
async def test_profile_exposes_only_its_linked_analysis_evidence():
    repo = MemoryStudioRepository()
    aid = uuid.uuid4()
    repo.analyses[aid] = {'channel_id': 1, 'evidence_post_ids': [12, 18]}
    repo.profiles[1] = {'channel_id': 1, 'current_analysis_id': aid}
    serialized = _profile(await repo.get_profile(1))
    assert serialized['evidence_post_ids'] == [12, 18]
    repo.profiles[2] = {'channel_id': 2}
    assert _profile(await repo.get_profile(2))['evidence_post_ids'] == []


@pytest.mark.asyncio
async def test_memory_prompt_has_the_same_conflict_contract_as_postgres():
    repo = MemoryStudioRepository()
    await repo.set_system_prompt(1, 'First', expected_prompt='')
    with pytest.raises(SystemPromptConflict):
        await repo.set_system_prompt(1, 'Stale', expected_prompt='')
    assert await repo.get_system_prompt(1) == 'First'


@pytest.mark.asyncio
async def test_postgres_profile_save_retains_scoped_evidence(client, settings, app, channel_id):
    import re
    from datetime import datetime, timezone
    settings.studio_test_mode = True
    repo = app.state.studio_repository
    own = (await repo.performance_rows(channel_id))[0]['post_id']
    other_channel = await repo.db.upsert_channel('@profile_other', 'Other', 98764)
    other_post = await repo.db.upsert_post(other_channel, 70, datetime.now(timezone.utc), 'Other context')
    await client.post('/login', data={'username': settings.admin_username, 'password': settings.admin_password})
    page = await client.get('/studio')
    token = re.search(r'<meta name="studio-csrf-token" content="([^"]+)"', page.text).group(1)
    body = {'channel_id': channel_id, 'expected_version': 0, 'topics_text': 'Topic', 'evidence_post_ids': [other_post]}
    headers = {'x-csrf-token': token}
    assert (await client.put('/studio/api/profile', json=body, headers=headers)).status_code == 422
    body['evidence_post_ids'] = [own]
    saved = await client.put('/studio/api/profile', json=body, headers=headers)
    assert saved.status_code == 200
    assert saved.json()['profile']['evidence_post_ids'] == [own]
    assert saved.json()['profile']['built_from_posts'] == 1
    # A manual edit preserves the prior build provenance.
    body.pop('evidence_post_ids')
    body['expected_version'] = 1
    assert (await client.put('/studio/api/profile', json=body, headers=headers)).status_code == 200
    restored = await client.get('/studio/api/profile', params={'channel_id': channel_id})
    assert restored.json()['profile']['evidence_post_ids'] == [own]
    assert restored.json()['profile']['built_from_posts'] == 1


@pytest.mark.asyncio
async def test_postgres_profile_simultaneous_saves_have_one_winner(app, channel_id, monkeypatch):
    import asyncio
    from app.studio.drafts import DraftConflictError
    repo = app.state.studio_repository
    await repo.upsert_profile_text({'channel_id': channel_id, 'expected_version': 0, 'topics_text': 'Original'})
    original = repo.get_profile
    barrier = asyncio.Event()
    readers = 0
    async def synchronized_read(channel):
        nonlocal readers
        row = await original(channel)
        readers += 1
        if readers == 2:
            barrier.set()
        await barrier.wait()
        return row
    monkeypatch.setattr(repo, 'get_profile', synchronized_read)
    results = await asyncio.gather(*[
        repo.upsert_profile_text({'channel_id': channel_id, 'expected_version': 1, 'topics_text': value})
        for value in ['First', 'Second']
    ], return_exceptions=True)
    assert sum(isinstance(r, dict) for r in results) == 1
    assert sum(isinstance(r, DraftConflictError) for r in results) == 1
    winner = next(r for r in results if isinstance(r, dict))
    assert (await original(channel_id))['topics_text'] == winner['topics_text']
