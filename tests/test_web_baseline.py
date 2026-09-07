from datetime import datetime, timezone


async def test_login_page_is_public_and_renders(client):
    response = await client.get("/login")

    assert response.status_code == 200
    assert "Welcome back" in response.text
    assert 'name="username"' in response.text
    assert 'name="password"' in response.text


async def test_protected_pages_redirect_unauthenticated_users(client):
    for path in ("/", "/post/1", "/export.csv", "/export-comments.csv"):
        response = await client.get(path, follow_redirects=False)

        assert response.status_code == 303, path
        assert response.headers["location"] == "/login", path


async def test_invalid_login_is_rejected(client):
    response = await client.post(
        "/login",
        data={"username": "test-admin", "password": "wrong-password"},
        follow_redirects=False,
    )

    assert response.status_code == 401
    assert "Invalid username or password" in response.text


async def test_valid_login_renders_seeded_dashboard(client):
    login = await client.post(
        "/login",
        data={"username": "test-admin", "password": "test-password"},
        follow_redirects=False,
    )

    assert login.status_code == 303
    assert login.headers["location"] == "/"
    assert "session" in client.cookies

    dashboard = await client.get("/")

    assert dashboard.status_code == 200
    assert "Overview" in dashboard.text
    assert 'href="/studio"' in dashboard.text
    assert "Sample channel" in dashboard.text
    assert "A representative collected post" in dashboard.text
    assert "120" in dashboard.text


async def test_post_detail_renders_the_complete_stored_body(client, app):
    db = app.state.db
    channel = db.get_channels()[0]
    body = "Opening line\n\n" + ("Full history body. " * 25) + "END-OF-FULL-BODY"
    assert len(body) > 180
    post_id = db.upsert_post(
        int(channel["id"]),
        message_id=99,
        posted_at=datetime.now(timezone.utc),
        text=body,
    )

    await client.post(
        "/login",
        data={"username": "test-admin", "password": "test-password"},
    )
    response = await client.get(f"/post/{post_id}")

    assert response.status_code == 200
    assert body in response.text


async def test_post_detail_renders_telegram_entities_as_safe_html(client, app):
    db = app.state.db
    channel = db.get_channels()[0]
    post_id = db.upsert_post(
        int(channel["id"]),
        message_id=100,
        posted_at=datetime.now(timezone.utc),
        text="Bold <tag>",
        formatting_entities=[{"type": "bold", "offset": 0, "length": 10}],
    )

    await client.post(
        "/login",
        data={"username": "test-admin", "password": "test-password"},
    )
    response = await client.get(f"/post/{post_id}")

    assert response.status_code == 200
    assert 'class="detail-post-body"' in response.text
    from app.web.routes import static_asset_version

    assert f"/static/style.css?v={static_asset_version()}" in response.text
    assert "<strong>Bold &lt;tag&gt;</strong>" in response.text
    assert "<strong>Bold <tag>" not in response.text


async def test_logout_clears_the_authenticated_session(client):
    await client.post(
        "/login",
        data={"username": "test-admin", "password": "test-password"},
    )

    logout = await client.get("/logout", follow_redirects=False)
    after_logout = await client.get("/", follow_redirects=False)

    assert logout.status_code == 303
    assert logout.headers["location"] == "/login"
    assert after_logout.status_code == 303
    assert after_logout.headers["location"] == "/login"


async def test_studio_is_not_registered_in_the_baseline_app(client):
    """Studio is always registered; unauthenticated access redirects to login."""
    response = await client.get("/studio", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"
