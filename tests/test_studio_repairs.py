import re
import uuid

import pytest
from app.studio.drafts import DraftValidationError, validate_draft_input
from app.studio.semantic_profile import SemanticProfile, SemanticTopic, apply_semantics
from app.studio.profile import _style
from app.studio.repository import MemoryStudioRepository
from tests.test_studio_profile import _analytics


def test_semantic_topics_have_server_scores_and_exact_evidence():
    analytics = _analytics()
    ids = [p.post_id for p in analytics.top_posts[:2]]
    result = SemanticProfile(topics=[SemanticTopic(name="Технологии прогнозирования", scope="Прогнозы", post_ids=ids)])
    profile, analysis = apply_semantics(analytics, [], result, model="test")
    assert [t.name for t in profile.topics] == ["Технологии прогнозирования"]
    assert profile.topics[0].sample_size == 2
    assert profile.topics[0].representative_post_ids == ids
    assert profile.editorial_rules["extraction_version"] == "channel.semantic.v1"
    assert analysis.provider == "openrouter"
    result.topics[0].post_ids = [999999]
    with pytest.raises(ValueError):
        apply_semantics(analytics, [], result, model="test")


def test_cyrillic_is_not_emoji_and_style_uses_full_paragraphs():
    post = _analytics().top_posts[0].model_copy(update={"excerpt": "Привет мир.\n\nВторой абзац. " * 40})
    style = _style([post], confidence="low", limitations=[])
    assert style.emoji_rate == 0
    assert style.typical_length_chars > 600
    assert style.paragraph_count > 1


def test_invalid_draft_reports_correctable_fields_without_echoing_input():
    with pytest.raises(DraftValidationError) as error:
        validate_draft_input({"body": "test", "claim_support": [{"statement": "secret content"}]})
    assert "claim_support.0.claim" in str(error.value)
    assert "secret content" not in str(error.value)


@pytest.mark.asyncio
async def test_auto_title_does_not_overwrite_user_title():
    repo = MemoryStudioRepository()
    c = await repo.create_conversation(channel_id=1)
    await repo.rename_conversation(c["id"], title="Первый запрос", only_default=True)
    assert (await repo.get_conversation(c["id"]))["title"] == "Первый запрос"
    await repo.rename_conversation(c["id"], title="Мой заголовок")
    await repo.rename_conversation(c["id"], title="Следующий запрос", only_default=True)
    assert (await repo.get_conversation(c["id"]))["title"] == "Мой заголовок"


@pytest.mark.asyncio
async def test_rename_endpoint_validation_and_csrf(client, settings, app):
    settings.studio_test_mode = True
    await client.post("/login", data={"username": settings.admin_username, "password": settings.admin_password})
    home = await client.get("/studio")
    token = re.search(r'<meta name="studio-csrf-token" content="([^"]+)"', home.text).group(1)
    repo = app.state.studio_repository
    c = await repo.create_conversation(channel_id=1)
    url = f'/studio/api/conversations/{c["id"]}'
    assert (await client.patch(url, json={"title": "Name"})).status_code == 403
    headers = {"x-csrf-token": token}
    for title in [" ", "x" * 161, None, 42]:
        assert (await client.patch(url, json={"title": title}, headers=headers)).status_code == 422
    response = await client.patch(url, json={"title": "Новая тема"}, headers=headers)
    assert response.json()["conversation"]["title"] == "Новая тема"
    assert (await client.patch(f'/studio/api/conversations/{uuid.uuid4()}', json={"title": "Name"}, headers=headers)).status_code == 404
