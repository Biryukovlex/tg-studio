"""Process-role contracts for community and split deployments."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings


def test_web_role_does_not_require_telegram_credentials():
    settings = Settings(process_role="web", admin_password="web-password")

    problems = settings.validate_required()

    assert problems == []


def test_worker_role_does_not_require_web_admin_password():
    settings = Settings(
        process_role="worker",
        api_id=123,
        api_hash="hash",
        session_string="session",
        channels="@channel",
    )

    problems = settings.validate_required()

    assert problems == []


def test_unknown_process_role_is_rejected():
    settings = Settings(process_role="scheduler", admin_password="password")

    assert any("PROCESS_ROLE" in problem for problem in settings.validate_required())


def test_worker_scheduler_is_started_once_before_role_branch():
    source = (Path(__file__).resolve().parents[1] / "app" / "main.py").read_text(
        encoding="utf-8"
    )

    assert source.count("scheduler.start()") == 1
    assert source.index("scheduler.start()") < source.index('if role == "worker":')


@pytest.mark.asyncio
async def test_web_refresh_is_delegated_to_worker(client, settings):
    settings.process_role = "web"
    login = await client.post(
        "/login",
        data={"username": settings.admin_username, "password": settings.admin_password},
        follow_redirects=False,
    )
    assert login.status_code == 303

    response = await client.post("/refresh", follow_redirects=False)

    assert response.status_code == 303
    assert "collector+worker" in response.headers["location"]
