"""T34 profile-build-and-run-details acceptance tests."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from pydantic_ai.exceptions import ModelHTTPError

from app.config import Settings
from app.db import Database
from app.studio.analytics import analyze_posts
from app.studio.profile import _build_draft_from_analytics, _is_template_line
from app.studio.service import _safe_event_payload
from app.web.routes import create_app


def _settings(tmp_path, **overrides) -> Settings:
    base = dict(
        _env_file=None,
        api_id=1,
        api_hash="hash",
        session_string="session",
        channels="@sample_channel",
        data_dir=str(tmp_path),
        admin_username="admin",
        admin_password="pw",
        session_secret="secret",
        studio_test_mode=True,
    )
    base.update(overrides)
    return Settings(**base)


def _rows(n: int):
    now = datetime.now(timezone.utc)
    rows = []
    for i in range(n):
        rows.append({
            "post_id": i + 1, "message_id": 100 + i, "channel_id": 1,
            "posted_at": now - timedelta(days=i + 1), "snapshot_at": now,
            "text": f"Story {i} about the city budget vote " + "x" * 90,
            "views": 100 + i * 7, "reactions": 5, "comments": 1, "shares": 1,
            "formatting_entities": [],
        })
    return rows


@pytest.mark.asyncio
async def test_profile_build_maps_provider_error_to_safe_code(tmp_path):
    settings = _settings(tmp_path)
    db = Database(tmp_path / "t34.sqlite")
    db.init_db()
    app = create_app(SimpleNamespace(db=db, client=object()), settings)
    service = app.state.studio_service

    async def _rows5(channel_id: int, limit: int = 2000):
        now = datetime.now(timezone.utc)
        return [
            {"post_id": i, "message_id": i, "posted_at": now, "text": "row",
             "views": 1, "reactions": 0, "comments": 0, "shares": 0,
             "formatting_entities": []}
            for i in range(1, 6)
        ]

    async def _boom(channel_id: int):
        raise ModelHTTPError(402, "x/y", {"secret": "https://private.example/body"})

    service.repository.performance_rows = _rows5  # type: ignore[method-assign]
    service.build_profile_draft = _boom  # type: ignore[method-assign]

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        login = await client.post("/login", data={"username": "admin", "password": "pw"})
        assert login.status_code == 303
        home = await client.get("/studio")
        import re

        token = re.search(r'<meta name="studio-csrf-token" content="([^"]+)"', home.text).group(1)
        response = await client.post(
            "/studio/api/profile/build",
            json={"channel_id": 1},
            headers={"x-csrf-token": token, "content-type": "application/json"},
        )
        assert response.status_code == 502, response.text
        error = response.json()["error"]
        assert error["code"] == "provider_payment_required"
        assert "http" not in error["message"].lower()
        assert "https://private.example" not in response.text


def test_heuristic_topics_are_deduplicated():
    analytics = analyze_posts(_rows(5), 1, now=datetime.now(timezone.utc), identifier="@t34")
    draft = _build_draft_from_analytics(analytics, _rows(5))
    assert draft.topics.count("Story — appears in successful posts") == 1
    assert len(draft.topics) == len({line.lower() for line in draft.topics})
    assert all(len(line.split()[0]) >= 3 for line in draft.topics)


def test_then_rules_survive_template_filter():
    assert _is_template_line("Explain the problem, then the fix") is False
    assert _is_template_line("Start with a headline, then two paragraphs") is True
    assert _is_template_line("End with a signature line") is True
    assert _is_template_line("Always use the format X") is True
    assert _is_template_line("1. First do this") is True


def test_run_event_payload_carries_draft_id_without_none_strings():
    event = SimpleNamespace(
        type="TOOL_CALL_RESULT",
        tool_call_name="create_draft",
        tool_call_id="call-1",
        delta=None,
        content=json.dumps({
            "draft": {"id": "draft-abc", "analysis_id": None, "story_cluster_id": None},
            "analysis_id": None,
            "story_cluster_id": None,
        }),
    )
    payload = _safe_event_payload(event)
    assert payload["draft_id"] == "draft-abc"
    assert "None" not in json.dumps(payload)
    assert "analysis_id" not in payload
    assert "story_cluster_id" not in payload
