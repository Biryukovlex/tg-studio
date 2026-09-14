from __future__ import annotations

import asyncio

import pytest
from pydantic_ai.models.test import TestModel

from app.config import Settings
from app.studio.agent import StudioDeps, build_agent
from app.studio.provenance import SourceEvidence
from app.studio.repository import MemoryStudioRepository
from app.studio.research import ResearchService


class DraftingTestModel(TestModel):
    def __init__(self, *, tool_args: dict[str, dict], **kwargs):
        super().__init__(**kwargs)
        self.tool_args = tool_args

    def gen_tool_args(self, tool_def):
        if tool_def.name in self.tool_args:
            return self.tool_args[tool_def.name]
        return super().gen_tool_args(tool_def)


def _deps(repository: MemoryStudioRepository, conversation_id, *, required_tools=()):
    return StudioDeps(
        repository=repository,
        workspace_id=repository.workspace_id,
        conversation_id=conversation_id,
        channel_id=1,
        cancel_event=asyncio.Event(),
        required_tools=required_tools,
    )


async def _bundle(repository: MemoryStudioRepository, conversation_id, *, source_ids=("s1",)):
    await repository.persist_research_bundle(
        conversation_id=conversation_id,
        channel_id=1,
        bundle={"sources": [{"source_id": source_id, "url": f"https://example.test/{source_id}"} for source_id in source_ids], "stories": []},
    )


@pytest.mark.asyncio
async def test_revision_empty_source_list_inherits_existing_evidence():
    repository = MemoryStudioRepository()
    conversation = await repository.create_conversation(channel_id=1)
    await _bundle(repository, conversation["id"])
    draft = await repository.create_draft(
        conversation_id=conversation["id"],
        channel_id=1,
        payload={"body": "First version", "source_ids": ["s1"]},
    )
    model = DraftingTestModel(
        call_tools=["get_draft", "revise_draft"],
        custom_output_text="Revision saved.",
        tool_args={"revise_draft": {"draft_id": draft["id"], "body": "Revised version", "source_ids": []}},
    )
    result = await build_agent(Settings(studio_test_mode=True), model=model).run(
        "Make this draft shorter.",
        deps=_deps(repository, conversation["id"], required_tools=("get_draft", "revise_draft")),
    )
    current = await repository.get_current_draft(conversation_id=conversation["id"], channel_id=1)
    assert result.output == "Revision saved."
    assert current is not None
    assert current["source_ids"] == ["s1"]
    assert current["body"] == "Revised version"


@pytest.mark.asyncio
async def test_partial_unknown_ids_are_salvaged_and_all_unknown_ids_block():
    repository = MemoryStudioRepository()
    conversation = await repository.create_conversation(channel_id=1)
    await _bundle(repository, conversation["id"])

    partial_model = DraftingTestModel(
        call_tools=["get_channel_context", "create_draft", "create_draft"],
        custom_output_text="Saved with the known source.",
        tool_args={"create_draft": {"body": "Known evidence", "source_ids": ["s1", "bogus"]}},
    )
    result = await build_agent(Settings(studio_test_mode=True), model=partial_model).run(
        "Write a post about this.",
        deps=_deps(repository, conversation["id"], required_tools=("get_channel_context", "create_draft")),
    )
    draft = await repository.get_current_draft(conversation_id=conversation["id"], channel_id=1)
    assert result.output == "Saved with the known source."
    assert draft is not None
    assert draft["source_ids"] == ["s1"]
    assert any("bogus" in warning for warning in draft["warnings"])

    blocked_model = DraftingTestModel(
        call_tools=["get_channel_context", "create_draft", "create_draft"],
        custom_output_text="The draft is blocked because bogus is not a known source ID.",
        tool_args={"create_draft": {"body": "No evidence", "source_ids": ["bogus"]}},
    )
    blocked = await build_agent(Settings(studio_test_mode=True), model=blocked_model).run(
        "Write another post.",
        deps=_deps(repository, conversation["id"], required_tools=("get_channel_context", "create_draft")),
    )
    assert "bogus" in blocked.output
    assert await repository.get_current_draft(conversation_id=conversation["id"], channel_id=1) == draft


@pytest.mark.asyncio
async def test_factual_draft_without_research_uses_channel_context_warning():
    repository = MemoryStudioRepository()
    conversation = await repository.create_conversation(channel_id=1)
    model = DraftingTestModel(
        call_tools=["get_channel_context", "create_draft"],
        custom_output_text="Saved from channel context.",
        tool_args={"create_draft": {"body": "A channel-context post", "source_ids": []}},
    )
    result = await build_agent(Settings(studio_test_mode=True), model=model).run(
        "Write a post about our channel.",
        deps=_deps(repository, conversation["id"], required_tools=("get_channel_context", "create_draft")),
    )
    draft = await repository.get_current_draft(conversation_id=conversation["id"], channel_id=1)
    assert result.output == "Saved from channel context."
    assert draft is not None
    assert draft["source_ids"] == []
    assert draft["channel_evidence"]
    assert "No web sources were used; the post is based on channel context only." in draft["warnings"]


@pytest.mark.asyncio
async def test_bundle_without_ids_returns_reason_after_two_blocked_attempts():
    repository = MemoryStudioRepository()
    conversation = await repository.create_conversation(channel_id=1)
    await _bundle(repository, conversation["id"])
    model = DraftingTestModel(
        call_tools=["get_channel_context", "create_draft", "create_draft"],
        custom_output_text="The draft is blocked because a research source ID is required.",
        tool_args={"create_draft": {"body": "Missing web evidence", "source_ids": []}},
    )
    result = await build_agent(Settings(studio_test_mode=True), model=model).run(
        "Write a sourced post.",
        deps=_deps(repository, conversation["id"], required_tools=("get_channel_context", "create_draft")),
    )
    assert "blocked" in result.output.lower()
    assert "invalid response" not in result.output.lower()


@pytest.mark.asyncio
async def test_loaded_research_merges_sources_stored_during_the_read():
    started = asyncio.Event()
    release = asyncio.Event()

    class Repository:
        async def get_research_bundle(self, *, conversation_id, channel_id):
            started.set()
            await release.wait()
            return {"sources": [{"source_id": "old", "url": "https://example.test/old"}], "stories": []}

    service = ResearchService(Settings(studio_test_mode=True), repository=Repository())
    load = asyncio.create_task(service._ensure_loaded(workspace_id="w", conversation_id="c", channel_id=1))
    await started.wait()
    new = SourceEvidence(source_id="new", url="https://example.test/new", canonical_url="https://example.test/new")
    await service._store(workspace_id="w", conversation_id="c", channel_id=1, query="new", values=[new])
    release.set()
    state = await load
    assert set(state.sources) == {"old", "new"}
