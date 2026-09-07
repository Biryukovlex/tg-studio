import pytest


@pytest.mark.asyncio
async def test_studio_is_feature_flagged_and_exposes_setup_state(client, settings):
    login = await client.post(
        "/login",
        data={"username": settings.admin_username, "password": settings.admin_password},
        follow_redirects=False,
    )
    assert login.status_code == 303
    assert (await client.get("/studio")).status_code == 200
    page = await client.get("/studio")
    assert page.status_code == 200
    assert 'class="sidebar-link active" href="/studio"' in page.text
    dashboard = await client.get("/")
    assert 'href="/studio"' in dashboard.text
    assert ">Studio</span>" in dashboard.text
    assert "Finish Studio setup" in page.text
    setup = await client.get("/studio/api/setup")
    assert setup.status_code == 200
    assert setup.json()["ready"] is False
    assert {item["code"] for item in setup.json()["blockers"]} >= {
        "openrouter_key_missing", "postgres_required"
    }
