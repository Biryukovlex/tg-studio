"""Application-owned orchestration for the M4 research tools.

The service is intentionally small: one provider-neutral search adapter, one
safe source reader, deterministic provenance helpers, and an optional
workspace-scoped repository projection.  Normalized records are retained in a
bounded process cache for the current run and upserted into PostgreSQL when the
production repository is available, so reloads never depend on one process's
memory. Scope keys include workspace, conversation, and channel so a tool can
never reuse another conversation's source set accidentally.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Sequence

from .provenance import (
    _aware,
    _now,
    ResearchBundle,
    SourceEvidence,
    StoryCluster,
    build_research_bundle,
    cluster_stories,
    deduplicate_sources,
    select_sources,
    story_from_dict,
    to_source_evidence,
)
from .search import SearchProvider, SearchResponse, SearchCache, build_search_provider
from .sources import SafeSourceReader, SourceDocument


def _clean_instruction(value: str, limit: int = 2_000) -> str:
    return " ".join(str(value or "").split())[:limit].strip()


def _topic_names(value: Any) -> list[str]:
    if isinstance(value, str):
        return [_clean_instruction(value, 120)] if _clean_instruction(value, 120) else []
    if isinstance(value, dict):
        name = value.get("name") or value.get("topic") or value.get("label")
        return [_clean_instruction(name, 120)] if name else []
    return [name for item in (value or []) for name in _topic_names(item) if name]


def _scope(workspace_id: Any, conversation_id: Any, channel_id: int) -> str:
    return f"{workspace_id}:{conversation_id}:{int(channel_id)}"


def _source_for_agent(source: SourceEvidence) -> dict[str, Any]:
    """Keep model-facing provenance useful without returning the full archive."""

    value = source.model_dump(mode="json")
    if source.published_at and source.published_at > _now():
        value["warnings"] = [*value["warnings"], "Publication date is in the future; verify it before treating this as a current event."]
    return {
        key: value[key]
        for key in (
            "source_id",
            "url",
            "title",
            "publisher",
            "domain",
            "published_at",
            "retrieved_at",
            "accessible",
            "status",
            "quality_score",
            "quality_notes",
            "warnings",
            "injection_flags",
            "query",
        )
    } | {
        "excerpt": str(value.get("excerpt") or "")[:1_500],
        "search_relevance": dict((value.get("metadata") or {}).get("provenance") or {}),
        "source_role": str((value.get("metadata") or {}).get("source_role") or "editorial_or_official"),
    }


def _story_for_agent(story: StoryCluster) -> dict[str, Any]:
    value = story.model_dump(mode="json")
    return {
        key: value[key]
        for key in (
            "cluster_id",
            "headline",
            "summary",
            "source_ids",
            "primary_source_id",
            "supporting_source_ids",
            "score",
            "topic_relevance",
            "freshness",
            "source_quality",
            "novelty",
            "matched_topics",
            "conflict_flags",
            "warnings",
            "channel_evidence_ids",
            "published_at",
            "retrieved_at",
        )
    }


def _document_for_agent(document: SourceDocument) -> dict[str, Any]:
    value = document.model_dump(mode="json")
    return {
        key: value[key]
        for key in (
            "url",
            "final_url",
            "status",
            "accessible",
            "status_code",
            "title",
            "fetched_at",
            "source_id",
            "injection_flags",
            "warnings",
        )
    } | {"text": str(value.get("text") or value.get("excerpt") or "")[:3_500]}


@dataclass(slots=True)
class ResearchState:
    sources: dict[str, SourceEvidence] = field(default_factory=dict)
    query: str = ""
    bundle: ResearchBundle | None = None
    persistence_warning: str | None = None


class ResearchService:
    """Coordinate bounded search/read/compare operations for one app process."""

    def __init__(
        self,
        settings,
        *,
        provider: SearchProvider | None = None,
        reader: SafeSourceReader | None = None,
        cache: SearchCache | None = None,
        repository: Any | None = None,
        scorer: Any | None = None,
        max_sources: int = 12,
        max_queries: int | None = None,
        concurrency: int = 6,
    ) -> None:
        self.settings = settings
        self.provider = provider or build_search_provider(settings, cache=cache)
        self.reader = reader or SafeSourceReader(settings)
        self.repository = repository
        self.scorer = scorer
        self.max_sources = max(1, min(int(max_sources), 12))
        self.max_queries = max(1, min(int(max_queries if max_queries is not None else getattr(settings, "studio_search_max_queries", 12)), 12))
        self.concurrency = max(1, min(int(concurrency), 6))
        self._search_slots = asyncio.Semaphore(self.concurrency)
        self._states: dict[str, ResearchState] = {}
        self._lock = asyncio.Lock()

    def _state(self, workspace_id: Any, conversation_id: Any, channel_id: int) -> ResearchState:
        key = _scope(workspace_id, conversation_id, channel_id)
        return self._states.setdefault(key, ResearchState())

    async def _store(
        self,
        *,
        workspace_id: Any,
        conversation_id: Any,
        channel_id: int,
        query: str,
        values: Iterable[SourceEvidence],
        topics: Sequence[str] = (),
        recent_posts: Sequence[str] = (),
        channel_evidence_ids: Sequence[int] = (),
        channel_evidence: Sequence[dict[str, Any]] = (),
    ) -> ResearchBundle:
        async with self._lock:
            state = self._state(workspace_id, conversation_id, channel_id)
            merged = list({source.source_id: source for source in [*state.sources.values(), *values]}.values())
            state.sources = {source.source_id: source for source in merged}
            state.query = query or state.query
            state.bundle = build_research_bundle(
                merged,
                query=state.query,
                topics=topics,
                recent_posts=recent_posts,
                channel_evidence_ids=channel_evidence_ids,
                channel_evidence=channel_evidence,
                scorer=self.scorer,
            )
            bundle = state.bundle
        persist = getattr(self.repository, "persist_research_bundle", None)
        if persist is not None:
            try:
                await persist(
                    conversation_id=conversation_id,
                    channel_id=int(channel_id),
                    bundle=bundle.model_dump(mode="json"),
                )
            except Exception:  # noqa: BLE001 - research persistence must not stop a run
                async with self._lock:
                    state = self._state(workspace_id, conversation_id, channel_id)
                    state.persistence_warning = "Research results could not be persisted; this result is available for the current run only."
                    state.bundle = replace(bundle, warnings=tuple(dict.fromkeys([*bundle.warnings, state.persistence_warning])))
                    bundle = state.bundle
        return bundle

    async def _ensure_loaded(self, *, workspace_id: Any, conversation_id: Any, channel_id: int) -> ResearchState:
        state = self._state(workspace_id, conversation_id, channel_id)
        if state.bundle is not None:
            return state
        loader = getattr(self.repository, "get_research_bundle", None)
        if loader is None:
            return state
        try:
            raw = await loader(conversation_id=conversation_id, channel_id=int(channel_id))
        except Exception:  # noqa: BLE001 - a reload should fail closed to an empty bundle
            return state
        if not raw:
            return state
        sources = [to_source_evidence(item) for item in (raw.get("sources") or [])]
        stories = [story_from_dict(item) for item in (raw.get("stories") or []) if isinstance(item, dict)]
        state.sources = {source.source_id: source for source in sources}
        state.query = str(raw.get("query") or "")
        state.bundle = ResearchBundle(
            query=state.query,
            sources=tuple(sources),
            stories=tuple(stories),
            warnings=tuple(str(item) for item in (raw.get("warnings") or [])[:20]),
            retrieved_at=_aware(raw.get("retrieved_at")) or _now(),
            selected_source_ids=tuple(str(item) for item in (raw.get("selected_source_ids") or [])[:12]),
        )
        return state

    async def _record_activity(
        self,
        *,
        workspace_id: Any,
        conversation_id: Any,
        channel_id: int,
        event_type: str,
        provider: str,
        query_count: int,
        result_count: int,
        cache_hit: bool,
        degraded: bool,
        trace_id: str,
    ) -> dict[str, Any]:
        activity = {
            "event_type": str(event_type)[:80],
            "provider": str(provider)[:80],
            "query_count": max(0, int(query_count)),
            "result_count": max(0, int(result_count)),
            "cache_hit": bool(cache_hit),
            "degraded": bool(degraded),
            "trace_id": str(trace_id)[:128],
        }
        recorder = getattr(self.repository, "record_research_event", None)
        if recorder is not None:
            try:
                await recorder(conversation_id=conversation_id, channel_id=int(channel_id), event=activity)
                activity["persisted"] = True
            except Exception:  # noqa: BLE001 - activity is best effort and never leaks provider details
                activity["persisted"] = False
        else:
            activity["persisted"] = False
        return activity

    async def search(
        self,
        *,
        workspace_id: Any,
        conversation_id: Any,
        channel_id: int,
        query: str,
        alternate_queries: Sequence[str] = (),
        topics: Sequence[str] = (),
        recent_posts: Sequence[str] = (),
        channel_evidence_ids: Sequence[int] = (),
        channel_evidence: Sequence[dict[str, Any]] = (),
        language: str | None = None,
        recency_days: int | None = None,
        limit: int | None = None,
        categories: Sequence[str] = (),
        domains: Sequence[str] = (),
        engines: Sequence[str] = (),
        exclude_domains: Sequence[str] = (),
    ) -> dict[str, Any]:
        query = _clean_instruction(query, 500)
        if not query:
            raise ValueError("research query must not be empty")
        queries = list(
            dict.fromkeys(
                candidate
                for candidate in [query, *(_clean_instruction(item, 500) for item in alternate_queries)]
                if candidate
            )
        )[: self.max_queries]

        async def run(candidate: str) -> SearchResponse:
            async with self._search_slots:
                return await self.provider.search(
                    candidate,
                    language=language,
                    recency_days=recency_days,
                    limit=limit,
                    categories=tuple(categories),
                    domains=tuple(domains),
                    engines=tuple(engines),
                    excluded_domains=tuple(exclude_domains),
                )

        responses = await asyncio.gather(*(run(candidate) for candidate in queries))
        values = [to_source_evidence(result) for response in responses for result in response.results]
        combined_query = " | ".join(queries)
        bundle = await self._store(
            workspace_id=workspace_id,
            conversation_id=conversation_id,
            channel_id=channel_id,
            query=combined_query,
            values=values,
            topics=topics,
            recent_posts=recent_posts,
            channel_evidence_ids=channel_evidence_ids,
            channel_evidence=channel_evidence,
        )
        activity = await self._record_activity(
            workspace_id=workspace_id,
            conversation_id=conversation_id,
            channel_id=channel_id,
            event_type="search",
            provider=",".join(sorted({response.provider for response in responses})),
            query_count=len(queries),
            result_count=sum(len(response.results) for response in responses),
            cache_hit=all(response.cache_hit for response in responses),
            degraded=any(response.degraded for response in responses),
            trace_id=responses[0].trace_id,
        )
        # Preserve the provider/cache/degraded activity fields while attaching
        # the deterministic clusters and source links to the tool result.
        # Return this batch, not a ranked subset of the conversation archive.
        # The model decides relevance, credibility and domain diversity.
        selected = list({source.source_id: source for source in values}.values())
        selected_ids = {source.source_id for source in selected}
        stories = [story for story in bundle.stories if selected_ids.intersection(story.source_ids)][:8]
        response_bundle = {
            "query": query,
            "queries": queries,
            "strategy": {
                "language": language,
                "recency_days": recency_days,
                "categories": list(categories),
                "domains": list(domains),
                "engines": list(engines),
                "excluded_domains": list(exclude_domains),
            },
            "sources": [_source_for_agent(source) for source in selected],
            "stories": [_story_for_agent(story) for story in stories],
            "selected_source_ids": [source.source_id for source in selected],
            "retrieved_at": bundle.model_dump(mode="json")["retrieved_at"],
        }
        response_bundle["provider"] = ",".join(sorted({response.provider for response in responses}))
        response_bundle["cache_hit"] = all(response.cache_hit for response in responses)
        response_bundle["degraded"] = any(response.degraded for response in responses)
        response_bundle["trace_id"] = responses[0].trace_id
        response_bundle["activity"] = activity
        response_bundle["warnings"] = list(dict.fromkeys([*(warning for response in responses for warning in response.warnings), *bundle.warnings]))
        return response_bundle  # type: ignore[return-value]

    async def read_sources(
        self,
        *,
        workspace_id: Any,
        conversation_id: Any,
        channel_id: int,
        urls: Sequence[str],
        query: str = "",
        topics: Sequence[str] = (),
        recent_posts: Sequence[str] = (),
        channel_evidence_ids: Sequence[int] = (),
        channel_evidence: Sequence[dict[str, Any]] = (),
    ) -> dict[str, Any]:
        unique_urls = list(dict.fromkeys(str(url).strip() for url in urls if str(url).strip()))[: min(self.max_sources, 6)]
        semaphore = asyncio.Semaphore(self.concurrency)

        async def read_one(url: str) -> SourceDocument:
            async with semaphore:
                return await self.reader.read(url)

        documents = await asyncio.gather(*(read_one(url) for url in unique_urls))
        prior_state = await self._ensure_loaded(
            workspace_id=workspace_id,
            conversation_id=conversation_id,
            channel_id=channel_id,
        )
        values: list[SourceEvidence] = []
        for document in documents:
            value = to_source_evidence(document)
            prior = next(
                (
                    source
                    for source in prior_state.sources.values()
                    if source.canonical_url and source.canonical_url == value.canonical_url
                ),
                None,
            )
            if prior is not None:
                value = replace(
                    value,
                    source_id=prior.source_id,
                    published_at=value.published_at or prior.published_at,
                    provider=value.provider or prior.provider,
                    query=value.query or prior.query,
                )
            values.append(value)
        bundle = await self._store(
            workspace_id=workspace_id,
            conversation_id=conversation_id,
            channel_id=channel_id,
            query=_clean_instruction(query, 500),
            values=values,
            topics=topics,
            recent_posts=recent_posts,
            channel_evidence_ids=channel_evidence_ids,
            channel_evidence=channel_evidence,
        )
        activity = await self._record_activity(
            workspace_id=workspace_id,
            conversation_id=conversation_id,
            channel_id=channel_id,
            event_type="read_sources",
            provider=next((source.provider for source in values if source.provider), "source_reader"),
            query_count=0,
            result_count=len(documents),
            cache_hit=False,
            degraded=any(not document.accessible for document in documents),
            trace_id=str(uuid.uuid4()),
        )
        selected = values
        selected_ids = {source.source_id for source in selected}
        stories = [story for story in bundle.stories if selected_ids.intersection(story.source_ids)][:8]
        return {
            "documents": [_document_for_agent(document) for document in documents],
            "sources": [_source_for_agent(source) for source in selected],
            "stories": [_story_for_agent(story) for story in stories],
            "warnings": list(bundle.warnings),
            "selected_source_ids": [source.source_id for source in selected],
            "retrieved_at": bundle.model_dump(mode="json")["retrieved_at"],
            "activity": activity,
        }

    async def compare_sources(
        self,
        *,
        workspace_id: Any,
        conversation_id: Any,
        channel_id: int,
        source_ids: Sequence[str] = (),
        urls: Sequence[str] = (),
        query: str = "",
        topics: Sequence[str] = (),
        recent_posts: Sequence[str] = (),
        channel_evidence_ids: Sequence[int] = (),
        channel_evidence: Sequence[dict[str, Any]] = (),
    ) -> dict[str, Any]:
        state = await self._ensure_loaded(workspace_id=workspace_id, conversation_id=conversation_id, channel_id=channel_id)
        wanted = set(str(item) for item in source_ids if str(item))
        wanted_urls = set(str(item) for item in urls if str(item))
        selected = [
            source
            for source in state.sources.values()
            if (not wanted and not wanted_urls) or source.source_id in wanted or source.url in wanted_urls or source.canonical_url in wanted_urls
        ][: min(self.max_sources, 8)]
        if not selected:
            return {"sources": [], "stories": [], "agreements": [], "conflicts": [], "missing_support": ["No stored sources match this request."], "warnings": ["Read or search sources before comparing them."]}
        bundle = await self._store(
            workspace_id=workspace_id,
            conversation_id=conversation_id,
            channel_id=channel_id,
            query=_clean_instruction(query, 500) or state.query,
            values=selected,
            topics=topics,
            recent_posts=recent_posts,
            channel_evidence_ids=channel_evidence_ids,
            channel_evidence=channel_evidence,
        )
        stories = cluster_stories(
            selected,
            query=query or state.query,
            topics=topics,
            recent_posts=recent_posts,
            channel_evidence_ids=channel_evidence_ids,
            channel_evidence=channel_evidence,
            scorer=self.scorer,
        )
        conflicts = [flag for story in stories for flag in story.conflict_flags if flag not in {"single_source", "undated_source"}]
        common_terms = self._common_terms(selected)
        missing = []
        if any(source.published_at is None for source in selected):
            missing.append("publication_date")
        if any(not source.excerpt for source in selected):
            missing.append("bounded_excerpt")
        return {
            "sources": [_source_for_agent(source) for source in selected],
            "stories": [_story_for_agent(story) for story in stories[:8]],
            "agreements": common_terms,
            "conflicts": list(dict.fromkeys(conflicts)),
            "missing_support": missing,
            "warnings": list(bundle.warnings),
            "selected_source_ids": list(bundle.selected_source_ids),
            "retrieved_at": bundle.model_dump(mode="json")["retrieved_at"],
        }

    @staticmethod
    def _common_terms(sources: Sequence[SourceEvidence]) -> list[str]:
        token_sets = []
        for source in sources:
            token_sets.append({token.lower() for token in re.findall(r"[\w\u0400-\u04ff]{3,}", f"{source.title} {source.excerpt}")})
        if not token_sets:
            return []
        return sorted(set.intersection(*token_sets))[:20]

    async def find_novel_topics(
        self,
        *,
        workspace_id: Any,
        conversation_id: Any,
        channel_id: int,
        topics: Sequence[str] = (),
        instruction: str = "",
        recent_posts: Sequence[str] = (),
        channel_evidence_ids: Sequence[int] = (),
        channel_evidence: Sequence[dict[str, Any]] = (),
        categories: Sequence[str] = (),
        domains: Sequence[str] = (),
    ) -> dict[str, Any]:
        requested = _clean_instruction(instruction, 500)
        base_topics = [topic for topic in _topic_names(topics) if topic]
        queries: list[str] = [requested] if requested else []
        queries.extend(base_topics)
        if not queries:
            queries.append("emerging topics and stories")
        queries = list(dict.fromkeys(queries))[: self.max_queries]

        async def run(query: str):
            async with self._search_slots:
                return await self.provider.search(
                    query,
                    limit=getattr(self.settings, "studio_search_max_results", 10),
                    categories=tuple(categories),
                    domains=tuple(domains),
                )

        responses = await asyncio.gather(*(run(query) for query in queries))
        values = [to_source_evidence(result) for response in responses for result in response.results]
        combined_query = " | ".join(queries)
        bundle = await self._store(
            workspace_id=workspace_id,
            conversation_id=conversation_id,
            channel_id=channel_id,
            query=combined_query,
            values=values,
            topics=base_topics,
            recent_posts=recent_posts,
            channel_evidence_ids=channel_evidence_ids,
            channel_evidence=channel_evidence,
        )
        selected = list({source.source_id: source for source in values}.values())
        selected_ids = {source.source_id for source in selected}
        selected_stories = [story for story in bundle.stories if selected_ids.intersection(story.source_ids)][:6]
        suggestions = []
        for story in selected_stories:
            suggestions.append(
                {
                    **_story_for_agent(story),
                    "reason": f"Fits the inferred channel topics {', '.join(base_topics[:3]) or 'from the request'}; compare its linked sources before drafting.",
                }
            )
        warnings = list(bundle.warnings)
        warnings.extend(warning for response in responses for warning in response.warnings)
        activities = []
        for response in responses:
            activities.append(await self._record_activity(
                workspace_id=workspace_id,
                conversation_id=conversation_id,
                channel_id=channel_id,
                event_type="find_novel_topics",
                provider=response.provider,
                query_count=1,
                result_count=len(response.results),
                cache_hit=response.cache_hit,
                degraded=response.degraded,
                trace_id=response.trace_id,
            ))
        return {
            "queries": queries,
            "suggestions": suggestions,
            "sources": [_source_for_agent(source) for source in selected],
            "warnings": list(dict.fromkeys(warnings)),
            "degraded": any(response.degraded for response in responses),
            "providers": sorted({response.provider for response in responses}),
            "activity": activities,
        }

    async def get_source(self, *, workspace_id: Any, conversation_id: Any, channel_id: int, source_id: str) -> SourceEvidence | None:
        state = await self._ensure_loaded(workspace_id=workspace_id, conversation_id=conversation_id, channel_id=channel_id)
        return state.sources.get(str(source_id))

    async def get_bundle(self, *, workspace_id: Any, conversation_id: Any, channel_id: int) -> ResearchBundle | None:
        state = await self._ensure_loaded(workspace_id=workspace_id, conversation_id=conversation_id, channel_id=channel_id)
        return state.bundle
