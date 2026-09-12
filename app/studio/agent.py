"""Bounded PydanticAI agent with channel intelligence and M4 research tools."""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic_ai import Agent, ModelRetry, RunContext

from .. import limits
from .model import build_model
from .prompts import SYSTEM_INSTRUCTIONS
from .analytics import analyze_posts
from .context import ContextAssembler, ContextPack
from .profile import (
    ChannelProfile,
    apply_confirmed_topic_change,
    build_profile,
    propose_topic_change,
)
from .drafts import ClaimSupport, DraftConflictError, DraftValidationError, diff_summary
from .schemas import ChannelContext
from .research import ResearchService


class StudioContextReader(Protocol):
    async def channel_context(self, channel_id: int) -> dict[str, Any]: ...

    async def performance_rows(self, channel_id: int) -> list[dict[str, Any]]: ...


@dataclass(slots=True)
class StudioDeps:
    """Authorized resources for one conversation run.

    ``workspace_id``, ``conversation_id`` and ``channel_id`` are populated by
    the server after it validates the persisted conversation. They are never
    accepted from model tool arguments.
    """

    repository: StudioContextReader
    workspace_id: Any
    conversation_id: Any
    channel_id: int
    cancel_event: asyncio.Event
    # The server may provide one scoped service per application process. When
    # absent (for direct unit tests), the agent creates a degraded, no-network
    # service from server settings; browser input never controls this object.
    research: ResearchService | None = None
    required_tools: tuple[str, ...] = ()
    completed_tools: set[str] = field(default_factory=set)
    # Terse follow-ups should advance through the evidence workflow exactly
    # once. This prevents a provider that ignores named tool_choice from
    # repeatedly calling the previous search tool instead of reading results.
    strict_workflow: bool = False
    tool_call_counts: dict[str, int] = field(default_factory=dict)


_RESEARCH_INTENT = re.compile(
    r"\b(search|research|web|news|source|sources|current|latest|fresh|story|stories|"
    r"найди|найти|поищ|поиск|ищем|исслед|новост|источник|свеж|актуальн|истори|"
    r"инет|интернет|онлайн|сайт)\w*\b",
    re.IGNORECASE,
)
_CONTINUATION_INTENT = re.compile(
    r"\b(again|continue|more|next|retry|repeat|searching|еще|ещё|дальше|"
    r"продолж|повтор|снова|добавь|попробуй)\w*\b",
    re.IGNORECASE,
)
_CROSS_CHECK_INTENT = re.compile(
    r"\b(cross[- ]?check|verify|compare|corroborat|проверь|сравн|подтверд)\w*\b",
    re.IGNORECASE,
)
_DRAFT_INTENT = re.compile(
    r"\b(draft|write|prepare|create|telegram-ready|драфт|черновик|напиши|подготов|создай|сделай)\w*\b",
    re.IGNORECASE,
)
_PERFORMANCE_INTENT = re.compile(
    r"\b(analy[sz]|perform|traction|best post|metrics?|проанализ|эффектив|лучши|метрик|вовлеч)\w*\b",
    re.IGNORECASE,
)


_REVISION_INTENT = re.compile(
    r"\b(add|remove|delete|drop|change|replace|rewrite|rephrase|reword|shorten|expand|"
    r"tighten|soften|fix|edit|update|adjust|tweak|make\s+it|bold|italic|title|subtitle|"
    r"headline|paragraph|emoji|signature|link|tone|shorter|longer|"
    r"добав|убер|удали|измени|замени|перепиши|перефраз|сократи|расшир|исправ|"
    r"отредакт|обнови|подправ|сделай|жирн|курсив|заголов|подзаголов|абзац|эмодзи|подпис|"
    r"ссылк|тон|короче|длиннее)\w*\b",
    re.IGNORECASE,
)


def is_revision_request(content: str) -> bool:
    """Return whether a message asks to change the existing post.

    When a draft exists in the conversation, requests like "add bold title and
    subtitles" or "make it shorter" must produce a saved revision, not a chat
    reply describing the change.
    """

    text = " ".join(str(content or "").split())
    return bool(_REVISION_INTENT.search(text))


def is_short_continuation_request(content: str) -> bool:
    """Return whether a message is a terse request to continue prior work.

    Continuations need a fresh tool pass, but sending the entire transcript to
    a small/free provider makes it easy for the model to repeat an old answer.
    The server therefore uses this predicate to provide a compact history and
    an explicit workflow reminder while retaining the user's exact message in
    the persisted conversation.
    """

    text = " ".join(str(content or "").split())
    return bool(_CONTINUATION_INTENT.search(text)) and len(text.split()) <= 12


def workflow_tool_sequence(content: str, *, history: list[dict[str, Any]] | tuple[dict[str, Any], ...] = ()) -> tuple[str, ...]:
    """Choose only the mandatory evidence workflow for an explicit user intent.

    PydanticAI still owns the agent loop. This sequence merely prevents a model
    from answering a research/draft request without the application-owned tools
    that make its claims auditable.
    """

    text = " ".join(str(content or "").split())[:32_000]
    # Short follow-ups such as "Ищем еще" are common in an agent chat. Infer
    # their workflow from the user's recent requests so the model cannot
    # answer from stale conversation text without re-running evidence tools.
    research_declined = bool(re.search(r"\b(?:без\s+(?:(?:нового|повторного)\s+)?поиск\w*|не\s+(?:ищи|искать)|no\s+(?:new\s+)?search|without\s+(?:a\s+)?(?:new\s+)?search|do\s+not\s+search)\b", text, re.I))
    wants_research = bool(_RESEARCH_INTENT.search(text)) and not research_declined
    draft_declined = bool(re.search(r"\b(?:без\s+(?:черновик\w*|поста)|не\s+(?:пиши|создавай|готовь|делай)\s+(?:пост\w*|черновик\w*)|no\s+draft|without\s+(?:a\s+)?draft|do\s+not\s+(?:write|create)\s+(?:a\s+)?(?:post|draft))\b", text, re.I))
    wants_draft = bool(_DRAFT_INTENT.search(text)) and not draft_declined
    wants_cross_check = bool(_CROSS_CHECK_INTENT.search(text))
    if wants_draft and wants_research:
        tools = ["get_channel_context", "search_web"]
        if wants_cross_check:
            tools.append("compare_sources")
        tools.append("create_draft")
        return tuple(tools)
    if wants_draft:
        return ("get_channel_context", "create_draft")
    if wants_research:
        tools = ["get_channel_context", "search_web"]
        if wants_cross_check:
            tools.append("compare_sources")
        return tuple(tools)
    if _PERFORMANCE_INTENT.search(text):
        return ("get_performance_evidence",)
    if _CONTINUATION_INTENT.search(text):
        # Prefer the most recent explicit user intent. A draft continuation
        # should remain a draft task; otherwise inherit research or metrics.
        for row in reversed(list(history or [])):
            if not isinstance(row, dict) or row.get("role") != "user":
                continue
            previous = " ".join(str(row.get("content") or "").split())
            if not previous or _CONTINUATION_INTENT.search(previous) and len(previous.split()) <= 12:
                continue
            inherited = workflow_tool_sequence(previous)
            if inherited:
                return inherited
    return ()


def _check_cancel(ctx: RunContext[StudioDeps]) -> None:
    if ctx.deps.cancel_event.is_set():
        raise asyncio.CancelledError


_SERVICE_COMMENTARY_PATTERN = re.compile(
    r"(?:основной\s+первоисточник\s*[—:-]|не\s+открылись\s+у\s+меня|"
    r"данные\s+по\s+ним\s+взяты|без\s+полного\s+чтения|"
    r"(?:я|мне)\s+не\s+(?:смог\w*|удалось)\s+(?:открыть|прочитать)|"
    r"I\s+(?:could\s+not|couldn't|was\s+unable\s+to)\s+(?:access|read|open)|"
    r"(?:source|research)\s+(?:review|limitations)\s*:)",
    re.I,
)


def _require_publication_text(body: str) -> None:
    """Reject obvious service commentary that remains after safe cleanup."""
    if _SERVICE_COMMENTARY_PATTERN.search(body):
        raise ModelRetry("Remove service/research-process commentary from the post body. Put limitations in warnings or the chat, and retry with publication text only.")


_SOURCE_LIST_HEADER = re.compile(
    r"^\s*(?:\*\*|__)?\s*(?:sources?|references?|links?|источники?|ссылки|материалы|подробнее)\s*(?:\*\*|__)?\s*[:：—–-]?\s*$",
    re.I,
)
_SOURCE_LIST_LINE = re.compile(r"https?://|\]\(https?://", re.I)


def _strip_trailing_source_list(body: str) -> tuple[str, bool]:
    """Remove a trailing "Sources:" block; sources live on the artifact, not in the post.

    A block is removed only when it sits at the end, opens with a header such
    as "Sources:" or "Источники:", and every following line carries a link.
    Inline links inside the prose are untouched.
    """

    paragraphs = re.split(r"\n\s*\n", str(body or "").strip())
    removed = False
    while paragraphs:
        lines = [line for line in paragraphs[-1].splitlines() if line.strip()]
        if not lines:
            paragraphs.pop()
            continue
        header, rest = lines[0], lines[1:]
        header_only = _SOURCE_LIST_HEADER.match(header)
        inline_header = re.match(r"^\s*(?:\*\*|__)?\s*(?:sources?|references?|links?|источники?|ссылки)\s*(?:\*\*|__)?\s*[:：—–-]\s*\S", header, re.I)
        is_list = bool(
            (header_only and rest and all(_SOURCE_LIST_LINE.search(line) for line in rest))
            or (inline_header and _SOURCE_LIST_LINE.search(header) and all(_SOURCE_LIST_LINE.search(line) for line in rest))
        )
        if not is_list:
            break
        paragraphs.pop()
        removed = True
    return "\n\n".join(paragraphs).strip(), removed


def _clean_publication_text(body: str) -> tuple[str, bool]:
    """Drop service-note paragraphs and a trailing source list without rewriting prose."""

    parts = re.split(r"(\n\s*\n)", str(body or ""))
    kept: list[str] = []
    removed = False
    for part in parts:
        if part.strip() and _SERVICE_COMMENTARY_PATTERN.search(part):
            removed = True
            continue
        if kept or part.strip():
            kept.append(part)
    cleaned, removed_sources = _strip_trailing_source_list("".join(kept).strip())
    return cleaned, removed or removed_sources


def _require_predecessors(ctx: RunContext[StudioDeps], tool_name: str) -> None:
    """Evidence is checked at completion; independent tools may run in parallel."""
    _check_cancel(ctx)


def _research(ctx: RunContext[StudioDeps], settings) -> ResearchService:
    service = ctx.deps.research
    if service is None:
        service = ResearchService(settings, repository=ctx.deps.repository)
        ctx.deps.research = service
    return service


def _draft_error(exc: BaseException) -> dict[str, Any]:
    if isinstance(exc, (DraftValidationError, DraftConflictError)):
        result: dict[str, Any] = {
            "status": "blocked",
            "error": {"code": getattr(exc, "code", "draft_error"), "message": str(exc)},
        }
        if isinstance(exc, DraftConflictError):
            result["draft"] = exc.current
            result["expected_revision"] = exc.expected_revision
        return result
    return {"status": "blocked", "error": {"code": "draft_unavailable", "message": "The draft could not be saved."}}


def _draft_summary(row: dict[str, Any]) -> dict[str, Any]:
    """Small explainability envelope; never expose hidden model reasoning."""

    channel_evidence = []
    for item in list(row.get("channel_evidence") or [])[:6]:
        if not isinstance(item, dict):
            continue
        channel_evidence.append(
            {
                key: item[key]
                for key in ("claim", "post_id", "message_id", "link", "metrics", "scores", "confidence")
                if key in item
            }
        )
    web_evidence = []
    for item in list(row.get("web_evidence") or [])[:6]:
        if not isinstance(item, dict):
            continue
        web_evidence.append(
            {
                key: item[key]
                for key in ("source_id", "cluster_id", "headline", "title", "url", "publisher", "published_at", "summary", "warnings")
                if key in item
            }
        )
    return {
        "channel_evidence": channel_evidence,
        "web_evidence": web_evidence,
        "source_ids": list(row.get("source_ids") or [])[:12],
        "assumptions": list(row.get("assumptions") or [])[:8],
        "warnings": list(row.get("warnings") or [])[:12],
        "confidence": row.get("confidence", "low"),
    }


async def _research_context(ctx: RunContext[StudioDeps]) -> tuple[list[str], list[str], list[int], list[dict[str, Any]]]:
    """Collect only compact server-owned topic/text signals for ranking."""

    raw = await ctx.deps.repository.channel_context(ctx.deps.channel_id)
    topics: list[str] = []
    getter = getattr(ctx.deps.repository, "get_profile", None)
    if getter is not None:
        profile = await getter(ctx.deps.channel_id)
        if profile:
            for item in profile.get("topics", []) or []:
                if isinstance(item, str):
                    topics.append(item)
                elif isinstance(item, dict) and item.get("name"):
                    topics.append(str(item["name"]))
    recent_posts = [str(item.get("text") or "")[:600] for item in raw.get("recent_posts", []) if isinstance(item, dict) and item.get("text")]
    evidence_ids: list[int] = []
    channel_evidence: list[dict[str, Any]] = []
    reader = getattr(ctx.deps.repository, "performance_rows", None)
    if reader is not None:
        rows = await reader(ctx.deps.channel_id)
        if rows:
            analysis = analyze_posts(rows, ctx.deps.channel_id, identifier=raw.get("identifier"))
            evidence_ids = [post.post_id for post in analysis.evidence_posts]
            channel_evidence = [post.model_dump(mode="json") for post in analysis.evidence_posts[:20]]
    return topics[:20], recent_posts[:6], evidence_ids[:20], channel_evidence[:20]


async def _draft_context(ctx: RunContext[StudioDeps]) -> dict[str, Any] | None:
    """Build a bounded history-aware draft envelope for the model."""

    getter = getattr(ctx.deps.repository, "get_current_draft", None)
    if getter is None:
        return None
    current = await getter(conversation_id=ctx.deps.conversation_id, channel_id=ctx.deps.channel_id)
    if not current:
        return None
    versions_getter = getattr(ctx.deps.repository, "list_draft_versions", None)
    versions = (
        await versions_getter(
            draft_id=current["id"],
            conversation_id=ctx.deps.conversation_id,
            channel_id=ctx.deps.channel_id,
        )
        if versions_getter is not None
        else []
    )
    versions = list(versions or [])
    previous = next((item for item in reversed(versions) if int(item.get("version", 0)) < int(current.get("current_version", 0))), None)
    return {
        "draft_id": str(current["id"]),
        "working_title": str(current.get("working_title") or "Untitled draft")[:240],
        "body": str(current.get("body") or "")[:4_096],
        "revision": int(current.get("revision", 1)),
        "current_version": int(current.get("current_version", 1)),
        "current_version_origin": str(current.get("current_version_origin") or "generated"),
        "version_history": [
            {
                "version": int(item.get("version", 0)),
                "origin": str(item.get("origin") or "generated"),
                "character_count": int(item.get("character_count", len(str(item.get("body") or "")))),
                "instruction": str(item.get("instruction") or "")[:240],
            }
            for item in versions[-6:]
        ],
        "latest_edit_summary": diff_summary(str(previous.get("body") or "") if previous else "", str(current.get("body") or "")) if previous else "No prior version; this is the first draft.",
    }


def build_agent(settings, *, model=None) -> Agent[StudioDeps, str]:
    """Create the single bounded agent used by the production Studio route."""

    def workflow_model_settings(ctx: RunContext[StudioDeps]) -> dict[str, Any]:
        missing = set(ctx.deps.required_tools) - ctx.deps.completed_tools
        artifact_ready = bool(
            {"create_draft", "revise_draft", "save_draft"}.intersection(ctx.deps.completed_tools)
        )
        return {
            # Once a requested artifact is durable, reserve the next provider
            # turn for the short chat acknowledgement. Smaller tool-calling
            # models otherwise tend to search and revise the same artifact
            # again until the request budget is exhausted.
            "tool_choice": "required" if missing else ("none" if artifact_ready else "auto"),
            "parallel_tool_calls": True,
            "timeout": max(1.0, float(limits.PROVIDER_TIMEOUT_SECONDS)),
        }

    def workflow_tool_visibility(ctx: RunContext[StudioDeps], tool_definition):
        name = str(getattr(tool_definition, "name", ""))
        # A search call can contain twelve concurrent query variants. Four
        # rounds preserve broad agent-led research while reserving a model turn
        # for reading and answering instead of looping until the run fails.
        if name == "search_web" and ctx.deps.tool_call_counts.get(name, 0) >= 4:
            return None
        return tool_definition

    async def normalize_draft_evidence(
        ctx: RunContext[StudioDeps],
        source_ids: list[str] | None,
        claim_support: list[dict[str, Any]] | None,
        *,
        creative: bool,
        fallback_source_ids: list[str] | None = None,
    ) -> tuple[list[str], list[dict[str, Any]]]:
        """Keep only conversation-scoped IDs and tolerate imperfect tool JSON."""

        bundle = await _research(ctx, settings).get_bundle(
            workspace_id=ctx.deps.workspace_id,
            conversation_id=ctx.deps.conversation_id,
            channel_id=ctx.deps.channel_id,
        )
        known_order = [source.source_id for source in bundle.sources] if bundle else []
        known = set(known_order)
        url_to_id = {str(source.url).strip(): source.source_id for source in (bundle.sources if bundle else [])}
        # also include canonical_url if available
        for source in (bundle.sources if bundle else []):
            cu = getattr(source, "canonical_url", None)
            if cu:
                url_to_id[str(cu).strip()] = source.source_id

        def valid_ids(values: Any) -> list[str]:
            if isinstance(values, str):
                # Malformed claim_support where source_ids is a string should be treated as invalid
                return []
            if not isinstance(values, (list, tuple, set)):
                return []
            result: list[str] = []
            for value in values:
                candidate = str(value).strip()
                if not candidate:
                    continue
                # Map URL to source_id if it matches a known source's URL
                if candidate not in known and candidate in url_to_id:
                    candidate = url_to_id[candidate]
                if candidate and candidate in known and candidate not in result:
                    result.append(candidate)
            return result[:12]

        # Check for unknown IDs: if source_ids provided but after mapping still has unknown, block
        if source_ids is not None:
            # Normalize provided for check, handling string case
            provided_raw = source_ids if isinstance(source_ids, (list, tuple, set, str)) else []
            if isinstance(provided_raw, str):
                provided_raw = [provided_raw]
            provided = [str(v).strip() for v in provided_raw if str(v).strip()]
            # Map URLs to IDs for check
            mapped_provided = []
            for v in provided:
                if v in known:
                    mapped_provided.append(v)
                elif v in url_to_id:
                    mapped_provided.append(url_to_id[v])
                else:
                    mapped_provided.append(v)
            # If any still unknown, block
            if provided and any(v not in known for v in mapped_provided):
                return [], []
            if not provided and not creative:
                return [], []
        selected = valid_ids(source_ids)
        if not selected:
            selected = valid_ids(fallback_source_ids)

        claims: list[dict[str, Any]] = []
        for item in claim_support or []:
            if isinstance(item, ClaimSupport):
                item = item.model_dump()
            if not isinstance(item, dict):
                continue
            claim = " ".join(str(item.get("claim") or "").split())[:500]
            ids = valid_ids(item.get("source_ids"))
            if claim and ids:
                claims.append({"claim": claim, "source_ids": ids})
                for source_id in ids:
                    if source_id not in selected:
                        selected.append(source_id)
        return selected[:40], claims[:40]

    agent = Agent(
        model if model is not None else build_model(settings),
        deps_type=StudioDeps,
        output_type=str,
        instructions=(
            f"{SYSTEM_INSTRUCTIONS}\n\nRuntime date: {datetime.now(timezone.utc).date().isoformat()} (UTC). "
            "Build freshness windows and date-bearing search queries from this date. "
            "Never substitute a different year from model memory.\n"
            f"Run budget: {limits.MAX_TOOL_CALLS} tool calls, {limits.RUN_TIMEOUT_SECONDS:g} seconds, "
            f"{limits.MAX_OUTPUT_TOKENS} cumulative output tokens. "
            f"Queries per search_web: {limits.SEARCH_MAX_QUERIES}; results per query: {limits.SEARCH_MAX_RESULTS}. "
            f"Configured engine names (availability is not guaranteed): {limits.SEARCH_ALLOWED_ENGINES or 'all provider engines'}. "
            f"Default engines: {limits.SEARCH_ENGINES or 'provider defaults'}."
        ),
        model_settings=workflow_model_settings,
        retries=4,
        tool_timeout=max(1.0, float(limits.TOOL_TIMEOUT_SECONDS)),
        max_concurrency=max(1, int(limits.TOOL_CONCURRENCY)),
    )

    @agent.output_validator
    def enforce_required_workflow(ctx: RunContext[StudioDeps], output: str) -> str:
        missing = [name for name in ctx.deps.required_tools if name not in ctx.deps.completed_tools]
        if missing:
            raise ModelRetry(
                f"The required evidence workflow is incomplete. Call {missing[0]} next; do not claim completion yet."
            )
        return output

    @agent.tool(prepare=workflow_tool_visibility)
    async def get_channel_context(ctx: RunContext[StudioDeps]) -> ChannelContext:
        """Read only the authenticated conversation's selected channel."""

        _check_cancel(ctx)
        _require_predecessors(ctx, "get_channel_context")
        raw = await ctx.deps.repository.channel_context(ctx.deps.channel_id)
        _check_cancel(ctx)
        # Build the M3 context only after the authorized channel has been
        # resolved. Older test repositories may not expose performance rows;
        # those runs retain the safe M2 metadata-only context.
        performance_rows = []
        reader = getattr(ctx.deps.repository, "performance_rows", None)
        if reader is not None:
            performance_rows = await reader(ctx.deps.channel_id)
            _check_cancel(ctx)
        assembler = ContextAssembler(
            max_chars=int(limits.CONTEXT_MAX_CHARS),
            max_evidence_posts=int(limits.MAX_EVIDENCE_POSTS),
        )
        profile_getter = getattr(ctx.deps.repository, "get_profile", None)
        profile = await profile_getter(ctx.deps.channel_id) if profile_getter is not None else None
        conversation_getter = getattr(ctx.deps.repository, "get_conversation", None)
        conversation = await conversation_getter(ctx.deps.conversation_id) if conversation_getter is not None else None
        pack = assembler.assemble_from_rows(
            raw,
            performance_rows,
            channel_id=ctx.deps.channel_id,
            identifier=raw.get("identifier"),
            profile=profile,
            profile_version=profile.get("version") if profile else None,
            conversation_summary=str((conversation or {}).get("summary") or "")[:4_000],
        )
        draft_context = await _draft_context(ctx)
        context_payload = pack.model_dump(mode="json")
        if draft_context is not None:
            context_payload["draft_context"] = draft_context
        bundle = await _research(ctx, settings).get_bundle(
            workspace_id=ctx.deps.workspace_id, conversation_id=ctx.deps.conversation_id,
            channel_id=ctx.deps.channel_id,
        )
        if bundle is not None:
            context_payload["research_sources"] = [
                {key: value for key, value in source.model_dump(mode="json").items()
                 if key in {"source_id", "url", "title", "excerpt", "published_at", "retrieved_at", "warnings", "status", "accessible"}}
                for source in sorted(bundle.sources, key=lambda item: item.retrieved_at, reverse=True)[:12]
            ]
        raw = {**raw, "studio_context": context_payload, "draft_context": draft_context}
        result = ChannelContext.model_validate(raw)
        ctx.deps.completed_tools.add("get_channel_context")
        return result

    @agent.tool(prepare=workflow_tool_visibility)
    async def get_performance_evidence(ctx: RunContext[StudioDeps]) -> dict[str, Any]:
        """Return bounded, scored evidence for the authenticated channel."""

        _check_cancel(ctx)
        _require_predecessors(ctx, "get_performance_evidence")
        reader = getattr(ctx.deps.repository, "performance_rows", None)
        if reader is None:
            return {"analytics_version": "m3.v1", "eligible_post_count": 0, "evidence_posts": [], "low_data": True}
        raw = await ctx.deps.repository.channel_context(ctx.deps.channel_id)
        rows = await reader(ctx.deps.channel_id)
        _check_cancel(ctx)
        analysis = analyze_posts(rows, ctx.deps.channel_id, identifier=raw.get("identifier"))
        result = analysis.model_dump(mode="json")
        ctx.deps.completed_tools.add("get_performance_evidence")
        return result

    @agent.tool(prepare=workflow_tool_visibility)
    async def get_context_pack(ctx: RunContext[StudioDeps]) -> ContextPack:
        """Return the same bounded context envelope used for reasoning."""

        _check_cancel(ctx)
        raw = await ctx.deps.repository.channel_context(ctx.deps.channel_id)
        reader = getattr(ctx.deps.repository, "performance_rows", None)
        rows = await reader(ctx.deps.channel_id) if reader is not None else []
        _check_cancel(ctx)
        profile_getter = getattr(ctx.deps.repository, "get_profile", None)
        profile = await profile_getter(ctx.deps.channel_id) if profile_getter is not None else None
        conversation_getter = getattr(ctx.deps.repository, "get_conversation", None)
        conversation = await conversation_getter(ctx.deps.conversation_id) if conversation_getter is not None else None
        return ContextAssembler(
            max_chars=int(limits.CONTEXT_MAX_CHARS),
            max_evidence_posts=int(limits.MAX_EVIDENCE_POSTS),
        ).assemble_from_rows(
            raw,
            rows,
            channel_id=ctx.deps.channel_id,
            identifier=raw.get("identifier"),
            profile=profile,
            profile_version=profile.get("version") if profile else None,
            conversation_summary=str((conversation or {}).get("summary") or "")[:4_000],
        )

    def _profile_block_from_row(row: dict[str, Any] | None, channel_id: int) -> str:
        if not row:
            return ""
        topics_text = str(row.get("topics_text") or "").strip()
        editorial_text = str(row.get("editorial_text") or "").strip()
        style_text = str(row.get("style_text") or "").strip()
        version = row.get("version", "?")
        if not topics_text and not editorial_text and not style_text:
            topics = row.get("topics") or []
            if topics and isinstance(topics, list):
                topics_text = "\n".join(str(t.get("name") or "") for t in topics if isinstance(t, dict) and t.get("name"))
        if not topics_text and not editorial_text and not style_text:
            return ""
        parts = [f"CHANNEL PROFILE (written and approved by the channel owner, version {version})"]
        if topics_text:
            parts.append("Topics:\n" + "\n".join(f"- {line}" for line in topics_text.splitlines() if line.strip()))
        if editorial_text:
            parts.append("Editorial rules:\n" + "\n".join(f"- {line}" for line in editorial_text.splitlines() if line.strip()))
        if style_text:
            parts.append("Style rules:\n" + style_text)
        parts.append("These lines are guidelines, not a template. Choose the form each post needs; do not copy the structure or distinctive wording of past posts. Formatting shown in Markdown (**bold**, *italic*, [links](url)) is to be reproduced in the draft body using the same Markdown.")
        return "\n\n".join(parts)

    @agent.tool(prepare=workflow_tool_visibility)
    async def get_channel_profile(ctx: RunContext[StudioDeps]) -> dict[str, Any]:
        """Read the current channel profile as a single Markdown block."""

        _check_cancel(ctx)
        getter = getattr(ctx.deps.repository, "get_profile", None)
        row = await getter(ctx.deps.channel_id) if getter is not None else None
        if not row or not any(str(row.get(k) or "").strip() for k in ("topics_text", "editorial_text", "style_text")):
            # Fallback to legacy empty case
            legacy = row or {"channel_id": ctx.deps.channel_id, "version": 0}
            return {"channel_id": ctx.deps.channel_id, "version": int(legacy.get("version", 0)), "profile_block": "", "status": "not_built"}
        block = _profile_block_from_row(row, ctx.deps.channel_id)
        return {"channel_id": ctx.deps.channel_id, "version": int(row.get("version", 0)), "profile_block": block, "topics_text": row.get("topics_text", ""), "editorial_text": row.get("editorial_text", ""), "style_text": row.get("style_text", "")}

    @agent.tool(prepare=workflow_tool_visibility)
    async def get_topic_profile(ctx: RunContext[StudioDeps]) -> dict[str, Any]:
        """Alias for get_channel_profile (kept for one release)."""

        return await get_channel_profile(ctx)

    @agent.tool(prepare=workflow_tool_visibility)
    async def search_web(
        ctx: RunContext[StudioDeps],
        query: str,
        alternate_queries: list[str] | None = None,
        recency_days: int | None = None,
        language: str | None = None,
        limit: int | None = None,
        categories: list[str] | None = None,
        domains: list[str] | None = None,
        engines: list[str] | None = None,
        exclude_domains: list[str] | None = None,
    ) -> dict[str, Any]:
        """Run an agent-designed multi-query search and return diverse evidence.

        Up to 12 query variants run concurrently (six network slots shared by
        batches). All candidates are returned without lexical/semantic ranking
        filters. Make parallel calls for independent language, engine, recency
        or domain settings. Choose wording and exclusions from user intent.
        """

        _check_cancel(ctx)
        # Increment at tool start to enforce cap under concurrency
        current = ctx.deps.tool_call_counts.get("search_web", 0)
        if current >= 4:
            return {"status": "blocked", "error": {"code": "search_limit_exceeded", "message": "Search limit reached for this run."}}
        ctx.deps.tool_call_counts["search_web"] = current + 1
        _require_predecessors(ctx, "search_web")
        topics, recent_posts, evidence_ids, channel_evidence = await _research_context(ctx)
        service = _research(ctx, settings)
        result = await service.search(
            workspace_id=ctx.deps.workspace_id,
            conversation_id=ctx.deps.conversation_id,
            channel_id=ctx.deps.channel_id,
            query=query,
            alternate_queries=(alternate_queries or [])[:11],
            topics=topics,
            recent_posts=recent_posts,
            channel_evidence_ids=evidence_ids,
            channel_evidence=channel_evidence,
            language=language,
            recency_days=recency_days,
            limit=limit,
            categories=categories or (),
            domains=domains or (),
            engines=engines or (),
            exclude_domains=exclude_domains or (),
        )
        _check_cancel(ctx)
        ctx.deps.completed_tools.add("search_web")
        return result if isinstance(result, dict) else result.model_dump(mode="json")

    @agent.tool(prepare=workflow_tool_visibility)
    async def read_sources(ctx: RunContext[StudioDeps], urls: list[str]) -> dict[str, Any]:
        """Read selected URLs through the SSRF-safe bounded source reader."""

        _check_cancel(ctx)
        _require_predecessors(ctx, "read_sources")
        topics, recent_posts, evidence_ids, channel_evidence = await _research_context(ctx)
        service = _research(ctx, settings)
        result = await service.read_sources(
            workspace_id=ctx.deps.workspace_id,
            conversation_id=ctx.deps.conversation_id,
            channel_id=ctx.deps.channel_id,
            urls=urls[:6],
            topics=topics,
            recent_posts=recent_posts,
            channel_evidence_ids=evidence_ids,
            channel_evidence=channel_evidence,
        )
        _check_cancel(ctx)
        ctx.deps.completed_tools.add("read_sources")
        return result

    @agent.tool(prepare=workflow_tool_visibility)
    async def compare_sources(
        ctx: RunContext[StudioDeps],
        source_ids: list[str] | None = None,
        urls: list[str] | None = None,
    ) -> dict[str, Any]:
        """Compare stored sources and expose agreement/conflict flags."""

        _check_cancel(ctx)
        _require_predecessors(ctx, "compare_sources")
        topics, recent_posts, evidence_ids, channel_evidence = await _research_context(ctx)
        service = _research(ctx, settings)
        result = await service.compare_sources(
            workspace_id=ctx.deps.workspace_id,
            conversation_id=ctx.deps.conversation_id,
            channel_id=ctx.deps.channel_id,
            source_ids=(source_ids or [])[:12],
            urls=(urls or [])[:12],
            topics=topics,
            recent_posts=recent_posts,
            channel_evidence_ids=evidence_ids,
            channel_evidence=channel_evidence,
        )
        _check_cancel(ctx)
        ctx.deps.completed_tools.add("compare_sources")
        return result

    @agent.tool(prepare=workflow_tool_visibility)
    async def find_novel_topics(
        ctx: RunContext[StudioDeps],
        instruction: str = "",
        categories: list[str] | None = None,
        domains: list[str] | None = None,
    ) -> dict[str, Any]:
        """Find adjacent/current topics and explain their channel fit."""

        _check_cancel(ctx)
        topics, recent_posts, evidence_ids, channel_evidence = await _research_context(ctx)
        service = _research(ctx, settings)
        result = await service.find_novel_topics(
            workspace_id=ctx.deps.workspace_id,
            conversation_id=ctx.deps.conversation_id,
            channel_id=ctx.deps.channel_id,
            topics=topics,
            instruction=instruction,
            recent_posts=recent_posts,
            channel_evidence_ids=evidence_ids,
            channel_evidence=channel_evidence,
            categories=categories or (),
            domains=domains or (),
        )
        _check_cancel(ctx)
        return result

    async def propose_topic_changes(ctx: RunContext[StudioDeps], instruction: str) -> dict[str, Any]:
        """Profile is edited in the Profile dialog, not by the agent."""

        _check_cancel(ctx)
        return {"status": "blocked", "reason": "Profile changes are edited in the Profile dialog by the channel owner."}

    async def apply_confirmed_topic_changes(ctx: RunContext[StudioDeps], change_id: str) -> dict[str, Any]:
        """Profile is edited in the Profile dialog, not by the agent."""

        _check_cancel(ctx)
        try:
            parsed = __import__("uuid").UUID(str(change_id))
        except (TypeError, ValueError):
            return {"status": "blocked", "reason": "A valid profile change id is required."}
        getter = getattr(ctx.deps.repository, "get_profile_change", None)
        if getter is not None:
            scoped = await getter(parsed, channel_id=ctx.deps.channel_id)
            if scoped is None:
                return {"status": "blocked", "reason": "The profile change is not part of this channel."}
        return {"status": "blocked", "reason": "Profile changes are edited in the Profile dialog by the channel owner."}

    @agent.tool(prepare=workflow_tool_visibility)
    async def explain_recommendation(
        ctx: RunContext[StudioDeps],
        post_id: int | None = None,
        cluster_id: str | None = None,
        source_id: str | None = None,
    ) -> dict[str, Any]:
        """Return channel/web evidence without exposing private reasoning."""

        _check_cancel(ctx)
        _require_predecessors(ctx, "explain_recommendation")
        reader = getattr(ctx.deps.repository, "performance_rows", None)
        channel_evidence: dict[str, Any] = {"claim": "No performance evidence is available.", "evidence_post_ids": []}
        if reader is not None:
            raw = await ctx.deps.repository.channel_context(ctx.deps.channel_id)
            analysis = analyze_posts(await reader(ctx.deps.channel_id), ctx.deps.channel_id, identifier=raw.get("identifier"))
            if post_id is not None:
                evidence = next((post for post in analysis.evidence_posts if post.post_id == int(post_id)), None)
                channel_evidence = evidence.model_dump(mode="json") if evidence else {"claim": "The requested post is not in the evidence set.", "evidence_post_ids": []}
            elif analysis.observations:
                channel_evidence = analysis.observations[0].model_dump(mode="json")
        service = _research(ctx, settings)
        bundle = await service.get_bundle(
            workspace_id=ctx.deps.workspace_id,
            conversation_id=ctx.deps.conversation_id,
            channel_id=ctx.deps.channel_id,
        )
        if bundle is None:
            ctx.deps.completed_tools.add("explain_recommendation")
            return {"channel_evidence": channel_evidence, **channel_evidence, "web_evidence": [], "source_links": [], "assumption": "No web story has been selected.", "confidence": "low"}
        stories = list(bundle.stories)
        if cluster_id:
            stories = [story for story in stories if story.cluster_id == str(cluster_id)]
        sources = list(bundle.sources)
        if source_id:
            sources = [source for source in sources if source.source_id == str(source_id)]
        source_links = [source.url for source in sources if source.url]
        confidence = "high" if stories and all("possible_conflict" not in story.conflict_flags for story in stories) and any(len(story.source_ids) > 1 for story in stories) else "medium" if stories else "low"
        result = {
            "channel_evidence": channel_evidence,
            "web_evidence": [story.model_dump(mode="json") for story in stories[:6]],
            "source_links": source_links[:12],
            "assumption": "The selected story is relevant to the inferred channel topics; the user can request a different angle.",
            "confidence": confidence,
            "warnings": list(bundle.warnings),
        }
        ctx.deps.completed_tools.add("explain_recommendation")
        return result

    @agent.tool(prepare=workflow_tool_visibility)
    async def create_draft(
        ctx: RunContext[StudioDeps],
        body: str,
        working_title: str = "Untitled draft",
        source_ids: list[str] | None = None,
        claim_support: list[dict[str, Any]] | None = None,
        assumptions: list[str] | None = None,
        warnings: list[str] | None = None,
        channel_evidence: list[dict[str, Any]] | None = None,
        web_evidence: list[dict[str, Any]] | None = None,
        confidence: str = "low",
        creative: bool = False,
        story_cluster_id: str | None = None,
        analysis_id: str | None = None,
    ) -> dict[str, Any]:
        """Save the draft artifact. claim_support items must be {claim, source_ids}.

        Copy exact source_id values from get_channel_context/search/read results,
        never URLs or invented IDs. Use creative only for explicitly requested
        fictional/opinion content, not to bypass missing factual evidence.
        """

        _check_cancel(ctx)
        _require_predecessors(ctx, "create_draft")
        body, removed_commentary = _clean_publication_text(body)
        _require_publication_text(body)
        normalized_source_ids, normalized_claims = await normalize_draft_evidence(
            ctx,
            source_ids,
            claim_support,
            creative=creative,
        )
        normalized_warnings = list(warnings or [])
        if removed_commentary:
            normalized_warnings.append("Service commentary or a trailing source list was removed from the publication text; sources stay on the artifact.")
        creator = getattr(ctx.deps.repository, "create_draft", None)
        if creator is None:
            return {"status": "blocked", "error": {"code": "draft_unavailable", "message": "Draft persistence is unavailable."}}
        payload = {
            "body": body,
            "working_title": working_title,
            "source_ids": normalized_source_ids,
            "claim_support": normalized_claims,
            "assumptions": assumptions or [],
            "warnings": normalized_warnings,
            "channel_evidence": channel_evidence or [],
            "web_evidence": web_evidence or [],
            "confidence": confidence,
            "creative": creative,
            "story_cluster_id": story_cluster_id,
            "analysis_id": analysis_id,
            "provider": "local" if getattr(settings, "studio_test_mode", False) else "openrouter",
            "model": getattr(settings, "openrouter_model", ""),
            "prompt_version": "m5.draft.v1",
        }
        try:
            row = await creator(
                conversation_id=ctx.deps.conversation_id,
                channel_id=ctx.deps.channel_id,
                payload=payload,
                origin="generated",
                instruction="Created from the user's request.",
            )
        except (DraftValidationError, DraftConflictError) as exc:
            return _draft_error(exc)
        _check_cancel(ctx)
        ctx.deps.completed_tools.add("create_draft")
        return {"status": "created", "draft": row, "version": row.get("current_version", 1), "decision_summary": _draft_summary(row)}

    @agent.tool(prepare=workflow_tool_visibility)
    async def revise_draft(
        ctx: RunContext[StudioDeps],
        draft_id: str,
        body: str,
        instruction: str = "",
        working_title: str | None = None,
        source_ids: list[str] | None = None,
        claim_support: list[dict[str, Any]] | None = None,
        assumptions: list[str] | None = None,
        warnings: list[str] | None = None,
        channel_evidence: list[dict[str, Any]] | None = None,
        web_evidence: list[dict[str, Any]] | None = None,
        confidence: str | None = None,
        creative: bool | None = None,
        story_cluster_id: str | None = None,
        analysis_id: str | None = None,
    ) -> dict[str, Any]:
        """Append a generated revision without overwriting a direct user edit."""

        _check_cancel(ctx)
        body, removed_commentary = _clean_publication_text(body)
        _require_publication_text(body)
        try:
            parsed_id = __import__("uuid").UUID(str(draft_id))
        except (TypeError, ValueError):
            return {"status": "blocked", "error": {"code": "draft_not_found", "message": "A valid draft id is required."}}
        getter = getattr(ctx.deps.repository, "get_draft", None)
        if getter is None:
            return {"status": "blocked", "error": {"code": "draft_unavailable", "message": "Draft persistence is unavailable."}}
        current = await getter(parsed_id, conversation_id=ctx.deps.conversation_id, channel_id=ctx.deps.channel_id)
        if current is None:
            return {"status": "blocked", "error": {"code": "draft_not_found", "message": "Draft not found in this conversation."}}
        normalized_source_ids, normalized_claims = await normalize_draft_evidence(
            ctx,
            source_ids,
            claim_support,
            creative=bool(current.get("creative")) if creative is None else creative,
            fallback_source_ids=list(current.get("source_ids") or []),
        )
        normalized_warnings = list(warnings if warnings is not None else current.get("warnings") or [])
        if removed_commentary:
            normalized_warnings.append("Service commentary or a trailing source list was removed from the publication text; sources stay on the artifact.")
        # Use normalized claims only when body changed; otherwise keep current
        if body != current.get("body"):
            claim_support_value = normalized_claims
        else:
            claim_support_value = normalized_claims or (current.get("claim_support") or [])
        payload: dict[str, Any] = {
            "body": body,
            "working_title": working_title,
            "source_ids": normalized_source_ids,
            "claim_support": claim_support_value,
            "assumptions": assumptions,
            "warnings": normalized_warnings,
            "channel_evidence": channel_evidence,
            "web_evidence": web_evidence,
            "confidence": confidence,
            "creative": creative,
            "story_cluster_id": story_cluster_id,
            "analysis_id": analysis_id,
            "provider": "local" if getattr(settings, "studio_test_mode", False) else "openrouter",
            "model": getattr(settings, "openrouter_model", ""),
            "prompt_version": "m5.draft.v1",
        }
        payload = {key: value for key, value in payload.items() if value is not None}
        try:
            row = await ctx.deps.repository.revise_draft(draft_id=parsed_id, payload=payload, instruction=instruction)
        except (DraftValidationError, DraftConflictError) as exc:
            return _draft_error(exc)
        _check_cancel(ctx)
        ctx.deps.completed_tools.add("revise_draft")
        return {
            "status": "revised",
            "draft": row,
            "version": row.get("current_version"),
            "decision_summary": _draft_summary(row),
        }

    @agent.tool(prepare=workflow_tool_visibility)
    async def save_draft(
        ctx: RunContext[StudioDeps],
        draft_id: str,
        expected_revision: int,
        body: str,
        working_title: str | None = None,
    ) -> dict[str, Any]:
        """Save an agent-written body as a *regenerated* version.

        ``user_edit`` is reserved for the owner's own edits through the draft
        panel; a model-authored save must never claim that origin, otherwise
        later revisions would be preserved away as if the owner had typed it.
        """

        _check_cancel(ctx)
        body, _ = _clean_publication_text(body)
        _require_publication_text(body)
        try:
            parsed_id = __import__("uuid").UUID(str(draft_id))
        except (TypeError, ValueError):
            return {"status": "blocked", "error": {"code": "draft_not_found", "message": "A valid draft id is required."}}
        payload: dict[str, Any] = {"body": body}
        if working_title is not None:
            payload["working_title"] = working_title
        getter = getattr(ctx.deps.repository, "get_draft", None)
        current = await getter(
            parsed_id,
            conversation_id=ctx.deps.conversation_id,
            channel_id=ctx.deps.channel_id,
        ) if getter is not None else None
        if current is None:
            return {
                "status": "blocked",
                "error": {
                    "code": "draft_not_found",
                    "message": "Draft not found in this conversation.",
                },
            }
        try:
            row = await ctx.deps.repository.update_draft(
                draft_id=parsed_id,
                payload=payload,
                expected_revision=expected_revision,
                origin="regenerated",
                instruction="Saved at the user's request.",
            )
        except (DraftValidationError, DraftConflictError) as exc:
            return _draft_error(exc)
        return {"status": "saved", "draft": row, "version": row.get("current_version"), "decision_summary": _draft_summary(row)}

    @agent.tool(prepare=workflow_tool_visibility)
    async def get_draft(ctx: RunContext[StudioDeps], draft_id: str | None = None) -> dict[str, Any]:
        """Read the active draft or a scoped draft by id."""

        _check_cancel(ctx)
        try:
            if draft_id:
                parsed_id = __import__("uuid").UUID(str(draft_id))
                row = await ctx.deps.repository.get_draft(parsed_id, conversation_id=ctx.deps.conversation_id, channel_id=ctx.deps.channel_id)
            else:
                row = await ctx.deps.repository.get_current_draft(conversation_id=ctx.deps.conversation_id, channel_id=ctx.deps.channel_id)
        except (TypeError, ValueError):
            row = None
        if row is None:
            return {"status": "empty", "draft": None, "message": "There is no draft in this conversation yet."}
        versions_getter = getattr(ctx.deps.repository, "list_draft_versions", None)
        versions = (
            await versions_getter(
                draft_id=row["id"],
                conversation_id=ctx.deps.conversation_id,
                channel_id=ctx.deps.channel_id,
            )
            if versions_getter is not None
            else []
        )
        return {
            "status": "ready",
            "draft": row,
            "versions": [
                {
                    "version": int(item.get("version", 0)),
                    "origin": str(item.get("origin") or "generated"),
                    "character_count": int(item.get("character_count", len(str(item.get("body") or "")))),
                    "instruction": str(item.get("instruction") or "")[:240],
                }
                for item in list(versions or [])[-6:]
            ],
            "decision_summary": _draft_summary(row),
        }

    @agent.tool(prepare=workflow_tool_visibility)
    async def list_draft_versions(ctx: RunContext[StudioDeps], draft_id: str | None = None) -> dict[str, Any]:
        """List append-only versions for the active/scoped draft."""

        _check_cancel(ctx)
        try:
            if draft_id:
                parsed_id = __import__("uuid").UUID(str(draft_id))
            else:
                current = await ctx.deps.repository.get_current_draft(conversation_id=ctx.deps.conversation_id, channel_id=ctx.deps.channel_id)
                parsed_id = __import__("uuid").UUID(str(current["id"])) if current else None
        except (TypeError, ValueError):
            parsed_id = None
        if parsed_id is None:
            return {"status": "empty", "versions": []}
        versions = await ctx.deps.repository.list_draft_versions(
            draft_id=parsed_id,
            conversation_id=ctx.deps.conversation_id,
            channel_id=ctx.deps.channel_id,
        )
        return {"status": "ready", "draft_id": str(parsed_id), "versions": versions}

    return agent
