"""Authenticated Studio page and M2 conversation/run API."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from ..web.dependencies import require_auth, require_csrf
from .consent import PROVIDER_NAME, configuration_fingerprint, consent_state, disclosure
from .drafts import DraftConflictError, DraftValidationError
from .repository import ActiveRunExists, ConversationNotFound, DraftNotFound, MemoryStudioRepository, RunNotFound, StudioRepository, StudioRepositoryError
from .profile import ChannelProfile, propose_topic_change
from .prompts import PROMPT_VERSION
from .schemas import ConversationCreate, DraftPatchRequest, ProfileChangeRequest, ProviderConsentRequest, StudioSettingsPatch
from .service import StudioService
from .setup import build_setup_state
from .search_health import check_search_health
from .observability import duration_ms, normalize_usage, safe_error_for_code
from .run_ids import resolve_run_id

_TEMPLATES = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "web" / "templates"))


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _conversation(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "workspace_id": str(row["workspace_id"]),
        "channel_id": int(row["channel_id"]),
        "channel_identifier": row.get("channel_identifier", "") or "",
        "channel_title": row.get("channel_title", "") or "",
        "title": row.get("title", "New conversation") or "New conversation",
        "summary": row.get("summary", "") or "",
        "active_draft_id": str(row["active_draft_id"]) if row.get("active_draft_id") else None,
        "created_at": _iso(row.get("created_at")),
        "updated_at": _iso(row.get("updated_at")),
        "archived_at": _iso(row.get("archived_at")),
    }


def _draft(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    value = dict(row)
    value["id"] = str(value["id"])
    for key in ("workspace_id", "conversation_id", "analysis_id"):
        if value.get(key) is not None:
            value[key] = str(value[key])
    for key in ("created_at", "updated_at", "copied_at"):
        value[key] = _iso(value.get(key))
    body = str(value.get("body") or "")
    value["body"] = body
    value["character_count"] = len(body)
    value["over_limit"] = len(body) > 4096
    value["warning_threshold"] = len(body) >= 3800
    for key, default in (("source_ids", []), ("claim_support", []), ("assumptions", []), ("warnings", []), ("channel_evidence", []), ("web_evidence", [])):
        raw = value.get(key)
        if isinstance(raw, str):
            try:
                raw = __import__("json").loads(raw)
            except (TypeError, ValueError):
                raw = default
        value[key] = raw if isinstance(raw, (list, dict)) else default
    return value


def _draft_version(row: dict[str, Any]) -> dict[str, Any]:
    value = dict(row)
    value["id"] = int(value["id"])
    value["draft_id"] = str(value["draft_id"])
    value["version"] = int(value["version"])
    value["body"] = str(value.get("body") or "")
    value["character_count"] = len(value["body"])
    value["created_at"] = _iso(value.get("created_at"))
    return value


def _message(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "role": row["role"],
        "content": row["content"],
        "metadata": row.get("metadata_json", {}) or {},
        "created_at": _iso(row.get("created_at")),
    }


def _run(row: dict[str, Any]) -> dict[str, Any]:
    error_code, error_message, _ = safe_error_for_code(row.get("error_code"))
    return {
        "id": str(row["id"]),
        "conversation_id": str(row["conversation_id"]),
        "status": row["status"],
        "stage": row.get("stage", "") or "",
        "provider": row.get("provider", "openrouter") or "openrouter",
        "requested_model": row.get("requested_model", "") or "",
        "actual_model": row.get("actual_model"),
        "usage": normalize_usage(row.get("usage")),
        "error_code": error_code or None,
        "error_message": error_message or None,
        "created_at": _iso(row.get("created_at")),
        "started_at": _iso(row.get("started_at")),
        "finished_at": _iso(row.get("finished_at")),
        "duration_ms": duration_ms(row.get("started_at"), row.get("finished_at")),
    }


_PUBLIC_EVENT_KEYS = {
    "tool_name",
    "delta_length",
    "result_length",
    "provider",
    "providers",
    "degraded",
    "cache_hit",
    "persisted",
    "query_count",
    "result_count",
    "source_count",
    "story_count",
    "analysis_id",
    "story_cluster_id",
    "draft_id",
    "code",
    "usage",
}


def _public_event_payload(raw: Any) -> dict[str, Any]:
    """Filter persisted event projections before returning them to a browser."""

    if not isinstance(raw, dict):
        return {}
    payload: dict[str, Any] = {}
    for key, value in raw.items():
        if key not in _PUBLIC_EVENT_KEYS or value is None:
            continue
        if key == "usage":
            payload[key] = normalize_usage(value)
        elif key == "providers" and isinstance(value, list):
            payload[key] = [str(item)[:80] for item in value[:4]]
        elif key in {"tool_name", "provider", "analysis_id", "story_cluster_id", "draft_id", "code"}:
            payload[key] = str(value)[:160]
        elif key in {"degraded", "cache_hit", "persisted"}:
            payload[key] = bool(value)
        else:
            try:
                payload[key] = max(0, min(int(value), 1_000_000_000))
            except (TypeError, ValueError, OverflowError):
                continue
    return payload


def _run_activity(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate only safe event counters for the collapsed details panel."""

    payloads = [_public_event_payload(event.get("safe_payload", {}) or {}) for event in events]
    tools = sum(1 for event in events if str(event.get("event_type")) == "TOOL_CALL_START")
    source_count = max((int(payload.get("source_count") or 0) for payload in payloads), default=0)
    story_count = max((int(payload.get("story_count") or 0) for payload in payloads), default=0)
    result_count = max((int(payload.get("result_count") or 0) for payload in payloads), default=0)
    cache_hits = sum(1 for payload in payloads if bool(payload.get("cache_hit")))
    degraded = any(bool(payload.get("degraded")) for payload in payloads)
    references: dict[str, list[str]] = {}
    for key in ("analysis_id", "story_cluster_id", "draft_id"):
        values = []
        for payload in payloads:
            value = payload.get(key)
            if value is not None and str(value) not in values:
                values.append(str(value)[:160])
        if values:
            references[key] = values[:12]
    return {
        "event_count": len(events),
        "tool_call_count": tools,
        "result_count": result_count,
        "source_count": source_count,
        "story_count": story_count,
        "cache_hit_count": cache_hits,
        "degraded": degraded,
        "references": references,
    }


def _profile(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "id": str(row["id"]) if row.get("id") is not None else None,
        "workspace_id": str(row["workspace_id"]) if row.get("workspace_id") is not None else None,
        "channel_id": int(row["channel_id"]),
        "topics": row.get("topics", []) or [],
        "style_profile": row.get("style_profile", {}) or {},
        "editorial_rules": row.get("editorial_rules", {}) or {},
        "confidence": row.get("confidence", "low") or "low",
        "current_analysis_id": str(row["current_analysis_id"]) if row.get("current_analysis_id") else None,
        "version": int(row.get("version", 1)),
        "created_at": _iso(row.get("created_at")),
        "updated_at": _iso(row.get("updated_at")),
    }


def _profile_change(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "workspace_id": str(row["workspace_id"]) if row.get("workspace_id") is not None else None,
        "channel_id": int(row["channel_id"]),
        "base_profile_version": int(row["base_profile_version"]),
        "proposed_topics": row.get("proposed_topics", []) or [],
        "style_diff": row.get("style_diff", {}) or {},
        "editorial_rules": row.get("editorial_rules", {}) or {},
        "reason": row.get("reason", "") or "",
        "status": row.get("status", "proposed"),
        "created_at": _iso(row.get("created_at")),
        "confirmed_at": _iso(row.get("confirmed_at")),
        "applied_at": _iso(row.get("applied_at")),
    }


def _safe_error(code: str, message: str, *, status_code: int, retryable: bool = False) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message, "retryable": retryable}}, status_code=status_code)


def build_router() -> APIRouter:
    router = APIRouter(prefix="/studio", tags=["studio"])

    def _settings(request: Request):
        return request.app.state.settings

    def _service(request: Request) -> StudioService:
        service = getattr(request.app.state, "studio_service", None)
        if service is None:
            raise HTTPException(status_code=503, detail="Studio service is not ready")
        return service

    def _ensure_enabled(request: Request) -> None:
        if not _settings(request).studio_enabled:
            raise HTTPException(status_code=404, detail="Studio is disabled")

    @router.get("/api/settings")
    async def studio_settings(request: Request):
        _ensure_enabled(request)
        require_auth(request)
        return {"system_prompt": await _service(request).repository.get_system_prompt()}

    @router.patch("/api/settings")
    async def patch_studio_settings(request: Request):
        _ensure_enabled(request)
        require_auth(request)
        require_csrf(request)
        try:
            payload = StudioSettingsPatch.model_validate(await request.json())
        except (ValidationError, ValueError):
            return _safe_error("invalid_settings", "System prompt must be text, up to 12,000 characters.", status_code=422)
        value = await _service(request).repository.set_system_prompt(payload.system_prompt)
        return {"system_prompt": value}

    def _ensure_ready(request: Request) -> dict[str, Any]:
        return build_setup_state(_settings(request), request.app.state.db)

    async def _consent(request: Request, context=None) -> dict[str, Any]:
        context = context or require_auth(request)
        getter = getattr(_service(request).repository, "get_provider_consent", None)
        row = None
        if getter is not None:
            row = await getter(
                provider=PROVIDER_NAME,
                configuration_fingerprint=configuration_fingerprint(_settings(request)),
                user_id=context.user_id,
            )
        return consent_state(_settings(request), row)

    @router.get("", response_class=HTMLResponse)
    async def studio_home(request: Request):
        _ensure_enabled(request)
        require_auth(request)
        setup = _ensure_ready(request)
        return _TEMPLATES.TemplateResponse(
            request,
            "studio.html",
            {
                "setup": setup,
                "csrf_token": request.app.state.csrf_token(request),
                "studio_assets": setup["ready"],
            },
        )

    @router.get("/api/setup")
    async def studio_setup(request: Request):
        _ensure_enabled(request)
        require_auth(request)
        return _ensure_ready(request)

    @router.get("/api/research/health")
    async def research_health(request: Request):
        """Return bounded private-search health without blocking setup."""

        _ensure_enabled(request)
        require_auth(request)
        return (await check_search_health(_settings(request))).as_dict()

    @router.post("/api/setup/validate")
    async def studio_setup_validate(request: Request):
        _ensure_enabled(request)
        require_auth(request)
        require_csrf(request)
        return _ensure_ready(request)

    @router.get("/api/bootstrap")
    async def studio_bootstrap(request: Request):
        _ensure_enabled(request)
        context = require_auth(request)
        setup = _ensure_ready(request)
        service = _service(request)
        channels = await service.repository.list_channels()
        conversations: list[dict[str, Any]] = []
        if setup["ready"]:
            await service.recover_stale_runs()
            conversations = [_conversation(row) for row in await service.repository.list_conversations()]
        selected = conversations[0] if conversations else None
        consent = await _consent(request, context)
        selected_channel_id = selected["channel_id"] if selected else (channels[0]["id"] if channels else None)
        profile = None
        profile_status = "no_channel" if selected_channel_id is None else "needs_consent"
        if selected_channel_id is not None and setup["ready"]:
            profile_getter = getattr(service.repository, "get_profile", None)
            profile = await profile_getter(int(selected_channel_id)) if profile_getter is not None else None
            if profile is None and consent["granted"]:
                profile = await service.ensure_profile(int(selected_channel_id))
            if profile is not None:
                profile_status = "low_confidence" if profile.get("confidence") == "low" else "ready"
            elif not consent["required"]:
                profile_status = "not_analyzed"
        current_draft = None
        active_run = None
        if selected is not None and setup["ready"]:
            getter = getattr(service.repository, "get_current_draft", None)
            if getter is not None:
                current_draft = await getter(conversation_id=uuid.UUID(str(selected["id"])), channel_id=int(selected["channel_id"]))
            active_getter = getattr(service.repository, "get_active_run", None)
            if active_getter is not None:
                active_run = await active_getter(uuid.UUID(str(selected["id"])))
        return {
            "setup": setup,
            "workspace": {"id": str(context.workspace_id or service.repository.workspace_id), "slug": context.workspace_slug, "role": context.role},
            "user": {"id": str(context.user_id) if context.user_id else None},
            "provider": {"name": "openrouter", "model": _settings(request).openrouter_model.strip() or "openai/gpt-4o-mini", "configured": bool(_settings(request).openrouter_api_key or getattr(_settings(request), "studio_test_mode", False))},
            "research": setup.get("research", {}),
            "channels": [dict(row) for row in channels],
            "selected_channel_id": selected_channel_id,
            "conversations": conversations,
            "current_conversation": selected,
            "draft": _draft(current_draft),
            "active_run": _run(active_run) if active_run is not None else None,
            "consent": consent,
            "profile": _profile(profile),
            "profile_status": profile_status,
        }

    @router.get("/api/consent")
    async def get_consent(request: Request):
        _ensure_enabled(request)
        context = require_auth(request)
        _ensure_ready(request)
        return {"consent": await _consent(request, context)}

    @router.post("/api/consent")
    async def grant_consent(request: Request):
        _ensure_enabled(request)
        context = require_auth(request)
        require_csrf(request)
        setup = _ensure_ready(request)
        if not setup["ready"]:
            return _safe_error("studio_not_ready", "Finish Studio setup before granting provider consent.", status_code=409)
        try:
            payload = ProviderConsentRequest.model_validate(await request.json())
        except (ValidationError, ValueError, TypeError):
            return _safe_error("invalid_consent", "Consent details are invalid.", status_code=422)
        expected = configuration_fingerprint(_settings(request))
        if not payload.confirm:
            return _safe_error("consent_required", "Confirm the OpenRouter disclosure to continue.", status_code=409)
        if payload.configuration_fingerprint and payload.configuration_fingerprint != expected:
            return _safe_error("consent_stale", "Provider configuration changed; review the disclosure again.", status_code=409)
        granter = getattr(_service(request).repository, "grant_provider_consent", None)
        if granter is None or context.user_id is None:
            return _safe_error("consent_unavailable", "Provider consent persistence is unavailable.", status_code=503)
        row = await granter(user_id=context.user_id, provider=PROVIDER_NAME, configuration_fingerprint=expected)
        # First-run profile preparation is local/deterministic and can happen
        # immediately after consent without sending a provider request.
        channels = await _service(request).repository.list_channels()
        channel_id = int(channels[0]["id"]) if channels else None
        profile = await _service(request).ensure_profile(channel_id) if channel_id is not None else None
        return {"consent": consent_state(_settings(request), row), "profile": _profile(profile), "disclosure": disclosure(_settings(request), fingerprint=expected)}

    @router.post("/api/consent/revoke")
    async def revoke_consent(request: Request):
        _ensure_enabled(request)
        context = require_auth(request)
        require_csrf(request)
        _ensure_ready(request)
        revoker = getattr(_service(request).repository, "revoke_provider_consent", None)
        if revoker is None or context.user_id is None:
            return _safe_error("consent_unavailable", "Provider consent persistence is unavailable.", status_code=503)
        row = await revoker(user_id=context.user_id, provider=PROVIDER_NAME)
        return {"consent": consent_state(_settings(request), row)}

    @router.get("/api/profile")
    async def get_profile(request: Request, channel_id: int | None = None):
        _ensure_enabled(request)
        require_auth(request)
        setup = _ensure_ready(request)
        if not setup["ready"]:
            return _safe_error("studio_not_ready", "Finish Studio setup before viewing the channel profile.", status_code=409)
        service = _service(request)
        channels = await service.repository.list_channels()
        selected = channel_id or (int(channels[0]["id"]) if channels else None)
        if selected is None:
            return _safe_error("channel_required", "Configure a Telegram channel before opening Studio.", status_code=409)
        profile = await service.repository.get_profile(selected)
        return {"profile": _profile(profile), "status": "ready" if profile else "not_analyzed", "channel_id": selected}

    @router.post("/api/profile/analyze")
    async def analyze_profile(request: Request, channel_id: int):
        _ensure_enabled(request)
        context = require_auth(request)
        require_csrf(request)
        if not _ensure_ready(request)["ready"]:
            return _safe_error("studio_not_ready", "Finish Studio setup first.", status_code=409)
        consent = await _consent(request, context)
        if consent["required"] and not consent["granted"]:
            return _safe_error("provider_consent_required", "Allow OpenRouter before analyzing.", status_code=409)
        service = _service(request)
        if channel_id not in {int(row["id"]) for row in await service.repository.list_channels()}:
            return _safe_error("channel_not_found", "Channel not found.", status_code=404)
        try:
            profile = await service.ensure_profile(channel_id, force=True, semantic=True)
        except Exception:
            return _safe_error("profile_analysis_failed", "Profile analysis failed. Your previous profile is unchanged. Retry shortly.", status_code=502, retryable=True)
        return {"profile": _profile(profile)}

    @router.post("/api/profile/changes")
    async def propose_profile_change(request: Request):
        _ensure_enabled(request)
        context = require_auth(request)
        require_csrf(request)
        setup = _ensure_ready(request)
        if not setup["ready"]:
            return _safe_error("studio_not_ready", "Finish Studio setup before changing the channel profile.", status_code=409)
        try:
            payload = ProfileChangeRequest.model_validate(await request.json())
        except (ValidationError, ValueError, TypeError):
            return _safe_error("invalid_profile_change", "Profile change details are invalid.", status_code=422)
        service = _service(request)
        channels = await service.repository.list_channels()
        channel_id = payload.channel_id or (int(channels[0]["id"]) if channels else None)
        if channel_id is None:
            return _safe_error("channel_required", "Configure a Telegram channel before changing topics.", status_code=409)
        current_row = await service.repository.get_profile(channel_id)
        if current_row is None:
            return _safe_error("profile_not_ready", "Analyze the channel before changing its topics.", status_code=409)
        current = ChannelProfile.model_validate(
            {
                "channel_id": channel_id,
                "topics": current_row.get("topics", []),
                "style_profile": current_row.get("style_profile", {}),
                "editorial_rules": current_row.get("editorial_rules", {}),
                "confidence": current_row.get("confidence", "low"),
                "analysis_id": str(current_row["current_analysis_id"]) if current_row.get("current_analysis_id") else None,
                "version": current_row.get("version", 1),
            }
        )
        proposal = propose_topic_change(current, payload.instruction)
        row = await service.repository.create_profile_change(
            {
                "channel_id": channel_id,
                "base_profile_version": proposal.base_profile_version,
                "proposed_topics": proposal.proposed_topics,
                "style_diff": proposal.style_diff,
                "editorial_rules": proposal.editorial_rules,
                "reason": proposal.reason,
                "status": proposal.status,
                "requested_by": context.user_id,
            }
        )
        return {"change": _profile_change(row), "requires_confirmation": proposal.requires_confirmation}

    @router.post("/api/profile/changes/{change_id}/confirm")
    async def confirm_profile_change(request: Request, change_id: str):
        _ensure_enabled(request)
        require_auth(request)
        require_csrf(request)
        try:
            parsed = uuid.UUID(change_id)
        except ValueError:
            return _safe_error("profile_change_not_found", "Profile change not found.", status_code=404)
        row = await _service(request).repository.confirm_profile_change(parsed)
        if row is None:
            return _safe_error("profile_change_not_found", "Profile change not found or already confirmed.", status_code=404)
        return {"change": _profile_change(row)}

    @router.post("/api/profile/changes/{change_id}/apply")
    async def apply_profile_change(request: Request, change_id: str):
        _ensure_enabled(request)
        require_auth(request)
        require_csrf(request)
        try:
            parsed = uuid.UUID(change_id)
        except ValueError:
            return _safe_error("profile_change_not_found", "Profile change not found.", status_code=404)
        try:
            row = await _service(request).repository.apply_profile_change(parsed)
        except (ConversationNotFound, StudioRepositoryError) as exc:
            return _safe_error("profile_change_blocked", str(exc), status_code=409)
        return {"profile": _profile(row)}

    @router.get("/api/conversations")
    async def list_conversations(request: Request):
        _ensure_enabled(request)
        require_auth(request)
        setup = _ensure_ready(request)
        if not setup["ready"]:
            return _safe_error("studio_not_ready", "Finish Studio setup before creating a conversation.", status_code=409)
        rows = await _service(request).repository.list_conversations()
        return {"conversations": [_conversation(row) for row in rows]}

    @router.post("/api/conversations")
    async def create_conversation(request: Request):
        _ensure_enabled(request)
        require_auth(request)
        require_csrf(request)
        setup = _ensure_ready(request)
        if not setup["ready"]:
            return _safe_error("studio_not_ready", "Finish Studio setup before creating a conversation.", status_code=409)
        try:
            payload = ConversationCreate.model_validate(await request.json())
        except (ValidationError, ValueError, TypeError):
            return _safe_error("invalid_conversation", "Conversation details are invalid.", status_code=422)
        service = _service(request)
        channels = await service.repository.list_channels()
        channel_id = payload.channel_id or (int(channels[0]["id"]) if channels else None)
        if channel_id is None:
            return _safe_error("channel_required", "Configure a Telegram channel before opening Studio.", status_code=409)
        try:
            row = await service.create_conversation(channel_id=channel_id, title=payload.title)
        except ConversationNotFound:
            return _safe_error("channel_not_found", "Selected channel is not part of this workspace.", status_code=404)
        return {"conversation": _conversation(row)}

    @router.patch("/api/conversations/{conversation_id}")
    async def rename_conversation(request: Request, conversation_id: str):
        _ensure_enabled(request)
        require_auth(request)
        require_csrf(request)
        try:
            parsed = uuid.UUID(conversation_id)
            payload = await request.json()
            title = payload.get("title")
            if set(payload) != {"title"} or not isinstance(title, str) or not title.strip() or len(title) > 160:
                raise ValueError("invalid title")
        except (ValueError, TypeError, AttributeError):
            return _safe_error("invalid_conversation_title", "Enter a title between 1 and 160 characters.", status_code=422)
        row = await _service(request).repository.rename_conversation(parsed, title=title.strip())
        if row is None or row.get("archived_at") is not None:
            return _safe_error("conversation_not_found", "Conversation not found.", status_code=404)
        return {"conversation": _conversation(row)}

    @router.delete("/api/conversations/{conversation_id}")
    async def delete_conversation(request: Request, conversation_id: str):
        """Permanently remove one conversation and its Studio-only records."""

        _ensure_enabled(request)
        require_auth(request)
        require_csrf(request)
        try:
            parsed = uuid.UUID(conversation_id)
        except ValueError:
            return _safe_error("conversation_not_found", "Conversation not found.", status_code=404)
        deleter = getattr(_service(request).repository, "delete_conversation", None)
        if deleter is None:
            return _safe_error(
                "conversation_delete_unavailable",
                "Conversation deletion is unavailable for this storage backend.",
                status_code=503,
                retryable=True,
            )
        try:
            deleted = await deleter(parsed)
        except ActiveRunExists:
            return _safe_error(
                "conversation_active_run",
                "Stop the active run before deleting this conversation.",
                status_code=409,
                retryable=True,
            )
        if not deleted:
            return _safe_error("conversation_not_found", "Conversation not found.", status_code=404)
        return {"conversation_id": conversation_id, "deleted": True}

    @router.get("/api/conversations/{conversation_id}/messages")
    async def conversation_messages(request: Request, conversation_id: str):
        _ensure_enabled(request)
        require_auth(request)
        try:
            parsed = uuid.UUID(conversation_id)
        except ValueError:
            return _safe_error("conversation_not_found", "Conversation not found.", status_code=404)
        service = _service(request)
        conversation = await service.repository.get_conversation(parsed)
        if conversation is None or conversation.get("archived_at") is not None:
            return _safe_error("conversation_not_found", "Conversation not found.", status_code=404)
        return {"conversation_id": conversation_id, "messages": [_message(row) for row in await service.repository.list_messages(parsed)]}

    @router.get("/api/conversations/{conversation_id}/active-run")
    async def conversation_active_run(request: Request, conversation_id: str):
        """Discover durable work after a reload or conversation switch."""

        _ensure_enabled(request)
        require_auth(request)
        try:
            parsed = uuid.UUID(conversation_id)
        except ValueError:
            return _safe_error("conversation_not_found", "Conversation not found.", status_code=404)
        service = _service(request)
        conversation = await service.repository.get_conversation(parsed)
        if conversation is None or conversation.get("archived_at") is not None:
            return _safe_error("conversation_not_found", "Conversation not found.", status_code=404)
        getter = getattr(service.repository, "get_active_run", None)
        run = await getter(parsed) if getter is not None else None
        return {"conversation_id": conversation_id, "run": _run(run) if run is not None else None}

    @router.get("/api/conversations/{conversation_id}/draft")
    async def conversation_draft(request: Request, conversation_id: str):
        """Return the conversation's active artifact after a reload."""

        _ensure_enabled(request)
        require_auth(request)
        try:
            parsed = uuid.UUID(conversation_id)
        except ValueError:
            return _safe_error("conversation_not_found", "Conversation not found.", status_code=404)
        service = _service(request)
        conversation = await service.repository.get_conversation(parsed)
        if conversation is None or conversation.get("archived_at") is not None:
            return _safe_error("conversation_not_found", "Conversation not found.", status_code=404)
        getter = getattr(service.repository, "get_current_draft", None)
        draft = await getter(conversation_id=parsed, channel_id=int(conversation["channel_id"])) if getter is not None else None
        bundle = await service.research_service.get_bundle(
            workspace_id=service.repository.workspace_id,
            conversation_id=parsed,
            channel_id=int(conversation["channel_id"]),
        )
        referenced = set((_draft(draft) or {}).get("source_ids", []))
        sources = [
            {"id": source.source_id, "title": source.title, "url": source.url}
            for source in (bundle.sources if bundle else [])
            if source.source_id in referenced
        ]
        return {"conversation_id": conversation_id, "draft": _draft(draft), "sources": sources}

    @router.get("/api/drafts/{draft_id}")
    async def get_draft(request: Request, draft_id: str):
        _ensure_enabled(request)
        require_auth(request)
        try:
            parsed = uuid.UUID(draft_id)
        except ValueError:
            return _safe_error("draft_not_found", "Draft not found.", status_code=404)
        row = await _service(request).repository.get_draft(parsed)
        if row is None:
            return _safe_error("draft_not_found", "Draft not found.", status_code=404)
        return {"draft": _draft(row)}

    @router.patch("/api/drafts/{draft_id}")
    async def patch_draft(request: Request, draft_id: str):
        _ensure_enabled(request)
        require_auth(request)
        require_csrf(request)
        try:
            parsed = uuid.UUID(draft_id)
        except ValueError:
            return _safe_error("draft_not_found", "Draft not found.", status_code=404)
        try:
            payload = DraftPatchRequest.model_validate(await request.json())
        except (ValidationError, ValueError, TypeError):
            return _safe_error("invalid_draft", "Draft details are invalid.", status_code=422)
        service = _service(request)
        try:
            if payload.restore_version is not None:
                row = await service.repository.restore_draft_version(
                    draft_id=parsed,
                    version=payload.restore_version,
                    expected_revision=payload.expected_revision,
                    instruction=f"Restored version {payload.restore_version}",
                )
            else:
                values = payload.model_dump(exclude={"expected_revision", "restore_version"}, exclude_none=True)
                row = await service.repository.save_draft(
                    draft_id=parsed,
                    payload=values,
                    expected_revision=payload.expected_revision,
                )
        except DraftConflictError as exc:
            local = {key: value for key, value in payload.model_dump(exclude={"expected_revision", "restore_version"}, exclude_none=True).items()}
            return JSONResponse(
                {
                    "error": {"code": exc.code, "message": str(exc), "retryable": True},
                    "server_draft": _draft(exc.current),
                    "local_draft": local,
                    "current_revision": int(exc.current.get("revision", 1)),
                },
                status_code=409,
            )
        except DraftValidationError as exc:
            return _safe_error(exc.code, exc.message, status_code=422)
        except DraftNotFound:
            return _safe_error("draft_not_found", "Draft not found.", status_code=404)
        return {"draft": _draft(row), "saved": True}

    @router.post("/api/drafts/{draft_id}/copied")
    async def copied_draft(request: Request, draft_id: str):
        _ensure_enabled(request)
        require_auth(request)
        require_csrf(request)
        try:
            parsed = uuid.UUID(draft_id)
        except ValueError:
            return _safe_error("draft_not_found", "Draft not found.", status_code=404)
        try:
            row = await _service(request).repository.mark_draft_copied(draft_id=parsed)
        except DraftValidationError as exc:
            return _safe_error(exc.code, exc.message, status_code=409)
        except DraftNotFound:
            return _safe_error("draft_not_found", "Draft not found.", status_code=404)
        copied_at = _iso(row.get("copied_at")) or ""
        return {"draft": _draft(row), "copied_text": str(row.get("body") or ""), "copied_at": copied_at}

    @router.get("/api/drafts/{draft_id}/versions")
    async def draft_versions(request: Request, draft_id: str):
        _ensure_enabled(request)
        require_auth(request)
        try:
            parsed = uuid.UUID(draft_id)
        except ValueError:
            return _safe_error("draft_not_found", "Draft not found.", status_code=404)
        try:
            versions = await _service(request).repository.list_draft_versions(draft_id=parsed)
        except DraftNotFound:
            return _safe_error("draft_not_found", "Draft not found.", status_code=404)
        return {"draft_id": draft_id, "versions": [_draft_version(row) for row in versions]}

    @router.post("/api/agent")
    async def studio_agent(request: Request):
        _ensure_enabled(request)
        context = require_auth(request)
        require_csrf(request)
        setup = _ensure_ready(request)
        if not setup["ready"]:
            return _safe_error("studio_not_ready", "Finish Studio setup before starting an agent run.", status_code=409)
        provider_consent = await _consent(request, context)
        if provider_consent["required"] and not provider_consent["granted"]:
            return _safe_error("provider_consent_required", "Review and confirm the OpenRouter disclosure before using Studio.", status_code=409)
        return await _service(request).stream_request(request, await request.body())

    @router.get("/api/runs/{run_id}/events")
    async def run_events(request: Request, run_id: str, after: int = 0):
        _ensure_enabled(request)
        require_auth(request)
        service = _service(request)
        try:
            parsed = resolve_run_id(run_id, workspace_id=service.repository.workspace_id)
        except ValueError:
            return _safe_error("run_not_found", "Run not found.", status_code=404)
        try:
            run = await service.repository.get_run(parsed)
            if run is None:
                raise RunNotFound("run not found")
            events = await service.repository.get_events(parsed, after=after)
        except RunNotFound:
            return _safe_error("run_not_found", "Run not found.", status_code=404)
        return {
            "run": _run(run),
            "events": [
                {"id": int(event["id"]), "sequence": int(event["sequence"]), "event_type": event["event_type"], "safe_payload": _public_event_payload(event.get("safe_payload", {}) or {}), "created_at": _iso(event.get("created_at"))}
                for event in events
            ],
        }

    @router.get("/api/runs/{run_id}/details")
    async def run_details(request: Request, run_id: str):
        """Return a quiet, privacy-safe operational summary for one run."""

        _ensure_enabled(request)
        require_auth(request)
        service = _service(request)
        try:
            parsed = resolve_run_id(run_id, workspace_id=service.repository.workspace_id)
        except ValueError:
            return _safe_error("run_not_found", "Run not found.", status_code=404)
        try:
            run = await service.repository.get_run(parsed)
            if run is None:
                raise RunNotFound("run not found")
            events = await service.repository.get_events(parsed, after=0)
        except RunNotFound:
            return _safe_error("run_not_found", "Run not found.", status_code=404)
        public_run = _run(run)
        error = None
        if public_run["error_code"]:
            code, message, retryable = safe_error_for_code(public_run["error_code"])
            error = {"code": code, "message": message, "retryable": retryable}
        return {
            "run": public_run,
            "details": {
                "provider": public_run["provider"],
                "requested_model": public_run["requested_model"],
                "actual_model": public_run["actual_model"],
                "prompt_version": PROMPT_VERSION,
                "status": public_run["status"],
                "stage": public_run["stage"],
                "duration_ms": public_run["duration_ms"],
                "usage": public_run["usage"],
                "activity": _run_activity(events),
                "error": error,
            },
            "events": [
                {
                    "id": int(event["id"]),
                    "sequence": int(event["sequence"]),
                    "event_type": event["event_type"],
                    "safe_payload": _public_event_payload(event.get("safe_payload", {}) or {}),
                    "created_at": _iso(event.get("created_at")),
                }
                for event in events[-100:]
            ],
        }

    @router.post("/api/runs/{run_id}/cancel")
    async def cancel_run(request: Request, run_id: str):
        _ensure_enabled(request)
        require_auth(request)
        require_csrf(request)
        service = _service(request)
        try:
            parsed = resolve_run_id(run_id, workspace_id=service.repository.workspace_id)
        except ValueError:
            return _safe_error("run_not_found", "Run not found.", status_code=404)
        try:
            run = await service.cancel(parsed)
        except RunNotFound:
            return _safe_error("run_not_found", "Run not found.", status_code=404)
        return {"run": _run(run), "run_id": run_id, "status": "cancel_requested" if run["status"] in {"queued", "running"} else run["status"]}

    return router
