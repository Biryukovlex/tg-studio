"""Shared test factories for isolated Settings instances."""

from __future__ import annotations

from typing import Any

from app.config import Settings


def make_settings(**overrides: Any) -> Settings:
    """Build a Settings object with synthetic test defaults plus overrides.

    The autouse env-isolation fixture in tests/conftest.py guarantees no
    operator ``.env`` values leak in; every field here is synthetic.
    """

    values: dict[str, Any] = {
        "api_id": 1,
        "api_hash": "test-api-hash",
        "session_string": "test-session",
        "channels": "@sample_channel",
        "admin_username": "test-admin",
        "admin_password": "test-password",
        "session_secret": "test-session-secret",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)
