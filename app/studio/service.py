"""Conversation, streaming, and scoped Studio research service."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import time
import uuid
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable

from pydantic import ValidationError
from ag_ui.core import (
    RunErrorEvent,
    RunFinishedEvent,
    TextMessageContentEvent,
    TextMessageEndEvent,
    TextMessageStartEvent,
)
from pydantic_ai import UsageLimits
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart
from pydantic_ai.ui import SSE_CONTENT_TYPE
from pydantic_ai.ui.ag_ui import AGUIAdapter
from starlette.responses import JSONResponse, StreamingResponse

from .agent import StudioDeps, build_agent, is_short_continuation_request, workflow_tool_sequence
from .analytics import analyze_posts
from .profile import build_profile
from .semantic_profile import build_semantic_profile
from .research import ResearchService
from .prompts import PROMPT_VERSION
from .model import model_name
from .observability import emit_observation, normalize_usage, safe_error
from .run_ids import resolve_run_id
from .repository import (
    ActiveRunExists,
    ConversationNotFound,
    RunNotFound,
    RunClaimLost,
    StudioRepositoryError,
    StudioRepositoryProtocol,
)


log = logging.getLogger("studio")


class _UpstreamRunError(RuntimeError):
    """Internal sentinel for an error already emitted by the UI adapter."""


@dataclass(slots=True)
class RunHandle:
    run_id: uuid.UUID
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[Any] | None = None
    cancelled: bool = False
    cancel_reason: str | None = None
    active: bool = True

    def attach(self, task: asyncio.Task[Any]) -> None:
        self.task = task
        if self.cancelled and not task.done():
            task.cancel()

    def cancel(self, *, reason: str = "user") -> bool:
        if not self.active:
            return False
        self.cancelled = True
        self.cancel_reason = reason
        self.cancel_event.set()
        if self.task is not None and not self.task.done():
            self.task.cancel()
        return True

    def finish(self) -> None:
        self.active = False
        self.task = None


@dataclass(slots=True)
class RunRegistry:
    """Local cancellation accelerator; PostgreSQL remains authoritative."""

    max_runs: int = 64
    _runs: dict[uuid.UUID, RunHandle] = field(default_factory=dict)

    def begin(self, run_id: uuid.UUID) -> RunHandle:
        existing = self._runs.get(run_id)
        if existing is not None and existing.active:
            raise ActiveRunExists("run already active")
        handle = RunHandle(run_id=run_id)
        self._runs[run_id] = handle
        while len(self._runs) > self.max_runs:
            oldest = next(iter(self._runs))
            del self._runs[oldest]
        return handle

    def attach(self, run_id: uuid.UUID, task: asyncio.Task[Any]) -> None:
        handle = self._runs.get(run_id)
        if handle is not None:
            handle.attach(task)

    def cancel(self, run_id: uuid.UUID, *, reason: str = "user") -> bool:
        handle = self._runs.get(run_id)
        return handle.cancel(reason=reason) if handle is not None else False

    def finish(self, run_id: uuid.UUID) -> None:
        handle = self._runs.get(run_id)
        if handle is not None:
            handle.finish()


@dataclass(slots=True)
class RunCoordinator:
    """Bound provider execution within one process.

    PostgreSQL leases prevent duplicate ownership across processes; this
    semaphore enforces the configured local provider-call ceiling.
    """

    max_concurrency: int = 2
    _slots: asyncio.Semaphore = field(init=False, repr=False)
    active: int = 0

    def __post_init__(self) -> None:
        self.max_concurrency = max(1, int(self.max_concurrency))
        self._slots = asyncio.Semaphore(self.max_concurrency)

    @asynccontextmanager
    async def slot(self):
        await self._slots.acquire()
        self.active += 1
        try:
            yield
        finally:
            self.active -= 1
            self._slots.release()


def _event_type(event: Any) -> str:
    value = getattr(event, "type", event.__class__.__name__)
    return str(getattr(value, "value", value))


def _safe_event_payload(event: Any) -> dict[str, Any]:
    """Project an AG-UI event without storing text, prompts, or raw payloads."""

    payload: dict[str, Any] = {}
    tool_name = getattr(event, "tool_call_name", None)
    if tool_name:
        payload["tool_name"] = str(tool_name)
    delta = getattr(event, "delta", None)
    if delta:
        payload["delta_length"] = len(str(delta))
    content = getattr(event, "content", None)
    if content and _event_type(event) == "TOOL_CALL_RESULT":
        payload["result_length"] = len(str(content))
        # AG-UI serializes tool results to JSON strings. Decode only for this
        # bounded projection so operational source/story counts survive the
        # protocol boundary; raw URLs and excerpts are still never persisted.
        if isinstance(content, str) and len(content) <= 2_000_000:
            try:
                decoded = json.loads(content)
            except (json.JSONDecodeError, TypeError):
                decoded = None
            if isinstance(decoded, dict):
                content = decoded
        # Research tools return structured activity metadata. Keep only the
        # bounded operational fields required by the collapsed activity row;
        # URLs, excerpts, prompts, and provider responses never enter events.
        candidate = content.get("activity") if isinstance(content, dict) else None
        if candidate is None and isinstance(content, dict):
            candidate = content
        if isinstance(candidate, list):
            candidate = candidate[0] if candidate else None
        if isinstance(candidate, dict):
            for key in ("provider", "degraded", "cache_hit", "persisted", "query_count", "result_count"):
                if key in candidate:
                    value = candidate[key]
                    if key == "provider":
                        payload[key] = str(value)[:80]
                    elif key in {"degraded", "cache_hit", "persisted"}:
                        payload[key] = bool(value)
                    else:
                        try:
                            payload[key] = max(0, min(int(value), 100))
                        except (TypeError, ValueError):
                            continue
        if isinstance(content, dict):
            if "sources" in content and isinstance(content["sources"], list):
                payload["source_count"] = min(len(content["sources"]), 100)
            if "stories" in content and isinstance(content["stories"], list):
                payload["story_count"] = min(len(content["stories"]), 100)
            providers = content.get("providers")
            if isinstance(providers, list):
                payload["providers"] = [str(item)[:80] for item in providers[:4]]
            # Resource IDs are operational references, not private content.
            # Keep them available for quiet run details and structured logs,
            # while deliberately ignoring URLs, excerpts, headlines, and
            # arbitrary provider fields.
            for key in ("analysis_id", "story_cluster_id", "draft_id"):
                candidate_value = content.get(key)
                if candidate_value is None and isinstance(candidate, dict):
                    candidate_value = candidate.get(key)
                if candidate_value is not None and not isinstance(candidate_value, (dict, list)):
                    payload[key] = str(candidate_value)[:160]
            for nested_key in ("analysis", "draft", "proposal", "change"):
                nested = content.get(nested_key)
                if not isinstance(nested, dict):
                    continue
                for key in ("analysis_id", "story_cluster_id", "draft_id", "id"):
                    if key not in nested or isinstance(nested[key], (dict, list)):
                        continue
                    mapped = "story_cluster_id" if nested_key == "proposal" and key == "id" else key
                    payload.setdefault(mapped, str(nested[key])[:160])
    return payload


def _text_from_user_message(message: Any) -> str:
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content.strip()
    parts: list[str] = []
    for item in content or []:
        text = getattr(item, "text", None)
        if text:
            parts.append(str(text))
    return "".join(parts).strip()


def _model_history(rows: list[dict[str, Any]]) -> list[Any]:
    history: list[Any] = []
    bounded: list[dict[str, Any]] = []
    remaining = 48000
    for row in reversed(rows[-40:]):
        content = str(row.get("content") or "")[:6000]
        if len(content) > remaining:
            break
        bounded.append({**row, "content": content})
        remaining -= len(content)
    for row in reversed(bounded):
        content = str(row.get("content") or "")
        if not content:
            continue
        role = row.get("role")
        if role == "user":
            history.append(ModelRequest(parts=[UserPromptPart(content=content)]))
        elif role == "assistant":
            history.append(ModelResponse(parts=[TextPart(content=content)]))
    return history


def _continuation_history(rows: list[dict[str, Any]]) -> list[Any]:
    """Compatibility helper: continuations retain completed assistant replies."""
    return _model_history(rows)


def _safe_error(exc: BaseException) -> tuple[str, str, bool]:
    return safe_error(exc)


class StudioService:
    """Orchestrates validated persistence, PydanticAI, and safe event output."""

    def __init__(
        self,
        repository: StudioRepositoryProtocol,
        settings,
        *,
        agent_factory: Callable[..., Any] = build_agent,
        research_service: ResearchService | None = None,
    ) -> None:
        self.repository = repository
        self.settings = settings
        self.agent_factory = agent_factory
        self.registry = RunRegistry()
        self.coordinator = RunCoordinator(getattr(settings, "studio_run_concurrency", 2))
        self.research_service = research_service or ResearchService(settings, repository=repository)

    async def create_conversation(self, *, channel_id: int, title: str | None = None) -> dict[str, Any]:
        return await self.repository.create_conversation(channel_id=channel_id, title=title)

    async def recover_stale_runs(self) -> int:
        return await self.repository.mark_stale_runs_interrupted(
            queued_grace_seconds=max(1, int(getattr(self.settings, "studio_queued_run_grace_seconds", 60)))
        )

    async def _watch_run_control(self, run_id: uuid.UUID, worker_id: str, handle: RunHandle) -> None:
        """Renew the durable lease and observe cancellation from any process."""

        lease_seconds = max(30, int(getattr(self.settings, "studio_run_lease_seconds", 120)))
        configured = float(getattr(self.settings, "studio_run_heartbeat_seconds", 20.0))
        interval = max(0.05, min(configured, lease_seconds / 3))
        consecutive_failures = 0
        while handle.active:
            await asyncio.sleep(interval)
            try:
                state = await self.repository.renew_run_lease(
                    run_id,
                    worker_id=worker_id,
                    lease_seconds=lease_seconds,
                )
            except Exception:
                consecutive_failures += 1
                if consecutive_failures >= 3:
                    handle.cancel(reason="lease_lost")
                    return
                continue
            consecutive_failures = 0
            if state is None:
                handle.cancel(reason="lease_lost")
                return
            if state.get("cancel_requested"):
                handle.cancel(reason="user")
                return

    async def ensure_profile(self, channel_id: int, *, force: bool = False, semantic: bool = False) -> dict[str, Any] | None:
        """Reuse a profile; semantic=True permits a consent-gated model refresh."""

        getter = getattr(self.repository, "get_profile", None)
        existing = await getter(channel_id) if getter is not None else None
        if existing is not None and not force:
            return existing
        reader = getattr(self.repository, "performance_rows", None)
        if reader is None:
            return existing
        channel = await self.repository.channel_context(channel_id)
        rows = await reader(channel_id)
        analytics = analyze_posts(rows, channel_id, identifier=channel.get("identifier"))
        profile, analysis = build_profile(
            analytics,
            provider="local",
            model="",
        )
        if semantic:
            profile, analysis = await build_semantic_profile(analytics, rows, self.settings)
        analysis_row = None
        create_analysis = getattr(self.repository, "create_analysis", None)
        if create_analysis is not None:
            analysis_row = await create_analysis(
                {
                    "channel_id": channel_id,
                    "analysis_start": analytics.analysis_start,
                    "analysis_end": analytics.analysis_end,
                    "eligible_post_count": analytics.eligible_post_count,
                    "style_eligible_post_count": analytics.style_eligible_post_count,
                    "evidence_post_ids": analysis.evidence_post_ids,
                    "scoring_version": analytics.analytics_version,
                    "scoring_weights": analytics.scoring_weights,
                    "topic_insights": [topic.model_dump(mode="json") for topic in analysis.topic_insights],
                    "style_insights": analysis.style_insights,
                    "limitations": analysis.limitations,
                    "confidence": analysis.confidence,
                    "confidence_score": analysis.confidence_score,
                    "input_hash": analysis.input_hash,
                    "provider": analysis.provider,
                    "model": analysis.model,
                    "prompt_version": analysis.prompt_version,
                }
            )
        upsert = getattr(self.repository, "upsert_profile", None)
        if upsert is None:
            return existing
        return await upsert(
            {
                "channel_id": channel_id,
                "topics": [topic.model_dump(mode="json") for topic in profile.topics],
                "style_profile": profile.style_profile.model_dump(mode="json"),
                "editorial_rules": profile.editorial_rules,
                "confidence": profile.confidence,
                "current_analysis_id": analysis_row["id"] if analysis_row else None,
            }
        )

    async def build_profile_draft(self, channel_id: int) -> dict[str, Any]:
        """Build a draft profile text from channel posts (no save)."""
        reader = getattr(self.repository, "performance_rows", None)
        if reader is None:
            raise ValueError("performance rows unavailable")
        channel = await self.repository.channel_context(channel_id)
        rows = await reader(channel_id)
        if not rows:
            raise ValueError("too few posts")
        min_posts = int(getattr(self.settings, "studio_min_profile_posts", 5))
        if len(rows) < min_posts:
            raise ValueError(f"too few posts: {len(rows)} < {min_posts}")
        # Bound by studio_analysis_max_posts if configured
        max_posts = int(getattr(self.settings, "studio_analysis_max_posts", 0) or 0)
        if max_posts > 0:
            rows = list(rows)[:max_posts]
        analytics = analyze_posts(rows, channel_id, identifier=channel.get("identifier"))
        from .semantic_profile import build_profile_text_draft
        current = None
        getter = getattr(self.repository, "get_profile", None)
        if getter is not None:
            try:
                current = await getter(channel_id)
            except Exception:  # noqa: BLE001 - existing text only refines the prompt
                current = None
        draft = await build_profile_text_draft(analytics, rows, self.settings, current=current)
        return {
            "topics_text": "\n".join(draft.topics),
            "editorial_text": "\n".join(draft.editorial_rules),
            "style_text": "\n".join(draft.style_rules),
            "built_from_posts": draft.built_from_posts,
            "limitations": draft.limitations,
            "formatting_facts": draft.formatting_facts,
        }

    async def stream_request(self, request, body: bytes) -> StreamingResponse | JSONResponse:
        if len(body) > 512 * 1024:
            return JSONResponse({"error": {"code": "request_too_large", "message": "Studio request is too large.", "retryable": False}}, status_code=413)
        try:
            run_input = AGUIAdapter.build_run_input(body)
        except (ValidationError, ValueError, TypeError):
            return JSONResponse({"error": {"code": "invalid_agent_request", "message": "Invalid Studio agent request.", "retryable": False}}, status_code=422)

        try:
            conversation_id = uuid.UUID(str(run_input.thread_id))
            run_id = resolve_run_id(run_input.run_id, workspace_id=self.repository.workspace_id)
        except (TypeError, ValueError):
            return JSONResponse({"error": {"code": "invalid_identifier", "message": "Conversation or run identifier is invalid.", "retryable": False}}, status_code=422)
        conversation = await self.repository.get_conversation(conversation_id)
        if conversation is None or conversation.get("archived_at") is not None:
            return JSONResponse({"error": {"code": "conversation_not_found", "message": "Conversation not found.", "retryable": False}}, status_code=404)
        user_message = next((message for message in reversed(run_input.messages) if getattr(message, "role", "") == "user"), None)
        content = _text_from_user_message(user_message) if user_message is not None else ""
        if not content:
            return JSONResponse({"error": {"code": "message_required", "message": "Send a text message to start the run.", "retryable": False}}, status_code=422)
        if len(content) > 32_000:
            return JSONResponse({"error": {"code": "message_too_large", "message": "Message is too large.", "retryable": False}}, status_code=413)

        history_rows = await self.repository.list_messages(conversation_id)
        try:
            user_row = await self.repository.append_message(conversation_id=conversation_id, role="user", content=content)
            run = await self.repository.create_run(
                conversation_id=conversation_id,
                user_message_id=int(user_row["id"]),
                requested_model=model_name(self.settings),
                provider="openrouter",
                run_id=run_id,
            )
            handle = self.registry.begin(run_id)
            renamer = getattr(self.repository, "rename_conversation", None)
            if renamer is not None:
                title = " ".join(content.split())
                title = title[:77].rsplit(" ", 1)[0] + "…" if len(title) > 80 else title
                await renamer(conversation_id, title=title, only_default=True)
        except ActiveRunExists:
            return JSONResponse({"error": {"code": "run_already_active", "message": "This conversation already has an active run.", "retryable": False}}, status_code=409)
        except ConversationNotFound:
            return JSONResponse({"error": {"code": "conversation_not_found", "message": "Conversation not found.", "retryable": False}}, status_code=404)
        except StudioRepositoryError:
            return JSONResponse({"error": {"code": "conversation_write_failed", "message": "The conversation could not be updated.", "retryable": True}}, status_code=409)

        # Strip client-supplied history, frontend tools, and context. Only the
        # newly accepted user message is sent through AG-UI; persisted history
        # is supplied separately by the server below.
        run_input = run_input.model_copy(
            update={
                "run_id": str(run_id),
                "messages": [user_message],
                "tools": [],
                "context": [],
            }
        )
        try:
            agent = self.agent_factory(self.settings)
        except Exception as exc:  # noqa: BLE001 - convert setup failures to a safe response
            code, message, retryable = _safe_error(exc)
            await self.repository.set_run_status(
                run_id,
                status="failed",
                stage="failed",
                error_code=code,
                error_message=message,
                usage=normalize_usage(None, latency_ms=0),
            )
            emit_observation(
                log,
                "run",
                conversation_id=conversation_id,
                run_id=run_id,
                status="failed",
                stage="failed",
                duration_ms=0,
                provider="openrouter",
                requested_model=model_name(self.settings),
                prompt_version=PROMPT_VERSION,
                error_code=code,
            )
            self.registry.finish(run_id)
            return JSONResponse({"error": {"code": code, "message": message, "retryable": retryable}}, status_code=409)
        adapter = AGUIAdapter(agent=agent, run_input=run_input, accept=SSE_CONTENT_TYPE, manage_system_prompt="server")
        required_tools = workflow_tool_sequence(content, history=history_rows)
        # A draft request that follows a completed research turn already has a
        # conversation-scoped, persisted evidence bundle. Do not make the
        # provider repeat retrieval unless fresh research was requested. Read
        # current context first so the model receives persisted source IDs.
        if "create_draft" in required_tools and "search_web" not in required_tools:
            bundle = await self.research_service.get_bundle(
                workspace_id=self.repository.workspace_id,
                conversation_id=conversation_id,
                channel_id=int(conversation["channel_id"]),
            )
            if bundle is not None and bundle.sources:
                required_tools = ("get_channel_context", "create_draft")
        if "create_draft" in required_tools:
            current_draft = await self.repository.get_current_draft(
                conversation_id=conversation_id,
                channel_id=int(conversation["channel_id"]),
            )
            if current_draft is not None:
                required_tools = tuple("revise_draft" if name == "create_draft" else name for name in required_tools)
        # Retain completed assistant replies: stripping them makes old user
        # requests look unanswered. History is context, not factual evidence.
        server_history = _model_history(history_rows)
        workflow_instructions = None
        if required_tools:
            workflow_instructions = (
                "Server evidence contract for this run (not an execution order): "
                f"{', '.join(required_tools)}. Choose the tool order yourself; independent searches may run in parallel. "
                "Search, read and other tools stay available even when a draft is pending. "
                "Only report searches, source counts, URLs, dates, or findings returned by tools in this run; "
                "never copy or invent claims from earlier assistant messages. A terse continuation means fresh "
                "work is requested. If a tool returns no useful evidence, say that plainly instead of making up "
                "a search history or result set."
            )
        instruction_getter = getattr(self.repository, "get_system_prompt", None)
        workspace_instructions = await instruction_getter() if instruction_getter else ""
        workflow_instructions = (workflow_instructions or "") + (
            "\nAnswer the latest user message. Earlier turns are completed context, not a backlog. "
            "Do not repeat completed research or drafts unless asked. Previous assistant prose is not source evidence."
        )
        if workspace_instructions:
            workflow_instructions += (
                "\nWorkspace owner's standing editorial instructions (apply across conversations; "
                "the latest user request may refine them; security and evidence rules still apply):\n"
                + workspace_instructions
            )

        queue: asyncio.Queue[Any] = asyncio.Queue()
        complete = object()
        worker_id = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:12]}"
        run_started_monotonic = time.monotonic()
        usage_holder: dict[str, Any] = {}
        actual_model_holder: dict[str, str] = {}
        final_output_holder: dict[str, str] = {}
        observed_tool_calls = 0
        deps = StudioDeps(
            repository=self.repository,
            workspace_id=self.repository.workspace_id,
            conversation_id=conversation_id,
            channel_id=int(conversation["channel_id"]),
            cancel_event=handle.cancel_event,
            research=self.research_service,
            required_tools=required_tools,
            strict_workflow=is_short_continuation_request(content),
        )

        async def capture_completion(result: Any) -> None:
            """Capture the final reply and bounded usage, excluding retry prose."""

            output = getattr(result, "output", None)
            if isinstance(output, str):
                final_output_holder["value"] = output
            try:
                usage_holder.clear()
                usage_holder.update(normalize_usage(getattr(result, "usage", None)))
            except Exception:  # noqa: BLE001 - observability must not fail a run
                usage_holder.clear()
            try:
                response = getattr(result, "response", None)
                model = getattr(response, "model_name", None)
                if model:
                    actual_model_holder["value"] = str(model)[:160]
            except Exception:  # noqa: BLE001 - observability must not fail a run
                return

        async def execute_run() -> None:
            nonlocal observed_tool_calls
            output_parts: list[str] = []
            finished_event = None
            watchdog: asyncio.Task[Any] | None = None
            run_finished = False
            try:
                async with self.coordinator.slot():
                    claimed = await self.repository.claim_run(
                        run_id,
                        worker_id=worker_id,
                        lease_seconds=max(30, int(getattr(self.settings, "studio_run_lease_seconds", 120))),
                    )
                    if claimed is None:
                        return
                    watchdog = asyncio.create_task(
                        self._watch_run_control(run_id, worker_id, handle),
                        name=f"studio-run-watchdog-{run_id}",
                    )
                    async with asyncio.timeout(max(1.0, float(self.settings.studio_run_timeout_seconds))):
                        async for event in adapter.run_stream(
                            message_history=server_history,
                            instructions=workflow_instructions,
                            conversation_id=str(conversation_id),
                            run_id=str(run_id),
                            deps=deps,
                            usage_limits=UsageLimits(
                                request_limit=max(2, int(self.settings.studio_max_tool_calls) + 2),
                                tool_calls_limit=max(1, int(self.settings.studio_max_tool_calls)),
                                output_tokens_limit=max(64, int(self.settings.studio_max_output_tokens)),
                            ),
                            on_complete=capture_completion,
                        ):
                            event_type = _event_type(event)
                            if event_type == "TOOL_CALL_START":
                                observed_tool_calls += 1
                            delta = getattr(event, "delta", None)
                            if event_type == "TEXT_MESSAGE_START":
                                output_parts.clear()
                            if event_type == "TEXT_MESSAGE_CONTENT" and delta:
                                output_parts.append(str(delta))
                            # Tool activity remains live. Only the accepted
                            # final reply is shown, not provisional narration.
                            if event_type.startswith(("TEXT_MESSAGE_", "THINKING_", "REASONING_")):
                                continue
                            event_payload = _safe_event_payload(event)
                            if event_type == "RUN_FINISHED":
                                run_finished = True
                                event_payload["usage"] = normalize_usage(
                                    usage_holder,
                                    latency_ms=int(max(0, (time.monotonic() - run_started_monotonic) * 1000)),
                                )
                            elif event_type == "RUN_ERROR":
                                # The adapter exposes provider/model failures as
                                # terminal protocol events. Let the recovery
                                # block decide whether this is a failed run or
                                # a successfully saved artifact with only its
                                # optional chat acknowledgement missing.
                                raise _UpstreamRunError("The provider ended the AG-UI run with an error.")
                            await self.repository.append_event(
                                run_id,
                                event_type=event_type,
                                safe_payload=event_payload,
                            )
                            if event_type.startswith("TOOL_CALL_"):
                                emit_observation(
                                    log,
                                    "tool",
                                    conversation_id=conversation_id,
                                    run_id=run_id,
                                    tool_name=event_payload.get("tool_name"),
                                    event_type=event_type,
                                    status="completed" if event_type == "TOOL_CALL_RESULT" else "started",
                                    duration_ms=int(max(0, (time.monotonic() - run_started_monotonic) * 1000)),
                                    result_count=event_payload.get("result_count"),
                                    source_count=event_payload.get("source_count"),
                                    story_count=event_payload.get("story_count"),
                                    cache_hit=event_payload.get("cache_hit"),
                                    degraded=event_payload.get("degraded"),
                                    provider=event_payload.get("provider"),
                                    analysis_id=event_payload.get("analysis_id"),
                                    story_cluster_id=event_payload.get("story_cluster_id"),
                                    draft_id=event_payload.get("draft_id"),
                                )
                            if handle.cancelled:
                                raise asyncio.CancelledError
                            if event_type == "RUN_FINISHED":
                                finished_event = event
                            else:
                                await queue.put(event)
                    if not run_finished:
                        raise _UpstreamRunError("The AG-UI run ended without a completion event.")
                    if handle.cancelled:
                        raise asyncio.CancelledError
                assistant_text = (final_output_holder["value"] if "value" in final_output_holder else "".join(output_parts)).strip()
                if not assistant_text:
                    assistant_text = "Работа завершена. Результаты инструментов сохранены; текстовый ответ модели не поступил."
                if assistant_text:
                    await self.repository.append_message(
                        conversation_id=conversation_id,
                        role="assistant",
                        content=assistant_text,
                        metadata={"run_id": str(run_id), "prompt_version": PROMPT_VERSION},
                    )
                usage = normalize_usage(
                    usage_holder,
                    latency_ms=int(max(0, (time.monotonic() - run_started_monotonic) * 1000)),
                )
                usage["tool_calls"] = max(int(usage.get("tool_calls", 0)), observed_tool_calls)
                actual_model = actual_model_holder.get("value") or model_name(self.settings)
                await self.repository.set_run_status(
                    run_id,
                    status="succeeded",
                    stage="complete",
                    actual_model=actual_model,
                    usage=usage,
                    worker_id=worker_id,
                )
                final_message_id = f"studio-final-{run_id}"
                await queue.put(TextMessageStartEvent(message_id=final_message_id, role="assistant"))
                await queue.put(TextMessageContentEvent(message_id=final_message_id, delta=assistant_text))
                await queue.put(TextMessageEndEvent(message_id=final_message_id))
                if finished_event is not None:
                    await queue.put(finished_event)
                emit_observation(
                    log,
                    "run",
                    conversation_id=conversation_id,
                    run_id=run_id,
                    status="succeeded",
                    stage="complete",
                    duration_ms=usage.get("latency_ms"),
                    provider="openrouter",
                    requested_model=model_name(self.settings),
                    actual_model=actual_model,
                    prompt_version=PROMPT_VERSION,
                    usage=usage,
                )
            except asyncio.CancelledError:
                current = await self.repository.get_run(run_id)
                user_cancelled = bool(current and current.get("cancel_requested")) or handle.cancel_reason == "user"
                event_type = "RUN_CANCELLED" if user_cancelled else "RUN_INTERRUPTED"
                status = "cancelled" if user_cancelled else "interrupted"
                code = "run_cancelled" if user_cancelled else "run_interrupted"
                message = "Run cancelled by the user." if user_cancelled else "The worker stopped before completion."
                if current and current.get("status") in {"queued", "running"}:
                    usage = normalize_usage(
                        usage_holder,
                        latency_ms=int(max(0, (time.monotonic() - run_started_monotonic) * 1000)),
                    )
                    await self.repository.append_event(run_id, event_type=event_type, safe_payload={"usage": usage})
                    with suppress(RunClaimLost):
                        await self.repository.set_run_status(
                            run_id,
                            status=status,
                            stage=status,
                            error_code=code,
                            error_message=message,
                            usage=usage,
                            worker_id=worker_id,
                        )
                    emit_observation(
                        log,
                        "run",
                        conversation_id=conversation_id,
                        run_id=run_id,
                        status=status,
                        stage=status,
                        duration_ms=usage.get("latency_ms"),
                        provider="openrouter",
                        requested_model=model_name(self.settings),
                        actual_model=actual_model_holder.get("value"),
                        prompt_version=PROMPT_VERSION,
                        usage=usage,
                        error_code=code,
                    )
            except Exception as exc:  # noqa: BLE001 - persist only a safe classification
                code, message, _ = _safe_error(exc)
                current = await self.repository.get_run(run_id)
                if current and current.get("status") in {"queued", "running"}:
                    usage = normalize_usage(
                        usage_holder,
                        latency_ms=int(max(0, (time.monotonic() - run_started_monotonic) * 1000)),
                    )
                    usage["tool_calls"] = max(int(usage.get("tool_calls", 0)), observed_tool_calls)
                    artifact_ready = bool(
                        {"create_draft", "revise_draft", "save_draft"}.intersection(deps.completed_tools)
                    )
                    if artifact_ready:
                        failure_text = (
                            "The draft artifact is ready. The model's separate chat summary was unavailable, "
                            "so the saved artifact is the result of this run."
                        )
                    elif "search_web" in deps.completed_tools:
                        failure_text = (
                            "Web results were saved, but the model did not produce a final answer. "
                            "Retry to continue from the saved research."
                        )
                    else:
                        failure_text = message
                    await self.repository.append_message(
                        conversation_id=conversation_id,
                        role="assistant",
                        content=failure_text,
                        metadata={
                            "run_id": str(run_id),
                            "prompt_version": PROMPT_VERSION,
                            "run_failed": not artifact_ready,
                            "run_recovered": artifact_ready,
                        },
                    )
                    failure_message_id = f"studio-failure-{run_id}"
                    await queue.put(TextMessageStartEvent(message_id=failure_message_id, role="assistant"))
                    await queue.put(TextMessageContentEvent(message_id=failure_message_id, delta=failure_text))
                    await queue.put(TextMessageEndEvent(message_id=failure_message_id))
                    if artifact_ready:
                        await self.repository.append_event(
                            run_id,
                            event_type="RUN_FINISHED",
                            safe_payload={"recovered": True, "usage": usage},
                        )
                        with suppress(RunClaimLost):
                            await self.repository.set_run_status(
                                run_id,
                                status="succeeded",
                                stage="complete",
                                actual_model=actual_model_holder.get("value") or model_name(self.settings),
                                usage=usage,
                                worker_id=worker_id,
                            )
                        await queue.put(
                            RunFinishedEvent(
                                threadId=str(conversation_id),
                                runId=str(run_id),
                                outcome={"type": "success"},
                            )
                        )
                        emit_observation(
                            log,
                            "run",
                            conversation_id=conversation_id,
                            run_id=run_id,
                            status="succeeded",
                            stage="complete",
                            duration_ms=usage.get("latency_ms"),
                            provider="openrouter",
                            requested_model=model_name(self.settings),
                            actual_model=actual_model_holder.get("value"),
                            prompt_version=PROMPT_VERSION,
                            usage=usage,
                            error_code="final_reply_recovered",
                        )
                        return
                    await queue.put(RunErrorEvent(message=message, code=code))
                    await self.repository.append_event(
                        run_id,
                        event_type="RUN_ERROR",
                        safe_payload={"code": code, "usage": usage},
                    )
                    with suppress(RunClaimLost):
                        await self.repository.set_run_status(
                            run_id,
                            status="failed",
                            stage="failed",
                            error_code=code,
                            error_message=message,
                            usage=usage,
                            worker_id=worker_id,
                        )
                    emit_observation(
                        log,
                        "run",
                        conversation_id=conversation_id,
                        run_id=run_id,
                        status="failed",
                        stage="failed",
                        duration_ms=usage.get("latency_ms"),
                        provider="openrouter",
                        requested_model=model_name(self.settings),
                        actual_model=actual_model_holder.get("value"),
                        prompt_version=PROMPT_VERSION,
                        usage=usage,
                        error_code=code,
                    )
            finally:
                if watchdog is not None:
                    watchdog.cancel()
                    try:
                        with suppress(asyncio.CancelledError, Exception):
                            await watchdog
                    finally:
                        self.registry.finish(run_id)
                        await queue.put(complete)
                else:
                    self.registry.finish(run_id)
                    await queue.put(complete)

        task = asyncio.create_task(execute_run(), name=f"studio-run-{run_id}")
        self.registry.attach(run_id, task)

        async def event_stream() -> AsyncIterator[Any]:
            # The worker task is intentionally not cancelled when the HTTP
            # stream disconnects. Durable run/event state remains authoritative
            # and the completed message is available after reload.
            while True:
                event = await queue.get()
                if event is complete:
                    break
                yield event

        return StreamingResponse(adapter.encode_stream(event_stream()), media_type=SSE_CONTENT_TYPE)

    async def cancel(self, run_id: uuid.UUID) -> dict[str, Any]:
        run = await self.repository.get_run(run_id)
        if run is None:
            raise RunNotFound("run is not part of the active workspace")
        if run["status"] not in {"queued", "running"}:
            return run
        updated = await self.repository.request_cancel(run_id)
        self.registry.cancel(run_id, reason="user")
        # A queued run may not have attached its HTTP stream yet. Complete its
        # durable cancellation immediately; an attached stream is idempotent.
        if updated.get("status") == "queued":
            updated = await self.repository.set_run_status(
                run_id,
                status="cancelled",
                stage="cancelled",
                error_code="run_cancelled",
                error_message="Run cancelled by the user.",
            )
            await self.repository.append_event(run_id, event_type="RUN_CANCELLED", safe_payload={})
        return updated
