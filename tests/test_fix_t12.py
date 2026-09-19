"""T12 acceptance tests: CI and test isolation."""
from __future__ import annotations

import os
from pathlib import Path

from app.config import Settings

ROOT = Path(__file__).resolve().parents[1]
CI = ROOT / ".github" / "workflows" / "ci.yml"


def test_operator_env_does_not_leak_into_settings(monkeypatch):
    # The autouse isolation fixture deletes operator keys, so a default
    # Settings() build must see defaults rather than deployment values.
    assert os.environ.get("API_HASH") is None
    assert os.environ.get("DATABASE_URL") in (None, "")
    assert Settings().api_hash == ""
    # Explicitly set test values still flow through (isolation is not breakage).
    monkeypatch.setenv("API_HASH", "canary-test-value")
    assert Settings().api_hash == "canary-test-value"


def test_no_test_reads_the_repo_env_file():
    # The autouse isolation fixture nulls the env file for the whole test
    # session, so Settings() can never consult BASE_DIR/.env here.
    assert Settings.model_config.get("env_file") is None


def test_make_settings_helper_is_isolated():
    from tests.helpers.settings import make_settings

    first = make_settings()
    second = make_settings(api_hash="other-synthetic-value")
    assert first.api_hash == "test-api-hash"
    assert second.api_hash == "other-synthetic-value"


def test_ci_installs_locked_dependencies():
    text = CI.read_text(encoding="utf-8")
    assert "requirements.lock" in text
    assert "requirements-dev.txt" in text
    assert "pip install -r requirements-dev.txt\n" not in text


def test_ci_fails_on_stale_lock():
    text = CI.read_text(encoding="utf-8")
    assert "check_lock_consistency" in text


def test_ci_fails_on_stale_frontend_bundle():
    text = CI.read_text(encoding="utf-8")
    assert "studio-dist" in text
    assert "git diff --exit-code" in text


def test_ci_builds_the_container_image():
    text = CI.read_text(encoding="utf-8")
    assert "docker build" in text


def test_ci_runs_linters_and_type_checks():
    text = CI.read_text(encoding="utf-8")
    assert "ruff check" in text
    assert "mypy" in text
    assert "npm" in text and "lint" in text


def test_ci_runs_postgres_integration():
    text = CI.read_text(encoding="utf-8")
    assert "postgres" in text
    assert "TEST_POSTGRES_URL" in text
    assert "-m postgres" in text


def test_ci_runs_csrf_sweep_via_test_suite():
    text = CI.read_text(encoding="utf-8")
    assert "pytest" in text
    sweep = ROOT / "tests" / "test_studio_m6_security.py"
    assert "test_every_studio_route_requires_auth_and_every_mutation_requires_csrf" in sweep.read_text(
        encoding="utf-8"
    )


def test_postgres_marker_is_registered_and_strict():
    config = (ROOT / "pytest.ini").read_text(encoding="utf-8")
    assert "postgres" in config
    assert "--strict-markers" in config
