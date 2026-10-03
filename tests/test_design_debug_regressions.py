"""Regression coverage for the deployed cumulative/cohort mismatch."""
from datetime import datetime, timedelta, timezone
import pytest

@pytest.mark.integration
@pytest.mark.asyncio
async def test_default_overview_has_one_cohort_and_no_cumulative_double_count(client, app):
    db = app.state.db
    channel = await db.upsert_channel('@design_cohort', 'Design cohort', 7878)
    today = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0)
    for mid, age, views in [(1, 0, 20), (2, 2, 30), (3, 40, 10000)]:
        post = await db.upsert_post(channel, mid, today - timedelta(days=age), f'synthetic post {mid}')
        await db.add_snapshot_if_changed(post, views=views, reactions=views // 10, comments=1, shares=2)
    await client.post('/login', data={'username':'test-admin','password':'test-password'})
    for endpoint in ['/', '/api/overview']:
        response = await client.get(endpoint, params={'channel':channel}, headers={'Accept':'application/json'})
        assert response.status_code == 200
        payload = response.json()
        series = payload.get('series') or payload['chart']
        assert len(series.get('days') or series['labels']) == 14
        assert payload['kpis']['posts'] == 2
        assert payload['kpis']['views'] == 50
        assert sum(series['views']) == 50
        assert series['views'][-1] == 20
        assert series['views'][-2] == 0
        assert series['views'][-3] == 30
        for metric in ['reactions','comments','shares']:
            assert sum(series[metric]) == payload['kpis'][metric]
    all_history = await client.get('/api/overview', params={'channel':channel,'days':'all'})
    assert all_history.status_code == 200
    payload = all_history.json()
    assert sum(payload['series']['views']) == payload['kpis']['views'] == 10050
    page = await client.get('/settings')
    assert f'href="/studio?channel={channel}"' in page.text
