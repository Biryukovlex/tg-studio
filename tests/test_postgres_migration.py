"""Verify the PostgreSQL upgrade chain can be applied repeatedly."""

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text

from app.db_session import DatabaseSessionManager
from tests.helpers.postgres import pg_url

pytestmark = pytest.mark.integration
REPO_ROOT = Path(__file__).resolve().parents[1]


def test_alembic_upgrade_is_repeatable_and_reaches_current_schema():
    database_url = pg_url("M0_POSTGRES_URL")
    if not database_url:
        pytest.skip("set TEST_POSTGRES_URL to run the isolated PostgreSQL proof")
    environment = os.environ.copy()
    environment["DATABASE_URL"] = database_url
    environment.pop("M0_POSTGRES_URL", None)
    for _ in range(2):
        subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=REPO_ROOT, check=True, capture_output=True, text=True, env=environment,
        )
    head = ScriptDirectory.from_config(Config(str(REPO_ROOT / "alembic.ini"))).get_current_head()

    async def inspect_schema():
        sessions = DatabaseSessionManager(database_url)
        try:
            async with sessions.session() as session:
                assert (await session.execute(text("SELECT version_num FROM alembic_version"))).scalar_one() == head
                for table in ("workspaces", "channels", "posts", "snapshots", "comments", "studio_conversations"):
                    assert (await session.execute(text("SELECT to_regclass(:table)"), {"table": table})).scalar_one() == table
        finally:
            await sessions.dispose()

    asyncio.run(inspect_schema())
