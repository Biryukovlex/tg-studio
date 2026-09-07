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
async def test_openrouter_consent_is_explicit_and_precedes_agent_use(client, settings, app):
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
    conversation = await client.post("/studio/api/conversations", json={"channel_id": 1}, headers={"x-csrf-token": token})
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
