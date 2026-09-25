"""T35 collection-lease-and-login-lockout acceptance tests."""
from __future__ import annotations

import inspect

import pytest

from app.config import Settings
from app.main import login_rate_limit_warning
from app.postgres_db import PostgresDatabase
from app.web.routes import reset_login_rate_limiter


@pytest.fixture(autouse=True)
def _reset_limiter():
    reset_login_rate_limiter()
    yield
    reset_login_rate_limiter()


def _settings(tmp_path, **overrides) -> Settings:
    base = dict(
        api_id=1,
        api_hash="test-api-hash",
        session_string="test-session",
        channels="@sample_channel",
        data_dir=str(tmp_path),
        admin_username="owner",
        admin_password="correct",
        session_secret="test-secret-t35",
    )
    base.update(overrides)
    return Settings(**base)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_failed_logins_for_one_user_do_not_block_another(client, settings):
    for _ in range(5):
        resp = await client.post("/login", data={"username": "a", "password": "wrong"}, follow_redirects=False)
        assert resp.status_code == 401
    # The owner logs in from the same address while user `a` is throttled.
    ok = await client.post("/login", data={"username": settings.admin_username, "password": settings.admin_password}, follow_redirects=False)
    assert ok.status_code == 303

    sixth = await client.post("/login", data={"username": "a", "password": "wrong"}, follow_redirects=False)
    assert sixth.status_code == 429
    retry_after = int(sixth.headers["Retry-After"])
    assert 0 < retry_after <= 300


def test_collection_lease_defaults_to_three_minutes():
    assert inspect.signature(PostgresDatabase.claim_collection_job).parameters["lease_seconds"].default == 180
    assert inspect.signature(PostgresDatabase.renew_collection_job).parameters["lease_seconds"].default == 180


def test_proxy_warning_only_for_non_loopback_without_trusted_proxies(tmp_path):
    assert login_rate_limit_warning(_settings(tmp_path, web_host="0.0.0.0")) is not None
    assert "TRUSTED_PROXY_IPS" in login_rate_limit_warning(_settings(tmp_path, web_host="0.0.0.0"))
    assert login_rate_limit_warning(_settings(tmp_path, web_host="127.0.0.1")) is None
    assert login_rate_limit_warning(_settings(tmp_path, web_host="::1")) is None
    assert login_rate_limit_warning(_settings(tmp_path, web_host="0.0.0.0", trusted_proxy_ips="10.0.0.1")) is None
