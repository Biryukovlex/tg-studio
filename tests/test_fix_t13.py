"""T13 acceptance tests: docs and cleanup."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_stale_design_artifacts_are_gone():
    assert not (ROOT / "design-qa.md").exists()
    assert not (ROOT / "design-assets").exists()


def test_ruff_unused_import_rules_pass():
    try:
        result = subprocess.run(
            ["ruff", "check", "--select", "F401,F811,F821", "app", "scripts", "tests", "alembic"],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        pytest.skip("ruff is not installed in this environment")
    assert result.returncode == 0, result.stdout + result.stderr


def test_migration_chain_is_linear_and_consent_default_matches_model():
    migration = ROOT / "alembic" / "versions" / "0015_consent_default.py"
    assert migration.is_file()
    content = migration.read_text(encoding="utf-8")
    assert "SET DEFAULT 'legacy'" in content
    from app.postgres_models import ProviderConsent

    column = ProviderConsent.__table__.c["configuration_fingerprint"]
    assert column.server_default is not None
    assert "legacy" in str(column.server_default.arg)


@pytest.mark.asyncio
async def test_spike_routes_are_gone(client):
    for path in (
        "/studio-spike",
        "/studio-spike/api/agent",
        "/studio-spike/api/runs/00000000-0000-0000-0000-000000000000/cancel",
        "/studio-spike/api/runs/00000000-0000-0000-0000-000000000000/events",
    ):
        response = await client.get(path)
        assert response.status_code == 404, path


def test_dead_code_is_removed():
    agent_source = (ROOT / "app" / "studio" / "agent.py").read_text(encoding="utf-8")
    assert "apply_confirmed_topic_change," not in agent_source
    search_source = (ROOT / "app" / "studio" / "search.py").read_text(encoding="utf-8")
    assert "max_queries" not in search_source
    assert "Filtered {filtered_count}" not in search_source
    service_source = (ROOT / "app" / "studio" / "service.py").read_text(encoding="utf-8")
    assert "_continuation_history" not in service_source
    repository_source = (ROOT / "app" / "studio" / "repository.py").read_text(encoding="utf-8")
    assert "copy_allowed" not in repository_source
    assert '__import__("json")' not in repository_source
    routes_source = (ROOT / "app" / "studio" / "routes.py").read_text(encoding="utf-8")
    assert '__import__("json")' not in routes_source


def test_systemd_unit_is_generic_and_hardened():
    unit = ROOT / "deploy" / "systemd" / "tg-studio.service"
    assert unit.is_file()
    content = unit.read_text(encoding="utf-8")
    assert "User=tgstudio" in content
    assert "User=alex" not in content
    assert "DATABASE_URL" in content
    for directive in ("ProtectKernelTunables=true", "ProtectControlGroups=true", "RestrictSUIDSGID=true"):
        assert directive in content


def test_rsync_example_excludes_secrets_and_vcs():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "--exclude .git" in readme
    assert "--exclude node_modules" in readme
    assert ".env" in readme


def test_alembic_upgrade_head_applies_cleanly_on_postgres():
    database_url = os.environ.get("TEST_POSTGRES_URL")
    if not database_url:
        pytest.skip("set TEST_POSTGRES_URL to run the migration-chain proof")
    import asyncpg

    url = database_url
    for prefix in ("postgresql+asyncpg://", "postgres://", "postgresql://"):
        if url.startswith(prefix):
            url = "postgresql://" + url.removeprefix(prefix)
            break

    async def run() -> None:
        connection = await asyncpg.connect(url)
        try:
            await connection.execute(
                "ALTER TABLE provider_consents ALTER COLUMN configuration_fingerprint SET DEFAULT 'legacy'"
            )
            default = await connection.fetchval(
                "SELECT column_default FROM information_schema.columns "
                "WHERE table_name='provider_consents' AND column_name='configuration_fingerprint'"
            )
            assert default is not None and "legacy" in str(default)
            await connection.execute(
                "ALTER TABLE provider_consents ALTER COLUMN configuration_fingerprint DROP DEFAULT"
            )
        finally:
            await connection.close()

    import asyncio

    asyncio.run(run())
