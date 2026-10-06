import uuid

from datetime import datetime, timedelta, timezone

import pytest

from app.studio.analytics import analyze_posts
from app.studio.profile import (
    EvidenceIntegrityError,
    apply_confirmed_topic_change,
    build_profile,
    propose_topic_change,
    validate_profile_evidence,
)


AS_OF = datetime(2026, 9, 3, 12, tzinfo=timezone.utc)


def _analytics():
    rows = []
    subjects = ["climate", "markets", "climate", "technology", "markets", "science", "technology", "climate", "science", "markets"]
    for index, subject in enumerate(subjects, start=1):
        rows.append(
            {
                "post_id": index,
                "message_id": index + 100,
                "channel_id": 4,
                "posted_at": AS_OF - timedelta(days=index + 1),
                "snapshot_at": AS_OF,
                "text": f"{subject} analysis: this is a sufficiently long editorial post with context and useful details for readers. " * 2,
                "views": index * 100,
                "comments": index,
                "reactions": index * 3,
                "shares": index,
            }
        )
    return analyze_posts(rows, 4, now=AS_OF, identifier="@fixture")


def test_profile_is_versioned_and_every_observation_has_valid_evidence():
    analytics = _analytics()
    profile, analysis = build_profile(analytics)
    assert profile.profile_version == "m3.profile.v1"
    assert 5 <= len(profile.topics) <= 8
    assert analysis.input_hash == analytics.input_hash
    assert profile.style_profile.evidence_post_ids
    assert validate_profile_evidence(profile, analytics).channel_id == 4
    assert all(set(topic.representative_post_ids) <= {post.post_id for post in analytics.evidence_posts} for topic in profile.topics)


def test_profile_evidence_validation_rejects_unknown_post_ids():
    analytics = _analytics()
    profile, _ = build_profile(analytics)
    bad = profile.model_copy(update={"style_profile": profile.style_profile.model_copy(update={"evidence_post_ids": [9999]})})
    with pytest.raises(EvidenceIntegrityError):
        validate_profile_evidence(bad, analytics)


def test_exact_topic_replacement_can_be_applied_but_broad_request_waits_for_confirmation():
    analytics = _analytics()
    profile, _ = build_profile(analytics)
    exact = propose_topic_change(profile, "replace topics with climate, science")
    assert exact.requires_confirmation is True
    assert exact.status == "proposed"
    with pytest.raises(ValueError):
        apply_confirmed_topic_change(profile, exact)

    broad = propose_topic_change(profile, "please rethink the topics around our audience")
    assert broad.requires_confirmation is True
    with pytest.raises(ValueError):
        apply_confirmed_topic_change(profile, broad)


def test_add_and_remove_topic_requests_are_confirmation_gated():
    profile, _ = build_profile(_analytics())
    added = propose_topic_change(profile, "add topics robotics, design")
    assert added.status == "proposed"
    assert "robotics" in added.proposed_topics
    removed = propose_topic_change(profile, "remove topics climate")
    assert removed.requires_confirmation is True
    assert "climate" not in removed.proposed_topics


@pytest.mark.asyncio
async def test_profile_change_api_requires_csrf_and_confirmation(client, settings, app):
    settings.studio_test_mode = True
    response = await client.post("/login", data={"username": settings.admin_username, "password": settings.admin_password}, follow_redirects=False)
    assert response.status_code == 303
    home = await client.get("/studio")
    import re
    token = re.search(r'<meta name="studio-csrf-token" content="([^"]+)"', home.text).group(1)
    # After T14 the profile changes API is removed; all old routes return 404
    denied = await client.post("/studio/api/profile/changes", json={"instruction": "replace topics with climate"})
    assert denied.status_code == 404
    proposed = await client.post(
        "/studio/api/profile/changes",
        json={"instruction": "replace topics with climate, science"},
        headers={"x-csrf-token": token},
    )
    assert proposed.status_code == 404
    # Use a dummy UUID for confirm/apply
    import uuid
    dummy = str(uuid.uuid4())
    confirmed = await client.post(f"/studio/api/profile/changes/{dummy}/confirm", headers={"x-csrf-token": token})
    assert confirmed.status_code == 404
    applied = await client.post(f"/studio/api/profile/changes/{dummy}/apply", headers={"x-csrf-token": token})
    assert applied.status_code == 404


@pytest.mark.asyncio
async def test_openrouter_consent_is_explicit_and_precedes_agent_use(client, settings, app, channel_id):
    settings.studio_test_mode = False
    settings.openrouter_api_key = "router-test-key"
    settings.telegram_session_encryption_key = "a" * 48
    # Keep the deterministic memory repository while exercising the production
    # setup/consent branch; no provider request is made by this test.
    app.state.db.is_postgres = True
    await client.post("/login", data={"username": settings.admin_username, "password": settings.admin_password}, follow_redirects=False)
    home = await client.get("/studio")
    import re
    token = re.search(r'<meta name="studio-csrf-token" content="([^"]+)"', home.text).group(1)
    current = await client.get("/studio/api/consent")
    assert current.status_code == 200
    assert current.json()["consent"]["required"] is True
    assert current.json()["consent"]["granted"] is False
    assert "comment bodies" in current.json()["consent"]["disclosure"]["message"]

    denied = await client.post(
        "/studio/api/consent", json={"confirm": False}, headers={"x-csrf-token": token}
    )
    assert denied.status_code == 409
    granted = await client.post(
        "/studio/api/consent", json={"confirm": True, "configuration_fingerprint": current.json()["consent"]["configuration_fingerprint"]}, headers={"x-csrf-token": token}
    )
    assert granted.status_code == 200
    assert granted.json()["consent"]["granted"] is True
    assert granted.json()["profile"] is not None
    revoked = await client.post("/studio/api/consent/revoke", headers={"x-csrf-token": token})
    assert revoked.status_code == 200
    assert revoked.json()["consent"]["granted"] is False
    import uuid
    conversation = await client.post("/studio/api/conversations", json={"channel_id": channel_id}, headers={"x-csrf-token": token})
    assert conversation.status_code == 200
    blocked = await client.post(
        "/studio/api/agent",
        json={
            "threadId": conversation.json()["conversation"]["id"],
            "runId": str(uuid.uuid4()),
            "messages": [{"id": str(uuid.uuid4()), "role": "user", "content": "Analyze"}],
            "tools": [], "context": [], "forwardedProps": {},
        },
        headers={"x-csrf-token": token, "accept": "text/event-stream"},
    )
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "provider_consent_required"


@pytest.mark.asyncio
async def test_save_profile_accepts_supporting_posts_older_than_recent_archive(client, settings, app, channel_id):
    """Build may select an old top performer beyond the latest 2,000 posts."""
    settings.studio_test_mode = True
    from sqlalchemy import text
    db = app.state.db
    async with db.sessions.session() as session:
        oldest = (await session.execute(text("SELECT id FROM posts WHERE workspace_id=:workspace_id AND channel_id=:channel_id"),
                    {"workspace_id": db.workspace_id, "channel_id": channel_id})).scalar_one()
        await session.execute(text("""INSERT INTO posts (workspace_id, channel_id, message_id, posted_at, text, created_at)
            SELECT :workspace_id, :channel_id, n, '2025-01-01'::timestamptz, 'Synthetic post', now()
              FROM generate_series(100, 2100) AS n"""),
            {"workspace_id": db.workspace_id, "channel_id": channel_id})
        await session.execute(text("""INSERT INTO snapshots (workspace_id, post_id, taken_at, views, comments, reactions, shares)
            SELECT workspace_id, id, now(), 1, 0, 0, 0 FROM posts
             WHERE workspace_id=:workspace_id AND channel_id=:channel_id AND id<>:oldest"""),
            {"workspace_id": db.workspace_id, "channel_id": channel_id, "oldest": oldest})
        await session.commit()
    import re
    await client.post("/login", data={"username": settings.admin_username, "password": settings.admin_password})
    home = await client.get("/studio")
    token = re.search(r'<meta name="studio-csrf-token" content="([^"]+)"', home.text).group(1)
    current = (await client.get(f"/studio/api/profile?channel_id={channel_id}")).json()["profile"]
    version = current["version"] if current else 0
    payload = {"channel_id": channel_id, "expected_version": version, "topics_text": "Topic from old top performer",
               "editorial_text": "One rule", "style_text": "One style rule", "evidence_post_ids": [oldest]}
    saved = await client.put("/studio/api/profile", json=payload, headers={"x-csrf-token": token})
    assert saved.status_code == 200, saved.text
    profile = saved.json()["profile"]
    assert profile["version"] == version + 1
    assert profile["evidence_post_ids"] == [oldest]
    assert profile["built_from_posts"] == 1
    # An ordinary text edit keeps the saved sample metadata.
    payload.pop("evidence_post_ids")
    payload.update(expected_version=profile["version"], topics_text="Edited topic")
    edited = await client.put("/studio/api/profile", json=payload, headers={"x-csrf-token": token})
    assert edited.status_code == 200, edited.text
    assert edited.json()["profile"]["built_from_posts"] == 1
    assert edited.json()["profile"]["evidence_post_ids"] == [oldest]


@pytest.mark.asyncio
async def test_profile_save_rejects_foreign_and_missing_supporting_posts(client, settings, app, channel_id):
    settings.studio_test_mode = True
    import re
    from app.postgres_db import PostgresDatabase
    db = app.state.db
    other_channel = await db.upsert_channel("@other_synthetic", "Other", 987654)
    other_post = await db.upsert_post(other_channel, message_id=1, posted_at=datetime.now(timezone.utc), text="Other channel")
    await db.add_snapshot_if_changed(other_post, views=1, comments=0, reactions=0, shares=0)
    foreign = PostgresDatabase(settings.database_url, workspace_slug="profile-save-" + uuid.uuid4().hex)
    await foreign.init_db(admin_username="synthetic-owner")
    try:
        foreign_channel = await foreign.upsert_channel("@foreign_synthetic", "Foreign", 123)
        foreign_post = await foreign.upsert_post(foreign_channel, message_id=1, posted_at=datetime.now(timezone.utc), text="Foreign workspace")
        await foreign.add_snapshot_if_changed(foreign_post, views=1, comments=0, reactions=0, shares=0)
        await client.post("/login", data={"username": settings.admin_username, "password": settings.admin_password})
        home = await client.get("/studio")
        token = re.search(r'<meta name="studio-csrf-token" content="([^"]+)"', home.text).group(1)
        current = (await client.get(f"/studio/api/profile?channel_id={channel_id}")).json()["profile"]
        version = current["version"] if current else 0
        for post_id in (other_post, foreign_post, -1, 9223372036854775807, 2**64):
            response = await client.put("/studio/api/profile", json={"channel_id": channel_id, "expected_version": version,
                "topics_text": "Synthetic topic", "evidence_post_ids": [post_id]}, headers={"x-csrf-token": token})
            assert response.status_code == 422
            assert response.json()["error"]["code"] == "invalid_profile_evidence"
        after = (await client.get(f"/studio/api/profile?channel_id={channel_id}")).json()["profile"]
        assert after == current
    finally:
        await foreign._execute("DELETE FROM workspaces WHERE id=:workspace_id")
        await foreign.close()
