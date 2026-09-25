"""T36 postgres-only-runtime acceptance tests."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.config import Settings
from app.studio.setup import build_setup_state

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_database_url_is_required_for_every_role():
    for role in ("all", "web", "worker"):
        settings = Settings(_env_file=None, database_url="", process_role=role)
        problems = settings.validate_required()
        assert any("DATABASE_URL is required (PostgreSQL)" in p for p in problems), (role, problems)
    assert "db_path" not in Settings.model_fields
    assert "allow_legacy_sqlite" not in Settings.model_fields


def test_sqlite_runtime_modules_are_gone():
    with pytest.raises(ModuleNotFoundError):
        import app.db  # noqa: F401

    with pytest.raises(ModuleNotFoundError):
        import app.async_compat  # noqa: F401

    hits = []
    for path in list((REPO_ROOT / "app").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "maybe_await" in text or "is_postgres" in text:
            hits.append(str(path.relative_to(REPO_ROOT)))
    assert hits == []


def test_setup_state_has_no_postgres_required_blocker():
    assert "postgres_required" not in (REPO_ROOT / "app" / "studio" / "setup.py").read_text(encoding="utf-8")
    settings = Settings(_env_file=None, channels="@setup")
    state = build_setup_state(settings, object())
    codes = {item["code"] for item in state["blockers"]}
    assert "postgres_required" not in codes
    assert {"openrouter_key_missing", "session_key_missing"} <= codes


def test_settings_template_has_no_readonly_banner():
    html = (REPO_ROOT / "app" / "web" / "templates" / "settings.html").read_text(encoding="utf-8")
    assert "read from .env until PostgreSQL" not in html
    assert "{% if available %}" not in html


def test_importer_modules_import_cleanly():
    import app.migration.sqlite_inventory  # noqa: F401
    import app.migration.sqlite_to_postgres  # noqa: F401
    import app.migration.legacy_schema  # noqa: F401


@pytest.mark.integration
@pytest.mark.asyncio
async def test_web_app_runs_on_postgres_without_skips(client, settings):
    """With TEST_POSTGRES_URL set, the shared web fixture serves the dashboard."""
    login = await client.post(
        "/login",
        data={"username": settings.admin_username, "password": settings.admin_password},
        follow_redirects=False,
    )
    assert login.status_code == 303
    dashboard = await client.get("/")
    assert dashboard.status_code == 200
    assert "A representative collected post" in dashboard.text
    health = await client.get("/healthz")
    assert health.json()["storage"] == "postgresql"


def test_conftest_skip_reason_without_postgres_url(monkeypatch):
    """Without TEST_POSTGRES_URL the web tests skip with a clear reason."""
    monkeypatch.delenv("TEST_POSTGRES_URL", raising=False)
    assert os.environ.get("TEST_POSTGRES_URL", "") == ""
    with pytest.raises(pytest.skip.Exception, match="TEST_POSTGRES_URL is required"):
        from tests.conftest import _pg_url

        _pg_url()
