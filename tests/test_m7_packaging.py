"""Release packaging contracts for PostgreSQL and optional web research."""

from __future__ import annotations

from pathlib import Path

import yaml

from app import limits
from app.config import Settings
from app.studio.search_health import configured_search_state


ROOT = Path(__file__).resolve().parents[1]


def _compose() -> dict:
    with (ROOT / "docker-compose.yml").open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def test_compose_has_explicit_migration_and_persistent_postgres():
    document = _compose()
    services = document["services"]

    assert document["name"] == "tg-studio"
    assert "postgres-data" in document["volumes"]
    assert services["postgres"]["healthcheck"]["test"]
    assert services["postgres"]["ports"] == [
        "${POSTGRES_BIND_ADDRESS:-127.0.0.1}:${POSTGRES_HOST_PORT:-55432}:5432"
    ]
    assert services["migrate"]["profiles"] == ["migrate"]
    assert services["migrate"]["command"] == ["python", "-m", "alembic", "upgrade", "head"]
    assert "postgres" in services["tg-studio"]["depends_on"]


def test_optional_search_is_private_and_not_a_runtime_dependency():
    document = _compose()
    services = document["services"]
    search = services["searxng"]

    assert search["profiles"] == ["studio-search"]
    assert "ports" not in search
    assert "searxng" not in services["tg-studio"].get("depends_on", {})
    assert "searxng-data" in document["volumes"]


def test_split_profile_assigns_one_role_per_process():
    services = _compose()["services"]

    assert services["tg-studio-web"]["profiles"] == ["split"]
    assert services["tg-studio-web"]["environment"]["PROCESS_ROLE"] == "web"
    assert services["tg-studio-worker"]["profiles"] == ["split"]
    assert services["tg-studio-worker"]["environment"]["PROCESS_ROLE"] == "worker"


def test_search_configuration_is_explicitly_degraded_when_endpoint_is_missing():
    state = configured_search_state(
        Settings(studio_search_enabled=True, studio_search_base_url="")
    )

    assert state.enabled is True
    assert state.configured is False
    assert state.degraded is True
    assert state.available is False
