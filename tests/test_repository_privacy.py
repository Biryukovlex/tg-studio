"""Prevent private artifacts in product code, fixtures, or documentation."""
from __future__ import annotations

import pytest

from scripts.check_repository_privacy import private_path


@pytest.mark.parametrize("path", [
    ".env", "tests/.env.production", "tests/data/posts.json",
    "data/media/picture.png", "backups/history.dump", "tests/archive.sqlite3",
    "tests/telegram.session-journal", "docs/provider.log", "AGENTS.md",
    "nested/.agent-workspace/status.json", ".codex/auth.json",
    "credentials.json", "studio-frontend/settings.local.json",
    ".venv/pyvenv.cfg", "studio-frontend/node_modules/auth.json",
])
def test_private_artifacts_are_blocked_even_under_tests(path):
    assert private_path(path)


@pytest.mark.parametrize("path", [
    ".env.example", ".github/workflows/ci.yml", "app/workspace_settings.py",
    "tests/test_session_crypto.py", "docs/screenshots/channel-overview.png",
    "app/web/static/brand/tgstudio-ghost-overview.svg", ".gitleaks.toml",
])
def test_public_examples_code_and_approved_artwork_are_allowed(path):
    assert not private_path(path)
