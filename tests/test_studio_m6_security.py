from __future__ import annotations

import ast
import asyncio
import os
from pathlib import Path
import re
from types import SimpleNamespace
import uuid

import pytest

from app.config import Settings
from app.studio.agent import StudioDeps, build_agent
from app.studio.repository import MemoryStudioRepository


ROOT = Path(__file__).resolve().parents[1]


def test_every_studio_route_requires_auth_and_every_mutation_requires_csrf():
    source = (ROOT / "app/studio/routes.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    route_count = 0
    mutation_count = 0
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        methods: list[str] = []
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Attribute):
                continue
            owner = decorator.func.value
            if isinstance(owner, ast.Name) and owner.id == "router":
                methods.append(decorator.func.attr.lower())
        if not methods:
            continue
        route_count += 1
        body = ast.get_source_segment(source, node) or ""
        assert "require_auth(request)" in body, f"{node.name} lacks authentication"
        if any(method in {"post", "put", "patch", "delete"} for method in methods):
            mutation_count += 1
            assert "require_csrf(request)" in body, f"{node.name} lacks CSRF validation"
    assert route_count >= 20
    assert mutation_count >= 10


@pytest.mark.asyncio
async def test_agent_supplied_ids_cannot_cross_conversation_or_channel_scope():
    repository = MemoryStudioRepository()
    repository.channels.append(
        {"id": 2, "identifier": "@other_channel", "title": "Other channel", "active": True}
    )
    first = await repository.create_conversation(channel_id=1)
    second = await repository.create_conversation(channel_id=2)
    foreign_draft = await repository.create_draft(
        conversation_id=second["id"],
        channel_id=2,
        payload={"body": "Foreign creative draft", "creative": True},
    )
    await repository.upsert_profile(
        {"channel_id": 1, "topics": [], "style_profile": {}, "editorial_rules": {}, "confidence": "low"}
    )
    await repository.upsert_profile(
        {"channel_id": 2, "topics": [], "style_profile": {}, "editorial_rules": {}, "confidence": "low"}
    )
    foreign_change = await repository.create_profile_change(
        {
            "channel_id": 2,
            "base_profile_version": 1,
            "proposed_topics": [{"name": "foreign"}],
            "status": "proposed",
        }
    )
    await repository.confirm_profile_change(foreign_change["id"])

    deps = StudioDeps(
        repository=repository,
        workspace_id=repository.workspace_id,
        conversation_id=first["id"],
        channel_id=1,
        cancel_event=asyncio.Event(),
    )
    ctx = SimpleNamespace(deps=deps)
    agent = build_agent(Settings(studio_test_mode=True))

    save = agent._function_toolset.tools["save_draft"].function
    blocked_draft = await save(ctx, str(foreign_draft["id"]), 1, "Overwrite attempt")
    assert blocked_draft["status"] == "blocked"
    unchanged = await repository.get_draft(foreign_draft["id"])
    assert unchanged and unchanged["body"] == "Foreign creative draft"

    apply_change = agent._function_toolset.tools["apply_confirmed_topic_changes"].function
    blocked_change = await apply_change(ctx, str(foreign_change["id"]))
    assert blocked_change["status"] == "blocked"
    assert "not part of this channel" in blocked_change["reason"]
    assert (await repository.get_profile(2))["version"] == 1


async def _login(client, settings) -> None:
    response = await client.post(
        "/login",
        data={"username": settings.admin_username, "password": settings.admin_password},
        follow_redirects=False,
    )
    assert response.status_code == 303


@pytest.mark.asyncio
async def test_browser_and_exports_do_not_return_runtime_secrets(client, settings):
    settings.studio_enabled = True
    settings.studio_test_mode = True
    secrets = {
        "openrouter": "or-secret-m6-never-return",
        "telegram": "telethon-session-m6-never-return",
        "api_hash": "telegram-api-hash-m6-never-return",
        "encryption": "encryption-key-m6-never-return-123456789",
    }
    settings.openrouter_api_key = secrets["openrouter"]
    settings.session_string = secrets["telegram"]
    settings.api_hash = secrets["api_hash"]
    settings.telegram_session_encryption_key = secrets["encryption"]
    await _login(client, settings)
    responses = [
        await client.get("/studio/api/setup"),
        await client.get("/studio/api/bootstrap"),
        await client.get("/export.csv"),
        await client.get("/export-comments.csv"),
    ]
    serialized = "\n".join(response.text for response in responses)
    assert all(value not in serialized for value in secrets.values())
    assert "encrypted_session" not in serialized


def test_default_log_calls_do_not_include_private_body_or_prompt_values():
    for relative in ("app/studio/service.py", "app/studio/research.py", "app/studio/sources.py"):
        source = (ROOT / relative).read_text(encoding="utf-8")
        for line in source.splitlines():
            if re.search(r"\b(log|logger)\.(debug|info|warning|error|exception)\(", line):
                lowered = line.lower()
                assert not any(token in lowered for token in ("prompt", "body", "excerpt", "session_string", "api_key"))


@pytest.mark.integration
@pytest.mark.asyncio
async def test_postgres_repository_and_routes_fail_closed_across_workspaces():
    database_url = os.environ.get("M6_POSTGRES_URL")
    if not database_url:
        pytest.skip("set M6_POSTGRES_URL to run the M6 tenant/encryption proof")

    import httpx

    from app.postgres_db import PostgresDatabase
    from app.session_crypto import SessionCipher
    from app.studio.repository import StudioRepository
    from app.web.routes import create_app

    suffix = uuid.uuid4().hex[:10]
    slug_a, slug_b = f"m6-sec-a-{suffix}", f"m6-sec-b-{suffix}"
    admin_a, admin_b = f"{slug_a}-admin", f"{slug_b}-admin"
    db_a = PostgresDatabase(database_url, workspace_slug=slug_a)
    db_b = PostgresDatabase(database_url, workspace_slug=slug_b)

    class Collector:
        def __init__(self, db):
            self.db = db

        async def poll_all(self, reason="test"):
            return {"reason": reason, "posts_seen": 0}

    try:
        await db_a.init_db(admin_username=admin_a)
        await db_b.init_db(admin_username=admin_b)
        await db_a.upsert_channel(f"@{slug_a}", "Workspace A", 6201)
        channel_b = await db_b.upsert_channel(f"@{slug_b}", "Workspace B", 6202)
        repo_a, repo_b = StudioRepository(db_a), StudioRepository(db_b)
        conversation_b = await repo_b.create_conversation(channel_id=channel_b)
        await repo_b.persist_research_bundle(
            conversation_id=conversation_b["id"],
            channel_id=channel_b,
            bundle={
                "sources": [{"source_id": "workspace-b-source", "url": "https://example.test/b"}],
                "stories": [],
            },
        )
        draft_b = await repo_b.create_draft(
            conversation_id=conversation_b["id"],
            channel_id=channel_b,
            payload={"body": "Workspace B private draft", "creative": True},
        )
        message_b = await repo_b.append_message(
            conversation_id=conversation_b["id"], role="user", content="Workspace B message"
        )
        run_b = await repo_b.create_run(
            conversation_id=conversation_b["id"],
            user_message_id=message_b["id"],
            requested_model="test",
        )

        assert await repo_a.get_conversation(conversation_b["id"]) is None
        assert await repo_a.get_draft(draft_b["id"]) is None
        assert await repo_a.get_research_bundle(
            conversation_id=conversation_b["id"], channel_id=channel_b
        ) is None
        assert await repo_a.get_run(run_b["id"]) is None
        assert await db_a.claim_collection_job(channel_b) is None

        session_plaintext = "m6-plaintext-telegram-session-never-return"
        cipher = SessionCipher("m6-external-encryption-key" * 2)
        await db_b.persist_telegram_session(
            label="m6-security",
            api_id=1,
            api_hash="m6-api-hash",
            session_string=session_plaintext,
            cipher=cipher,
        )
        encrypted_row = await db_b._execute(
            "SELECT encrypted_session FROM telegram_connections WHERE workspace_id=:workspace_id AND label=:label",
            {"label": "m6-security"},
        )
        encrypted = bytes(encrypted_row.scalar_one())
        assert session_plaintext.encode() not in encrypted
        assert cipher.decrypt(encrypted) == session_plaintext

        settings = Settings(
            api_id=1,
            api_hash="m6-api-hash",
            session_string="m6-environment-session",
            channels=f"@{slug_a}",
            admin_username=admin_a,
            admin_password="m6-test-password",
            session_secret="m6-http-session-secret",
            database_url=database_url,
            telegram_session_encryption_key="m6-external-encryption-key" * 2,
            local_workspace_slug=slug_a,
            studio_enabled=True,
            studio_test_mode=True,
        )
        app = create_app(Collector(db_a), settings)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            await _login(client, settings)
            responses = [
                await client.get(f"/studio/api/conversations/{conversation_b['id']}/messages"),
                await client.get(f"/studio/api/drafts/{draft_b['id']}"),
                await client.get(f"/studio/api/runs/{run_b['id']}/events"),
            ]
            assert [response.status_code for response in responses] == [404, 404, 404]
            serialized = "\n".join(response.text for response in responses)
            assert "Workspace B" not in serialized
            assert session_plaintext not in serialized
    finally:
        await db_a.close()
        await db_b.close()
