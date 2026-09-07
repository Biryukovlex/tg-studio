from __future__ import annotations

import re
import uuid

import pytest

from app.studio.repository import MemoryStudioRepository


async def _login(client, settings):
    response = await client.post(
        "/login",
        data={"username": settings.admin_username, "password": settings.admin_password},
        follow_redirects=False,
    )
    assert response.status_code == 303


async def _csrf(client) -> str:
    response = await client.get("/studio")
    assert response.status_code == 200
    match = re.search(r'<meta name="studio-csrf-token" content="([^"]+)"', response.text)
    assert match
    return match.group(1)


async def _conversation_and_draft(client, app, settings, *, body: str = "Line one\n\nLine two"):
    settings.studio_enabled = True
    settings.studio_test_mode = True
    await _login(client, settings)
    token = await _csrf(client)
    created = await client.post("/studio/api/conversations", json={"channel_id": 1}, headers={"x-csrf-token": token})
    assert created.status_code == 200
    conversation = created.json()["conversation"]
    repository: MemoryStudioRepository = app.state.studio_repository
    draft = await repository.create_draft(
        conversation_id=uuid.UUID(conversation["id"]),
        channel_id=1,
        payload={"body": body, "creative": True},
    )
    return token, conversation, draft


@pytest.mark.asyncio
async def test_draft_routes_require_auth_and_csrf(client, settings):
    settings.studio_enabled = True
    settings.studio_test_mode = True
    unknown = str(uuid.uuid4())
    unauth = await client.get(f"/studio/api/drafts/{unknown}")
    assert unauth.status_code == 401
    assert (unauth.json().get("error") or unauth.json().get("detail", {}).get("error", {})).get("code") == "unauthenticated"
    await _login(client, settings)
    assert (await client.post(f"/studio/api/drafts/{unknown}/copied")).status_code == 403


@pytest.mark.asyncio
async def test_draft_get_patch_conflict_versions_and_exact_copy(client, app, settings):
    token, conversation, draft = await _conversation_and_draft(client, app, settings)
    fetched = await client.get(f"/studio/api/drafts/{draft['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["draft"]["body"] == "Line one\n\nLine two"
    patch = await client.patch(
        f"/studio/api/drafts/{draft['id']}",
        json={"expected_revision": 1, "body": "Edited 🙂\nsecond", "save_as_new_version": True},
        headers={"x-csrf-token": token},
    )
    assert patch.status_code == 200
    current = patch.json()["draft"]
    assert current["character_count"] == len("Edited 🙂\nsecond")
    conflict = await client.patch(
        f"/studio/api/drafts/{draft['id']}",
        json={"expected_revision": 1, "body": "Local stale text"},
        headers={"x-csrf-token": token},
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "draft_conflict"
    assert conflict.json()["server_draft"]["body"] == "Edited 🙂\nsecond"
    assert conflict.json()["local_draft"]["body"] == "Local stale text"
    restored = await client.patch(
        f"/studio/api/drafts/{draft['id']}",
        json={"expected_revision": current["revision"], "choose_version": 1},
        headers={"x-csrf-token": token},
    )
    assert restored.status_code == 200
    assert restored.json()["draft"]["body"] == "Line one\n\nLine two"
    versions = await client.get(f"/studio/api/drafts/{draft['id']}/versions")
    assert versions.status_code == 200
    # Choose moves the current pointer back to v1; it does not mint a version.
    assert [item["version"] for item in versions.json()["versions"]] == [1, 2]
    assert restored.json()["draft"]["current_version"] == 1
    copied = await client.post(f"/studio/api/drafts/{draft['id']}/copied", headers={"x-csrf-token": token})
    assert copied.status_code == 200
    assert copied.json()["copied_text"] == "Line one\n\nLine two"
    assert copied.json()["draft"]["copied_at"]


@pytest.mark.asyncio
async def test_draft_copy_is_blocked_server_side_above_telegram_limit(client, app, settings):
    token, _, draft = await _conversation_and_draft(client, app, settings, body="x" * 4097)
    current = await client.get(f"/studio/api/drafts/{draft['id']}")
    assert current.json()["draft"]["over_limit"] is True
    copied = await client.post(f"/studio/api/drafts/{draft['id']}/copied", headers={"x-csrf-token": token})
    assert copied.status_code == 409
    assert copied.json()["error"]["code"] == "draft_too_long_to_copy"


@pytest.mark.asyncio
async def test_conversation_draft_reload_returns_active_artifact(client, app, settings):
    _, conversation, draft = await _conversation_and_draft(client, app, settings, body="reload me")
    response = await client.get(f"/studio/api/conversations/{conversation['id']}/draft")
    assert response.status_code == 200
    assert response.json()["draft"]["id"] == draft["id"]


@pytest.mark.asyncio
async def test_generated_candidate_is_visible_without_replacing_owner_edit(client, app, settings):
    _, conversation, draft = await _conversation_and_draft(client, app, settings, body="initial")
    repository: MemoryStudioRepository = app.state.studio_repository
    owner = await repository.save_draft(
        draft_id=uuid.UUID(draft["id"]),
        payload={"body": "owner's visible edit"},
        expected_revision=draft["revision"],
    )
    candidate = await repository.revise_draft(
        draft_id=uuid.UUID(draft["id"]),
        payload={"body": "model candidate"},
        instruction="Try a tighter version.",
    )
    assert candidate["current_version_origin"] == "regenerated"
    current = await client.get(f"/studio/api/drafts/{draft['id']}")
    assert current.status_code == 200
    assert current.json()["draft"]["body"] == "model candidate"
    versions = await client.get(f"/studio/api/drafts/{draft['id']}/versions")
    assert versions.status_code == 200
    assert versions.json()["versions"][-1]["body"] == "model candidate"
